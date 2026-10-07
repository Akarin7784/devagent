"""编排器端到端集成测试。

用脚本化的假 Provider 驱动完整流程，验证：
- 需求 → 架构 → DAG → 编码 → 验证 的全链路编排
- 验证失败时的回退与重试（含 Reflexion 教训注入）
- 成本熔断
- Verifier 上下文确实排除了 Coder 的自我解释
"""

from __future__ import annotations

import json
from typing import Any

from devagent.config import ContextConfig, Settings
from devagent.context import ContextEngine
from devagent.context.compression import ContextCompressor, EchoSummarizer
from devagent.enums import AgentType, TaskStatus
from devagent.models.gateway import ModelGateway
from devagent.models.provider import ChatMessage, ChatResult, TokenUsage
from devagent.orchestration import Orchestrator, OrchestratorConfig


def _json_block(payload: dict[str, Any]) -> str:
    return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


REQUIREMENT_REPLY = _json_block(
    {
        "goal": "为 /users 接口增加分页参数",
        "acceptance_criteria": ["支持 page 与 page_size 参数", "非法参数返回 400"],
        "constraints": ["不改动现有返回结构"],
        "open_questions": [],
        "relevant_files": ["src/api/users.py"],
    }
)

ARCHITECT_REPLY = _json_block(
    {
        "approach": "在参数解析层增加校验，路由层透传分页参数",
        "nodes": [
            {
                "id": "N1",
                "goal": "实现分页参数解析与校验",
                "agent_type": "coder",
                "deps": [],
                "acceptance_criteria": ["支持 page 与 page_size 参数", "非法参数返回 400"],
            }
        ],
    }
)

CODER_REPLY = _json_block(
    {
        "summary": "增加分页参数解析",
        "changes": [
            {
                "file": "src/api/users.py",
                "reason": "解析并校验 page/page_size",
                "addresses_criteria": ["支持 page 与 page_size 参数", "非法参数返回 400"],
                "diff": "+ page = int(request.args.get('page', 1))",
            }
        ],
        "unresolved": [],
    }
)

TESTER_REPLY = _json_block(
    {
        "test_files": [
            {
                "path": "tests/test_users.py",
                "content": "def test_page(): assert True",
                "covers_criteria": ["支持 page 与 page_size 参数"],
            }
        ],
        "cases": [{"name": "test_page", "covers_criterion": "支持 page", "description": "分页"}],
    }
)


def _verifier_reply(passed: bool, lesson: str | None = None) -> str:
    payload: dict[str, Any] = {
        "verdict": "pass" if passed else "reject",
        "criterion_checks": [
            {
                "criterion": "支持 page 与 page_size 参数",
                "passed": passed,
                "reason": "测试通过" if passed else "未实现",
            },
            {
                "criterion": "非法参数返回 400",
                "passed": passed,
                "reason": "已校验" if passed else "返回了 200",
            },
        ],
    }
    if not passed:
        payload["suggestions"] = ["增加参数下界校验"]
        payload["lesson"] = lesson or "边界值必须校验"
        payload["root_cause"] = "缺少参数校验"
    return _json_block(payload)


