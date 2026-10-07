"""Reviewer Agent：代码质量审查。

职责：从**工程质量**维度独立评审改动（与 Verifier 的「功能是否正确」
形成正交视角）。

分工说明（面试常被问）：
- **Verifier** 回答「**做对了吗**」——对照验收标准判断功能是否满足；
- **Reviewer** 回答「**做好了吗**」——可维护性、安全性、性能、一致性。

两者结论交叉比对，构成多层验证。
"""

from __future__ import annotations

import json
import re
from typing import Any

from devagent.agents.base import AgentContextError, AgentInvocation, AgentOutput, BaseAgent
from devagent.enums import AgentType, MessageType
from devagent.models.provider import ChatMessage

SYSTEM_PROMPT = """\
你是一名严格的代码审查员，负责从**工程质量**维度评审改动。

审查维度：
1. **正确性风险**：边界值、异常路径、并发问题；
2. **安全性**：注入、越权、敏感信息泄露；
3. **可维护性**：命名、职责单一、重复代码、可测试性；
4. **性能**：不必要的全量扫描、N+1 查询、内存放大；
5. **一致性**：是否符合既有代码风格与项目约定。

你必须遵守：
- 每个问题都要给出**严重级别**（blocker / major / minor / nit）与**具体位置**；
- 只报告**真实存在**的问题，不要为了凑数而臆造；
- 若无问题，如实返回空的 issues 列表。

严格按以下 JSON 格式输出（不要输出其他内容）：

```json
{
  "summary": "审查概述",
  "issues": [
    {
      "severity": "major",
      "dimension": "correctness",
      "file": "相对路径",
      "description": "问题描述",
      "suggestion": "修改建议"
    }
  ]
}
```
"""

SEVERITIES = {"blocker", "major", "minor", "nit"}


class ReviewerAgent(BaseAgent):
    """代码审查 Agent。"""

    agent_type = AgentType.REVIEWER
    description = "从工程质量维度独立审查改动"

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def validate(self, invocation: AgentInvocation) -> None:
        if invocation.handoff is None:
            raise AgentContextError("Reviewer 需要上游提供任务规格与改动内容。")

    def build_messages(self, invocation: AgentInvocation) -> list[ChatMessage]:
        return [
            ChatMessage(role="system", content=self.system_prompt),
            ChatMessage(
                role="user",
                content=f"请审查以下改动：\n\n{self.render_context(invocation)}",
            ),
        ]

    def parse_output(self, raw_content: str, invocation: AgentInvocation) -> AgentOutput:
        data = _extract_json(raw_content)
        summary = str(data.get("summary") or "").strip()
        issues = _parse_issues(data.get("issues") or [])

        blockers = [i for i in issues if i["severity"] == "blocker"]
        content = _render_review(summary, issues)

        # Review 结论不阻断流程（blocker 由编排器决定是否回退），
        # 因此只作为产出物传递，不产出 FeedbackPayload。
        return AgentOutput(
            content=content,
            handoff=invocation.handoff,
            message_type=MessageType.ARTIFACT,
            raw={
                "issues": issues,
                "blocker_count": len(blockers),
                "issue_count": len(issues),
            },
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
        return {"summary": text.strip()[:300], "issues": []}


def _parse_issues(raw: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        if not description:
            continue
        severity = str(item.get("severity") or "minor").strip().lower()
        if severity not in SEVERITIES:
            severity = "minor"
        out.append(
            {
                "severity": severity,
                "dimension": str(item.get("dimension") or "general").strip(),
                "file": str(item.get("file") or "").strip(),
                "description": description,
                "suggestion": str(item.get("suggestion") or "").strip(),
            }
        )
    return out


def _render_review(summary: str, issues: list[dict[str, str]]) -> str:
    lines = ["# 代码审查报告", summary or "（未提供概述）", ""]
    if not issues:
        lines.append("未发现问题。")
        return "\n".join(lines)
    order = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}
    for issue in sorted(issues, key=lambda i: order[i["severity"]]):
        loc = f" [{issue['file']}]" if issue["file"] else ""
        lines.append(f"- **{issue['severity'].upper()}** ({issue['dimension']}){loc}")
        lines.append(f"  问题：{issue['description']}")
        if issue["suggestion"]:
            lines.append(f"  建议：{issue['suggestion']}")
    return "\n".join(lines)


__all__ = ["SEVERITIES", "SYSTEM_PROMPT", "ReviewerAgent"]
