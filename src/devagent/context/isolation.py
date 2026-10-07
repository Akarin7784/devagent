"""上下文隔离（L2）与聚合门面。

对齐 ``docs/02-上下文工程深度设计.md`` 的隔离设计。

核心原则：**每个 Agent 一个独立上下文空间**。
Agent 之间不共享原始对话，只通过 ``AgentHandoff`` 传递结构化摘要 +
引用（``context_refs``），从而：

- **防污染**：Coder 的调试噪音不会干扰 Reviewer 的判断；
- **防膨胀**：上下文不会随协作轮次线性增长；
- **防角色混淆**：每个 Agent 只看到与自己职责相关的信息。

本模块还提供 ``ContextEngine`` 门面，把
路由 / 隔离 / 压缩 / 装配 / 预算 五层能力串联为单一入口。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from devagent.config import ContextConfig
from devagent.context.assembly import (
    AssemblyResult,
    ContextAssembler,
    ScoringWeights,
    make_chunk,
)
from devagent.context.budget import BudgetAllocator
from devagent.context.compression import CompressionResult, ContextCompressor
from devagent.context.routing import ComplexityRouter
from devagent.context.tokenizer import TokenCounter, Vector
from devagent.context.trust import InjectionGuard
from devagent.enums import AgentType, ContextKind
from devagent.models.domain import (
    AgentHandoff,
    BudgetAllocation,
    ContextChunk,
    RoutingSignals,
)
from devagent.observability import get_observability


class ContextPolicyError(RuntimeError):
    """上下文策略被违反（如试图访问他人上下文空间）。"""


@dataclass(slots=True)
class AgentContextSpace:
    """单个 Agent 的独立上下文空间。

    只接纳显式投喂的片段，不自动继承其他 Agent 的上下文。
    """

    agent: AgentType
    chunks: list[ContextChunk] = field(default_factory=list)
    handoff: AgentHandoff | None = None

    def add(self, chunk: ContextChunk) -> None:
        self.chunks.append(chunk)

    def extend(self, chunks: Iterable[ContextChunk]) -> None:
        self.chunks.extend(chunks)

    def drop_source_prefix(self, prefix: str) -> int:
        """移除 ``source`` 以 ``prefix`` 开头的片段，返回移除条数。

        用途：同一次交付（handoff / 验证）重跑时，旧片段必须被**替换**而不是
        继续追加。否则节点每重试一次，目标/验收标准/约束就在空间里多一份，
        且硬约束不受预算约束 —— 重试几次就能把上下文窗口挤爆。
        """
        before = len(self.chunks)
        self.chunks = [c for c in self.chunks if not c.source.startswith(prefix)]
        return before - len(self.chunks)

    @property
    def total_tokens(self) -> int:
        return sum(c.tokens for c in self.chunks)

    def clear(self) -> None:
        self.chunks.clear()


class ContextIsolator:
    """上下文隔离容器：管理各 Agent 的独立空间。

    关键规则：
    - 每个 Agent 拥有自己的空间；
    - 跨空间读取必须显式声明（``borrow``），并在快照中留痕；
    - Verifier 空间**强制排除**上游的自我解释类片段。
    """

    def __init__(self) -> None:
        self._spaces: dict[AgentType, AgentContextSpace] = {}
        self._borrow_log: list[dict[str, str]] = []

    def space_for(self, agent: AgentType) -> AgentContextSpace:
        """获取（或创建）某 Agent 的上下文空间。"""
        if agent not in self._spaces:
            self._spaces[agent] = AgentContextSpace(agent=agent)
        return self._spaces[agent]

    def reset(self) -> None:
        self._spaces.clear()
        self._borrow_log.clear()

    def snapshot(self) -> ContextIsolator:
        """复制当前隔离容器，得到一份**互不影响**的新容器。

        为什么要它：编排器全进程只有一个实例，而服务层允许并发跑多个任务。
        如果各任务共用同一个 isolator，任务 B 的 Agent 就会在同一个空间里
        读到任务 A 的需求原文（已实测复现）——「上下文隔离」在**任务之间**
        完全失效。每次 ``run()`` 从快照开始，运行期间的写入只落在本次运行
        的副本里，任务之间重新隔离；同时保留了「运行前预置片段」这种用法
        （调用方先往 ``ContextEngine.isolator`` 里塞内容，作为初始上下文）。
        """
        clone = ContextIsolator()
        for agent, space in self._spaces.items():
            new_space = AgentContextSpace(agent=agent, chunks=list(space.chunks))
            new_space.handoff = space.handoff
            clone._spaces[agent] = new_space
        clone._borrow_log = list(self._borrow_log)
        return clone

    def borrow(
        self,
        *,
        borrower: AgentType,
        source: AgentType,
        reason: str,
    ) -> list[ContextChunk]:
        """显式借用其他 Agent 的上下文片段。

        设计上禁止「隐式可见」：任何跨空间读取都必须经过此方法并留痕，
        便于审计与调试。

        Args:
            borrower: 借用方。
            source: 出让方。
            reason: 借用理由（用于审计）。

        Returns:
            出让方可见的片段副本。

        Raises:
            ContextPolicyError: 当借出方为 VERIFIER 时——
                Verifier 的验证视角必须保持独立，其上下文不可被借用。
        """
        if source is AgentType.VERIFIER:
            raise ContextPolicyError(
                "禁止借用 Verifier 的上下文：验证视角必须独立，否则交叉验证能力会被污染。"
            )
        chunks = list(self.space_for(source).chunks)
        self._borrow_log.append(
            {
                "borrower": borrower.value,
                "source": source.value,
                "reason": reason,
                "chunks": str(len(chunks)),
            }
        )
        return chunks

    @property
    def borrow_log(self) -> list[dict[str, str]]:
        """借用审计日志。"""
        return list(self._borrow_log)

    def handoff_to(self, agent: AgentType, handoff: AgentHandoff) -> AgentContextSpace:
        """把结构化交付物投喂给目标 Agent 的独立空间。

        注意：只投喂 **handoff 的结构化字段**（目标/验收标准/约束/引用），
        不投喂上游的原始对话历史。这正是隔离的核心机制。

        同一次交付重跑（例如节点被 Verifier 驳回后重试）时，**先移除上一轮
        投喂的 handoff 片段再写入**。理由：这些片段是 ``is_hard=True`` 的，
        而硬约束在装配阶段无条件纳入、且不参与软片段去重 —— 若只追加，
        每次重试都会让目标/标准/约束多一份，几轮之后硬约束自己就把
        上下文窗口撑爆（预算护栏对硬约束不生效）。
        """
        space = self.space_for(agent)
        space.drop_source_prefix("handoff://")

        # 硬约束：目标与验收标准、约束
        if handoff.goal:
            space.add(
                make_chunk(
                    f"# 任务目标\n{handoff.goal}",
                    ContextKind.TASK_SPEC,
                    is_hard=True,
                    source=f"handoff://{handoff.task_id}",
                )
            )
        if handoff.acceptance_criteria:
            criteria = "\n".join(f"- [ ] {c}" for c in handoff.acceptance_criteria)
            space.add(
                make_chunk(
                    f"# 验收标准（必须逐条满足）\n{criteria}",
                    ContextKind.TASK_SPEC,
                    is_hard=True,
                    source=f"handoff://{handoff.task_id}",
                )
            )
        if handoff.constraints:
            constraints = "\n".join(f"- {c}" for c in handoff.constraints)
            space.add(
                make_chunk(
                    f"# 硬约束（不可违反）\n{constraints}",
                    ContextKind.TASK_SPEC,
                    is_hard=True,
                    source=f"handoff://{handoff.task_id}",
                )
            )

        space.handoff = handoff
        return space


@dataclass(slots=True)
class ContextBundle:
    """装配后的最终上下文：可直接渲染为 messages 交给模型。"""

    agent: AgentType
    chunks: list[ContextChunk]
    decision: object  # AssemblyDecision（避免循环导入用 object）
    budget: BudgetAllocation

    @property
    def total_tokens(self) -> int:
        return sum(c.tokens for c in self.chunks)

    def render(self, *, separator: str = "\n\n---\n\n") -> str:
        """把片段渲染为单一文本（用于 prompt 拼接）。

        **不包含信任边界。** 需要边界时用 ``render_guarded()`` ——
        保留这个方法是为了让对比实验（"开/关防护"）能拿到基线文本。
        """
        return separator.join(c.content for c in self.chunks)

    def render_guarded(
        self,
        guard: InjectionGuard,
        *,
        separator: str = "\n\n---\n\n",
    ) -> str:
        """渲染为带信任边界的文本（注入防护）。

        与 ``render()`` 的差异**只在不可信片段上**：全部可信时两者
        逐字节相同，因此开启防护对可信路径零成本、零影响。
        """
        text = guard.render(self.chunks)
        # guard.render 内部固定用 "---" 分隔；这里只在需要替换分隔符时接管，
        # 以免让 guard 感知"分隔符"这种纯展示参数。
        if separator == "\n\n---\n\n":
            return text
        return text.replace("\n\n---\n\n", separator)

    def by_kind(self, kind: ContextKind) -> list[ContextChunk]:
        return [c for c in self.chunks if c.kind is kind]

    @property
    def trust_summary(self) -> dict[str, object]:
        """各信任等级的片段分布（供报告与前端展示）。"""
        return InjectionGuard().summarize(self.chunks)


class ContextEngine:
    """上下文工程门面：串联五层能力。

    用法::

        engine = ContextEngine(settings.context)
        engine.isolator.handoff_to(AgentType.CODER, handoff)
        bundle = await engine.build(
            agent=AgentType.CODER,
            task_embedding=vec,
            current_step="T-003",
        )
    """

    def __init__(
        self,
        config: ContextConfig,
        *,
        assembler: ContextAssembler | None = None,
        allocator: BudgetAllocator | None = None,
        compressor: ContextCompressor | None = None,
        router: ComplexityRouter | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self._config = config
        self.isolator = ContextIsolator()
        self._assembler = assembler or ContextAssembler(
            weights=ScoringWeights.from_config(config),
            token_counter=token_counter,
        )
        self._allocator = allocator or BudgetAllocator()
        self._compressor = compressor or ContextCompressor(
            token_counter=token_counter,
            protect_hard=config.hard_constraint_untouchable,
        )
        self._router = router or ComplexityRouter()
        # 注入防护：默认按配置开关。关闭时 render() 与 render_guarded()
        # 输出一致，因此调用方无需分支判断。
        self._guard = InjectionGuard(enabled=config.injection_guard)

    # ------------------------------------------------------------------ #
    # 装配入口
    # ------------------------------------------------------------------ #

    async def build(
        self,
        *,
        agent: AgentType,
        task_embedding: Vector | None,
        current_step: str,
        budget_total: int | None = None,
        routing_signals: RoutingSignals | None = None,
        extra_chunks: Sequence[ContextChunk] = (),
        isolator: ContextIsolator | None = None,
        task_id: str = "",
        task_text: str = "",
    ) -> ContextBundle:
        """为指定 Agent 构建最终上下文。

        流程：取隔离空间 → （必要时）压缩 → 装配 → 附加路由决策。

        Args:
            isolator: 使用哪个隔离容器。编排器每次运行传自己的**运行级副本**，
                避免并发任务互相污染；不传则退化为实例级容器（单任务用法）。
            task_id: 任务标识，用于把上下文工程指标归因到具体任务
                （否则前端「本任务节省了多少 token」只能显示进程累计值）。
            task_text: 任务文本，无向量时用于相关性兜底。
        """
        active_isolator = isolator or self.isolator
        space = active_isolator.space_for(agent)
        if extra_chunks:
            space.extend(extra_chunks)

        total = budget_total or self._config.default_budget
        budget = self._allocator.allocate(agent, total=total)

        candidates = list(space.chunks)
        tokens_before_compression = sum(c.tokens for c in candidates)

        # 超阈值则先压缩（只在步骤边界调用本方法，满足「不在推理中途压缩」）
        compressed_flag = False
        if self._compressor.should_compress(
            tokens_before_compression, budget.total, self._config.compression_threshold
        ):
            compressed: CompressionResult = await self._compressor.compress(candidates)
            candidates = compressed.chunks
            compressed_flag = True

        result: AssemblyResult = self._assembler.assemble(
            candidates,
            task_embedding=task_embedding,
            current_step=current_step,
            budget=budget,
            agent=agent,
            step_id=current_step,
            task_text=task_text,
        )

        # 用装配决策的 tokens_before/tokens_after 反映**装配阶段**的取舍，
        # 用 compressed_flag 单独标记是否发生了**压缩阶段**的处理。
        # 二者语义不同，不可混用（早期实现曾用 used<tokens_before 判断，
        # 导致「装配丢弃」被误报为「压缩」）。
        decision = result.decision.model_copy(
            update={
                "compression_applied": compressed_flag,
                "tokens_before": tokens_before_compression,
            }
        )
        if routing_signals is not None:
            tier = self._router.select_tier(routing_signals)
            decision = decision.model_copy(update={"routing_tier": tier})

        # 可观测性：量化上下文工程的实际收益（省了多少 token、预算用掉多少）。
        # 这是本项目最核心的价值指标——它把「上下文工程」从口号变成可测数字。
        # 带 task_id 标签是必须的：不带就只能给出**进程累计值**，
        # 而前端把它当作「本任务」的数字展示（实测两个任务拿到完全相同的值）。
        get_observability().record_context_build(
            agent=agent.value,
            tokens_before=tokens_before_compression,
            tokens_after=decision.tokens_after,
            budget=budget.total,
            dropped_chunks=max(0, len(candidates) - len(result.chunks)),
            task_id=task_id,
            hard_overflow=decision.hard_overflow_tokens,
        )

        return ContextBundle(
            agent=agent,
            chunks=result.chunks,
            decision=decision,
            budget=budget,
        )

    # ------------------------------------------------------------------ #
    # 便捷访问器
    # ------------------------------------------------------------------ #

    @property
    def assembler(self) -> ContextAssembler:
        return self._assembler

    @property
    def allocator(self) -> BudgetAllocator:
        return self._allocator

    @property
    def router(self) -> ComplexityRouter:
        return self._router

    @property
    def guard(self) -> InjectionGuard:
        """注入防护器（供 Agent 渲染上下文时使用）。"""
        return self._guard


__all__ = [
    "AgentContextSpace",
    "ContextBundle",
    "ContextEngine",
    "ContextIsolator",
    "ContextPolicyError",
]