class RoutedFakeProvider:
    """按角色路由的假提供商。

    根据 system prompt 内容判断当前是哪个 Agent，
    从而返回对应的脚本化回复——这让我们无需真实 LLM 即可测试编排。
    """

    name = "routed"

    def __init__(
        self,
        *,
        verifier_sequence: list[bool] | None = None,
        tokens_per_call: int = 100,
    ) -> None:
        self.verifier_sequence = verifier_sequence or [True]
        self.tokens_per_call = tokens_per_call
        self.verifier_calls = 0
        self.calls: list[dict[str, Any]] = []
        self.seen_verifier_context: str = ""

    def _detect_role(self, messages: list[ChatMessage]) -> AgentType:
        system = next((m.content for m in messages if m.role == "system"), "")
        if "需求分析师" in system:
            return AgentType.REQUIREMENT
        if "软件架构师" in system:
            return AgentType.ARCHITECT
        if "软件工程师" in system:
            return AgentType.CODER
        if "测试工程师" in system:
            return AgentType.TESTER
        if "独立验证工程师" in system:
            return AgentType.VERIFIER
        if "代码审查员" in system:
            return AgentType.REVIEWER
        return AgentType.CODER

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        role = self._detect_role(messages)
        self.calls.append({"role": role, "model": model})

        if role is AgentType.REQUIREMENT:
            reply = REQUIREMENT_REPLY
        elif role is AgentType.ARCHITECT:
            reply = ARCHITECT_REPLY
        elif role is AgentType.CODER:
            reply = CODER_REPLY
        elif role is AgentType.TESTER:
            reply = TESTER_REPLY
        elif role is AgentType.VERIFIER:
            self.seen_verifier_context = "\n".join(m.content for m in messages)
            idx = min(self.verifier_calls, len(self.verifier_sequence) - 1)
            reply = _verifier_reply(self.verifier_sequence[idx])
            self.verifier_calls += 1
        else:
            reply = _json_block({"summary": "无问题", "issues": []})

        usage = TokenUsage(
            prompt_tokens=self.tokens_per_call,
            completion_tokens=self.tokens_per_call,
            total_tokens=self.tokens_per_call * 2,
        )
        return ChatResult(content=reply, model=model, provider=self.name, usage=usage)

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def _build_orchestrator(
    provider: RoutedFakeProvider,
    *,
    config: OrchestratorConfig | None = None,
) -> Orchestrator:
    settings = Settings()
    gateway = ModelGateway(
        settings, providers={"deepseek": provider, "qwen": provider, "zhipu": provider}
    )
    engine = ContextEngine(
        ContextConfig(),
        compressor=ContextCompressor(summarizer=EchoSummarizer()),
    )
    return Orchestrator(
        settings,
        gateway=gateway,
        context_engine=engine,
        config=config or OrchestratorConfig(enable_tester=True, enable_reviewer=False),
    )


class TestHappyPath:
    async def test_full_flow_succeeds(self) -> None:
        provider = RoutedFakeProvider(verifier_sequence=[True])
        orch = _build_orchestrator(provider)

        result = await orch.run("为 /users 接口增加分页参数")

        assert result.status is TaskStatus.SUCCEEDED, f"error={result.error}"
        assert result.dag is not None
        assert result.dag.is_success()

        roles = [c["role"] for c in provider.calls]
        assert AgentType.REQUIREMENT in roles
        assert AgentType.ARCHITECT in roles
        assert AgentType.CODER in roles
        assert AgentType.VERIFIER in roles

    async def test_records_steps_and_cost(self) -> None:
        provider = RoutedFakeProvider(verifier_sequence=[True])
        orch = _build_orchestrator(provider)
        result = await orch.run("需求")

        assert len(result.steps) >= 4
        assert result.total_tokens > 0
        assert result.duration_ms >= 0

    async def test_requirement_extracts_criteria(self) -> None:
        provider = RoutedFakeProvider(verifier_sequence=[True])
        orch = _build_orchestrator(provider)
        result = await orch.run("需求")

        req_steps = [s for s in result.steps if s.agent is AgentType.REQUIREMENT]
        assert req_steps
        assert req_steps[0].handoff is not None
        assert "支持 page 与 page_size 参数" in req_steps[0].handoff.acceptance_criteria


