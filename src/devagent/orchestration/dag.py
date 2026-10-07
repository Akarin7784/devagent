"""DAG 构建与调度。

职责：
- 把 Architect 产出的节点列表构建为 DAG；
- 提供拓扑排序、就绪判定、环检测、关键路径识别。

设计原则：DAG 是**不可变**的（构建后不再变更），
状态变化由 ``StepState`` 单独维护，便于回放与调试。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from devagent.enums import AgentType, StepStatus
from devagent.models.domain import TaskNode


class DAGError(RuntimeError):
    """DAG 构造或使用错误。"""


@dataclass(slots=True)
class StepState:
    """节点的运行时状态（与不可变的 DAG 分离）。"""

    node_id: str
    status: StepStatus = StepStatus.PENDING
    attempt: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    last_error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.SKIPPED}


@dataclass(slots=True)
class DAG:
    """有向无环图。

    不可变结构：``nodes`` 与邻接关系在构造时确定；
    运行时状态由 ``states`` 维护。
    """

    nodes: dict[str, TaskNode]
    dependents: dict[str, list[str]] = field(default_factory=dict)
    """反向邻接：node_id → 依赖它的节点列表（用于失败传播）。"""
    states: dict[str, StepState] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #

    @classmethod
    def build(cls, nodes: list[TaskNode]) -> DAG:
        """从节点列表构建 DAG。

        Raises:
            DAGError: 节点 id 重复、依赖指向不存在的节点、或存在环。
        """
        node_map: dict[str, TaskNode] = {}
        for node in nodes:
            if node.id in node_map:
                raise DAGError(f"节点 id 重复：{node.id}")
            node_map[node.id] = node

        dependents: dict[str, list[str]] = {nid: [] for nid in node_map}
        for node in node_map.values():
            for dep in node.deps:
                if dep not in node_map:
                    raise DAGError(f"节点 {node.id} 依赖不存在的节点：{dep}")
                dependents[dep].append(node.id)

        dag = cls(nodes=node_map, dependents=dependents)
        dag._assert_acyclic()

        for nid in node_map:
            dag.states[nid] = StepState(node_id=nid)
        return dag

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def topological_order(self) -> list[str]:
        """返回拓扑序（Kahn 算法）。同层按 priority 降序，加速关键路径。"""
        indegree = {nid: len(node.deps) for nid, node in self.nodes.items()}
        ready = [nid for nid, deg in indegree.items() if deg == 0]
        ready.sort(key=lambda nid: -self.nodes[nid].priority)

        order: list[str] = []
        queue = deque(ready)
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for dependent in self.dependents.get(nid, []):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    queue.append(dependent)

        if len(order) != len(self.nodes):
            raise DAGError("图中存在环，无法完成拓扑排序")
        return order

    def ready_nodes(self) -> list[str]:
        """返回当前可执行的节点（依赖全部成功，且自身为 PENDING）。

        依赖失败或已被跳过的节点会被标记为 SKIPPED，不进入就绪集。

        注意：失败传播必须**递归生效**——若只判断 ``FAILED``，
        则「上游的上游失败」导致本节点被跳过时，其下游仍是 PENDING，
        形成孤立的悬挂节点。因此同时检查 ``FAILED`` 与 ``SKIPPED``。
        """
        # 反复扫描直到无新跳过产生，保证传播闭环
        changed = True
        while changed:
            changed = False
            for nid, node in self.nodes.items():
                state = self.states[nid]
                if state.status is not StepStatus.PENDING:
                    continue
                dep_statuses = [self.states[d].status for d in node.deps]
                blocked = any(s in {StepStatus.FAILED, StepStatus.SKIPPED} for s in dep_statuses)
                if blocked:
                    state.status = StepStatus.SKIPPED
                    state.last_error = "上游节点失败或被跳过，本节点已跳过"
                    changed = True

        ready: list[str] = []
        for nid, node in self.nodes.items():
            state = self.states[nid]
            if state.status is not StepStatus.PENDING:
                continue
            if all(self.states[d].status is StepStatus.SUCCESS for d in node.deps):
                ready.append(nid)
        ready.sort(key=lambda nid: -self.nodes[nid].priority)
        return ready

    def blocked_nodes(self) -> list[str]:
        """返回仍被阻塞的节点（依赖尚未成功）。"""
        out: list[str] = []
        for nid, node in self.nodes.items():
            if self.states[nid].status is not StepStatus.PENDING:
                continue
            if any(self.states[d].status is not StepStatus.SUCCESS for d in node.deps):
                out.append(nid)
        return out

    def is_complete(self) -> bool:
        return all(s.is_terminal for s in self.states.values())

    def is_success(self) -> bool:
        return bool(self.states) and all(
            s.status is StepStatus.SUCCESS for s in self.states.values()
        )

    def has_failure(self) -> bool:
        return any(s.status is StepStatus.FAILED for s in self.states.values())

    def descendants_of(self, node_id: str) -> set[str]:
        """返回某节点的所有下游节点（用于失败时评估影响面）。"""
        out: set[str] = set()
        stack = list(self.dependents.get(node_id, []))
        while stack:
            nid = stack.pop()
            if nid in out:
                continue
            out.add(nid)
            stack.extend(self.dependents.get(nid, []))
        return out

    def ancestors_of(self, node_id: str) -> set[str]:
        """返回某节点的所有上游节点（用于回退时确定重跑范围）。"""
        out: set[str] = set()
        stack = list(self.nodes[node_id].deps)
        while stack:
            nid = stack.pop()
            if nid in out:
                continue
            out.add(nid)
            stack.extend(self.nodes[nid].deps)
        return out

    def critical_path_length(self) -> int:
        """最长依赖链长度（关键路径步数），用于预估最短完成时间。"""
        depth: dict[str, int] = {}
        for nid in self.topological_order():
            deps = self.nodes[nid].deps
            depth[nid] = 1 + max((depth[d] for d in deps), default=0)
        return max(depth.values()) if depth else 0

    # ------------------------------------------------------------------ #
    # 状态变更
    # ------------------------------------------------------------------ #

    def mark(self, node_id: str, status: StepStatus, **kwargs: object) -> None:
        """更新节点状态。

        未知字段**直接报错**而不是静默忽略：早先用 ``if hasattr(state, key)``
        过滤，于是把 ``tokens_used`` 拼成 ``token_used`` 不会有任何提示，
        只是数据悄悄丢了 —— 这类问题在状态图上表现为"数值偶尔为 0"，
        排查成本远高于一次显式异常。
        """
        if node_id not in self.states:
            raise DAGError(f"未知节点：{node_id}")
        state = self.states[node_id]
        unknown = [key for key in kwargs if not hasattr(state, key)]
        if unknown:
            raise DAGError(f"未知的节点状态字段：{unknown}（节点 {node_id}）")
        state.status = status
        for key, value in kwargs.items():
            setattr(state, key, value)

    def mark_attempt(self, node_id: str) -> None:
        """递增尝试次数。"""
        self.states[node_id].attempt += 1

    def reset_subgraph(self, node_id: str) -> list[str]:
        """把某节点及其所有下游重置为 PENDING（失败回退时使用）。

        Returns:
            被重置的节点 id 列表。
        """
        affected = [node_id, *self.descendants_of(node_id)]
        for nid in affected:
            state = self.states[nid]
            state.status = StepStatus.PENDING
            state.last_error = None
        return affected

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _assert_acyclic(self) -> None:
        indegree = {nid: len(n.deps) for nid, n in self.nodes.items()}
        queue = deque(nid for nid, deg in indegree.items() if deg == 0)
        visited = 0
        while queue:
            nid = queue.popleft()
            visited += 1
            for dependent in self.dependents.get(nid, []):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    queue.append(dependent)
        if visited != len(self.nodes):
            raise DAGError("DAG 中存在环")


def build_dag_from_architect_output(nodes: list[dict[str, object]]) -> DAG:
    """从 Architect 的解析产物构建 DAG。

    Args:
        nodes: Architect 输出的节点字典列表。
    """
    task_nodes: list[TaskNode] = []
    for raw in nodes:
        agent_raw = str(raw.get("agent_type") or "coder")
        try:
            agent_type = AgentType(agent_raw)
        except ValueError:
            agent_type = AgentType.CODER
        # raw 来自 LLM 输出的 JSON，字段类型不可信，故逐项显式收窄。
        # 直接对 ``object`` 做 tuple()/int() 会触发类型错误且掩盖脏数据。
        deps_raw = raw.get("deps")
        deps = [str(d) for d in deps_raw] if isinstance(deps_raw, list | tuple) else []
        criteria_raw = raw.get("acceptance_criteria")
        criteria = [str(c) for c in criteria_raw] if isinstance(criteria_raw, list | tuple) else []
        priority_raw = raw.get("priority")
        priority = int(priority_raw) if isinstance(priority_raw, int | float | str) else 0

        task_nodes.append(
            TaskNode(
                id=str(raw.get("id") or ""),
                goal=str(raw.get("goal") or ""),
                agent_type=agent_type,
                deps=tuple(deps),
                acceptance_criteria=tuple(criteria),
                priority=priority,
            )
        )
    return DAG.build(task_nodes)


__all__ = [
    "DAG",
    "DAGError",
    "StepState",
    "build_dag_from_architect_output",
]
