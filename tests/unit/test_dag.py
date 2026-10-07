"""DAG 的单元测试。

重点验证：拓扑序、就绪判定、失败传播、回退重置、环检测。
"""

from __future__ import annotations

import pytest

from devagent.enums import AgentType, StepStatus
from devagent.models.domain import TaskNode
from devagent.orchestration.dag import (
    DAG,
    DAGError,
    build_dag_from_architect_output,
)


def _node(nid: str, deps: list[str] | None = None, priority: int = 0) -> TaskNode:
    return TaskNode(
        id=nid,
        goal=f"goal-{nid}",
        agent_type=AgentType.CODER,
        deps=tuple(deps or []),
        acceptance_criteria=("criterion",),
        priority=priority,
    )


class TestBuild:
    def test_build_simple_chain(self) -> None:
        dag = DAG.build([_node("N1"), _node("N2", ["N1"]), _node("N3", ["N2"])])
        assert set(dag.nodes) == {"N1", "N2", "N3"}
        assert dag.dependents["N1"] == ["N2"]
        assert dag.dependents["N2"] == ["N3"]

    def test_duplicate_id_rejected(self) -> None:
        with pytest.raises(DAGError, match="id 重复"):
            DAG.build([_node("N1"), _node("N1")])

    def test_missing_dependency_rejected(self) -> None:
        with pytest.raises(DAGError, match="依赖不存在的节点"):
            DAG.build([_node("N1", ["GHOST"])])

    def test_cycle_rejected(self) -> None:
        with pytest.raises(DAGError, match="环"):
            DAG.build([_node("N1", ["N2"]), _node("N2", ["N1"])])

    def test_self_loop_rejected(self) -> None:
        with pytest.raises(DAGError, match="环"):
            DAG.build([_node("N1", ["N1"])])

    def test_empty_dag(self) -> None:
        dag = DAG.build([])
        assert dag.is_complete()
        assert not dag.is_success()  # 空图不算成功（避免误判）


class TestTopologicalOrder:
    def test_chain_order(self) -> None:
        dag = DAG.build([_node("N1"), _node("N2", ["N1"]), _node("N3", ["N2"])])
        assert dag.topological_order() == ["N1", "N2", "N3"]

    def test_diamond_order(self) -> None:
        dag = DAG.build(
            [
                _node("A"),
                _node("B", ["A"]),
                _node("C", ["A"]),
                _node("D", ["B", "C"]),
            ]
        )
        order = dag.topological_order()
        assert order.index("A") < order.index("B")
        assert order.index("A") < order.index("C")
        assert order.index("B") < order.index("D")
        assert order.index("C") < order.index("D")

    def test_priority_affects_order_within_layer(self) -> None:
        dag = DAG.build([_node("low", priority=0), _node("high", priority=10)])
        assert dag.topological_order()[0] == "high"


class TestReadyNodes:
    def test_only_roots_ready_initially(self) -> None:
        dag = DAG.build([_node("N1"), _node("N2", ["N1"])])
        assert dag.ready_nodes() == ["N1"]

    def test_parallel_roots_all_ready(self) -> None:
        dag = DAG.build([_node("A"), _node("B"), _node("C", ["A", "B"])])
        assert set(dag.ready_nodes()) == {"A", "B"}

    def test_dependent_becomes_ready_after_success(self) -> None:
        dag = DAG.build([_node("N1"), _node("N2", ["N1"])])
        dag.mark("N1", StepStatus.SUCCESS)
        assert dag.ready_nodes() == ["N2"]

    def test_failure_propagates_as_skip(self) -> None:
        """上游失败 → 下游标记 SKIPPED，不进入就绪集。"""
        dag = DAG.build([_node("N1"), _node("N2", ["N1"]), _node("N3", ["N2"])])
        dag.mark("N1", StepStatus.FAILED)

        ready = dag.ready_nodes()
        assert ready == []
        assert dag.states["N2"].status is StepStatus.SKIPPED
        assert dag.states["N3"].status is StepStatus.SKIPPED

    def test_running_node_not_ready(self) -> None:
        dag = DAG.build([_node("N1")])
        dag.mark("N1", StepStatus.RUNNING)
        assert dag.ready_nodes() == []

    def test_blocked_nodes_reported(self) -> None:
        dag = DAG.build([_node("N1"), _node("N2", ["N1"])])
        assert dag.blocked_nodes() == ["N2"]


