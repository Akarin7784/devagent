"""上下文装配算法的行为契约测试。

与 ``test_assembly.py`` 的区别：这里不测「单元」，而测**算法性质**
（property-based 思路），验证设计意图在边界条件下依然成立。

重点验证软打分 + 硬去重的双保险机制。
"""

from __future__ import annotations

import numpy as np
import pytest

from devagent.context.assembly import ContextAssembler, make_chunk
from devagent.enums import AgentType, ContextKind
from devagent.models.domain import BudgetAllocation


def _vec(*values: float) -> np.ndarray:
    return np.array(values, dtype=np.float64)


@pytest.fixture
def assembler() -> ContextAssembler:
    return ContextAssembler()


class TestDeduplicationContract:
    """硬去重机制的行为契约。

    背景（真实踩坑）：仅靠加权打分的冗余惩罚不足以保证去重——
    当某片段相关性满分（relevance=1.0）时，即使冗余惩罚拉满，
    其总分仍可能高于「低相关但不冗余」的片段，导致重复内容挤占预算。
    因此引入硬去重作为第二道保险。
    """

    def test_high_relevance_cannot_starve_unique_content(self, assembler: ContextAssembler) -> None:
        """高相关但重复的内容不应挤掉低相关但不冗余的内容。

        这是设计意图的核心契约：预算有限时，
        **信息多样性** 优先于 **单点高相关**。
        """
        task_vec = _vec(1.0, 0.0)
        dupes = [
            make_chunk(f"高相关重复内容 {i} " * 30, ContextKind.CODE, embedding=task_vec)
            for i in range(5)
        ]
        unique = make_chunk("低相关但独特的内容 " * 30, ContextKind.CODE, embedding=_vec(0.0, 1.0))

        result = assembler.assemble(
            [*dupes, unique],
            task_embedding=task_vec,
            current_step="T1",
            budget=BudgetAllocation(total=1500),
            agent=AgentType.CODER,
        )

        selected_ids = {c.id for c in result.chunks}
        assert unique.id in selected_ids, "独特内容必须被选中，避免信息同质化"

    def test_no_near_duplicate_pair_selected(self, assembler: ContextAssembler) -> None:
        """任意两个被选中的片段不应近似重复。"""
        emb = _vec(1.0, 0.0)
        chunks = [make_chunk(f"内容 {i} " * 40, ContextKind.CODE, embedding=emb) for i in range(10)]
        result = assembler.assemble(
            chunks,
            task_embedding=emb,
            current_step="T1",
            budget=BudgetAllocation(total=5000),
            agent=AgentType.CODER,
        )
        # 全部候选互相近似重复 → 最多只应选中 1 个
        assert len(result.chunks) == 1, (
            f"近似重复的候选最多选 1 个，实际选中 {len(result.chunks)} 个"
        )

    def test_diverse_chunks_all_survive(self, assembler: ContextAssembler) -> None:
        """互不冗余的片段应尽可能都被选中（预算充足时）。"""
        from devagent.context.tokenizer import cosine_similarity

        # 构造 4 个两两正交的向量（4 维空间）
        chunks = [
            make_chunk(
                f"独特内容 {i} " * 20,
                ContextKind.CODE,
                embedding=_vec(*(1.0 if j == i else 0.0 for j in range(4))),
            )
            for i in range(4)
        ]
        # 校验构造的向量确实两两正交
        for i in range(4):
            for j in range(i + 1, 4):
                sim = cosine_similarity(chunks[i].embedding, chunks[j].embedding)
                assert abs(sim) < 1e-9

        result = assembler.assemble(
            chunks,
            task_embedding=_vec(1.0, 0.0, 0.0, 0.0),
            current_step="T1",
            budget=BudgetAllocation(total=20_000),
            agent=AgentType.CODER,
        )
        assert len(result.chunks) == 4


class TestBudgetContract:
    """预算约束的行为契约。"""

    def test_total_never_exceeds_input_budget(self, assembler: ContextAssembler) -> None:
        """选中片段的总 token 不得超过可用输入预算（无硬约束时）。"""
        chunks = [
            make_chunk(f"chunk {i} " * 30, ContextKind.CODE, embedding=_vec(1.0, 0.0))
            for i in range(40)
        ]
        for total in (500, 1000, 3000, 8000):
            budget = BudgetAllocation(total=total)
            result = assembler.assemble(
                chunks,
                task_embedding=_vec(1.0, 0.0),
                current_step="T1",
                budget=budget,
                agent=AgentType.CODER,
            )
            assert result.total_tokens <= budget.available_for_input(), f"total={total} 时超出预算"

    def test_hard_constraints_may_exceed_budget(self, assembler: ContextAssembler) -> None:
        """硬约束可以突破预算（设计选择：丢约束比超预算危害更大）。"""
        hard = make_chunk("不可违反的系统指令 " * 100, ContextKind.SYSTEM_PROMPT, is_hard=True)
        result = assembler.assemble(
            [hard],
            task_embedding=_vec(1.0, 0.0),
            current_step="T1",
            budget=BudgetAllocation(total=50),
            agent=AgentType.CODER,
        )
        assert hard.id in {c.id for c in result.chunks}

    def test_tiny_budget_does_not_crash(self, assembler: ContextAssembler) -> None:
        """极小预算（低于默认输出预留）不应崩溃，且行为可预期。"""
        chunks = [make_chunk("内容 " * 50, ContextKind.CODE, embedding=_vec(1.0, 0.0))]
        result = assembler.assemble(
            chunks,
            task_embedding=_vec(1.0, 0.0),
            current_step="T1",
            budget=BudgetAllocation(total=10),
            agent=AgentType.CODER,
        )
        # 预算过小 → 无选中，但不报错
        assert result.total_tokens == 0
        assert result.decision.candidates == 1


class TestMonotonicityContract:
    """单调性契约：更大预算不应选出更少内容。"""

    def test_more_budget_selects_at_least_as_much(self, assembler: ContextAssembler) -> None:
        chunks = [
            make_chunk(
                f"片段 {i} " * 25,
                ContextKind.CODE,
                embedding=_vec(1.0, 0.0, 0.0) if i % 3 else _vec(0.0, 1.0, 0.0),
            )
            for i in range(20)
        ]
        prev_tokens = -1
        for total in (2000, 4000, 8000, 16000):
            result = assembler.assemble(
                chunks,
                task_embedding=_vec(1.0, 0.0, 0.0),
                current_step="T1",
                budget=BudgetAllocation(total=total),
                agent=AgentType.CODER,
            )
            assert result.total_tokens >= prev_tokens, f"预算 {total} 时选中 token 数少于更小预算"
            prev_tokens = result.total_tokens


class TestDeterminismContract:
    """确定性契约：相同输入必须产生相同输出。"""

    def test_same_input_same_output(self, assembler: ContextAssembler) -> None:
        chunks = [
            make_chunk(f"c{i} " * 30, ContextKind.CODE, embedding=_vec(float(i % 3), 1.0))
            for i in range(15)
        ]
        budget = BudgetAllocation(total=4000)
        results = [
            assembler.assemble(
                chunks,
                task_embedding=_vec(1.0, 0.0),
                current_step="T1",
                budget=budget,
                agent=AgentType.CODER,
            )
            for _ in range(3)
        ]
        id_lists = [[c.id for c in r.chunks] for r in results]
        assert id_lists[0] == id_lists[1] == id_lists[2]
