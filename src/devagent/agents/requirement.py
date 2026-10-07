"""Requirement Agent：需求澄清。

职责：把模糊的自然语言需求，转为**可验收的结构化规格**。
这是全链路的第一环，其输出质量决定了后续所有环节的效果。

关键设计：产出的验收标准必须**可逐条校验**（Verifier 后续据此判断），
因此禁止产出「性能良好」「体验流畅」这类不可验证的表述。
"""

from __future__ import annotations

import json
import re
from typing import Any

from devagent.agents.base import AgentInvocation, AgentOutput, BaseAgent
from devagent.enums import AgentType, MessageType
from devagent.models.domain import AgentHandoff
from devagent.models.provider import ChatMessage

SYSTEM_PROMPT = """\
你是一名资深需求分析师，负责把模糊的软件需求转为**可验收的结构化规格**。

你必须遵守：
1. 验收标准必须**可客观验证**：能被测试用例或明确检查项判定通过与否。
   反例（禁止）：性能良好、体验流畅、代码优雅。
   正例（要求）：非法 page_size 返回 400；page_size 缺省时返回 20 条。
2. 主动识别并列出**隐含约束**（兼容性、数据迁移、并发、权限等）。
3. 若需求存在歧义，列出「待澄清问题」，但不要停下来等待——先按最合理假设给出规格。

严格按以下 JSON 格式输出（不要输出任何其他内容）：

```json
{
  "goal": "一句话目标",
  "acceptance_criteria": ["可验证的标准1", "可验证的标准2"],
  "constraints": ["硬约束1", "硬约束2"],
  "open_questions": ["待澄清问题1"],
  "relevant_files": ["相对路径1"]
}
```
"""

_UNVERIFIABLE_PATTERNS = [
    r"良好",
    r"流畅",
    r"优雅",
    r"美观",
    r"合理",
    r"高(效|质量)",
    r"尽量",
    r"适当",
]


class RequirementAgent(BaseAgent):
    """需求澄清 Agent。"""

    agent_type = AgentType.REQUIREMENT
    description = "把模糊需求转为可验收的结构化规格"

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def build_messages(self, invocation: AgentInvocation) -> list[ChatMessage]:
        return [
            ChatMessage(role="system", content=self.system_prompt),
            ChatMessage(
                role="user",
                content=(f"请把以下需求转为结构化规格：\n\n{self.render_context(invocation)}"),
            ),
        ]

    def parse_output(self, raw_content: str, invocation: AgentInvocation) -> AgentOutput:
        data = _extract_json(raw_content)
        criteria = _strip_unverifiable(data.get("acceptance_criteria") or [])
        constraints = [str(c).strip() for c in (data.get("constraints") or []) if str(c).strip()]
        questions = [str(q).strip() for q in (data.get("open_questions") or []) if str(q).strip()]
        files = [str(f).strip() for f in (data.get("relevant_files") or []) if str(f).strip()]

        handoff = AgentHandoff(
            task_id=invocation.task_id,
            goal=str(data.get("goal") or "").strip() or "（需求目标待澄清）",
            acceptance_criteria=criteria,
            constraints=constraints,
            relevant_files=files,
        )
        content = _render_spec(handoff, questions)
        return AgentOutput(
            content=content,
            handoff=handoff,
            message_type=MessageType.ARTIFACT,
            raw={"open_questions": questions},
        )


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出中提取 JSON（容忍 markdown 代码块包裹）。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not fenced:
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    try:
        parsed: dict[str, Any] = json.loads(candidate)
        return parsed
    except json.JSONDecodeError:
        return {"goal": text.strip()[:200], "acceptance_criteria": [], "constraints": []}


def _strip_unverifiable(criteria: list[Any]) -> list[str]:
    """剔除不可验证的验收标准（保证下游可校验性）。"""
    out: list[str] = []
    for item in criteria:
        text = str(item).strip()
        if not text:
            continue
        if any(re.search(p, text) for p in _UNVERIFIABLE_PATTERNS):
            continue
        out.append(text)
    return out


def _render_spec(handoff: AgentHandoff, questions: list[str]) -> str:
    lines = [f"# 需求规格：{handoff.goal}", "", "## 验收标准"]
    lines += [f"- {c}" for c in handoff.acceptance_criteria] or ["（无）"]
    lines += ["", "## 硬约束"]
    lines += [f"- {c}" for c in handoff.constraints] or ["（无）"]
    if handoff.relevant_files:
        lines += ["", "## 相关文件"]
        lines += [f"- {f}" for f in handoff.relevant_files]
    if questions:
        lines += ["", "## 待澄清问题"]
        lines += [f"- {q}" for q in questions]
    return "\n".join(lines)


__all__ = ["SYSTEM_PROMPT", "RequirementAgent"]
