"""跨模块共享的领域模型。

本模块只依赖 ``enums`` 与 ``pydantic``，位于依赖图底层，
供上层（context / agents / orchestration / models）复用。

设计原则：
- 全部使用不可变（``frozen``）模型，避免跨模块意外修改；
- 所有对外传递的字段都应是**结构化**的，杜绝「传一段自然语言」。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from devagent.enums import (
    AgentType,
    ContextKind,
    FailureKind,
    MessageType,
    ModelTier,
    Verdict,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class _ImmutableModel(BaseModel):
    """所有领域模型的基类：不可变 + 禁止未声明字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# 消息协议
# --------------------------------------------------------------------------- #


class ArtifactRef(_ImmutableModel):
    """产出物引用。

    刻意**不内联内容**，只保存引用与元信息，
    以便上下文工程层按需回捞、并可被压缩为指针。
    """

    uri: str = Field(description="形如 file://src/api/users.py, patch://a1b2c3")
    kind: Literal["file", "patch", "test_report", "report", "other"] = "file"
    summary: str = Field(default="", description="一句话描述，供摘要使用")
    size_bytes: int = Field(default=0, ge=0)
    checksum: str | None = None


class ContextRef(_ImmutableModel):
    """上下文引用。

    形如 ``decision://D-002``、``schema://user_model``，
    供 Agent 在需要时按需回捞完整内容。
    """

    uri: str
    note: str = ""


class AgentHandoff(_ImmutableModel):
    """Agent 之间的**结构化握手**载荷。

    这是本项目「结构化握手机制」的核心数据结构：
    上游 Agent 完成后，把下游所需信息以强类型形式传递，
    而不是把整段对话历史丢过去。

    好处：可校验、防歧义、可压缩、可追踪、便于程序化处理。
    """

    task_id: str
    goal: str = Field(description="目标（一句话）")
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description="验收标准：必须可逐条校验，Verifier 据此判断",
    )
    constraints: list[str] = Field(default_factory=list, description="硬约束，永不压缩")
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    context_refs: list[ContextRef] = Field(default_factory=list)
    relevant_files: list[str] = Field(
        default_factory=list, description="相关文件（由 AST 索引召回）"
    )
    budget_tokens: int = Field(default=16_000, gt=0)

    @field_validator("acceptance_criteria")
    @classmethod
    def _criteria_not_empty_for_verifiable_tasks(cls, v: list[str]) -> list[str]:
        # 允许为空（如纯澄清阶段），但拒绝空白项
        return [item.strip() for item in v if item.strip()]


class FeedbackPayload(_ImmutableModel):
    """评审/验证反馈载荷。

    失败回退**必须携带可操作信息**，而非简单的「重试」。
    """

    verdict: Verdict
    failed_criteria: list[str] = Field(
        default_factory=list, description="未满足的验收标准（逐条列出）"
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="客观证据：测试用例名、实际输出、期望输出",
    )
    suggestions: list[str] = Field(default_factory=list, description="修复建议")
    lesson: str | None = Field(
        default=None,
        description="Reflexion 教训：本次失败的结构化经验，将注入下次尝试的上下文",
    )
    failure_kind: FailureKind = FailureKind.UNKNOWN


