"""Coder Agent：编码。

职责：按任务规格产出代码补丁，并给出**改动理由 ↔ 验收标准**的映射。

关键设计（防幻觉传播第一道防线）：Coder 必须为每处改动说明它对应哪条
验收标准。无法建立映射的改动会被 Verifier 打回——这迫使 Coder 的方案
必须可追溯，而不是「我觉得这样写比较好」。
"""

from __future__ import annotations

import json
import re
from typing import Any

from devagent.agents.base import AgentContextError, AgentInvocation, AgentOutput, BaseAgent
from devagent.enums import AgentType, MessageType
from devagent.models.domain import ArtifactRef
from devagent.models.provider import ChatMessage

SYSTEM_PROMPT = """\
你是一名资深软件工程师，负责按任务规格实现代码改动。

你必须遵守：
1. 严格遵循给定的**硬约束**，不得擅自改动约束之外的行为；
2. 每处代码改动都必须说明它对应哪条**验收标准**（建立可追溯映射）；
3. 若发现规格或约束本身存在冲突，不要猜测——在 unresolved 中明确列出；
4. 代码需符合工程规范，并考虑边界值与错误处理。

严格按以下 JSON 格式输出（不要输出其他内容）：

```json
{
  "summary": "改动概述",
  "changes": [
    {
      "file": "相对路径",
      "reason": "为什么这样改",
      "addresses_criteria": ["该改动满足的验收标准原文"],
      "diff": "统一 diff 或改动后的完整片段"
    }
  ],
  "unresolved": ["无法在此环节解决的问题"]
}
```
"""


class CoderAgent(BaseAgent):
    """编码 Agent。"""

    agent_type = AgentType.CODER
    description = "按规格实现改动并建立改动↔验收标准映射"

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def validate(self, invocation: AgentInvocation) -> None:
        handoff = invocation.handoff
        if handoff is None or not handoff.goal:
            raise AgentContextError("Coder 需要一个带目标的任务规格（AgentHandoff）。")

    def build_messages(self, invocation: AgentInvocation) -> list[ChatMessage]:
        return [
            ChatMessage(role="system", content=self.system_prompt),
            ChatMessage(
                role="user",
                content=f"请实现以下任务：\n\n{self.render_context(invocation)}",
            ),
        ]

    def parse_output(self, raw_content: str, invocation: AgentInvocation) -> AgentOutput:
        data = _extract_json(raw_content)
        summary = str(data.get("summary") or "").strip()
        raw_changes = data.get("changes") or []
        changes = _parse_changes(raw_changes)
        unresolved = [str(u).strip() for u in (data.get("unresolved") or []) if str(u).strip()]

        artifacts = [
            ArtifactRef(
                uri=f"file://{c['file']}",
                kind="file",
                summary=f"{c['reason'][:80]}" if c["reason"] else "代码改动",
                checksum=None,
            )
            for c in changes
            if c["file"]
        ]

        handoff = invocation.handoff
        assert handoff is not None  # validate 已保证

        content = _render_changes(summary, changes, unresolved)
        return AgentOutput(
            content=content,
            artifacts=artifacts,
            handoff=handoff.model_copy(update={"context_refs": list(handoff.context_refs)}),
            message_type=MessageType.ARTIFACT,
            raw={
                "changes": changes,
                "unresolved": unresolved,
                "criteria_map": _criteria_map(changes),
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
        return {"summary": text.strip()[:300], "changes": []}


def _parse_changes(raw: list[Any]) -> list[dict[str, Any]]:
    """解析改动列表，丢弃无文件名的无效项。"""
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        file = str(item.get("file") or "").strip()
        if not file:
            continue
        out.append(
            {
                "file": file,
                "reason": str(item.get("reason") or "").strip(),
                "addresses_criteria": [
                    str(c).strip() for c in (item.get("addresses_criteria") or []) if str(c).strip()
                ],
                "diff": str(item.get("diff") or "").strip(),
            }
        )
    return out


def _criteria_map(changes: list[dict[str, Any]]) -> dict[str, list[str]]:
    """构建「验收标准 → 涉及文件」的映射（供 Verifier 校验覆盖度）。"""
    mapping: dict[str, list[str]] = {}
    for change in changes:
        for criterion in change["addresses_criteria"]:
            mapping.setdefault(criterion, []).append(change["file"])
    return mapping


def _render_changes(summary: str, changes: list[dict[str, Any]], unresolved: list[str]) -> str:
    lines = ["# 代码改动", summary or "（未提供概述）", ""]
    for i, change in enumerate(changes, 1):
        lines.append(f"## {i}. {change['file']}")
        if change["reason"]:
            lines.append(f"理由：{change['reason']}")
        if change["addresses_criteria"]:
            lines.append("满足验收标准：")
            lines += [f"  - {c}" for c in change["addresses_criteria"]]
        if change["diff"]:
            lines += ["```diff", change["diff"], "```"]
        lines.append("")
    if unresolved:
        lines += ["## 未解决问题", *[f"- {u}" for u in unresolved]]
    return "\n".join(lines)


__all__ = ["SYSTEM_PROMPT", "CoderAgent"]