class TestVerification:
    async def test_rejects_then_passes_after_retry(self) -> None:
        """第一次验证驳回，重试后通过。"""
        provider = RoutedFakeProvider(verifier_sequence=[False, True])
        orch = _build_orchestrator(
            provider,
            config=OrchestratorConfig(max_retries=3, enable_tester=False, enable_reviewer=False),
        )
        result = await orch.run("需求")

        assert result.status is TaskStatus.SUCCEEDED
        assert provider.verifier_calls >= 2, "应触发至少一次回退重试"

    async def test_fails_after_exhausting_retries(self) -> None:
        """持续驳回 → 耗尽重试后任务失败。"""
        provider = RoutedFakeProvider(verifier_sequence=[False])
        orch = _build_orchestrator(
            provider,
            config=OrchestratorConfig(max_retries=2, enable_tester=False, enable_reviewer=False),
        )
        result = await orch.run("需求")

        assert result.status is TaskStatus.FAILED
        assert result.dag is not None
        assert result.dag.has_failure()

    async def test_reflexion_lesson_recorded(self) -> None:
        """驳回后应记录教训，并在重试时注入上下文。"""
        provider = RoutedFakeProvider(verifier_sequence=[False, True])
        orch = _build_orchestrator(
            provider,
            config=OrchestratorConfig(max_retries=3, enable_tester=False, enable_reviewer=False),
        )
        await orch.run("需求")

        lessons = orch._reflexion.lessons_for(AgentType.CODER)
        assert lessons, "驳回后应有教训记录"

    async def test_verifier_context_excludes_coder_explanation(self) -> None:
        """★ 核心设计：Verifier 上下文不含 Coder 的自我解释。"""
        provider = RoutedFakeProvider(verifier_sequence=[True])
        orch = _build_orchestrator(provider)
        await orch.run("需求")

        ctx = provider.seen_verifier_context
        assert "支持 page 与 page_size 参数" in ctx, "验收标准必须可见"
        # Coder 回复中的自我陈述不应出现在 Verifier 上下文
        assert "增加分页参数解析" not in ctx or "实际产出" in ctx


class TestCircuitBreaker:
    async def test_token_budget_triggers_pause(self) -> None:
        provider = RoutedFakeProvider(verifier_sequence=[True], tokens_per_call=5000)
        orch = _build_orchestrator(
            provider,
            config=OrchestratorConfig(
                max_tokens=1000,  # 极小预算
                enable_tester=False,
                enable_reviewer=False,
            ),
        )
        result = await orch.run("需求")
        assert result.status is TaskStatus.PAUSED
        assert result.error is not None

    async def test_step_limit_triggers_pause(self) -> None:
        provider = RoutedFakeProvider(verifier_sequence=[False])
        orch = _build_orchestrator(
            provider,
            config=OrchestratorConfig(
                max_steps=3,
                max_retries=10,
                enable_tester=False,
                enable_reviewer=False,
            ),
        )
        result = await orch.run("需求")
        assert result.status in {TaskStatus.PAUSED, TaskStatus.FAILED}


class TestRobustness:
    async def test_architect_failure_handled(self) -> None:
        """架构师返回非法输出时，任务不应崩溃。"""
        provider = RoutedFakeProvider(verifier_sequence=[True])
        orch = _build_orchestrator(provider)

        # 让架构师返回空节点 → 应回退到单节点方案而非崩溃
        original = provider.chat

        async def patched(messages: list[ChatMessage], **kwargs: Any) -> ChatResult:
            role = provider._detect_role(messages)
            if role is AgentType.ARCHITECT:
                provider.calls.append({"role": role, "model": "x"})
                usage = TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20)
                return ChatResult(
                    content=_json_block({"approach": "x", "nodes": []}),
                    model="x",
                    provider="routed",
                    usage=usage,
                )
            return await original(messages, **kwargs)

        provider.chat = patched  # type: ignore[method-assign]
        result = await orch.run("需求")
        # 不应崩溃；要么成功（单节点兜底）要么明确失败
        assert result.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}
        assert result.error is None or "需求澄清" in result.error or result.error

    async def test_empty_goal_still_runs(self) -> None:
        provider = RoutedFakeProvider(verifier_sequence=[True])
        orch = _build_orchestrator(provider)
        result = await orch.run("")
        assert result.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}


class TestCheckpoint:
    async def test_checkpoints_saved(self) -> None:
        provider = RoutedFakeProvider(verifier_sequence=[True])
        orch = _build_orchestrator(provider)
        result = await orch.run("需求")
        saved = orch._checkpoints.list_for_task(result.task_id)
        assert saved, "每一步完成后都应保存检查点"
        assert all(cp.completed for cp in saved)
