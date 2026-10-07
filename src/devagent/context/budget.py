"""预算分配（L5）。

对齐 ``docs/02-上下文工程深度设计.md`` 的预算分配设计。

核心职责：
1. 为不同 Agent 角色提供差异化预算模板；
2. 在配额未用满时执行**动态回流**（余量优先补给代码上下文）；
3. 硬性保护 ``system_prompt`` 与 ``task_spec`` 配额（丢了会跑偏）。
"""

from __future__ import annotations

from dataclasses import dataclass

from devagent.enums import AgentType
from devagent.models.domain import BudgetAllocation


@dataclass(frozen=True, slots=True)
class BudgetTemplate:
    """角色预算模板：定义各用途的基础配额比例。

    比例之和应尽量接近 1.0；不足部分由动态回流补足。
    """

    system_prompt: float
    task_spec: float
    code_context: float
    tool_results: float
    history: float
    reserved_output: float

    def total_ratio(self) -> float:
        return (
            self.system_prompt
            + self.task_spec
            + self.code_context
            + self.tool_results
            + self.history
            + self.reserved_output
        )


# 各角色的预算模板（经验值，可通过实验调优）
TEMPLATES: dict[AgentType, BudgetTemplate] = {
    AgentType.ORCHESTRATOR: BudgetTemplate(
        system_prompt=0.10,
        task_spec=0.25,
        code_context=0.15,
        tool_results=0.10,
        history=0.25,
        reserved_output=0.15,
    ),
    AgentType.REQUIREMENT: BudgetTemplate(
        system_prompt=0.10,
        task_spec=0.20,
        code_context=0.05,
        tool_results=0.05,
        history=0.45,
        reserved_output=0.15,
    ),
    AgentType.ARCHITECT: BudgetTemplate(
        system_prompt=0.08,
        task_spec=0.15,
        code_context=0.40,
        tool_results=0.05,
        history=0.20,
        reserved_output=0.12,
    ),
    AgentType.CODER: BudgetTemplate(
        # 编码最依赖代码上下文
        system_prompt=0.06,
        task_spec=0.10,
        code_context=0.50,
        tool_results=0.12,
        history=0.10,
        reserved_output=0.12,
    ),
    AgentType.TESTER: BudgetTemplate(
        system_prompt=0.06,
        task_spec=0.15,
        code_context=0.40,
        tool_results=0.20,
        history=0.07,
        reserved_output=0.12,
    ),
    AgentType.VERIFIER: BudgetTemplate(
        # 验证只看证据，不需要长历史
        system_prompt=0.08,
        task_spec=0.30,
        code_context=0.25,
        tool_results=0.25,
        history=0.02,
        reserved_output=0.10,
    ),
    AgentType.REVIEWER: BudgetTemplate(
        system_prompt=0.08,
        task_spec=0.15,
        code_context=0.45,
        tool_results=0.05,
        history=0.12,
        reserved_output=0.15,
    ),
}
"""缺省模板（当角色未定义时使用）。"""

DEFAULT_TEMPLATE = BudgetTemplate(
    system_prompt=0.08,
    task_spec=0.15,
    code_context=0.40,
    tool_results=0.10,
    history=0.15,
    reserved_output=0.12,
)


class BudgetAllocator:
    """按角色分配 token 预算。

    用法::

        allocator = BudgetAllocator()
        budget = allocator.allocate(AgentType.CODER, total=16000)
    """

    def __init__(self, templates: dict[AgentType, BudgetTemplate] | None = None) -> None:
        self._templates = templates or TEMPLATES

    def template_for(self, agent: AgentType) -> BudgetTemplate:
        return self._templates.get(agent, DEFAULT_TEMPLATE)

    def allocate(self, agent: AgentType, *, total: int) -> BudgetAllocation:
        """为指定角色分配预算。

        Args:
            agent: 角色。
            total: 总 token 预算。

        Returns:
            ``BudgetAllocation``，各用途配额按模板比例切分，
            余量已通过 ``suggest_rebalance`` 回流给代码上下文。
        """
        if total <= 0:
            raise ValueError(f"total 必须为正，收到 {total}")

        tpl = self.template_for(agent)
        # 归一化：防止自定义模板比例和不为 1
        ratio_sum = tpl.total_ratio() or 1.0
        scale = 1.0 / ratio_sum

        budget = BudgetAllocation(
            total=total,
            system_prompt=int(total * tpl.system_prompt * scale),
            task_spec=int(total * tpl.task_spec * scale),
            code_context=int(total * tpl.code_context * scale),
            tool_results=int(total * tpl.tool_results * scale),
            history=int(total * tpl.history * scale),
            reserved_output=int(total * tpl.reserved_output * scale),
        )
        # 余量回流给代码上下文（弹性最大、收益最高的用途）
        return budget.suggest_rebalance()

    def quota_for_kind(self, agent: AgentType, total: int, kind: str) -> int:
        """查询某角色在某用途上的配额（供装配阶段做分用途限制）。"""
        budget = self.allocate(agent, total=total)
        return {
            "system_prompt": budget.system_prompt,
            "task_spec": budget.task_spec,
            "code": budget.code_context,
            "tool_result": budget.tool_results,
            "history": budget.history,
        }.get(kind, 0)


__all__ = [
    "DEFAULT_TEMPLATE",
    "TEMPLATES",
    "BudgetAllocator",
    "BudgetTemplate",
]
