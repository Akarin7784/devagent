"""Architect Agent：方案设计 + 任务分解。

职责：根据需求规格，产出技术方案并把任务分解为 **DAG**。
DAG 的价值：无依赖节点可并行执行；失败时只重跑受影响子图。

关键设计：产出的节点必须与上游的验收标准**可追溯对应**，
否则下游 Verifier 无法验证「是否真的满足了需求」。
"""

from __future__ import annotations

import json
import re
from typing import Any

from devagent.agents.base import AgentContextError, AgentInvocation, AgentOutput, BaseAgent
from devagent.enums import AgentType, MessageType
from devagent.models.domain import AgentHandoff
from devagent.models.provider import ChatMessage

SYSTEM_PROMPT = """\
你是一名资深软件架构师，负责把需求规格转为技术方案与任务分解。

你必须遵守：
1. 把任务分解为**最小可独立验证**的节点，每个节点有明确目标；
2. 显式声明节点之间的依赖（用节点 id 引用），无依赖的节点将并行执行；
3. 每个节点的验收标准必须能追溯回需求规格中的某条验收标准；
4. 方案需说明关键技术选型与理由（体现取舍，而非罗列技术名词）。

严格按以下 JSON 格式输出（不要输出其他内容）：

```json
{
  "approach": "技术方案概述与选型理由",
  "nodes": [
    {
      "id": "N1",
      "goal": "节点目标",
      "agent_type": "coder",
      "deps": [],
      "acceptance_criteria": ["该节点需满足的标准"]
    }
  ]
}
```

`agent_type` 取值：coder / tester / reviewer。
"""

VALID_NODE_AGENTS = {AgentType.CODER, AgentType.TESTER, AgentType.REVIEWER}


class ArchitectAgent(BaseAgent):
    """方案设计 Agent。"""

    agent_type = AgentType.ARCHITECT
    description = "产出技术方案并把任务分解为 DAG"

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def validate(self, invocation: AgentInvocation) -> None:
        handoff = invocation.handoff
        if handoff is None or not handoff.acceptance_criteria:
            raise AgentContextError(
                "Architect 需要上游提供带验收标准的需求规格；请先执行 Requirement Agent。"
            )

    def build_messages(self, invocation: AgentInvocation) -> list[ChatMessage]:
        return [
            ChatMessage(role="system", content=self.system_prompt),
            ChatMessage(
                role="user",
                content=f"请为以下需求设计技术方案并分解任务：\n\n{self.render_context(invocation)}",
            ),
        ]

    def parse_output(self, raw_content: str, invocation: AgentInvocation) -> AgentOutput:
        data = _extract_json(raw_content)
        approach = str(data.get("approach") or "").strip()
        raw_nodes = data.get("nodes") or []
        nodes, warnings = _parse_nodes(raw_nodes)

        if not nodes:
            raise AgentContextError("Architect 未能产出有效任务节点（nodes 为空或格式非法）。")

        upstream = invocation.handoff
        assert upstream is not None  # validate 已保证

        # 关键：把架构产出转为下游可用的 handoff，
        # 其中 constraints 承载「方案要点」，便于 Coder 遵循设计。
        handoff = AgentHandoff(
            task_id=invocation.task_id,
            goal=upstream.goal,
            acceptance_criteria=list(upstream.acceptance_criteria),
            constraints=[*upstream.constraints, f"遵循架构方案：{approach[:200]}"],
            relevant_files=list(upstream.relevant_files),
            context_refs=list(upstream.context_refs),
        )
        content = _render_plan(approach, nodes, warnings)
        return AgentOutput(
            content=content,
            handoff=handoff,
            message_type=MessageType.ARTIFACT,
            raw={"approach": approach, "nodes": nodes, "warnings": warnings},
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
        return {"approach": text.strip()[:300], "nodes": []}


def _parse_nodes(raw_nodes: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """解析并校验 DAG 节点，返回 (节点列表, 警告列表)。"""
    nodes: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for i, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            warnings.append(f"第 {i} 个节点不是对象，已忽略")
            continue
        node_id = str(item.get("id") or f"N{i + 1}").strip()
        if node_id in seen_ids:
            warnings.append(f"节点 id 重复：{node_id}，已重命名")
            node_id = f"{node_id}_{i}"
        seen_ids.add(node_id)

        agent_type_raw = str(item.get("agent_type") or "coder").strip().lower()
        try:
            agent_type = AgentType(agent_type_raw)
        except ValueError:
            warnings.append(f"节点 {node_id} 的 agent_type 非法：{agent_type_raw}，回退为 coder")
            agent_type = AgentType.CODER
        if agent_type not in VALID_NODE_AGENTS:
            warnings.append(f"节点 {node_id} 的 agent_type 不在允许范围，回退为 coder")
            agent_type = AgentType.CODER

        nodes.append(
            {
                "id": node_id,
                "goal": str(item.get("goal") or "").strip(),
                "agent_type": agent_type.value,
                "deps": [str(d).strip() for d in (item.get("deps") or []) if str(d).strip()],
                "acceptance_criteria": [
                    str(c).strip()
                    for c in (item.get("acceptance_criteria") or [])
                    if str(c).strip()
                ],
            }
        )

    # 校验依赖是否指向存在的节点；悬空依赖会导致 DAG 永远无法就绪
    all_ids = {n["id"] for n in nodes}
    for node in nodes:
        dangling = [d for d in node["deps"] if d not in all_ids]
        if dangling:
            warnings.append(f"节点 {node['id']} 存在悬空依赖 {dangling}，已移除")
            node["deps"] = [d for d in node["deps"] if d in all_ids]

    # 检测环
    if _has_cycle(nodes):
        warnings.append("检测到依赖环，已移除造成环的依赖")
        nodes = _break_cycles(nodes)

    return nodes, warnings


def _has_cycle(nodes: list[dict[str, Any]]) -> bool:
    graph = {n["id"]: list(n["deps"]) for n in nodes}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)

    def visit(node: str) -> bool:
        color[node] = GRAY
        for dep in graph.get(node, []):
            if color.get(dep) == GRAY:
                return True
            if color.get(dep) == WHITE and visit(dep):
                return True
        color[node] = BLACK
        return False

    return any(color[n] == WHITE and visit(n) for n in graph)


def _break_cycles(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """通过移除回边打破环（保守策略，仅移除造成环的依赖）。"""
    # 简单做法：拓扑排序，跳过形成环的依赖
    ids = [n["id"] for n in nodes]
    index = {nid: i for i, nid in enumerate(ids)}
    out: list[dict[str, Any]] = []
    for node in nodes:
        kept = [d for d in node["deps"] if d in index and index[d] < index[node["id"]]]
        out.append({**node, "deps": kept})
    return out


def _render_plan(approach: str, nodes: list[dict[str, Any]], warnings: list[str]) -> str:
    lines = ["# 技术方案", approach or "（未提供）", "", "## 任务分解"]
    for node in nodes:
        deps = ", ".join(node["deps"]) or "无"
        lines.append(f"### [{node['id']}] {node['goal']} ({node['agent_type']})")
        lines.append(f"依赖：{deps}")
        for c in node["acceptance_criteria"]:
            lines.append(f"  - {c}")
    if warnings:
        lines += ["", "## 解析警告"]
        lines += [f"- {w}" for w in warnings]
    return "\n".join(lines)


__all__ = ["SYSTEM_PROMPT", "ArchitectAgent"]
