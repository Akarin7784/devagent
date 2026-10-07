"""Verifier Agent：独立验证（防幻觉传播的核心）。

设计要点（这是本项目区别于普通 Agent demo 的关键）：

1. **独立视角**：Verifier 的上下文**刻意排除**上游（Coder）的自我解释，
   只接收：验收标准 + 实际产出 + 客观证据（测试结果）。
   原因：若上游的辩解进入 Verifier 的上下文，Verifier 会被措辞带偏，
   交叉验证能力失效——幻觉就被「合理化」了。

2. **逐条校验**：对每条验收标准独立给出通过/未通过判定及理由。

3. **强制证据**：判定必须引用客观证据，禁止「我认为」。

4. **不缓存**：验证结论依赖具体产出，缓存会复用过时结论。
"""

from __future__ import annotations

import json
import re
from typing import Any

from devagent.agents.base import AgentContextError, AgentInvocation, AgentOutput, BaseAgent
from devagent.enums import AgentType, FailureKind, MessageType, Verdict
from devagent.models.domain import FeedbackPayload
from devagent.models.provider import ChatMessage

SYSTEM_PROMPT = """\
你是一名独立验证工程师，职责是**客观检验产出是否真正满足验收标准**。

你必须遵守：
1. 只依据提供给你的**客观证据**（验收标准、实际产出、测试结果）做判断；
2. 对每一条验收标准独立判定 passed（true/false），并给出简短理由；
3. 证据不足时判为未通过（**保守判定**），不得凭推测通过；
4. 未通过时，必须给出**可操作的修复建议**（指出具体缺什么、怎么补）。

严格按以下 JSON 格式输出（不要输出其他内容）：

```json
{
  "verdict": "pass",
  "criterion_checks": [
    {"criterion": "验收标准原文", "passed": true, "reason": "判定理由与所依据的证据"}
  ],
  "suggestions": ["未通过时的修复建议"],
  "root_cause": "未通过时的根因分析（通过时可为空）",
  "lesson": "可复用的教训（未通过时必填）"
}
```

`verdict` 只能取 `pass` 或 `reject`。**只要有一条标准未通过，verdict 就必须为 reject。**
"""


class VerifierAgent(BaseAgent):
    """独立验证 Agent。"""

    agent_type = AgentType.VERIFIER
    description = "以独立视角逐条校验验收标准，阻断幻觉传播"

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    @property
    def cache_enabled(self) -> bool:
        """关闭缓存：验证结论必须基于当前产出。"""
        return False

    def validate(self, invocation: AgentInvocation) -> None:
        handoff = invocation.handoff
        if handoff is None or not handoff.acceptance_criteria:
            raise AgentContextError("Verifier 需要带验收标准的任务规格；无标准则无法客观验证。")

    def build_messages(self, invocation: AgentInvocation) -> list[ChatMessage]:
        # ★ 关键：此处 render_context 使用的 bundle 由编排器构造时
        # 已刻意排除 Coder 的自我解释（见 orchestration.verifier_context）。
        return [
            ChatMessage(role="system", content=self.system_prompt),
            ChatMessage(
                role="user",
                content=(
                    "请基于以下客观材料逐条验证验收标准是否满足：\n\n"
                    f"{self.render_context(invocation)}"
                ),
            ),
        ]

    def parse_output(self, raw_content: str, invocation: AgentInvocation) -> AgentOutput:
        data = _extract_json(raw_content)
        handoff = invocation.handoff
        assert handoff is not None

        checks = _parse_checks(data.get("criterion_checks") or [])
        failed = [c["criterion"] for c in checks if not c["passed"]]

        # 一致性保护：模型可能声称 pass 但存在未通过项。
        # 以**逐条判定结果**为准，不采信顶层 verdict，避免自相矛盾。
        declared = str(data.get("verdict") or "").strip().lower()
        derived_pass = not failed
        if declared in {Verdict.PASS.value, Verdict.REJECT.value}:
            declared_pass = declared == Verdict.PASS.value
            if declared_pass and failed:
                # 顶层说要 pass 但有失败项 → 以失败项为准
                derived_pass = False
        if not checks:
            # 未给出任何逐条判定 → 保守判为未通过
            derived_pass = False
            failed = list(handoff.acceptance_criteria)

        verdict = Verdict.PASS if derived_pass else Verdict.REJECT
        suggestions = [str(s).strip() for s in (data.get("suggestions") or []) if str(s).strip()]
        lesson = str(data.get("lesson") or "").strip() or None
        root_cause = str(data.get("root_cause") or "").strip()

        feedback = FeedbackPayload(
            verdict=verdict,
            failed_criteria=failed,
            evidence={"criterion_checks": checks},
            suggestions=suggestions,
            lesson=lesson,
            failure_kind=FailureKind.CRITERIA_UNMET if failed else FailureKind.UNKNOWN,
        )

        content = _render_verdict(verdict, checks, suggestions)
        return AgentOutput(
            content=content,
            feedback=feedback,
            message_type=MessageType.FEEDBACK,
            raw={"checks": checks, "root_cause": root_cause, "lesson": lesson},
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
        return {"verdict": "reject", "criterion_checks": [], "suggestions": ["验证输出无法解析"]}


def _parse_checks(raw: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        criterion = str(item.get("criterion") or "").strip()
        if not criterion:
            continue
        out.append(
            {
                "criterion": criterion,
                "passed": bool(item.get("passed", False)),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return out


def _render_verdict(verdict: Verdict, checks: list[dict[str, Any]], suggestions: list[str]) -> str:
    lines = [f"# 验证结论：{'通过' if verdict is Verdict.PASS else '未通过'}", ""]
    if checks:
        lines.append("## 逐条判定")
        for c in checks:
            mark = "[x]" if c["passed"] else "[ ]"
            lines.append(f"{mark} {c['criterion']}")
            if c["reason"]:
                lines.append(f"     理由：{c['reason']}")
    if suggestions:
        lines += ["", "## 修复建议", *[f"- {s}" for s in suggestions]]
    return "\n".join(lines)


__all__ = ["SYSTEM_PROMPT", "VerifierAgent"]