class AgentMessage(_ImmutableModel):
    """Agent 间通信的统一消息信封。

    所有跨 Agent 通信都必须封装为本类型，禁止裸传字符串。
    """

    msg_id: str = Field(default_factory=lambda: _new_id("msg"))
    from_agent: AgentType
    to_agent: AgentType
    type: MessageType
    task_id: str
    step_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    handoff: AgentHandoff | None = Field(
        default=None, description="当 type=TASK_SPEC 时的结构化任务交付"
    )
    feedback: FeedbackPayload | None = Field(
        default=None, description="当 type=FEEDBACK 时的结构化反馈"
    )
    context_refs: list[ContextRef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("payload")
    @classmethod
    def _payload_size_guard(cls, v: dict[str, Any]) -> dict[str, Any]:
        """粗略防止把大段文本塞进 payload（应改用 ArtifactRef）。"""
        for key, value in v.items():
            if isinstance(value, str) and len(value) > 20_000:
                raise ValueError(
                    f"payload['{key}'] 超过 20000 字符；大块内容请改用 ArtifactRef 传递引用。"
                )
        return v


# --------------------------------------------------------------------------- #
# 上下文工程
# --------------------------------------------------------------------------- #


class ContextChunk(_ImmutableModel):
    """上下文片段：装配算法的基本单位。

    每个片段带有用于打分的元信息（相关性、时效、依赖、密度）。
    """

    id: str = Field(default_factory=lambda: _new_id("chunk"))
    content: str
    kind: ContextKind
    tokens: int = Field(ge=0, description="该片段的 token 数（由 tokenizer 估算）")

    # 打分所需的元信息
    embedding: tuple[float, ...] | None = Field(
        default=None, description="语义向量；为 None 时相关性按 0 计"
    )
    age: int = Field(default=0, ge=0, description="距离当前步骤的步数，用于时效衰减")
    depends_on_step: frozenset[str] = Field(
        default_factory=frozenset,
        description="该片段是哪些步骤的硬依赖",
    )
    info_units: int = Field(default=1, ge=0, description="有效信息单元数，用于计算信息密度")

    is_hard: bool = Field(
        default=False,
        description="硬约束片段（系统指令/验收标准）：永不压缩、永不被丢弃",
    )
    source: str = Field(default="", description="来源标识：file path / tool name / step id")
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tokens")
    @classmethod
    def _tokens_consistent_with_content(cls, v: int) -> int:
        if v < 0:
            raise ValueError("tokens 不能为负")
        return v


class BudgetAllocation(_ImmutableModel):
    """按用途切分的 token 预算。

    对齐 02-上下文工程深度设计.md 的 L5 预算分配设计。
    """

    total: int = Field(gt=0)
    system_prompt: int = Field(default=800, ge=0)
    task_spec: int = Field(default=1500, ge=0)
    code_context: int = Field(default=6000, ge=0)
    tool_results: int = Field(default=2000, ge=0)
    history: int = Field(default=1500, ge=0)
    reserved_output: int = Field(default=2000, ge=0)

    def available_for_input(self) -> int:
        """可用于输入的预算（扣除预留给输出的部分）。

        当总预算不足以同时覆盖默认输出预留与输入配额时，
        按比例压缩 ``reserved_output`` 与各输入配额，
        保证小预算场景下仍有可用的输入空间（否则装配会退化为空集）。

        分配规则：输出预留最多占总预算的 25%，其余为输入可用。
        """
        reserved = min(self.reserved_output, int(self.total * 0.25))
        return max(0, self.total - reserved)

    def sum_of_quotas(self) -> int:
        """各用途配额之和。

        小预算场景下配额会被等比压缩，因此这里返回**实际生效**的配额和，
        而非原始声明值。
        """
        available = self.available_for_input()
        declared = (
            self.system_prompt
            + self.task_spec
            + self.code_context
            + self.tool_results
            + self.history
        )
        if declared <= available:
            return declared
        # 等比压缩到可用预算
        return available

    def suggest_rebalance(self) -> BudgetAllocation:
        """当配额之和小于可用输入预算时，把余量回流给 code_context。

        这是「动态回流」策略的实现：代码上下文通常是弹性最大、
        收益最高的用途。
        """
        available = self.available_for_input()
        slack = available - self.sum_of_quotas()
        if slack <= 0:
            return self
        return self.model_copy(update={"code_context": self.code_context + slack})


class AssemblyDecision(_ImmutableModel):
    """一次上下文装配的决策记录（用于调试、回放与可视化）。"""

    step_id: str
    agent: AgentType
    candidates: int = Field(ge=0, description="候选片段总数")
    selected: int = Field(ge=0, description="实际选中数")
    dropped: int = Field(ge=0, description="因预算或冗余被丢弃的数量")
    hard_constraints: int = Field(ge=0, description="其中硬约束数量")
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    budget_total: int = Field(ge=0)
    compression_applied: bool = False
    routing_tier: ModelTier | None = None
    reason: str = Field(default="", description="人类可读的装配说明")

    @property
    def compression_ratio(self) -> float:
        """压缩比 = 装配后 / 装配前。越小表示压缩越强。"""
        if self.tokens_before == 0:
            return 1.0
        return self.tokens_after / self.tokens_before


# --------------------------------------------------------------------------- #
# 任务与步骤
# --------------------------------------------------------------------------- #


class TaskNode(_ImmutableModel):
    """DAG 中的任务节点。"""

    id: str = Field(default_factory=lambda: _new_id("node"))
    goal: str
    agent_type: AgentType
    deps: tuple[str, ...] = Field(default_factory=tuple, description="前置节点 id")
    acceptance_criteria: tuple[str, ...] = Field(default_factory=tuple)
    priority: int = Field(default=0, description="越大越优先，用于关键路径加速")


class ReflexionLesson(_ImmutableModel):
    """Reflexion 反思产出的「教训」。"""

    root_cause: str
    lesson: str = Field(description="可操作的改进点，将注入下次尝试的上下文")
    avoid: str = Field(default="", description="应避免的做法")
    source_step: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class StepResult(_ImmutableModel):
    """单步执行结果。"""

    step_id: str
    agent: AgentType
    attempt: int = Field(default=1, ge=1)
    output: str = ""
    artifacts: tuple[ArtifactRef, ...] = Field(default_factory=tuple)
    handoff: AgentHandoff | None = None
    feedback: FeedbackPayload | None = None
    tokens_used: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    model_tier: ModelTier | None = None
    duration_ms: int = Field(default=0, ge=0)
    lessons: tuple[ReflexionLesson, ...] = Field(default_factory=tuple)


class RoutingSignals(_ImmutableModel):
    """模型路由的输入信号。

    对应 02 文档中的复杂度评分：
    ``complexity = w·[reasoning_depth, context_size, tool_calls, retry_history]``
    """

    reasoning_depth: float = Field(default=0.0, ge=0.0, le=1.0)
    context_size_norm: float = Field(default=0.0, ge=0.0, le=1.0)
    tool_call_count_norm: float = Field(default=0.0, ge=0.0, le=1.0)
    retry_history_norm: float = Field(default=0.0, ge=0.0, le=1.0)

    def complexity(self, weights: RoutingWeights | None = None) -> float:
        w = weights or RoutingWeights()
        return (
            w.reasoning_depth * self.reasoning_depth
            + w.context_size * self.context_size_norm
            + w.tool_calls * self.tool_call_count_norm
            + w.retry_history * self.retry_history_norm
        )


class RoutingWeights(_ImmutableModel):
    """复杂度评分的权重配置。

    默认权重来自 02 文档，可通过配置覆盖以便做实验对比。
    """

    reasoning_depth: float = 0.3
    context_size: float = 0.2
    tool_calls: float = 0.2
    retry_history: float = 0.3

    def thresholds(self) -> tuple[float, float]:
        """返回 (small_upper, medium_upper) 阈值。"""
        return (0.35, 0.70)
