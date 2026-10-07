"""Tester Agent：测试生成与执行。

职责：为改动生成测试用例，并（在沙箱可用时）实际执行，产出**客观证据**。

关键设计：**「测试即事实」**。整个系统不采信 Agent 自述「我做完了」，
只采信沙箱里实际跑出的测试结果。Tester 的产出因此是
Verifier 判定时最重要的证据来源。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from devagent.agents.base import AgentContextError, AgentInvocation, AgentOutput, BaseAgent
from devagent.enums import AgentType, MessageType
from devagent.models.domain import ArtifactRef
from devagent.models.provider import ChatMessage

SYSTEM_PROMPT = """\
你是一名资深测试工程师，负责为代码改动设计并执行测试。

你必须遵守：
1. 测试用例需**覆盖每条验收标准**，并覆盖边界值与异常路径；
2. 明确标注每个用例验证的是哪条验收标准；
3. 若无法执行测试，如实说明，不要编造执行结果——编造结果是最严重的错误。

严格按以下 JSON 格式输出（不要输出其他内容）：

```json
{
  "test_files": [
    {
      "path": "tests/test_xxx.py",
      "content": "测试代码全文",
      "covers_criteria": ["覆盖的验收标准"]
    }
  ],
  "cases": [
    {"name": "test_case_name", "covers_criterion": "验收标准", "description": "验证点"}
  ]
}
```
"""


@dataclass(slots=True)
class TestRunOutcome:
    """测试执行结果（客观证据）。"""

    executed: bool
    passed: int = 0
    failed: int = 0
    errors: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        return self.executed and self.failed == 0 and self.errors == 0 and self.passed > 0

    def summary(self) -> str:
        if not self.executed:
            return "测试未执行（无沙箱或被跳过）"
        return f"{self.passed} passed, {self.failed} failed, {self.errors} errors"


@runtime_checkable
class TestRunner(Protocol):
    """测试执行器协议（由工具体系注入）。"""

    async def run_tests(
        self, test_files: list[dict[str, Any]], *, workdir: str = ""
    ) -> TestRunOutcome: ...


class TesterAgent(BaseAgent):
    """测试 Agent。"""

    agent_type = AgentType.TESTER
    description = "生成测试并执行，产出客观证据"

    def __init__(self, gateway: Any, test_runner: TestRunner | None = None) -> None:
        super().__init__(gateway)
        self._runner = test_runner

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    @property
    def cache_enabled(self) -> bool:
        """关闭缓存：测试结论依赖当前代码状态。"""
        return False

    def validate(self, invocation: AgentInvocation) -> None:
        if invocation.handoff is None:
            raise AgentContextError("Tester 需要上游提供任务规格与验收标准。")

    def build_messages(self, invocation: AgentInvocation) -> list[ChatMessage]:
        return [
            ChatMessage(role="system", content=self.system_prompt),
            ChatMessage(
                role="user",
                content=f"请为以下改动设计与执行测试：\n\n{self.render_context(invocation)}",
            ),
        ]

    def parse_output(self, raw_content: str, invocation: AgentInvocation) -> AgentOutput:
        data = _extract_json(raw_content)
        test_files = _parse_test_files(data.get("test_files") or [])
        cases = _parse_cases(data.get("cases") or [])

        artifacts = [
            ArtifactRef(
                uri=f"file://{tf['path']}",
                kind="test_report",
                summary=f"测试文件，覆盖 {len(tf['covers_criteria'])} 条标准",
            )
            for tf in test_files
        ]

        content = _render_tests(test_files, cases)
        handoff = invocation.handoff

        return AgentOutput(
            content=content,
            artifacts=artifacts,
            handoff=handoff,
            message_type=MessageType.ARTIFACT,
            raw={"test_files": test_files, "cases": cases},
        )

    async def generate_and_run(
        self, invocation: AgentInvocation, *, workdir: str = ""
    ) -> tuple[AgentOutput, TestRunOutcome]:
        """生成测试并（若配置了执行器）实际运行。

        这是编排器应调用的入口——它保证「生成的测试一定会被执行」，
        避免出现「写了测试但从未运行」的假证据。
        """
        raw_content, tokens_used, cost_usd, model_name = await self._generate(invocation)
        output = self.parse_output(raw_content, invocation)
        output.tokens_used = tokens_used
        output.cost_usd = cost_usd
        output.model = model_name

        test_files_raw = output.raw.get("test_files")
        test_files = (
            [f for f in test_files_raw if isinstance(f, dict)]
            if isinstance(test_files_raw, list)
            else []
        )
        outcome = await self.execute(test_files, workdir=workdir)
        # 把执行结果追加进内容，供下游 Verifier 读取客观证据
        output.content = f"{output.content}\n\n## 测试执行结果\n{outcome.summary()}"
        if outcome.stdout:
            output.content += f"\n\n```\n{outcome.stdout[-2000:]}\n```"
        output.raw["outcome"] = {
            "executed": outcome.executed,
            "passed": outcome.passed,
            "failed": outcome.failed,
            "errors": outcome.errors,
            "all_passed": outcome.all_passed,
        }
        return output, outcome

    async def execute(
        self, test_files: list[dict[str, Any]], *, workdir: str = ""
    ) -> TestRunOutcome:
        """执行测试文件；无执行器时返回「未执行」而非伪造结果。"""
        if self._runner is None:
            return TestRunOutcome(executed=False)
        return await self._runner.run_tests(test_files, workdir=workdir)

    async def _generate(self, invocation: AgentInvocation) -> tuple[str, int, float, str]:
        """生成测试代码，返回 (内容, tokens, 成本, 模型名)。"""
        messages = self.build_messages(invocation)
        result = await self._gateway.chat(
            messages,
            signals=invocation.routing_signals,
            step_id=invocation.step_id,
            # ★ 必须显式传 use_cache：``cache_enabled`` 只是本类的声明，
            # 而 gateway.chat 的默认值是 True。早先这里没传，于是
            # 「Tester 关闭缓存」这条注释与属性都成了摆设 ——
            # 测试生成会命中上一次（甚至上一个节点）的缓存结果。
            use_cache=self.cache_enabled,
        )
        provider = self._gateway._providers.get(result.provider)
        cost = (
            self._gateway._estimate_cost(provider, result.usage, result.model)
            if provider is not None
            else 0.0
        )
        return (
            result.content,
            result.usage.total_tokens,
            cost,
            f"{result.provider}:{result.model}",
        )


def _extract_json(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not fenced:
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    try:
        parsed: dict[str, Any] = json.loads(candidate)
        return parsed
    except json.JSONDecodeError:
        return {"test_files": [], "cases": []}


def _parse_test_files(raw: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        content = str(item.get("content") or "")
        if not path or not content.strip():
            continue
        out.append(
            {
                "path": path,
                "content": content,
                "covers_criteria": [
                    str(c).strip() for c in (item.get("covers_criteria") or []) if str(c).strip()
                ],
            }
        )
    return out


def _parse_cases(raw: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "covers_criterion": str(item.get("covers_criterion") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            }
        )
    return out


def _render_tests(test_files: list[dict[str, Any]], cases: list[dict[str, str]]) -> str:
    lines = ["# 测试设计与实现", ""]
    for tf in test_files:
        lines.append(f"## {tf['path']}")
        if tf["covers_criteria"]:
            lines.append("覆盖验收标准：")
            lines += [f"  - {c}" for c in tf["covers_criteria"]]
        lines += ["```python", tf["content"], "```", ""]
    if cases:
        lines += ["## 用例清单"]
        for c in cases:
            lines.append(f"- {c['name']}：{c['description']}（对应：{c['covers_criterion']}）")
    return "\n".join(lines)


__all__ = ["SYSTEM_PROMPT", "TestRunOutcome", "TestRunner", "TesterAgent"]
