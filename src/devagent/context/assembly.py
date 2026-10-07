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
             + w6·trust(c)                    # 来源可信度（注入防护）
             - w5·redundancy(c, selected)     # 与已选项的最大余弦相似度

选择算法：类 MMR（Maximal Marginal Relevance）贪心——
每轮对剩余候选**重新打分**，因为冗余项随已选集合动态变化。

位置编排：模型对序列首尾注意力更强（lost-in-the-middle），
因此把硬约束放头部、最新状态放尾部，辅助材料放中间。

信任度项（w6）对齐 ``context/trust.py``：来源越不可信，越需要更高的
相关性才能挤进预算。它**不替代**渲染层的边界标记 —— 打分只决定
"选不选"，边界标记决定"选了之后模型怎么看待它"，两者不可互相替代。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from devagent.config import ContextConfig
from devagent.context.tokenizer import (
    HeuristicTokenCounter,
    TokenCounter,
    Vector,
    cosine_similarity,
    estimate_info_units,
    lexical_similarity,
)
from devagent.context.trust import assess_trust
from devagent.enums import ContextKind
from devagent.models.domain import (
    AssemblyDecision,
    BudgetAllocation,
    ContextChunk,
)


def chunk_similarity(a: ContextChunk, b: ContextChunk) -> float:
    """两个片段的相似度：有向量用余弦，没向量退化为词法 Jaccard。

    **这是本模块最重要的一处兜底**。早先的实现只算余弦相似度，而
    ``cosine_similarity`` 在任一入参为 ``None`` 时返回 0 —— 偏偏
    embedding 是可选能力，生产路径上没有任何片段带向量（需要额外调用
    嵌入模型）。结果是相关性恒为 0、冗余惩罚恒为 0、**硬去重永不触发**，
    整套「加权打分 + 硬去重」退化成「按插入顺序取片段」，
    而单测因为手工构造了向量所以全绿。

    现在语义是：**有精确向量就信向量，没有就必须用词法兜底**，
    保证算法在任何配置下都不会静默失效。
    """
    if a.embedding is not None and b.embedding is not None:
        return cosine_similarity(a.embedding, b.embedding)
    return lexical_similarity(a.content, b.content)


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

    trust: float = 1.0
    """信任度权重。为 0 则完全忽略来源可信度（退回升级前的行为）。

    为什么要给信任度一个**权重**而不是直接乘到总分上：乘以一个常数会
    同时缩小所有片段，等价于把总预算放大 —— 排序不变，只是阈值漂移。
    作为加性项才真正改变排序，让"高可信但相关性一般"的片段有机会
    胜过"低可信但看起来很相关"的片段。这正是注入防护需要的效果。
    """

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
            trust=cfg.weight_trust,
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
    trust: float = 0.0
    trust_level: str = ""
    total: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "relevance": round(self.relevance, 4),
            "recency": round(self.recency, 4),
            "dependency": round(self.dependency, 4),
            "density": round(self.density, 4),
            "redundancy": round(self.redundancy, 4),
            "trust": round(self.trust, 6),
            "trust_level": self.trust_level,
            "total": round(self.total, 4),
        }


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
        task_text: str = "",
    ) -> ScoreBreakdown:
        """计算单个片段的得分明细。

        Args:
            chunk: 待打分片段。
            task_embedding: 当前任务的语义向量，用于相关性计算。
            current_step: 当前步骤 id，用于依赖判定。
            selected: 已选中的片段集合，用于冗余惩罚。
            task_text: 当前任务的**文本**（需求目标等）。当两侧都没有向量时，
                用它做词法相关性兜底 —— 否则相关性项恒为 0（见
                ``chunk_similarity`` 的说明）。
        """
        w = self._weights
        b = ScoreBreakdown(chunk_id=chunk.id)

        # 相关性：优先语义向量，缺失时退化为词法重叠
        if chunk.embedding is not None and task_embedding is not None:
            b.relevance = cosine_similarity(chunk.embedding, task_embedding)
        else:
            b.relevance = lexical_similarity(chunk.content, task_text)

        # 时效：指数衰减，越新越重要
        import math

        b.recency = math.exp(-w.recency_lambda * chunk.age)

        # 依赖：当前步骤是该片段的声明依赖时为 1
        b.dependency = 1.0 if current_step in chunk.depends_on_step else 0.0

        # 信息密度：有效信息单元 / token 数（做归一化上限，防长片段碾压）
        density_raw = chunk.info_units / max(chunk.tokens, 1)
        b.density = min(density_raw * 100.0, 1.0)

        # 冗余：与已选片段的最大相似度，做非线性放大
        # 相似度 1.0 → 罚满；相似度 0.5 → 仅罚 0.5^gamma（gamma=4 → 0.0625）
        # 使得「高度重复」被强压制，而「部分重叠」几乎不受影响。
        max_sim = 0.0
        for s in selected:
            sim = chunk_similarity(chunk, s)
            if sim > max_sim:
                max_sim = sim
        b.redundancy = max(0.0, max_sim) ** w.redundancy_gamma

        # 信任度：来源决定权重，与内容无关（见 context/trust.py）。
        # 放在这里而不是在装配主循环里，是为了让「为什么这个片段被选中」
        # 能在单条打分明细中完整回答 —— 调试注入问题时这是唯一线索。
        assessment = assess_trust(chunk)
        b.trust = assessment.weight
        b.trust_level = assessment.level.name

        b.total = (
            w.relevance * b.relevance
            + w.recency * b.recency
            + w.dependency * b.dependency
            + w.density * b.density
            + w.trust * b.trust
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
        task_text: str = "",
        task_spec_chunks_first: bool = True,  # noqa: ARG002  预留开关，当前位置编排恒按 age 排
    ) -> AssemblyResult:
        """执行装配。

        步骤：
        1. 预算回流（quota 未用满时把余量给 code_context，再取可用上限）；
        2. 硬约束无条件优先纳入（先按内容去重，见下）；
        3. 其余候选按带冗余惩罚的贪心逐轮选择；
        4. 位置编排输出。

        Args:
            candidates: 候选片段。
            task_embedding: 任务语义向量。
            current_step: 当前步骤 id。
            budget: 预算分配。
            agent: 当前 Agent 类型（仅用于决策记录）。
            step_id: 当前步骤 id（用于决策记录）。
            task_text: 任务文本，用于无向量时的相关性兜底。
            task_spec_chunks_first: 预留开关（位置编排由 placement 决定）。
        """
        tokens_before = sum(c.tokens for c in candidates)
        rebalanced = budget.suggest_rebalance()
        limit = rebalanced.available_for_input()

        hard, soft = self._partition(candidates)

        # 硬约束去重：硬片段**不参与**下面的软片段去重循环，也不受预算约束，
        # 因此一旦上游重复投喂（例如节点重试时再次 handoff），它们会无限累积
        # 并挤爆上下文窗口。内容完全相同即视为同一约束，只保留第一份。
        deduped_hard: list[ContextChunk] = []
        seen_content: set[str] = set()
        hard_duplicates = 0
        for chunk in hard:
            # 用内容本身（去首尾空白）而不是 hash()：精确、无碰撞，
            # 且不必依赖进程级哈希随机化。
            fingerprint = chunk.content.strip()
            if fingerprint in seen_content:
                hard_duplicates += 1
                continue
            seen_content.add(fingerprint)
            deduped_hard.append(chunk)
        hard = deduped_hard

        selected: list[ContextChunk] = []
        used = 0

        # ---- 硬约束：永不丢弃（即使超预算也保留，因为丢了会跑偏） ----
        for chunk in hard:
            selected.append(chunk)
            used += chunk.tokens

        # 硬约束本身就超预算时**必须留下痕迹**：此前是静默无限投递，
        # 实际会撞模型上下文窗口（实测预算 2000 时投递 13530 token）。
        hard_overflow = max(0, used - limit)

        # ---- 软片段：贪心选择 ----
        pool = list(soft)
        scores: list[ScoreBreakdown] = []
        dropped = 0
        deduplicated = 0

        while pool and used < limit:
            # 每轮重新打分：冗余度依赖已选集合，必须动态计算
            scored = [
                (c, self.score_chunk(c, task_embedding, current_step, selected, task_text))
                for c in pool
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
            hard_overflow_tokens=hard_overflow,
            # 压缩与否由 L3 压缩器决定，装配层**不能**用 used < tokens_before
            # 来推断（那只是"装配丢弃"，不是"压缩"）。ContextEngine 会用
            # 压缩器的真实结果覆盖这个字段。
            compression_applied=False,
            reason=(
                f"硬约束 {len(hard)} 项优先纳入（去重 {hard_duplicates} 项）；"
                f"软片段 {len(soft)} 项中选中 {len(ordered) - len(hard)} 项，"
                f"去重 {deduplicated} 项，预算 {used}/{limit}"
                + (f"；⚠ 硬约束超预算 {hard_overflow} token" if hard_overflow else "")
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

            [硬约束...] → [辅助材料（越旧越靠前）] → [最新内容（最新在最尾）]

        依据：模型对序列首尾的注意力权重更高，因此把**不可违反的约束**
        放头部保证不被忽略，把**最新任务状态**放尾部保证靠近生成位置
        （自回归模型对紧邻生成位置的内容利用最充分）。

        为什么就是「按 age 降序」这一件事：降序天然满足两个约束 ——
        最旧的在最前（紧跟硬约束之后）、最新的在最后。早先的实现在降序
        序列上又切了"前 N 个当尾部区"，等于把**最旧**的若干项搬到了尾部，
        同时把真正最新的项留在了中部 —— 方向正好相反，而当时的单测只用了
        2 个软片段，恰好掩盖了这个错误。
        """
        hard = [c for c in chunks if c.is_hard]
        soft = [c for c in chunks if not c.is_hard]

        # age 大 = 旧。降序即「越旧越靠前」，且最新者自然落在最末。
        soft_by_age = sorted(soft, key=lambda c: c.age, reverse=True)

        return [*hard, *soft_by_age]

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
        """判断片段是否与已选项冗余（超过阈值）。

        用 ``chunk_similarity`` 而非裸的 ``cosine_similarity``：
        没有向量时必须走词法兜底，否则去重恒不触发（见该函数说明）。
        """
        threshold = self._weights.redundancy_cosine_threshold
        return any(chunk_similarity(chunk, s) > threshold for s in selected)


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
    units = info_units if info_units is not None else estimate_info_units(content)
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
# 注：Any 已在顶部导入（ScoreBreakdown.to_dict 需要），此处保留说明以免
# 后来者误以为可以安全删除顶部那一行。

__all__ = [
    "AssemblyResult",
    "ContextAssembler",
    "ScoreBreakdown",
    "ScoringWeights",
    "chunk_similarity",
    "estimate_tokens",
    "make_chunk",
]
