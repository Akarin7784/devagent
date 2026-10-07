"""上下文工程五层能力的集成测试。

验证 Routing / Isolation / Compression / Assembly / Budget
能够协同工作，并遵守关键策略约束。
"""

from __future__ import annotations

import numpy as np
import pytest

from devagent.config import ContextConfig, RoutingConfig
from devagent.context import (
    ContextEngine,
    ContextPolicyError,
    EchoSummarizer,
    make_chunk,
)
from devagent.context.budget import BudgetAllocator
from devagent.context.compression import ContextCompressor
from devagent.context.routing import ComplexityRouter, estimate_signals
from devagent.enums import AgentType, ContextKind, ModelTier
from devagent.models.domain import AgentHandoff, ContextRef, RoutingSignals


def _vec(*values: float) -> np.ndarray:
    return np.array(values, dtype=np.float64)


@pytest.fixture
def engine() -> ContextEngine:
    return ContextEngine(ContextConfig(), compressor=ContextCompressor(summarizer=EchoSummarizer()))


@pytest.fixture
def handoff() -> AgentHandoff:
    return AgentHandoff(
        task_id="T-1",
        goal="为 /users 接口增加分页参数",
        acceptance_criteria=["支持 page/page_size", "非法参数返回 400", "有单测覆盖"],
        constraints=["不改动现有返回结构", "必须兼容旧调用方"],
        relevant_files=["src/api/users.py", "tests/test_users.py"],
        context_refs=[ContextRef(uri="decision://D-002", note="前端统一用 page 风格")],
        budget_tokens=16_000,
    )


class TestHandoffInjection:
    """握手注入：只投喂结构化字段，且标记为硬约束。"""

    def test_handoff_creates_hard_constraint_chunks(
        self, engine: ContextEngine, handoff: AgentHandoff
    ) -> None:
        space = engine.isolator.handoff_to(AgentType.CODER, handoff)

        hard = [c for c in space.chunks if c.is_hard]
        assert len(hard) >= 3, "目标 / 验收标准 / 约束 都应作为硬约束注入"

        rendered = "\n".join(c.content for c in space.chunks)
        assert "支持 page/page_size" in rendered
        assert "不改动现有返回结构" in rendered

    def test_handoff_does_not_inject_raw_history(self, engine: ContextEngine) -> None:
        """隔离的核心：只投喂结构化字段，不继承上游原始对话。"""
        # 先在 Architect 空间塞入大量噪声
        arch = engine.isolator.space_for(AgentType.ARCHITECT)
        arch.add(make_chunk("架构师的调试噪音 " * 100, ContextKind.HISTORY))

        code_handoff = AgentHandoff(task_id="T-1", goal="实现", acceptance_criteria=["A"])
        space = engine.isolator.handoff_to(AgentType.CODER, code_handoff)

        rendered = "\n".join(c.content for c in space.chunks)
        assert "架构师的调试噪音" not in rendered


class TestIsolation:
    """隔离容器：空间独立 + 借用审计 + Verifier 保护。"""

    def test_spaces_are_independent(self, engine: ContextEngine) -> None:
        coder = engine.isolator.space_for(AgentType.CODER)
        reviewer = engine.isolator.space_for(AgentType.REVIEWER)

        coder.add(make_chunk("coder-only", ContextKind.CODE))

        assert coder.total_tokens > 0
        assert reviewer.total_tokens == 0, "其他 Agent 的空间不应受影响"

    def test_verifier_context_cannot_be_borrowed(self, engine: ContextEngine) -> None:
        """Verifier 的上下文必须保持独立，不可被借用。"""
        engine.isolator.space_for(AgentType.VERIFIER).add(
            make_chunk("验证视角", ContextKind.TOOL_RESULT)
        )
        with pytest.raises(ContextPolicyError, match="禁止借用 Verifier"):
            engine.isolator.borrow(
                borrower=AgentType.CODER,
                source=AgentType.VERIFIER,
                reason="想看看验证结果",
            )

    def test_borrow_is_audited(self, engine: ContextEngine) -> None:
        engine.isolator.space_for(AgentType.ARCHITECT).add(
            make_chunk("设计方案", ContextKind.DECISION)
        )
        engine.isolator.borrow(
            borrower=AgentType.CODER, source=AgentType.ARCHITECT, reason="需要设计约束"
        )
        log = engine.isolator.borrow_log
        assert len(log) == 1
        assert log[0]["borrower"] == "coder"
        assert log[0]["source"] == "architect"


