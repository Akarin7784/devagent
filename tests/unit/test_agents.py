"""Agent 层单元测试。

使用 FakeProvider 注入，重点验证：
- 各 Agent 的 JSON 解析鲁棒性（容忍 markdown 包裹、非法字段）
- 业务规则（如 Requirement 剔除不可验证标准、Verifier 一致性保护）
- 上下文前提校验（validate）
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from devagent.agents import (
    AgentContextError,
    AgentInvocation,
    ArchitectAgent,
    CoderAgent,
    RequirementAgent,
    ReviewerAgent,
    TesterAgent,
    VerifierAgent,
)
from devagent.config import ContextConfig, Settings
from devagent.context import make_chunk
from devagent.context.isolation import ContextBundle, ContextEngine
from devagent.enums import AgentType, ContextKind, Verdict
from devagent.models.domain import AgentHandoff, BudgetAllocation
from devagent.models.gateway import ModelGateway
from devagent.models.provider import ChatMessage, ChatResult, TokenUsage


class ScriptedProvider:
    """按脚本返回预设内容的假提供商。"""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []

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
        idx = len(self.calls)
        self.calls.append({"messages": messages, "model": model})
        reply = self.replies[idx] if idx < len(self.replies) else self.replies[-1]
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        return ChatResult(content=reply, model=model, provider=self.name, usage=usage)

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def _gateway(replies: list[str]) -> ModelGateway:
    settings = Settings()
    provider = ScriptedProvider(replies)
    return ModelGateway(settings, providers={"deepseek": provider, "qwen": provider})


def _bundle(agent: AgentType = AgentType.CODER) -> ContextBundle:
    chunk = make_chunk("context content", ContextKind.CODE)
    return ContextBundle(
        agent=agent,
        chunks=[chunk],
        decision=type("D", (), {})(),
        budget=BudgetAllocation(total=8000),
    )


def _invocation(
    agent_type: AgentType = AgentType.CODER,
    handoff: AgentHandoff | None = None,
) -> AgentInvocation:
    return AgentInvocation(
        task_id="T-1",
        step_id="S-1",
        bundle=_bundle(agent_type),
        handoff=handoff,
    )


def _json_block(payload: dict[str, Any]) -> str:
    return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


# --------------------------------------------------------------------------- #
# Requirement Agent
# --------------------------------------------------------------------------- #


class TestRequirementAgent:
    async def test_parses_structured_spec(self) -> None:
        replies = [
            _json_block(
                {
                    "goal": "为 /users 增加分页",
                    "acceptance_criteria": ["非法 page_size 返回 400", "缺省返回 20 条"],
                    "constraints": ["不改动返回结构"],
                    "open_questions": ["是否支持游标分页"],
                    "relevant_files": ["src/api/users.py"],
                }
            )
        ]
        agent = RequirementAgent(_gateway(replies))
        msg = await agent.run(_invocation(AgentType.REQUIREMENT))

        assert msg.handoff is not None
        assert msg.handoff.goal == "为 /users 增加分页"
        assert len(msg.handoff.acceptance_criteria) == 2
        assert msg.handoff.constraints == ["不改动返回结构"]

    async def test_strips_unverifiable_criteria(self) -> None:
        """不可验证的验收标准必须被剔除（保证下游可校验）。"""
        replies = [
            _json_block(
                {
                    "goal": "优化接口",
                    "acceptance_criteria": [
                        "性能良好",  # 不可验证 → 剔除
                        "代码优雅",  # 不可验证 → 剔除
                        "非法参数返回 400",  # 可验证 → 保留
                        "响应时间低于 200ms",  # 可验证 → 保留
                    ],
                }
            )
        ]
        agent = RequirementAgent(_gateway(replies))
        msg = await agent.run(_invocation(AgentType.REQUIREMENT))
        assert msg.handoff is not None
        assert msg.handoff.acceptance_criteria == [
            "非法参数返回 400",
            "响应时间低于 200ms",
        ]

    async def test_tolerates_raw_json_without_fence(self) -> None:
        replies = [json.dumps({"goal": "g", "acceptance_criteria": ["a"]}, ensure_ascii=False)]
        agent = RequirementAgent(_gateway(replies))
        msg = await agent.run(_invocation(AgentType.REQUIREMENT))
        assert msg.handoff is not None
        assert msg.handoff.goal == "g"

    async def test_tolerates_malformed_output(self) -> None:
        """完全无法解析时不应崩溃，降级为原文。"""
        replies = ["这不是 JSON"]
        agent = RequirementAgent(_gateway(replies))
        msg = await agent.run(_invocation(AgentType.REQUIREMENT))
        assert msg.handoff is not None
        assert msg.handoff.acceptance_criteria == []


# --------------------------------------------------------------------------- #
# Architect Agent
# --------------------------------------------------------------------------- #


class TestArchitectAgent:
    def _handoff(self) -> AgentHandoff:
        return AgentHandoff(task_id="T-1", goal="实现分页", acceptance_criteria=["支持 page 参数"])

    async def test_requires_acceptance_criteria(self) -> None:
        agent = ArchitectAgent(_gateway(["{}"]))
        with pytest.raises(AgentContextError, match="验收标准"):
            await agent.run(_invocation(AgentType.ARCHITECT, AgentHandoff(task_id="T", goal="g")))

    async def test_parses_dag_nodes(self) -> None:
        replies = [
            _json_block(
                {
                    "approach": "在参数层加校验，改路由层聚合",
                    "nodes": [
                        {
                            "id": "N1",
                            "goal": "实现分页参数",
                            "agent_type": "coder",
                            "deps": [],
                            "acceptance_criteria": ["支持 page"],
                        },
                        {
                            "id": "N2",
                            "goal": "补测试",
                            "agent_type": "tester",
                            "deps": ["N1"],
                            "acceptance_criteria": ["有单测"],
                        },
                    ],
                }
            )
        ]
        agent = ArchitectAgent(_gateway(replies))
        msg = await agent.run(_invocation(AgentType.ARCHITECT, self._handoff()))
        # 节点信息在 payload 中，但 AgentMessage 只透传 content 字段
        assert "N1" in msg.payload["content"]
        assert "N2" in msg.payload["content"]

    async def test_removes_dangling_dependencies(self) -> None:
        """悬空依赖会导致节点永远不就绪，必须移除。"""
        replies = [
            _json_block(
                {
                    "approach": "x",
                    "nodes": [{"id": "N1", "goal": "a", "agent_type": "coder", "deps": ["N99"]}],
                }
            )
        ]
        agent = ArchitectAgent(_gateway(replies))
        output = agent.parse_output(replies[0], _invocation(AgentType.ARCHITECT, self._handoff()))
        nodes = output.raw["nodes"]
        assert nodes[0]["deps"] == []
        assert any("悬空依赖" in w for w in output.raw["warnings"])

    async def test_detects_and_breaks_cycles(self) -> None:
        """依赖环必须被打破，否则编排器会死锁。"""
        replies = [
            _json_block(
                {
                    "approach": "x",
                    "nodes": [
                        {"id": "N1", "goal": "a", "agent_type": "coder", "deps": ["N2"]},
                        {"id": "N2", "goal": "b", "agent_type": "coder", "deps": ["N1"]},
                    ],
                }
            )
        ]
        agent = ArchitectAgent(_gateway(replies))
        output = agent.parse_output(replies[0], _invocation(AgentType.ARCHITECT, self._handoff()))
        assert any("环" in w for w in output.raw["warnings"])

    async def test_raises_when_no_nodes(self) -> None:
        agent = ArchitectAgent(_gateway([_json_block({"approach": "x", "nodes": []})]))
        with pytest.raises(AgentContextError, match="有效任务节点"):
            await agent.run(_invocation(AgentType.ARCHITECT, self._handoff()))

    async def test_invalid_agent_type_falls_back(self) -> None:
        replies = [
            _json_block(
                {
                    "approach": "x",
                    "nodes": [{"id": "N1", "goal": "a", "agent_type": "wizard", "deps": []}],
                }
            )
        ]
        agent = ArchitectAgent(_gateway(replies))
        output = agent.parse_output(replies[0], _invocation(AgentType.ARCHITECT, self._handoff()))
        assert output.raw["nodes"][0]["agent_type"] == "coder"


# --------------------------------------------------------------------------- #
# Coder Agent
# --------------------------------------------------------------------------- #


class TestCoderAgent:
    def _handoff(self) -> AgentHandoff:
        return AgentHandoff(
            task_id="T-1", goal="加分页", acceptance_criteria=["支持 page", "非法返回400"]
        )

    async def test_builds_criteria_map(self) -> None:
        replies = [
            _json_block(
                {
                    "summary": "加分页参数",
                    "changes": [
                        {
                            "file": "src/api/users.py",
                            "reason": "解析 page 参数",
                            "addresses_criteria": ["支持 page"],
                            "diff": "+ page = request.args.get('page')",
                        }
                    ],
                    "unresolved": [],
                }
            )
        ]
        agent = CoderAgent(_gateway(replies))
        output = agent.parse_output(replies[0], _invocation(AgentType.CODER, self._handoff()))
        assert output.raw["criteria_map"] == {"支持 page": ["src/api/users.py"]}
        assert output.artifacts[0].uri == "file://src/api/users.py"

    async def test_requires_goal(self) -> None:
        agent = CoderAgent(_gateway(["{}"]))
        with pytest.raises(AgentContextError, match="任务规格"):
            await agent.run(_invocation(AgentType.CODER, AgentHandoff(task_id="T", goal="")))

    async def test_drops_changes_without_file(self) -> None:
        replies = [
            _json_block(
                {
                    "summary": "x",
                    "changes": [{"reason": "无文件名", "diff": "..."}],
                }
            )
        ]
        agent = CoderAgent(_gateway(replies))
        output = agent.parse_output(replies[0], _invocation(AgentType.CODER, self._handoff()))
        assert output.raw["changes"] == []


# --------------------------------------------------------------------------- #
# Verifier Agent（最关键）
# --------------------------------------------------------------------------- #


class TestVerifierAgent:
    def _handoff(self) -> AgentHandoff:
        return AgentHandoff(
            task_id="T-1",
            goal="加分页",
            acceptance_criteria=["支持 page", "非法参数返回 400"],
        )

    async def test_passes_when_all_criteria_met(self) -> None:
        replies = [
            _json_block(
                {
                    "verdict": "pass",
                    "criterion_checks": [
                        {"criterion": "支持 page", "passed": True, "reason": "测试通过"},
                        {"criterion": "非法参数返回 400", "passed": True, "reason": "有断言"},
                    ],
                }
            )
        ]
        agent = VerifierAgent(_gateway(replies))
        msg = await agent.run(_invocation(AgentType.VERIFIER, self._handoff()))
        assert msg.feedback is not None
        assert msg.feedback.verdict is Verdict.PASS

    async def test_rejects_when_any_criterion_fails(self) -> None:
        replies = [
            _json_block(
                {
                    "verdict": "pass",  # 顶层声称 pass，但有失败项 → 应以失败项为准
                    "criterion_checks": [
                        {"criterion": "支持 page", "passed": True, "reason": "ok"},
                        {"criterion": "非法参数返回 400", "passed": False, "reason": "返回了200"},
                    ],
                    "suggestions": ["增加参数校验"],
                    "lesson": "边界值需校验",
                }
            )
        ]
        agent = VerifierAgent(_gateway(replies))
        msg = await agent.run(_invocation(AgentType.VERIFIER, self._handoff()))
        assert msg.feedback is not None
        assert msg.feedback.verdict is Verdict.REJECT
        assert msg.feedback.failed_criteria == ["非法参数返回 400"]
        assert msg.feedback.suggestions == ["增加参数校验"]
        assert msg.feedback.lesson == "边界值需校验"

    async def test_no_checks_defaults_to_reject(self) -> None:
        """无逐条判定时保守判为未通过（不凭推测通过）。"""
        replies = [_json_block({"verdict": "pass", "criterion_checks": []})]
        agent = VerifierAgent(_gateway(replies))
        msg = await agent.run(_invocation(AgentType.VERIFIER, self._handoff()))
        assert msg.feedback is not None
        assert msg.feedback.verdict is Verdict.REJECT
        assert set(msg.feedback.failed_criteria) == {"支持 page", "非法参数返回 400"}

    async def test_requires_criteria(self) -> None:
        agent = VerifierAgent(_gateway(["{}"]))
        with pytest.raises(AgentContextError, match="验收标准"):
            await agent.run(_invocation(AgentType.VERIFIER, AgentHandoff(task_id="T", goal="g")))

    async def test_cache_disabled(self) -> None:
        assert VerifierAgent(_gateway(["{}"])).cache_enabled is False

    async def test_verifier_context_excludes_coder_explanation(self) -> None:
        """★ 核心设计验证：Verifier 收到的消息不应包含 Coder 的自我解释。

        构造一个 bundle，其中只放验收标准与客观证据，
        确认 build_messages 的输出不含「我已完成」这类辩解文本。
        """
        engine = ContextEngine(ContextConfig())
        handoff = self._handoff()
        engine.isolator.handoff_to(AgentType.VERIFIER, handoff)
        # 只补充客观证据（测试结果）
        engine.isolator.space_for(AgentType.VERIFIER).add(
            make_chunk(
                "# 测试执行结果\n2 passed, 0 failed",
                ContextKind.TOOL_RESULT,
                source="sandbox://pytest",
            )
        )
        bundle = await engine.build(
            agent=AgentType.VERIFIER,
            task_embedding=None,
            current_step="S-1",
            budget_total=8000,
        )

        invocation = AgentInvocation(task_id="T-1", step_id="S-1", bundle=bundle, handoff=handoff)
        gateway = _gateway([_json_block({"verdict": "pass", "criterion_checks": []})])
        agent = VerifierAgent(gateway)
        messages = agent.build_messages(invocation)
        text = "\n".join(m.content for m in messages)

        assert "支持 page" in text, "验收标准应可见"
        assert "2 passed" in text, "客观证据应可见"
        assert "我已完成" not in text
        assert "我已实现" not in text


# --------------------------------------------------------------------------- #
# Reviewer / Tester
# --------------------------------------------------------------------------- #


class TestReviewerAgent:
    async def test_parses_issues_sorted_by_severity(self) -> None:
        replies = [
            _json_block(
                {
                    "summary": "总体质量可接受",
                    "issues": [
                        {
                            "severity": "nit",
                            "dimension": "style",
                            "file": "a.py",
                            "description": "命名可改进",
                        },
                        {
                            "severity": "blocker",
                            "dimension": "security",
                            "file": "b.py",
                            "description": "SQL 拼接存在注入",
                            "suggestion": "改用参数化查询",
                        },
                    ],
                }
            )
        ]
        agent = ReviewerAgent(_gateway(replies))
        output = agent.parse_output(
            replies[0], _invocation(AgentType.REVIEWER, AgentHandoff(task_id="T", goal="g"))
        )
        assert output.raw["blocker_count"] == 1
        assert output.raw["issue_count"] == 2
        # blocker 应排在 nit 之前
        assert output.content.index("BLOCKER") < output.content.index("NIT")

    async def test_unknown_severity_falls_back_to_minor(self) -> None:
        replies = [
            _json_block(
                {
                    "summary": "x",
                    "issues": [{"severity": "catastrophic", "description": "问题"}],
                }
            )
        ]
        agent = ReviewerAgent(_gateway(replies))
        output = agent.parse_output(
            replies[0], _invocation(AgentType.REVIEWER, AgentHandoff(task_id="T", goal="g"))
        )
        assert output.raw["issues"][0]["severity"] == "minor"

    async def test_empty_issues_is_valid(self) -> None:
        replies = [_json_block({"summary": "无问题", "issues": []})]
        agent = ReviewerAgent(_gateway(replies))
        output = agent.parse_output(
            replies[0], _invocation(AgentType.REVIEWER, AgentHandoff(task_id="T", goal="g"))
        )
        assert output.raw["issue_count"] == 0
        assert "未发现问题" in output.content


class TestTesterAgent:
    async def test_parses_test_files(self) -> None:
        replies = [
            _json_block(
                {
                    "test_files": [
                        {
                            "path": "tests/test_users.py",
                            "content": "def test_page(): assert True",
                            "covers_criteria": ["支持 page"],
                        }
                    ],
                    "cases": [
                        {
                            "name": "test_page",
                            "covers_criterion": "支持 page",
                            "description": "验证分页",
                        }
                    ],
                }
            )
        ]
        agent = TesterAgent(_gateway(replies))
        output = agent.parse_output(
            replies[0], _invocation(AgentType.TESTER, AgentHandoff(task_id="T", goal="g"))
        )
        assert len(output.raw["test_files"]) == 1
        assert output.artifacts[0].kind == "test_report"

    async def test_execute_without_runner_reports_not_executed(self) -> None:
        """无执行器时不得伪造执行结果。"""
        agent = TesterAgent(_gateway(["{}"]))
        outcome = await agent.execute([])
        assert outcome.executed is False
        assert "未执行" in outcome.summary()

    async def test_generate_and_run_appends_evidence(self) -> None:
        from devagent.agents.tester import TestRunOutcome

        class FakeRunner:
            async def run_tests(self, test_files: list[dict[str, Any]], *, workdir: str = ""):
                return TestRunOutcome(
                    executed=True, passed=3, failed=1, stdout="3 passed, 1 failed"
                )

        replies = [
            _json_block(
                {
                    "test_files": [{"path": "t.py", "content": "assert True"}],
                    "cases": [],
                }
            )
        ]
        agent = TesterAgent(_gateway(replies), test_runner=FakeRunner())
        output, outcome = await agent.generate_and_run(
            _invocation(AgentType.TESTER, AgentHandoff(task_id="T", goal="g"))
        )
        assert outcome.failed == 1
        assert "3 passed, 1 failed" in output.content
        assert output.raw["outcome"]["all_passed"] is False


class TestAgentMessageEnvelope:
    async def test_message_has_correct_routing(self) -> None:
        replies = [_json_block({"goal": "g", "acceptance_criteria": ["a"]})]
        agent = RequirementAgent(_gateway(replies))
        msg = await agent.run(_invocation(AgentType.REQUIREMENT))
        assert msg.from_agent is AgentType.REQUIREMENT
        assert msg.to_agent is AgentType.ORCHESTRATOR
        assert msg.task_id == "T-1"
        assert msg.step_id == "S-1"
