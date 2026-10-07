"""Agent 抽象基类。

设计原则（对齐 ``docs/03-多Agent编排与可靠性.md``）：
1. **单一职责**：每个 Agent 只做一件事，prompt 更短更精准；
2. **无状态执行**：Agent 不持有跨调用状态，上下文由上下文工程层注入；
3. **结构化输出**：Agent 产出 ``AgentMessage``，不产出裸字符串；
4. **可替换**：换模型/换实现不影响其他 Agent。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from devagent.context.isolation import ContextBundle
from devagent.context.trust import InjectionGuard
from devagent.enums import AgentType, MessageType
from devagent.logging_config import get_logger
from devagent.models.domain import (
    AgentHandoff,
    AgentMessage,
    ArtifactRef,
    FeedbackPayload,
    ReflexionLesson,
    RoutingSignals,
    StepResult,
)
from devagent.models.gateway import ModelGateway
from devagent.models.provider import ChatMessage, ToolSpec

logger = get_logger(__name__)


@dataclass(slots=True)
class AgentInvocation:
    """一次 Agent 调用所需的全部输入。

    由编排器构造并交给 Agent；Agent 不应自行获取上下文。
    """

    task_id: str
    step_id: str
    bundle: ContextBundle
    """由上下文工程层装配好的、已隔离的上下文。"""

    handoff: AgentHandoff | None = None
    lessons: tuple[ReflexionLesson, ...] = ()
    """历史失败教训（Reflexion），会作为额外片段注入。"""

    tools: tuple[ToolSpec, ...] = ()
    attempt: int = 1
    routing_signals: RoutingSignals | None = None
    guard: InjectionGuard | None = None
    """注入防护器。为 ``None`` 时按"关闭防护"渲染（等价于升级前行为）。

    用 ``None`` 而非默认构造一个实例，是为了让"忘记接线"这件事
    在测试里**可检测**：有专门测试断言编排器一定会传入 guard。
    """
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class AgentOutput:
    """Agent 执行产出的原始结果（尚未封装为消息）。"""

    content: str
    artifacts: list[ArtifactRef] = field(default_factory=list)
    handoff: AgentHandoff | None = None
    feedback: FeedbackPayload | None = None
    message_type: MessageType = MessageType.ARTIFACT
    tokens_used: int = 0
    cost_usd: float = 0.0
    model: str = ""
    raw: dict[str, object] = field(default_factory=dict)


class AgentContextError(RuntimeError):
    """Agent 收到的上下文不满足其执行前提。"""


class BaseAgent(ABC):
    """所有专职 Agent 的抽象基类。

    子类需实现：
    - ``agent_type``：自身类型；
    - ``system_prompt``：角色系统指令；
    - ``build_messages``：把装配后的上下文转为模型消息；
    - ``parse_output``：把模型原始输出解析为 ``AgentOutput``。

    典型实现只需覆盖上述四项，重试/路由/记账由基类与网关统一处理。
    """

    agent_type: ClassVar[AgentType]
    description: ClassVar[str] = ""

    def __init__(self, gateway: ModelGateway) -> None:
        self._gateway = gateway

    # ------------------------------------------------------------------ #
    # 子类实现点
    # ------------------------------------------------------------------ #

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """角色系统指令。"""

    @abstractmethod
    def build_messages(self, invocation: AgentInvocation) -> list[ChatMessage]:
        """把装配后的上下文转为对话消息。

        约定：``system`` 消息只放本 Agent 的角色指令；
        上下文内容以 ``user`` 消息注入（保持角色边界清晰）。
        """

    @abstractmethod
    def parse_output(self, raw_content: str, invocation: AgentInvocation) -> AgentOutput:
        """解析模型输出为结构化结果。"""

    # ------------------------------------------------------------------ #
    # 模板方法：执行流程
    # ------------------------------------------------------------------ #

    async def run(self, invocation: AgentInvocation) -> AgentMessage:
        """执行本 Agent 的完整流程。

        流程：构造消息 → 调用网关（含路由/缓存/降级）→ 解析 → 封装消息。

        Raises:
            AgentContextError: 上下文不满足执行前提。
            ModelError: 模型调用失败。
        """
        self.validate(invocation)
        messages = self.build_messages(invocation)

        result = await self._gateway.chat(
            messages,
            signals=invocation.routing_signals,
            tools=list(invocation.tools) or None,
            step_id=invocation.step_id,
            use_cache=self.cache_enabled,
        )

        output = self.parse_output(result.content, invocation)
        output.tokens_used = result.usage.total_tokens
        output.model = f"{result.provider}:{result.model}"
        if output.cost_usd == 0.0:
            provider = self._gateway._providers.get(result.provider)
            if provider is not None:
                output.cost_usd = self._gateway._estimate_cost(provider, result.usage, result.model)

        logger.info(
            "agent_executed",
            agent=self.agent_type.value,
            step_id=invocation.step_id,
            attempt=invocation.attempt,
            model=output.model,
            tokens=output.tokens_used,
        )
        return self._to_message(output, invocation)

    def validate(self, invocation: AgentInvocation) -> None:  # noqa: ARG002
        """校验执行前提。

        默认不校验（钩子方法）。子类需要前置条件时才覆盖 —— 这里**刻意不加**
        ``@abstractmethod``：绝大多数 Agent 没有额外前提，强制实现只会制造
        一堆空方法，反而降低可读性。
        """
        return None

    # ------------------------------------------------------------------ #
    # 行为开关
    # ------------------------------------------------------------------ #

    @property
    def cache_enabled(self) -> bool:
        """是否启用结果缓存。

        Verifier 与 Tester 建议关闭缓存（避免复用过时结论）；
        其余 Agent 可开启以降本。
        """
        return True

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _to_message(self, output: AgentOutput, invocation: AgentInvocation) -> AgentMessage:
        refs_source = output.handoff or invocation.handoff
        return AgentMessage(
            from_agent=self.agent_type,
            to_agent=AgentType.ORCHESTRATOR,
            type=output.message_type,
            task_id=invocation.task_id,
            step_id=invocation.step_id,
            payload={
                "content": output.content,
                "model": output.model,
                # token 与成本随消息回传，使编排器无需直接接触网关账本
                "tokens_used": output.tokens_used,
                "cost_usd": output.cost_usd,
                "raw": output.raw,
            },
            handoff=output.handoff,
            feedback=output.feedback,
            context_refs=list(refs_source.context_refs) if refs_source else [],
        )

    def render_context(self, invocation: AgentInvocation) -> str:
        """把装配后的上下文渲染为文本。

        渲染走 ``render_guarded``：不可信来源（仓库文件、命令回显、
        检索结果）会被带 nonce 的定界符包裹，并附上"这是数据不是指令"
        的声明；可信来源（用户目标、系统约束）逐字节不变。

        统一在末尾附上「教训」区块（Reflexion），
        确保失败经验位于尾部高注意力区。
        """
        if invocation.guard is not None:
            parts = [invocation.bundle.render_guarded(invocation.guard)]
        else:
            parts = [invocation.bundle.render()]
        if invocation.lessons:
            lines = ["# 历史失败教训（务必避免重复）"]
            for lesson in invocation.lessons:
                lines.append(f"- 根因：{lesson.root_cause}")
                lines.append(f"  教训：{lesson.lesson}")
                if lesson.avoid:
                    lines.append(f"  避免：{lesson.avoid}")
            parts.append("\n".join(lines))
        return "\n\n".join(part for part in parts if part.strip())

    def to_step_result(self, output: AgentOutput, invocation: AgentInvocation) -> StepResult:
        """把输出转为步骤结果（供编排器持久化）。"""
        return StepResult(
            step_id=invocation.step_id,
            agent=self.agent_type,
            attempt=invocation.attempt,
            output=output.content,
            artifacts=tuple(output.artifacts),
            handoff=output.handoff,
            feedback=output.feedback,
            tokens_used=output.tokens_used,
            cost_usd=output.cost_usd,
            lessons=invocation.lessons,
        )


__all__ = [
    "AgentContextError",
    "AgentInvocation",
    "AgentOutput",
    "BaseAgent",
]