class TestBudgetAllocation:
    """预算分配：角色差异 + 动态回流。"""

    def test_coder_gets_more_code_budget_than_verifier(self) -> None:
        allocator = BudgetAllocator()
        coder = allocator.allocate(AgentType.CODER, total=16_000)
        verifier = allocator.allocate(AgentType.VERIFIER, total=16_000)

        # 按模板，Coder 的代码配额比例 (0.50) 应高于 Verifier (0.25)
        assert coder.code_context > verifier.code_context
        # Verifier 依赖证据，工具结果配额应更高
        assert verifier.tool_results > coder.tool_results

    def test_verifier_has_minimal_history(self) -> None:
        allocator = BudgetAllocator()
        verifier = allocator.allocate(AgentType.VERIFIER, total=16_000)
        coder = allocator.allocate(AgentType.CODER, total=16_000)
        assert verifier.history < coder.history

    def test_quotas_never_exceed_input_budget(self) -> None:
        allocator = BudgetAllocator()
        for agent in AgentType:
            budget = allocator.allocate(agent, total=8000)
            assert budget.sum_of_quotas() <= budget.available_for_input()


class TestRouting:
    """模型分级路由 + 失败升级。"""

    def test_simple_task_routes_to_small(self) -> None:
        router = ComplexityRouter(RoutingConfig())
        signals = RoutingSignals(
            reasoning_depth=0.1,
            context_size_norm=0.1,
            tool_call_count_norm=0.0,
            retry_history_norm=0.0,
        )
        assert router.select_tier(signals) is ModelTier.SMALL

    def test_complex_task_routes_to_large(self) -> None:
        router = ComplexityRouter(RoutingConfig())
        signals = RoutingSignals(
            reasoning_depth=1.0,
            context_size_norm=1.0,
            tool_call_count_norm=1.0,
            retry_history_norm=1.0,
        )
        assert router.select_tier(signals) is ModelTier.LARGE

    def test_retry_escalates_tier(self) -> None:
        """失败升级：同一任务随重试次数升高而升级档位。"""
        router = ComplexityRouter(RoutingConfig())
        signals = RoutingSignals(
            reasoning_depth=0.3,
            context_size_norm=0.2,
            tool_call_count_norm=0.1,
            retry_history_norm=0.0,
        )
        first = router.select_tier_for_retry(signals, attempt=1)
        later = router.select_tier_for_retry(signals, attempt=5)

        order = {ModelTier.SMALL: 0, ModelTier.MEDIUM: 1, ModelTier.LARGE: 2}
        assert order[later] >= order[first], "重试不应降级"

    def test_explain_is_structured(self) -> None:
        router = ComplexityRouter(RoutingConfig())
        info = router.explain(RoutingSignals(reasoning_depth=0.5))
        assert "complexity" in info
        assert "tier" in info
        assert "model" in info
        assert set(info["components"]) == {
            "reasoning_depth",
            "context_size",
            "tool_calls",
            "retry_history",
        }

    def test_model_spec_parsing(self) -> None:
        router = ComplexityRouter(RoutingConfig())
        spec = router.model_for(ModelTier.MEDIUM)
        assert spec.provider == "deepseek"
        assert spec.model == "deepseek-chat"

    def test_estimate_signals_normalizes(self) -> None:
        s = estimate_signals(
            prompt_tokens=16_000,
            max_context=32_000,
            tool_calls=5,
            max_tool_calls=10,
            attempt=2,
            max_attempts=3,
        )
        assert 0.0 <= s.context_size_norm <= 1.0
        assert 0.0 <= s.tool_call_count_norm <= 1.0
        assert 0.0 <= s.retry_history_norm <= 1.0


