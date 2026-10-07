"""上下文装配算法（本项目核心算法）。

对齐 ``docs/02-上下文工程深度设计.md`` 的 L4 装配设计。

核心思想
--------
在 token 预算约束下，从候选片段中选出**最该被模型看到**的子集，并按
位置编排输出。不是简单截断，而是加权打分 + 冗余惩罚的贪心选择。

打分函数::

    score(c) = w1·relevance(c, task)          # 语义相关性
             + w2·recency(c)                  # 时效衰减 e^(-λ·age)
             + w3·dependency(c, step)         # 是否为当前步骤硬依赖
             + w4·density(c)                  # 信息密度 = 有效信息 / tokens
             - w5·redundancy(c, selected)     # 与已选项的最大余弦相似度

选择算法：类 MMR（Maximal Marginal Relevance）贪心——
每轮对剩余候选**重新打分**，因为冗余项随已选集合动态变化。

位置编排：模型对序列首尾注意力更强（lost-in-the-middle），
因此把硬约束放头部、最新状态放尾部，辅助材料放中间。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from devagent.config import ContextConfig
from devagent.context.tokenizer import (
    HeuristicTokenCounter,
    TokenCounter,
    Vector,
    cosine_similarity,
)
from devagent.enums import ContextKind
from devagent.models.domain import (
    AssemblyDecision,
    BudgetAllocation,
    ContextChunk,
)


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """装配打分权重。

    默认值来自 ``ContextConfig``，写入 dataclass 以便算法内部高效访问。

    ``redundancy`` 采用**非线性放大**：相似度越高，惩罚增长越快
    （``sim ** redundancy_gamma``）。这样「与已选项高度重复」的片段会被
    显著压制，而「部分相关」的片段仍保留竞争力。
    """

    relevance: float = 1.0
    recency: float = 0.5
    dependency: float = 2.0
    density: float = 0.3
    redundancy: float = 0.8
    recency_lambda: float = 0.15
    redundancy_cosine_threshold: float = 0.90
    redundancy_gamma: float = 4.0
    """冗余惩罚的非线性指数：越大则「高度重复」被压制得越狠。"""

    @classmethod
    def from_config(cls, cfg: ContextConfig) -> ScoringWeights:
        return cls(
            relevance=cfg.weight_relevance,
            recency=cfg.weight_recency,
            dependency=cfg.weight_dependency,
            density=cfg.weight_density,
            redundancy=cfg.weight_redundancy,
            recency_lambda=cfg.recency_lambda,
            redundancy_cosine_threshold=cfg.redundancy_cosine_threshold,
            redundancy_gamma=cfg.redundancy_gamma,
        )


@dataclass(slots=True)
class ScoreBreakdown:
    """单个片段的打分明细（用于调试与可视化）。"""

    chunk_id: str
    relevance: float = 0.0
    recency: float = 0.0
    dependency: float = 0.0
    density: float = 0.0
    redundancy: float = 0.0
    total: float = 0.0


@dataclass(slots=True)
class AssemblyResult:
    """装配结果：选中的片段序列 + 决策记录。"""

    chunks: list[ContextChunk]
    decision: AssemblyDecision
    scores: list[ScoreBreakdown] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(c.tokens for c in self.chunks)


class ContextAssembler:
    """上下文装配器。

    无状态（除注入的 tokenizer 与权重），可安全并发复用。

    用法::

        assembler = ContextAssembler()
        result = assembler.assemble(
            candidates=chunks,
            task_embedding=task_vec,
            current_step="T-003",
            budget=BudgetAllocation(total=16000),
            agent=AgentType.CODER,
            step_id="T-003",
        )
    """

    def __init__(
        self,
        weights: ScoringWeights | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self._weights = weights or ScoringWeights()
        self._counter = token_counter or HeuristicTokenCounter()

    # ------------------------------------------------------------------ #
    # 打分
    # ------------------------------------------------------------------ #

    def score_chunk(
        self,
        chunk: ContextChunk,
        task_embedding: Vector | None,
        current_step: str,
        selected: Sequence[ContextChunk],
    ) -> ScoreBreakdown:
        """计算单个片段的得分明细。

        Args:
            chunk: 待打分片段。
            task_embedding: 当前任务的语义向量，用于相关性计算。
            current_step: 当前步骤 id，用于依赖判定。
            selected: 已选中的片段集合，用于冗余惩罚。
        """
        w = self._weights
        b = ScoreBreakdown(chunk_id=chunk.id)

        # 相关性：与任务语义向量的余弦相似度
        b.relevance = cosine_similarity(chunk.embedding, task_embedding)

        # 时效：指数衰减，越新越重要
        import math

        b.recency = math.exp(-w.recency_lambda * chunk.age)

        # 依赖：当前步骤是该片段的声明依赖时为 1
        b.dependency = 1.0 if current_step in chunk.depends_on_step else 0.0

        # 信息密度：有效信息单元 / token 数（做归一化上限，防长片段碾压）
        density_raw = chunk.info_units / max(chunk.tokens, 1)
        b.density = min(density_raw * 100.0, 1.0)

        # 冗余：与已选片段的最大余弦相似度，做非线性放大
        # 相似度 1.0 → 罚满；相似度 0.5 → 仅罚 0.5^gamma（gamma=4 → 0.0625）
        # 使得「高度重复」被强压制，而「部分重叠」几乎不受影响。
        max_sim = 0.0
        for s in selected:
            sim = cosine_similarity(chunk.embedding, s.embedding)
            if sim > max_sim:
                max_sim = sim
        b.redundancy = max(0.0, max_sim) ** w.redundancy_gamma

        b.total = (
            w.relevance * b.relevance
            + w.recency * b.recency
            + w.dependency * b.dependency
            + w.density * b.density
            - w.redundancy * b.redundancy
        )
        return b

    # ------------------------------------------------------------------ #
    # 装配主流程
    # ------------------------------------------------------------------ #

    def assemble(
        self,
        candidates: Sequence[ContextChunk],
        *,
        task_embedding: Vector | None,
        current_step: str,
        budget: BudgetAllocation,
        agent: Any = None,  # AgentType，避免循环导入用 Any
        step_id: str = "",
        task_spec_chunks_first: bool = True,  # noqa: ARG002  预留开关，当前位置编排恒按 age 排
    ) -> AssemblyResult:
        """执行装配。

        步骤：
        1. 预算回流（quota 未用满时把余量给 code_context，再取可用上限）；
        2. 硬约束无条件优先纳入；
        3. 其余候选按带冗余惩罚的贪心逐轮选择；
        4. 位置编排输出。

        Args:
            candidates: 候选片段。
            task_embedding: 任务语义向量。
            current_step: 当前步骤 id。
            budget: 预算分配。
            agent: 当前 Agent 类型（仅用于决策记录）。
            step_id: 当前步骤 id（用于决策记录）。
            task_spec_chunks_first: 预留开关（位置编排由 placement 决定）。
        """
        tokens_before = sum(c.tokens for c in candidates)
        rebalanced = budget.suggest_rebalance()
        limit = rebalanced.available_for_input()

        hard, soft = self._partition(candidates)

        selected: list[ContextChunk] = []
        used = 0

        # ---- 硬约束：永不丢弃（即使超预算也保留，因为丢了会跑偏） ----
        for chunk in hard:
            selected.append(chunk)
            used += chunk.tokens

        # ---- 软片段：贪心选择 ----
        pool = list(soft)
        scores: list[ScoreBreakdown] = []
        dropped = 0
        deduplicated = 0

        while pool and used < limit:
            # 每轮重新打分：冗余度依赖已选集合，必须动态计算
            scored = [
                (c, self.score_chunk(c, task_embedding, current_step, selected)) for c in pool
            ]
            best_chunk, best_score = max(scored, key=lambda pair: pair[1].total)

            if used + best_chunk.tokens > limit:
                pool.remove(best_chunk)
                dropped += 1
                continue

            selected.append(best_chunk)
            scores.append(best_score)
            used += best_chunk.tokens
            pool.remove(best_chunk)

            # 硬性去重：移除与刚选中片段高度重复的候选。
            # 理由：软打分只能"降权"，无法保证低相关但不冗余的片段胜出；
            # 硬去重确保预算不被近似重复的内容挤占。
            before = len(pool)
            pool = [c for c in pool if not self.is_redundant(c, [best_chunk])]
            deduplicated += before - len(pool)

        dropped += len(pool)

        ordered = self.placement_order(selected)
        decision = AssemblyDecision(
            step_id=step_id or current_step,
            agent=agent,
            candidates=len(candidates),
            selected=len(ordered),
            dropped=dropped,
            hard_constraints=len(hard),
            tokens_before=tokens_before,
            tokens_after=used,
            budget_total=budget.total,
            compression_applied=used < tokens_before,
            reason=(
                f"硬约束 {len(hard)} 项优先纳入；"
                f"软片段 {len(soft)} 项中选中 {len(ordered) - len(hard)} 项，"
                f"去重 {deduplicated} 项，预算 {used}/{limit}"
            ),
        )
        return AssemblyResult(chunks=ordered, decision=decision, scores=scores)

    # ------------------------------------------------------------------ #
    # 位置编排
    # ------------------------------------------------------------------ #

    @staticmethod
    def placement_order(chunks: Sequence[ContextChunk]) -> list[ContextChunk]:
        """位置编排：对抗 lost-in-the-middle。

        输出顺序（从首到尾）::

            [硬约束...] → [辅助材料...（越旧越靠前）] → [最新内容...（最新在最尾）]

        依据：模型对序列首尾的注意力权重更高，因此把**不可违反的约束**
        放头部保证不被忽略，把**最新任务状态**放尾部保证靠近生成位置
        （自回归模型对紧邻生成位置的内容利用最充分）。
        """
        hard = [c for c in chunks if c.is_hard]
        soft = [c for c in chunks if not c.is_hard]

        # 越旧越靠前：按 age 降序排列（age 大 = 旧在前）
        soft_by_age = sorted(soft, key=lambda c: c.age, reverse=True)
        newest_count = min(2, len(soft_by_age))
        recent = soft_by_age[:newest_count]  # 尾部区（age 最小的最新）
        middle = soft_by_age[newest_count:]  # 中部区（更旧）

        return [*hard, *middle, *recent]

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _partition(
        candidates: Sequence[ContextChunk],
    ) -> tuple[list[ContextChunk], list[ContextChunk]]:
        """按是否硬约束切分。"""
        hard: list[ContextChunk] = []
        soft: list[ContextChunk] = []
        for c in candidates:
            (hard if c.is_hard else soft).append(c)
        return hard, soft

    def is_redundant(self, chunk: ContextChunk, selected: Sequence[ContextChunk]) -> bool:
        """判断片段是否与已选项冗余（超过阈值）。"""
        threshold = self._weights.redundancy_cosine_threshold
        return any(cosine_similarity(chunk.embedding, s.embedding) > threshold for s in selected)


def estimate_tokens(text: str, counter: TokenCounter | None = None) -> int:
    """便捷函数：估算文本 token 数。"""
    return (counter or HeuristicTokenCounter()).count(text)


def make_chunk(
    content: str,
    kind: ContextKind,
    *,
    embedding: Vector | None = None,
    age: int = 0,
    depends_on_step: Iterable[str] = (),
    is_hard: bool = False,
    source: str = "",
    info_units: int | None = None,
    counter: TokenCounter | None = None,
    **meta: object,
) -> ContextChunk:
    """构建 ``ContextChunk`` 的便捷工厂，自动估算 token。

    避免调用方到处手写 ``tokens=estimate_tokens(...)``。
    """
    c = counter or HeuristicTokenCounter()
    tokens = c.count(content)
    units = info_units if info_units is not None else max(1, len(content.split(". ")))
    return ContextChunk(
        content=content,
        kind=kind,
        tokens=tokens,
        embedding=embedding,
        age=age,
        depends_on_step=frozenset(depends_on_step),
        info_units=units,
        is_hard=is_hard,
        source=source,
        meta=dict(meta),
    )


# 延迟导入 Any 避免顶部循环依赖（AgentType 仅用于类型标注）
from typing import Any  # noqa: E402

__all__ = [
    "AssemblyResult",
    "ContextAssembler",
    "ScoreBreakdown",
    "ScoringWeights",
    "estimate_tokens",
    "make_chunk",
]
