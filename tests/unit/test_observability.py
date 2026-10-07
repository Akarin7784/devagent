"""可观测性层测试。

覆盖三件事：
1. ``Tracer``/``Span`` 的父子关系与 trace 传播（并发下不串线）；
2. ``MetricsCollector`` 的计数、聚合、分位数与 Prometheus 文本导出；
3. **端到端埋点**：用假 Provider 驱动一次完整编排，断言指标真的被记录。

第 3 点是关键——「埋点写了但没生效」是可观测性最常见的失败模式，
只测组件本身测不出来。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from devagent.config import ContextConfig, Settings
from devagent.context import ContextEngine
from devagent.context.compression import ContextCompressor, EchoSummarizer
from devagent.enums import AgentType, TaskStatus
from devagent.models.gateway import ModelGateway
from devagent.models.provider import ChatMessage, ChatResult, TokenUsage
from devagent.observability import (
    MetricNames,
    Observability,
    configure_observability,
    get_observability,
    reset_observability,
)
from devagent.observability.metrics import MetricsCollector
from devagent.observability.tracing import NoopExporter, OTLPExporter, Span, Tracer
from devagent.orchestration import Orchestrator, OrchestratorConfig

# ---------------------------------------------------------------------- #
# Tracer
# ---------------------------------------------------------------------- #


class TestTracer:
    def test_span_exported_on_exit(self) -> None:
        exporter = NoopExporter()
        tracer = Tracer(exporter=exporter)
        with tracer.span("op"):
            pass
        assert [s.name for s in exporter.spans] == ["op"]
        assert exporter.spans[0].status == "ok"
        assert exporter.spans[0].duration_ms >= 0

    def test_parent_linkage_and_trace_propagation(self) -> None:
        exporter = NoopExporter()
        tracer = Tracer(exporter=exporter)
        with tracer.span("root") as root, tracer.span("child", parent=root):
            pass
        by_name = {s.name: s for s in exporter.spans}
        assert by_name["child"].parent_id == root.span_id
        assert by_name["child"].trace_id == root.trace_id
        assert by_name["root"].parent_id is None

    def test_exception_marks_error_and_still_exports(self) -> None:
        """异常路径必须也导出 span，否则失败点会在 trace 中凭空消失。"""
        exporter = NoopExporter()
        tracer = Tracer(exporter=exporter)
        with pytest.raises(ValueError, match="boom"), tracer.span("failing"):
            raise ValueError("boom")
        span = exporter.spans[0]
        assert span.status == "error"
        assert "ValueError" in span.error

    def test_disabled_tracer_is_noop(self) -> None:
        exporter = NoopExporter()
        tracer = Tracer(exporter=exporter, enabled=False)
        with tracer.span("ignored") as span:
            span.set_attribute("a", 1)
        assert exporter.spans == []

    def test_attribute_truncation(self) -> None:
        """超长属性必须被截断，否则单个大 prompt 会撑爆 collector。"""
        span = Span(name="s", trace_id="t", span_id="i")
        span.set_attribute("prompt", "x" * 5000)
        value = span.attributes["prompt"]
        assert len(value) < 1000
        assert "truncated" in value

    def test_manual_start_end_span(self) -> None:
        exporter = NoopExporter()
        tracer = Tracer(exporter=exporter)
        span = tracer.start_span("manual", k="v")
        assert exporter.spans == []
        tracer.end_span(span, error="failed")
        assert exporter.spans[0].status == "error"

    def test_events_recorded(self) -> None:
        exporter = NoopExporter()
        tracer = Tracer(exporter=exporter)
        with tracer.span("with_events") as span:
            span.add_event("cache_miss", key="abc")
        assert exporter.spans[0].events[0]["name"] == "cache_miss"

    def test_to_dict_roundtrip(self) -> None:
        span = Span(name="s", trace_id="t", span_id="i", start_ns=1, end_ns=1_000_001)
        data = span.to_dict()
        assert data["name"] == "s"
        assert data["duration_ms"] == pytest.approx(1.0)

    def test_exporter_failure_does_not_propagate(self) -> None:
        """导出器自身抛异常绝不能拖垮业务逻辑。"""

        class ExplodingExporter:
            def export(self, span: Span) -> None:
                raise RuntimeError("collector down")

            def shutdown(self) -> None:
                return None

        tracer = Tracer(exporter=ExplodingExporter())  # type: ignore[arg-type]
        with tracer.span("safe"):
            pass  # 不应抛异常

    def test_otlp_exporter_without_endpoint_is_unavailable(self) -> None:
        exporter = OTLPExporter(endpoint="")
        assert exporter.available is False
        exporter.export(Span(name="s", trace_id="t", span_id="i"))
        exporter.shutdown()


# ---------------------------------------------------------------------- #
# MetricsCollector
# ---------------------------------------------------------------------- #


class TestMetricsCollector:
    def test_counter_accumulates(self) -> None:
        m = MetricsCollector()
        m.inc_counter("calls", 1, model="a")
        m.inc_counter("calls", 2, model="a")
        assert m.counter("calls", model="a") == 3.0

    def test_labels_are_order_independent(self) -> None:
        """标签顺序不能影响序列归并，否则同一逻辑序列会被拆成两条。"""
        m = MetricsCollector()
        m.inc_counter("calls", 1, model="a", status="ok")
        assert m.counter("calls", status="ok", model="a") == 1.0

    def test_total_aggregates_across_labels(self) -> None:
        m = MetricsCollector()
        m.inc_counter("tokens", 10, model="a", direction="input")
        m.inc_counter("tokens", 5, model="b", direction="input")
        m.inc_counter("tokens", 7, model="a", direction="output")
        assert m.total("tokens") == 22.0
        assert m.total("tokens", direction="input") == 15.0
        assert m.total("tokens", model="a") == 17.0

    def test_gauge_overwrites(self) -> None:
        m = MetricsCollector()
        m.set_gauge("inflight", 3)
        m.set_gauge("inflight", 1)
        assert m.gauge("inflight") == 1

    def test_histogram_quantiles(self) -> None:
        m = MetricsCollector()
        for v in (10, 20, 30, 40, 100):
            m.observe("lat", v)
        stats = m.histogram_stats("lat")
        assert stats["count"] == 5
        assert stats["sum"] == 200
        assert stats["mean"] == 40
        assert stats["min"] == 10
        assert stats["max"] == 100
        assert stats["p50"] == 30

    def test_histogram_single_observation(self) -> None:
        m = MetricsCollector()
        m.observe("lat", 42)
        stats = m.histogram_stats("lat")
        assert stats["p50"] == 42
        assert stats["p99"] == 42

    def test_histogram_missing_series(self) -> None:
        m = MetricsCollector()
        assert m.histogram_stats("nope")["count"] == 0

    def test_observe_total_merges_series(self) -> None:
        m = MetricsCollector()
        m.observe("lat", 10, model="a")
        m.observe("lat", 30, model="b")
        merged = m.observe_total("lat")
        assert merged["count"] == 2
        assert merged["max"] == 30
        assert m.observe_total("lat", model="a")["count"] == 1

    def test_reservoir_sampling_bounds_memory(self) -> None:
        """超过上限后必须转为采样，否则高基数长尾会吃光内存。"""
        m = MetricsCollector(max_observations=50)
        for i in range(500):
            m.observe("lat", float(i))
        series = m._histograms["lat"][()]
        assert len(series.observations) == 50
        assert series.count == 500

    def test_snapshot_shape(self) -> None:
        m = MetricsCollector()
        m.inc_counter("c", 1, k="v")
        m.set_gauge("g", 2)
        m.observe("h", 3, k="v")
        snap = m.snapshot()
        assert snap["counters"]["c"]['k="v"'] == 1.0
        assert snap["gauges"]["g"][""] == 2
        assert snap["histograms"]["h"]['k="v"']["count"] == 1

    def test_reset(self) -> None:
        m = MetricsCollector()
        m.inc_counter("c", 1)
        m.reset()
        assert m.counter("c") == 0.0

    def test_prometheus_text_export(self) -> None:
        m = MetricsCollector()
        m.inc_counter("calls", 3, model="deepseek")
        m.set_gauge("inflight", 2)
        m.observe("lat", 100, model="deepseek")
        text = m.to_prometheus_text(prefix="devagent")
        assert "# TYPE devagent_calls_total counter" in text
        assert 'devagent_calls_total{model="deepseek"} 3.0' in text
        assert "# TYPE devagent_inflight gauge" in text
        assert "# TYPE devagent_lat summary" in text
        assert 'devagent_lat{model="deepseek",quantile="0.5"} 100' in text


# ---------------------------------------------------------------------- #
# Observability 门面
# ---------------------------------------------------------------------- #


class TestObservabilityFacade:
    def test_disabled_facade_is_noop(self) -> None:
        obs = Observability(enabled=False)
        obs.inc("c", 1)
        obs.observe("h", 5)
        obs.gauge("g", 1)
        assert obs.metrics.counter("c") == 0.0

    def test_record_llm_call(self) -> None:
        obs = Observability(enabled=True)
        obs.record_llm_call(
            model="deepseek-chat",
            tier="medium",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.002,
            latency_ms=1200,
        )
        m = obs.metrics
        assert (
            m.counter(MetricNames.LLM_CALLS, model="deepseek-chat", tier="medium", status="ok") == 1
        )
        assert m.total(MetricNames.LLM_TOKENS, direction="input") == 100
        assert m.total(MetricNames.LLM_TOKENS, direction="output") == 50
        assert m.total(MetricNames.LLM_COST_USD) == pytest.approx(0.002)

    def test_record_llm_call_cached_counts_hit(self) -> None:
        obs = Observability(enabled=True)
        obs.record_llm_call(
            model="m",
            tier="small",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.0,
            latency_ms=0.0,
            cached=True,
        )
        assert obs.metrics.counter(MetricNames.LLM_CACHE_HITS, model="m") == 1

    def test_record_context_build_quantifies_savings(self) -> None:
        """压缩节省量必须为正，且利用率/压缩比被记录。"""
        obs = Observability(enabled=True)
        obs.record_context_build(
            agent="coder", tokens_before=9000, tokens_after=5200, budget=8000, dropped_chunks=7
        )
        assert obs.metrics.total(MetricNames.CONTEXT_TOKENS_SAVED, agent="coder") == 3800
        assert (
            obs.metrics.counter(MetricNames.CONTEXT_CHUNKS_DROPPED, agent="coder", reason="budget")
            == 7
        )
        ratio = obs.metrics.histogram_stats(MetricNames.CONTEXT_COMPRESSION_RATIO, agent="coder")
        assert ratio["count"] == 1
        assert ratio["mean"] == pytest.approx(5200 / 9000)

    def test_record_context_build_no_savings_skips_counter(self) -> None:
        obs = Observability(enabled=True)
        obs.record_context_build(agent="coder", tokens_before=100, tokens_after=100, budget=100)
        assert obs.metrics.total(MetricNames.CONTEXT_TOKENS_SAVED) == 0

    def test_singleton_lifecycle(self) -> None:
        reset_observability()
        default = get_observability()
        assert default.enabled is False
        configured = configure_observability(enabled=True)
        assert configured.enabled is True
        assert get_observability() is configured
        reset_observability()

    def test_configure_with_explicit_exporter(self) -> None:
        reset_observability()
        exporter = NoopExporter()
        obs = configure_observability(enabled=True, exporter=exporter)
        with obs.span("x"):
            pass
        assert len(exporter.spans) == 1
        reset_observability()


# ---------------------------------------------------------------------- #
# 端到端埋点验证
# ---------------------------------------------------------------------- #


def _json_block(payload: dict[str, Any]) -> str:
    return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


_REQUIREMENT = _json_block(
    {
        "goal": "为 /users 接口增加分页参数",
        "acceptance_criteria": ["支持 page 与 page_size 参数"],
        "constraints": [],
        "open_questions": [],
        "relevant_files": [],
    }
)

_ARCHITECT = _json_block(
    {
        "approach": "参数解析层增加校验",
        "nodes": [
            {
                "id": "N1",
                "goal": "实现分页",
                "agent_type": "coder",
                "deps": [],
                "acceptance_criteria": ["支持 page 与 page_size 参数"],
            }
        ],
    }
)

_CODER = _json_block(
    {
        "summary": "分页",
        "changes": [
            {
                "file": "a.py",
                "reason": "r",
                "addresses_criteria": ["支持 page 与 page_size 参数"],
                "diff": "+1",
            }
        ],
        "unresolved": [],
    }
)

_VERIFIER = _json_block(
    {
        "verdict": "pass",
        "criterion_checks": [
            {"criterion": "支持 page 与 page_size 参数", "passed": True, "reason": "ok"}
        ],
    }
)


class _ScriptedProvider:
    """最小假 Provider：按 system prompt 路由到脚本回复。"""

    name = "scripted"

    def __init__(self, tokens_per_call: int = 100) -> None:
        self.tokens_per_call = tokens_per_call

    def _reply(self, messages: list[ChatMessage]) -> str:
        system = next((m.content for m in messages if m.role == "system"), "")
        if "需求分析师" in system:
            return _REQUIREMENT
        if "软件架构师" in system:
            return _ARCHITECT
        if "软件工程师" in system:
            return _CODER
        if "独立验证工程师" in system:
            return _VERIFIER
        return "ok"

    async def chat(self, messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
        model = str(kwargs.get("model") or "scripted")
        return ChatResult(
            content=self._reply(messages),
            model=model,
            provider=self.name,
            usage=TokenUsage(
                prompt_tokens=self.tokens_per_call, completion_tokens=self.tokens_per_call
            ),
            latency_ms=5,
        )

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]

    async def aclose(self) -> None:
        return None


@pytest.mark.integration
class TestEndToEndInstrumentation:
    """验证埋点在真实编排流程中生效。"""

    def _build(self, provider: _ScriptedProvider) -> tuple[Orchestrator, NoopExporter]:
        settings = Settings()
        exporter = NoopExporter()
        obs = configure_observability(enabled=True, exporter=exporter)

        gateway = ModelGateway(
            settings,
            providers=dict.fromkeys(("deepseek", "qwen", "zhipu"), provider),
        )
        context = ContextEngine(
            ContextConfig(),
            compressor=ContextCompressor(summarizer=EchoSummarizer()),
        )
        orch = Orchestrator(
            settings,
            gateway=gateway,
            context_engine=context,
            config=OrchestratorConfig(enable_tester=False, enable_reviewer=False),
        )
        obs.metrics.reset()
        return orch, exporter

    async def test_full_run_records_task_and_node_metrics(self) -> None:
        provider = _ScriptedProvider()
        orch, _exporter = self._build(provider)

        result = await orch.run("加个分页", task_id="t-obs")
        assert result.status is TaskStatus.SUCCEEDED

        metrics = get_observability().metrics
        assert metrics.total(MetricNames.TASK_TOTAL, status="succeeded") == 1
        assert metrics.total(MetricNames.NODE_EXECUTIONS) >= 1
        assert metrics.total(MetricNames.LLM_CALLS) >= 4  # req + arch + coder + verifier
        assert metrics.total(MetricNames.VERDICT_TOTAL, verdict="pass") >= 1
        assert metrics.total(MetricNames.CONTEXT_TOKENS_SAVED) >= 0

    async def test_task_span_is_root_of_node_spans(self) -> None:
        provider = _ScriptedProvider()
        orch, exporter = self._build(provider)
        await orch.run("加个分页", task_id="t-span")

        task_spans = exporter.find("task.run")
        assert len(task_spans) == 1
        task_span = task_spans[0]
        assert task_span.status == "ok"
        assert task_span.attributes["task_id"] == "t-span"

        node_spans = exporter.find("node.execute")
        assert node_spans
        assert all(s.parent_id == task_span.span_id for s in node_spans)
        assert all(s.trace_id == task_span.trace_id for s in node_spans)

    async def test_failed_task_marks_span_error(self) -> None:
        """任务失败时根 span 必须标记为 error，否则告警无法触发。"""
        provider = _ScriptedProvider()
        orch, exporter = _build_failing_orchestrator(provider)
        result = await orch.run("加个分页", task_id="t-fail")
        assert result.status is TaskStatus.PAUSED

        task_span = exporter.find("task.run")[0]
        assert task_span.status == "error"
        metrics = get_observability().metrics
        assert metrics.total(MetricNames.TASK_TOTAL, status="paused") == 1

    async def test_llm_metrics_are_superset_of_node_accounting(self) -> None:
        """指标口径必须**覆盖**编排器账目，且不小于它。

        这里刻意不断言「相等」：编排器的 ``total_tokens`` 只累计
        requirement/architect/各 DAG 节点的用量，而 Verifier 走独立验证路径，
        其 token 计入指标但不计入 ``total_tokens``。

        因此正确的不变量是：

            指标统计的总 token（input + output）  >=  业务账目 total_tokens

        若反过来（指标小于账目），说明有调用点没被埋点 —— 那才是真 bug。
        """
        provider = _ScriptedProvider(tokens_per_call=100)
        orch, _ = self._build(provider)
        result = await orch.run("加个分页", task_id="t-tokens")

        metrics = get_observability().metrics
        input_total = metrics.total(MetricNames.LLM_TOKENS, direction="input")
        output_total = metrics.total(MetricNames.LLM_TOKENS, direction="output")

        assert result.total_tokens > 0
        # 指标覆盖全部 LLM 调用，故总量不小于业务账目
        assert input_total + output_total >= result.total_tokens
        # input/output 对称：本假 Provider 每次返回相同的 prompt/completion
        assert input_total == output_total
        # 确认 requirement/architect 两次调用确实是「已记账于业务」而非游离
        requirement_steps = [s for s in result.steps if s.agent is AgentType.REQUIREMENT]
        architect_steps = [s for s in result.steps if s.agent is AgentType.ARCHITECT]
        assert requirement_steps and architect_steps


def _build_failing_orchestrator(
    provider: _ScriptedProvider,
) -> tuple[Orchestrator, NoopExporter]:
    """构造一个**必然暂停**的编排器：max_steps=1 不足以完成需求+架构两步。"""
    settings = Settings()
    exporter = NoopExporter()
    obs = configure_observability(enabled=True, exporter=exporter)
    gateway = ModelGateway(
        settings, providers=dict.fromkeys(("deepseek", "qwen", "zhipu"), provider)
    )
    context = ContextEngine(
        ContextConfig(), compressor=ContextCompressor(summarizer=EchoSummarizer())
    )
    orch = Orchestrator(
        settings,
        gateway=gateway,
        context_engine=context,
        config=OrchestratorConfig(
            enable_tester=False, enable_reviewer=False, max_steps=1, max_tokens=10**9
        ),
    )
    obs.metrics.reset()
    return orch, exporter

    def teardown_method(self) -> None:
        reset_observability()


@pytest.mark.integration
class TestContextInstrumentation:
    """验证上下文装配的收益被记录。"""

    async def test_build_records_savings(self) -> None:
        from devagent.context.assembly import make_chunk
        from devagent.enums import ContextKind

        reset_observability()
        obs = configure_observability(enabled=True)
        obs.metrics.reset()

        engine = ContextEngine(ContextConfig(default_budget=2000))
        space = engine.isolator.space_for(AgentType.CODER)
        for i in range(60):
            space.add(
                make_chunk(
                    f"函数 {i} 的实现细节" * 20,
                    ContextKind.CODE,
                    source=f"f{i}.py",
                    relevance=0.5,
                )
            )
        bundle = await engine.build(
            agent=AgentType.CODER, task_embedding=None, current_step="s1", budget_total=2000
        )

        metrics = obs.metrics
        assert metrics.total(MetricNames.CONTEXT_TOKENS_SAVED, agent="coder") > 0
        assert metrics.total(MetricNames.CONTEXT_CHUNKS_DROPPED, agent="coder") > 0
        util = metrics.histogram_stats(MetricNames.CONTEXT_UTILIZATION, agent="coder")
        assert util["count"] == 1
        assert bundle.decision.tokens_after <= 2000
        reset_observability()