class TestContextEngineEndToEnd:
    """门面集成：握手 → 装配 → 预算 → 路由 全链路。"""

    async def test_build_produces_bundle_with_hard_constraints(
        self, engine: ContextEngine, handoff: AgentHandoff
    ) -> None:
        engine.isolator.handoff_to(AgentType.CODER, handoff)

        # 加入一些软片段（相关代码）
        engine.isolator.space_for(AgentType.CODER).extend(
            [
                make_chunk(f"代码片段 {i} " * 30, ContextKind.CODE, embedding=_vec(1.0, 0.0))
                for i in range(20)
            ]
        )

        bundle = await engine.build(
            agent=AgentType.CODER,
            task_embedding=_vec(1.0, 0.0),
            current_step="T-1",
            budget_total=8000,
            routing_signals=RoutingSignals(reasoning_depth=0.8),
        )

        rendered = bundle.render()
        assert "支持 page/page_size" in rendered, "硬约束必须在最终上下文中"
        assert bundle.total_tokens <= bundle.budget.available_for_input()
        assert bundle.decision.routing_tier is not None

    async def test_build_compresses_when_over_threshold(self, engine: ContextEngine) -> None:
        space = engine.isolator.space_for(AgentType.CODER)
        # 塞入大量历史，确保超过压缩阈值（默认 0.7 × 预算）
        for i in range(60):
            space.add(
                make_chunk(
                    f"历史步骤 {i}：完成了某项修改，decided to use approach {i}。"
                    + "详细记录本次改动的背景与取舍 " * 20,
                    ContextKind.HISTORY,
                    age=60 - i,
                )
            )

        total_tokens = sum(c.tokens for c in space.chunks)
        assert total_tokens > 4000 * 0.7, "测试前提：历史总量需超过压缩阈值"

        bundle = await engine.build(
            agent=AgentType.CODER,
            task_embedding=_vec(1.0, 0.0),
            current_step="T-1",
            budget_total=4000,
        )
        assert bundle.decision.compression_applied is True
        assert bundle.decision.tokens_after < bundle.decision.tokens_before

    async def test_compression_flag_distinct_from_assembly_drop(
        self, engine: ContextEngine
    ) -> None:
        """compression_applied 只标记「压缩阶段」，不因装配丢弃而置位。

        回归测试：早期实现用 ``used < tokens_before`` 判断，
        导致「装配阶段因预算丢弃片段」被误报为「发生压缩」。
        """
        space = engine.isolator.space_for(AgentType.CODER)
        # 总量低于压缩阈值，但远超预算 → 只有装配丢弃，无压缩
        for i in range(20):
            space.add(make_chunk(f"短历史 {i} " * 10, ContextKind.HISTORY, age=20 - i))
        bundle = await engine.build(
            agent=AgentType.CODER,
            task_embedding=_vec(1.0, 0.0),
            current_step="T-1",
            budget_total=5000,
        )
        assert bundle.decision.compression_applied is False

    async def test_build_with_empty_space(self, engine: ContextEngine) -> None:
        bundle = await engine.build(
            agent=AgentType.CODER,
            task_embedding=_vec(1.0, 0.0),
            current_step="T-1",
            budget_total=4000,
        )
        assert bundle.chunks == []
        assert bundle.total_tokens == 0


class TestCompression:
    """分层压缩。"""

    async def test_structured_summary_schema(self) -> None:
        from devagent.context.compression import StructuredSummary

        s = StructuredSummary(
            decisions=["用 page 风格"],
            artifacts=["src/api/users.py 已修改"],
            constraints=["兼容旧调用"],
            open_questions=["page_size=0 如何处理"],
        )
        rendered = s.render()
        assert "已完成的决策" in rendered
        assert "已产出的产物" in rendered
        assert "已知约束" in rendered
        assert "待解决问题" in rendered

    async def test_summary_merge_dedupes(self) -> None:
        from devagent.context.compression import StructuredSummary

        a = StructuredSummary(decisions=["D1", "D2"], constraints=["C1"])
        b = StructuredSummary(decisions=["D2", "D3"], constraints=["C1", "C2"])
        merged = a.merge(b)
        assert merged.decisions == ["D1", "D2", "D3"]
        assert merged.constraints == ["C1", "C2"]

    async def test_hard_constraints_never_compressed(self) -> None:
        compressor = ContextCompressor(summarizer=EchoSummarizer(), hot_window=1, warm_window=1)
        chunks = [
            make_chunk("系统指令", ContextKind.SYSTEM_PROMPT, is_hard=True),
            make_chunk("验收标准", ContextKind.TASK_SPEC, is_hard=True),
            *[make_chunk(f"历史 {i}", ContextKind.HISTORY, age=i) for i in range(10)],
        ]
        result = await compressor.compress(chunks)
        hard_ids = {c.id for c in chunks if c.is_hard}
        out_ids = {c.id for c in result.chunks}
        assert hard_ids <= out_ids, "硬约束必须原样保留，不进入压缩"

    async def test_compression_reduces_tokens(self) -> None:
        compressor = ContextCompressor(summarizer=EchoSummarizer(), hot_window=2, warm_window=3)
        chunks = [
            make_chunk(f"很久以前的历史步骤 {i} " * 30, ContextKind.HISTORY, age=50 - i)
            for i in range(50)
        ]
        result = await compressor.compress(chunks)
        assert result.tokens_after < result.tokens_before
        assert result.compressed_count > 0

    async def test_should_compress_threshold(self) -> None:
        compressor = ContextCompressor()
        assert compressor.should_compress(8000, 10000, 0.7) is True
        assert compressor.should_compress(5000, 10000, 0.7) is False
        assert compressor.should_compress(1, 0, 0.7) is False
