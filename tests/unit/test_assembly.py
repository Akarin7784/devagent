"""上下文装配算法的单元测试。

覆盖：
- 打分函数各分量
- 硬约束优先级
- 冗余惩罚效果
- 预算约束
- 位置编排
"""

from __future__ import annotations

import numpy as np
import pytest

from devagent.context.assembly import (
    ContextAssembler,
    ScoringWeights,
    estimate_tokens,
    make_chunk,
)
from devagent.context.tokenizer import HeuristicTokenCounter, cosine_similarity
from devagent.enums import AgentType, ContextKind
from devagent.models.domain import BudgetAllocation


@pytest.fixture
def assembler() -> ContextAssembler:
    return ContextAssembler()


def _vec(*values: float) -> np.ndarray:
    return np.array(values, dtype=np.float64)


class TestTokenizer:
    def test_english_token_estimate(self) -> None:
        counter = HeuristicTokenCounter()
        # 28 个字符 / 4 ≈ 7 tokens
        assert 5 <= counter.count("hello world this is a test") <= 10

    def test_cjk_token_estimate(self) -> None:
        counter = HeuristicTokenCounter()
        # 4 个中文字符 ≈ 4 tokens
        assert counter.count("你好世界") == 4

    def test_empty_text(self) -> None:
        assert HeuristicTokenCounter().count("") == 0


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        v = _vec(1.0, 2.0, 3.0)
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        assert cosine_similarity(_vec(1.0, 0.0), _vec(0.0, 1.0)) == pytest.approx(0.0)

    def test_none_returns_zero(self) -> None:
        assert cosine_similarity(None, _vec(1.0)) == 0.0
        assert cosine_similarity(_vec(1.0), None) == 0.0

    def test_dimension_mismatch_returns_zero(self) -> None:
        assert cosine_similarity(_vec(1.0, 2.0), _vec(1.0)) == 0.0

    def test_zero_vector_returns_zero(self) -> None:
        assert cosine_similarity(_vec(0.0, 0.0), _vec(1.0, 1.0)) == 0.0


class TestScoring:
    def test_relevance_dominates_when_others_equal(self, assembler: ContextAssembler) -> None:
        task_vec = _vec(1.0, 0.0)
        relevant = make_chunk("relevant", ContextKind.CODE, embedding=_vec(1.0, 0.0))
        irrelevant = make_chunk("irrelevant", ContextKind.CODE, embedding=_vec(0.0, 1.0))

        s_rel = assembler.score_chunk(relevant, task_vec, "T1", [])
        s_irr = assembler.score_chunk(irrelevant, task_vec, "T1", [])

        assert s_rel.relevance > s_irr.relevance
        assert s_rel.total > s_irr.total

    def test_dependency_boost(self, assembler: ContextAssembler) -> None:
        task_vec = _vec(1.0, 0.0)
        dep = make_chunk("dep", ContextKind.CODE, embedding=task_vec, depends_on_step=["T1"])
        nodep = make_chunk("nodep", ContextKind.CODE, embedding=task_vec)

        s_dep = assembler.score_chunk(dep, task_vec, "T1", [])
        s_nodep = assembler.score_chunk(nodep, task_vec, "T1", [])

        assert s_dep.dependency == 1.0
        assert s_nodep.dependency == 0.0
        assert s_dep.total > s_nodep.total

    def test_recency_decay(self, assembler: ContextAssembler) -> None:
        task_vec = _vec(1.0, 0.0)
        fresh = make_chunk("fresh", ContextKind.HISTORY, embedding=task_vec, age=0)
        stale = make_chunk("stale", ContextKind.HISTORY, embedding=task_vec, age=20)

        s_fresh = assembler.score_chunk(fresh, task_vec, "T1", [])
        s_stale = assembler.score_chunk(stale, task_vec, "T1", [])

        assert s_fresh.recency > s_stale.recency

    def test_redundancy_penalty(self, assembler: ContextAssembler) -> None:
        task_vec = _vec(1.0, 0.0)
        candidate = make_chunk("dup", ContextKind.CODE, embedding=_vec(1.0, 0.0))
        already_selected = [make_chunk("orig", ContextKind.CODE, embedding=_vec(1.0, 0.0))]

        s_no_sel = assembler.score_chunk(candidate, task_vec, "T1", [])
        s_with_sel = assembler.score_chunk(candidate, task_vec, "T1", already_selected)

        # 与已选项近乎重复 → 冗余惩罚拉低总分
        assert s_with_sel.redundancy > 0.99
        assert s_with_sel.total < s_no_sel.total