class TestCompletion:
    def test_not_complete_when_pending(self) -> None:
        dag = DAG.build([_node("N1")])
        assert not dag.is_complete()

    def test_complete_when_all_terminal(self) -> None:
        dag = DAG.build([_node("N1"), _node("N2", ["N1"])])
        dag.mark("N1", StepStatus.SUCCESS)
        dag.mark("N2", StepStatus.SUCCESS)
        assert dag.is_complete()
        assert dag.is_success()
        assert not dag.has_failure()

    def test_has_failure(self) -> None:
        dag = DAG.build([_node("N1")])
        dag.mark("N1", StepStatus.FAILED)
        assert dag.is_complete()
        assert not dag.is_success()
        assert dag.has_failure()


class TestTraversal:
    def test_descendants(self) -> None:
        dag = DAG.build([_node("A"), _node("B", ["A"]), _node("C", ["B"]), _node("D")])
        assert dag.descendants_of("A") == {"B", "C"}
        assert dag.descendants_of("D") == set()

    def test_ancestors(self) -> None:
        dag = DAG.build([_node("A"), _node("B", ["A"]), _node("C", ["B"])])
        assert dag.ancestors_of("C") == {"A", "B"}
        assert dag.ancestors_of("A") == set()

    def test_critical_path_length(self) -> None:
        dag = DAG.build([_node("A"), _node("B", ["A"]), _node("C", ["B"]), _node("X")])
        # 最长链 A→B→C = 3
        assert dag.critical_path_length() == 3


class TestResetSubgraph:
    def test_reset_markes_node_and_descendants_pending(self) -> None:
        dag = DAG.build([_node("A"), _node("B", ["A"]), _node("C", ["B"])])
        dag.mark("A", StepStatus.SUCCESS)
        dag.mark("B", StepStatus.REJECTED)
        dag.mark("C", StepStatus.PENDING)

        affected = dag.reset_subgraph("B")
        assert set(affected) == {"B", "C"}
        assert dag.states["B"].status is StepStatus.PENDING
        assert dag.states["C"].status is StepStatus.PENDING
        # 上游 A 不受影响
        assert dag.states["A"].status is StepStatus.SUCCESS


class TestAttemptTracking:
    def test_mark_attempt_increments(self) -> None:
        dag = DAG.build([_node("N1")])
        dag.mark_attempt("N1")
        dag.mark_attempt("N1")
        assert dag.states["N1"].attempt == 2

    def test_mark_unknown_node_raises(self) -> None:
        dag = DAG.build([_node("N1")])
        with pytest.raises(DAGError, match="未知节点"):
            dag.mark("GHOST", StepStatus.SUCCESS)


class TestBuildFromArchitect:
    def test_builds_from_raw_dicts(self) -> None:
        raw = [
            {"id": "N1", "goal": "a", "agent_type": "coder", "deps": []},
            {"id": "N2", "goal": "b", "agent_type": "tester", "deps": ["N1"]},
        ]
        dag = build_dag_from_architect_output(raw)
        assert dag.nodes["N2"].agent_type is AgentType.TESTER
        assert dag.nodes["N2"].deps == ("N1",)

    def test_invalid_agent_type_defaults_to_coder(self) -> None:
        raw = [{"id": "N1", "goal": "a", "agent_type": "wizard", "deps": []}]
        dag = build_dag_from_architect_output(raw)
        assert dag.nodes["N1"].agent_type is AgentType.CODER

    def test_acceptance_criteria_propagates(self) -> None:
        raw = [
            {
                "id": "N1",
                "goal": "a",
                "agent_type": "coder",
                "deps": [],
                "acceptance_criteria": ["c1", "c2"],
            }
        ]
        dag = build_dag_from_architect_output(raw)
        assert dag.nodes["N1"].acceptance_criteria == ("c1", "c2")
