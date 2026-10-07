"""可观测性门面。

问题：可观测性埋点很容易「侵蚀」业务代码——每个函数开头塞三行
``metrics.inc_counter`` + ``tracer.start_span``，最后业务逻辑被淹没。

方案：提供 **进程级单例门面** ``Observability``，业务侧只需：

    obs = get_observability()
    with obs.span("node.execute", node_id=nid) as span:
        ...
        obs.inc(MetricNames.LLM_TOKENS, 100, model="deepseek")

好处：
1. 埋点调用短，不打断阅读；
2. 未启用时（``enabled=False``）全部退化为空操作，**零开销**；
3. 单测可直接注入自定义 exporter / collector 做断言。

这是一个典型的「门面模式 + 空对象模式」组合，也是面试里能讲的取舍点：
为什么不用 AOP / 装饰器？——因为 Agent 编排的埋点位置需要**业务语义**
（例如要在「去重前后」各取一次快照），装饰器无法表达这种中间态。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from devagent.logging_config import get_logger
from devagent.observability.metrics import MetricNames, MetricsCollector
from devagent.observability.tracing import (
    NoopExporter,
    OTLPExporter,
    Span,
    SpanExporter,
    Tracer,
)

logger = get_logger(__name__)


class Observability:
    """可观测性门面（组合 Tracer + MetricsCollector）。"""

    def __init__(
        self,
        *,
        tracer: Tracer | None = None,
        metrics: MetricsCollector | None = None,
        enabled: bool = True,
    ) -> None:
        self.tracer = tracer or Tracer(enabled=enabled)
        self.metrics = metrics or MetricsCollector()
        self.enabled = enabled

    # ------------------------------------------------------------------ #
    # 追踪
    # ------------------------------------------------------------------ #

    @contextlib.contextmanager
    def span(self, name: str, *, parent: Span | None = None, **attributes: Any) -> Iterator[Span]:
        with self.tracer.span(name, parent=parent, **attributes) as span:
            yield span

    def start_span(self, name: str, *, parent: Span | None = None, **attributes: Any) -> Span:
        return self.tracer.start_span(name, parent=parent, **attributes)

    def end_span(self, span: Span, *, error: str = "") -> None:
        self.tracer.end_span(span, error=error)

    # ------------------------------------------------------------------ #
    # 指标
    # ------------------------------------------------------------------ #

    def inc(self, name: str, value: float = 1.0, **labels: Any) -> None:
        if not self.enabled:
            return
        self.metrics.inc_counter(name, value, **labels)

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        if not self.enabled:
            return
        self.metrics.set_gauge(name, value, **labels)

    def observe(self, name: str, value: float, **labels: Any) -> None:
        if not self.enabled:
            return
        self.metrics.observe(name, value, **labels)

    # ------------------------------------------------------------------ #
    # 便捷组合
    # ------------------------------------------------------------------ #

    def record_llm_call(
        self,
        *,
        model: str,
        tier: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: float,
        cached: bool = False,
        status: str = "ok",
    ) -> None:
        """记录一次 LLM 调用的全套指标。

        抽成一个方法而不是让调用方自己 inc 五次，是为了保证
        「token 总量 = input + output」这类**跨指标一致性**——手写极易漏项。
        """
        if not self.enabled:
            return
        m = self.metrics
        m.inc_counter(MetricNames.LLM_CALLS, 1, model=model, tier=tier, status=status)
        m.inc_counter(MetricNames.LLM_TOKENS, input_tokens, model=model, direction="input")
        m.inc_counter(MetricNames.LLM_TOKENS, output_tokens, model=model, direction="output")
        m.inc_counter(MetricNames.LLM_COST_USD, cost_usd, model=model)
        # 缓存命中不产生真实延迟，绝不能用 0ms 去污染延迟分布：
        # 命中率一高，p50/p90 就会被拉向 0，看起来"模型变快了"，
        # 而实际上只是大量请求根本没打到模型。
        if not cached:
            m.observe(MetricNames.LLM_LATENCY_MS, latency_ms, model=model)
        if cached:
            m.inc_counter(MetricNames.LLM_CACHE_HITS, 1, model=model)

    def record_context_build(
        self,
        *,
        agent: str,
        tokens_before: int,
        tokens_after: int,
        budget: int,
        dropped_chunks: int = 0,
        task_id: str = "",
        hard_overflow: int = 0,
    ) -> None:
        """记录一次上下文装配的压缩效果。

        ``CONTEXT_TOKENS_SAVED`` 是本项目最有说服力的指标之一：它直接量化
        上下文工程的价值。面试时可以说「系统平均为每次调用节省 X% token，
        且在 golden set 上验证了准确率不降」。

        ``task_id`` 标签把指标**归因到具体任务**。没有它，前端「本任务节省了
        多少 token」只能显示进程累计值（实测两个不同任务拿到完全相同的数字），
        这个指标就失去了可信度 —— 一个不可复现的收益数字比没有数字更糟。
        """
        if not self.enabled:
            return
        m = self.metrics
        # 空 task_id **不进标签**：写一个值为空的标签只会制造一条无意义的
        # 序列，还会让按精确标签读取的调用方（histogram_stats 是精确匹配）
        # 突然读不到数据 —— 加标签必须只对"有值"的维度发生。
        base: dict[str, Any] = {"agent": agent}
        if task_id:
            base["task_id"] = task_id
        saved = max(0, tokens_before - tokens_after)
        if saved:
            m.inc_counter(
                MetricNames.CONTEXT_TOKENS_SAVED,
                saved,
                reason="compress+dedup",
                **base,
            )
        if tokens_before > 0:
            m.observe(
                MetricNames.CONTEXT_COMPRESSION_RATIO,
                tokens_after / tokens_before,
                **base,
            )
        if budget > 0:
            m.observe(MetricNames.CONTEXT_UTILIZATION, tokens_after / budget, **base)
        if dropped_chunks:
            m.inc_counter(
                MetricNames.CONTEXT_CHUNKS_DROPPED,
                dropped_chunks,
                reason="budget",
                **base,
            )
        if hard_overflow > 0:
            # 硬约束击穿预算必须可见：它是「上下文窗口即将溢出」的前兆。
            m.inc_counter(
                MetricNames.CONTEXT_HARD_OVERFLOW,
                hard_overflow,
                **base,
            )


_INSTANCE: Observability | None = None


def get_observability() -> Observability:
    """获取进程级可观测性单例。

    默认 **关闭**：避免在未显式配置时产生任何内存/CPU 开销。应用启动时调用
    ``configure_observability(enabled=True)`` 才会真正开启。
    """
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = Observability(enabled=False)
    return _INSTANCE


def configure_observability(
    *,
    enabled: bool = True,
    otlp_endpoint: str = "",
    service_name: str = "devagent",
    exporter: SpanExporter | None = None,
    metrics: MetricsCollector | None = None,
) -> Observability:
    """配置并替换全局单例。应在应用启动时调用。"""
    global _INSTANCE
    if exporter is None:
        if otlp_endpoint:
            otlp = OTLPExporter(endpoint=otlp_endpoint, service_name=service_name)
            exporter = otlp if otlp.available else NoopExporter()
        else:
            # 没有 endpoint：用内存导出器，保证 /traces 调试接口仍可用
            exporter = NoopExporter()
    tracer = Tracer(exporter=exporter, enabled=enabled, service_name=service_name)
    _INSTANCE = Observability(tracer=tracer, metrics=metrics or MetricsCollector(), enabled=enabled)
    logger.info("observability_configured", enabled=enabled, otlp=bool(otlp_endpoint))
    return _INSTANCE


def reset_observability() -> None:
    """重置单例（供测试使用）。"""
    global _INSTANCE
    _INSTANCE = None


__all__ = [
    "MetricNames",
    "Observability",
    "configure_observability",
    "get_observability",
    "reset_observability",
]