class TestAssembly:
    def test_hard_constraints_always_included(self, assembler: ContextAssembler) -> None:
        hard = [
            make_chunk("系统指令" * 10, ContextKind.SYSTEM_PROMPT, is_hard=True),
            make_chunk("验收标准" * 10, ContextKind.TASK_SPEC, is_hard=True),
        ]
        soft = [
            make_chunk(f"代码片段{i}" * 50, ContextKind.CODE, embedding=_vec(1.0, 0.0))
            for i in range(20)
        ]
        # 预算极小，但硬约束必须保留
        result = assembler.assemble(
            [*hard, *soft],
            task_embedding=_vec(1.0, 0.0),
            current_step="T1",
            budget=BudgetAllocation(total=200),
            agent=AgentType.CODER,
        )
        hard_ids = {c.id for c in hard}
        selected_ids = {c.id for c in result.chunks}
        assert hard_ids <= selected_ids

    def test_budget_respected_for_soft_chunks(self, assembler: ContextAssembler) -> None:
        soft = [
            make_chunk(f"chunk number {i} " * 20, ContextKind.CODE, embedding=_vec(1.0, 0.0))
            for i in range(50)
        ]
        budget = BudgetAllocation(total=3000)
        result = assembler.assemble(
            soft,
            task_embedding=_vec(1.0, 0.0),
            current_step="T1",
            budget=budget,
            agent=AgentType.CODER,
        )
        # 不超过可用输入预算
        assert result.total_tokens <= budget.available_for_input()

    def test_redundancy_prevents_duplicates(self, assembler: ContextAssembler) -> None:
        # 5 个近乎相同的片段 + 1 个独特的，预算只够 2 个
        dup_emb = _vec(1.0, 0.0)
        dupes = [
            make_chunk(f"重复内容变体{i} " * 30, ContextKind.CODE, embedding=dup_emb)
            for i in range(5)
        ]
        unique = make_chunk("独特的关键内容 " * 30, ContextKind.CODE, embedding=_vec(0.0, 1.0))
        result = assembler.assemble(
            [*dupes, unique],
            task_embedding=_vec(1.0, 0.0),
            current_step="T1",
            budget=BudgetAllocation(total=1200),
            agent=AgentType.CODER,
        )
        ids = [c.id for c in result.chunks]
        # 独特片段应被选中（不与重复项竞争）
        assert unique.id in ids

    def test_compression_ratio_reported(self, assembler: ContextAssembler) -> None:
        chunks = [
            make_chunk(f"content {i} " * 50, ContextKind.CODE, embedding=_vec(1.0, 0.0))
            for i in range(30)
        ]
        result = assembler.assemble(
            chunks,
            task_embedding=_vec(1.0, 0.0),
            current_step="T1",
            budget=BudgetAllocation(total=2000),
            agent=AgentType.CODER,
        )
        assert result.decision.tokens_after < result.decision.tokens_before
        assert 0.0 < result.decision.compression_ratio < 1.0
        assert result.decision.candidates == 30

    def test_empty_candidates(self, assembler: ContextAssembler) -> None:
        result = assembler.assemble(
            [],
            task_embedding=_vec(1.0, 0.0),
            current_step="T1",
            budget=BudgetAllocation(total=1000),
            agent=AgentType.CODER,
        )
        assert result.chunks == []
        assert result.total_tokens == 0


class TestPlacement:
    def test_placement_hard_first_recent_last(self, assembler: ContextAssembler) -> None:
        hard = make_chunk("HARD", ContextKind.SYSTEM_PROMPT, is_hard=True)
        middle = make_chunk("MIDDLE", ContextKind.CODE, age=10)
        recent = make_chunk("RECENT", ContextKind.CODE, age=0)

        ordered = assembler.placement_order([middle, recent, hard])

        assert ordered[0] is hard, "硬约束必须在头部（首位置高注意力区）"
        assert ordered[-1] is recent, "最新内容必须在尾部（尾位置高注意力区）"

    def test_placement_preserves_all_chunks(self, assembler: ContextAssembler) -> None:
        chunks = [make_chunk(f"c{i}", ContextKind.CODE, age=i) for i in range(6)]
        ordered = assembler.placement_order(chunks)
        assert len(ordered) == len(chunks)
        assert {c.id for c in ordered} == {c.id for c in chunks}


class TestMakeChunk:
    def test_auto_token_estimation(self) -> None:
        chunk = make_chunk("hello world", ContextKind.CODE)
        assert chunk.tokens > 0

    def test_hard_flag_propagates(self) -> None:
        chunk = make_chunk("x", ContextKind.TASK_SPEC, is_hard=True)
        assert chunk.is_hard is True

    def test_depends_on_step_as_frozenset(self) -> None:
        chunk = make_chunk("x", ContextKind.CODE, depends_on_step=["A", "B"])
        assert chunk.depends_on_step == frozenset({"A", "B"})


class TestEstimateTokens:
    def test_convenience_function(self) -> None:
        assert estimate_tokens("你好世界") == 4
        assert estimate_tokens("") == 0


class TestScoringWeights:
    def test_from_config(self) -> None:
        from devagent.config import ContextConfig

        cfg = ContextConfig()
        w = ScoringWeights.from_config(cfg)
        assert w.relevance == cfg.weight_relevance
        assert w.redundancy == cfg.weight_redundancy
        assert w.recency_lambda == cfg.recency_lambda
