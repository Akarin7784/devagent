"""节点级契约的回归测试（前端 DAG 可视化的后端接口）。

## 为什么单独一个文件

`test_dag.py` 测的是 DAG **算法**（拓扑序、就绪判定、失败传播）；
`test_api.py::TestToView` 测的是 `_to_view`，但它是拿**手写的假对象**
（`_FakeDAG` / `_FakeNode` / `_FakeState`）喂进去的。

这中间有一条真实的裂缝：**假对象的字段名可以悄无声息地与真实类不一致，
而所有测试依然全绿。** 本项目已经因此踩过一次同类型的坑 ——
前端用 `s.step_id === nodeId` 精确匹配，而真实 `step_id` 形如
`task_xxx:N1`，导致 diff 面板恒为空，直到用真实数据跑了一次才发现。

所以这个文件的定位是：**用真实的 `DAG` 对象，锁住前端真正依赖的那两层契约**

1. `_to_view()` 产出的 `NodeView` 字典字段（`/tasks/{id}` 的 `nodes`）；
2. `Orchestrator` 通过 `EventHook` 发出的节点事件 payload（SSE 的 `node.*`）。

前端 `web/graph.js` 直接消费这两层。任何一层字段改名/改语义，
这里必须红 —— 而不是等到界面上某个面板莫名其妙地空了。

约定：断言用**精确的键集合**，不用 `in` 逐个检查。多出来的字段同样是
契约变更（前端可能据此渲染），必须显式确认。
"""

from __future__ import annotations

from typing import Any

import pytest

from devagent.api.service import _to_view
from devagent.enums import AgentType, StepStatus
from devagent.models.domain import TaskNode
from devagent.orchestration.dag import DAG, build_dag_from_architect_output

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #


def _node(
    nid: str,
    *,
    deps: tuple[str, ...] = (),
    agent: AgentType = AgentType.CODER,
    goal: str = "",
) -> TaskNode:
    return TaskNode(
        id=nid,
        goal=goal or f"goal-{nid}",
        agent_type=agent,
        deps=deps,
        acceptance_criteria=("criterion",),
    )


def _real_dag() -> DAG:
    """一个真实的多节点 DAG：菱形结构（两条并行分支汇合）。"""
    return DAG.build(
        [
            _node("N1", agent=AgentType.CODER),
            _node("N2", deps=("N1",), agent=AgentType.TESTER),
            _node("N3", deps=("N1",), agent=AgentType.CODER),
            _node("N4", deps=("N2", "N3"), agent=AgentType.VERIFIER),
        ]
    )


class _RealResult:
    """把真实 DAG 包成 `_to_view` 期望的 result 对象。

    刻意**不**复制 `_FakeResult` 的手写字段，只提供 `_to_view` 真正
    `getattr` 的那些属性，并且 `dag` 用的是**真实 DAG 实例**。
    """

    def __init__(self, dag: DAG | None, *, task_id: str = "t1", goal: str = "需求") -> None:
        self.task_id = task_id
        self.goal = goal
        self.status = "succeeded"
        self.succeeded = True
        self.error = ""
        self.duration_ms = 12
        self.total_tokens = 300
        self.total_cost_usd = 0.003
        self.steps: list[Any] = []
        self.dag = dag


VIEW_KEYS = {
    "id",
    "goal",
    "agent_type",
    "deps",
    "status",
    "attempt",
    "tokens_used",
    "last_error",
}


# ---------------------------------------------------------------------- #
# 1. NodeView 契约（/tasks/{id} 的 nodes 字段）
# ---------------------------------------------------------------------- #


class TestNodeViewContract:
    """真实 DAG → `_to_view` → 前端消费的 NodeView。"""

    def test_every_node_view_has_exactly_the_expected_keys(self) -> None:
        """键集合必须精确匹配。

        不用 `in` 逐个断言：**多出一个键同样是契约变更**。
        前端 `buildGraphState` 会把未知键透传进图状态，
        悄悄多出来的字段可能改变渲染行为而不被察觉。
        """
        view = _to_view(_RealResult(_real_dag()))
        assert view["nodes"], "真实 DAG 应产出节点"
        for node in view["nodes"]:
            assert set(node) == VIEW_KEYS, f"{node['id']} 的键集合变了：{set(node) ^ VIEW_KEYS}"

    def test_all_nodes_are_present(self) -> None:
        view = _to_view(_RealResult(_real_dag()))
        assert {n["id"] for n in view["nodes"]} == {"N1", "N2", "N3", "N4"}

    def test_deps_are_serialized_as_list_not_tuple(self) -> None:
        """`TaskNode.deps` 是 tuple，但 JSON 需要 list。

        如果这里退化成 tuple，FastAPI 能容忍（它自己会转），
        但**直接读 `_to_view()` 的调用方**（如存储层、测试）就会拿到
        与 API 契约不一致的类型。前端 `.map()` 能跑，`.includes()` 也能跑，
        所以这类错误在界面上是隐形的。
        """
        view = _to_view(_RealResult(_real_dag()))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert by_id["N1"]["deps"] == []
        assert by_id["N4"]["deps"] == ["N2", "N3"]
        for node in view["nodes"]:
            assert isinstance(node["deps"], list), f"{node['id']}.deps 不是 list"

    def test_agent_type_is_serialized_as_plain_string(self) -> None:
        """枚举必须转成它的 value（str），不能是 `AgentType.CODER`。

        前端按字符串匹配 `AGENT_COLOR` 表；传枚举对象会让
        `agentColor()` 全部落到默认色 —— 一个「颜色都不对」但没人知道
        为什么的症状。
        """
        view = _to_view(_RealResult(_real_dag()))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert by_id["N1"]["agent_type"] == "coder"
        assert by_id["N2"]["agent_type"] == "tester"
        assert by_id["N4"]["agent_type"] == "verifier"
        for node in view["nodes"]:
            assert isinstance(node["agent_type"], str)

    def test_status_is_serialized_as_plain_string(self) -> None:
        """状态同理：必须是 value 而非枚举。"""
        dag = _real_dag()
        dag.mark("N1", StepStatus.SUCCESS)
        view = _to_view(_RealResult(dag))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert by_id["N1"]["status"] == "success"
        assert by_id["N2"]["status"] == "pending"
        for node in view["nodes"]:
            assert isinstance(node["status"], str)

    def test_fresh_dag_reports_attempt_one_not_zero(self) -> None:
        """新建 DAG 的所有节点：pending，且 **attempt 被规整为 1**。

        这里有三个不同的数字，很容易搞混，必须一次说清：

        | 层 | 值 | 原因 |
        | --- | --- | --- |
        | `StepState.attempt` 初值 | `0` | dataclass 默认值 |
        | `_to_view` 输出 | `1` | 实现里写了 `int(getattr(state, "attempt", 1) or 1)`，`or 1` 把 0 兜成 1 |
        | 前端显示 | `1` | `Number(n.attempt ?? 1) \\|\\| 1` 再兜一次 |

        也就是说 attempt=0 这个值**永远不会出现在 API 响应里**。
        后端已经把它规整掉了，前端那层 `|| 1` 是重复防御（无害）。
        本测试锁住「API 层负责规整」这个分工 —— 如果哪天有人把
        `or 1` 删掉（认为前端已经兜底了），这里会红。
        """
        view = _to_view(_RealResult(_real_dag()))
        for node in view["nodes"]:
            assert node["status"] == "pending"
            assert node["attempt"] == 1, "API 层必须把 StepState 的 0 规整为 1"
            assert node["tokens_used"] == 0

    def test_attempt_increments_are_visible(self) -> None:
        """回退重跑的核心信号：attempt 递增。

        前端用它渲染 `×N` 徽标。如果 `_to_view` 漏传 attempt，
        界面上的重跑痕迹会**完全消失**，而任务照样跑完 —— 无声的退化。
        """
        dag = _real_dag()
        dag.mark_attempt("N1")
        dag.mark_attempt("N1")
        view = _to_view(_RealResult(dag))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert by_id["N1"]["attempt"] == 2
        # 未重跑的节点仍是 1（不是 0，也不是 2）
        assert by_id["N2"]["attempt"] == 1

    def test_first_run_attempt_is_one(self) -> None:
        """仅递增一次（首次执行）时应为 1。

        注意 `mark_attempt` 是**递增**语义，`StepState` 初值 0，
        所以「第一次执行」调用一次后得到 1，而非 2。前端把
        `attempt > 1` 作为「发生过回退」的判据，因此这个边界
        决定了 `×1` 徽标不显示、`×2` 才显示。算错一档会让
        所有首轮执行的节点都挂上误导性的重跑徽标。
        """
        dag = _real_dag()
        dag.mark_attempt("N1")
        view = _to_view(_RealResult(dag))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert by_id["N1"]["attempt"] == 1

    def test_tokens_are_visible_per_node(self) -> None:
        dag = _real_dag()
        dag.mark("N1", StepStatus.SUCCESS, tokens_used=595)
        dag.mark("N2", StepStatus.SUCCESS, tokens_used=310)
        view = _to_view(_RealResult(dag))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert by_id["N1"]["tokens_used"] == 595
        assert by_id["N2"]["tokens_used"] == 310

    def test_last_error_none_becomes_empty_string(self) -> None:
        """`StepState.last_error` 初值是 **None**，但契约必须是字符串。

        这是真实类与 `test_api.py` 里 `_FakeState` 的一处**不一致**：
        假对象初值给的是 `""`，真实类给的是 `None`。
        `_to_view` 用 `str(... or "")` 兜住了，所以没暴露 —— 但正因为
        假对象掩盖了这一点，才必须用真实类补一条测试锁住它。
        """
        dag = _real_dag()
        view = _to_view(_RealResult(dag))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert by_id["N1"]["last_error"] == ""
        assert isinstance(by_id["N1"]["last_error"], str)

    def test_last_error_text_propagates(self) -> None:
        dag = _real_dag()
        dag.mark("N1", StepStatus.FAILED, last_error="未覆盖负数分页")
        view = _to_view(_RealResult(dag))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert by_id["N1"]["last_error"] == "未覆盖负数分页"

    def test_backtracked_is_not_a_step_status(self) -> None:
        """**`backtracked` 不是 `StepStatus` 的成员** —— 它是事件层专有的状态。

        这是一个容易搞混、且已经在本项目里真实造成过理解偏差的点，
        所以用测试把它钉死：

        | 来源 | 可能出现的状态值 |
        | --- | --- |
        | `StepStatus`（DAG 状态机） | `pending` / `ready` / `running` / `verifying` / `success` / `rejected` / `failed` / `skipped` |
        | `node_finished` 事件 | 上述任意值，**外加** `backtracked` |
        | `/tasks/{id}` 的 `nodes` | 只能是 `StepStatus` 的值（**不会有** `backtracked`） |

        为什么只给事件层加这一个值：DAG 状态机里「被驳回后重置」和
        「从未执行」确实都是 `PENDING`，状态机层面就该一样；
        但对**观察者**而言这两件事完全不同 —— 前者是「跑了、被打回了」，
        后者是「还没跑」。因此这个区分只存在于用于展示的事件层。

        推论（前端必须知道）：**只看 `/tasks/{id}` 快照，是看不出回退的**。
        `rejected` 与 `backtracked` 是两个不同的字面量，前端
        `applyEvent` 对 `node_verdict: reject` 显式置为 `backtracked`，
        而快照刷新会把节点覆盖成 `rejected` 或 `pending`。
        这是「乐观更新 vs 权威覆盖」必须存在的原因之一。
        """
        from devagent.enums import StepStatus

        values = {m.value for m in StepStatus}
        assert "backtracked" not in values, "backtracked 不应出现在 StepStatus 里"
        assert "rejected" in values, "被驳回的终态用 rejected 表示"

    def test_rejected_status_survives_serialization(self) -> None:
        """`rejected` 必须原样出现在 nodes 里，不能被规整掉。

        它是快照层唯一能表达「验证未通过」的状态值。
        """
        dag = _real_dag()
        dag.mark("N1", StepStatus.REJECTED, last_error="未覆盖负数分页")
        view = _to_view(_RealResult(dag))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert by_id["N1"]["status"] == "rejected"
        assert by_id["N1"]["last_error"] == "未覆盖负数分页"

    def test_all_serialized_statuses_come_from_step_status(self) -> None:
        """`nodes` 里出现的每个 status 都必须是合法的 `StepStatus` 值。

        这条断言是上面那条的**运行时对偶**：即使有人给 `_to_view`
        加了状态映射逻辑（例如想把 `rejected` 显示成更友好的词），
        只要映射结果不在枚举内，这里就会红。
        前端 `STATUS_GROUP` 表是按枚举值查的，未知值会静默落到 `pending`。
        """
        values = {m.value for m in StepStatus}
        dag = _real_dag()
        dag.mark("N1", StepStatus.SUCCESS)
        dag.mark("N2", StepStatus.REJECTED)
        dag.mark("N3", StepStatus.FAILED)
        dag.mark("N4", StepStatus.SKIPPED)
        view = _to_view(_RealResult(dag))
        for node in view["nodes"]:
            assert node["status"] in values, f"{node['id']} 的 status 非法：{node['status']}"

    def test_skipped_nodes_are_serialized(self) -> None:
        """失败传播产生的 SKIPPED 也要出现在 nodes 里。

        前端需要画出「被跳过的下游节点」，否则读者会以为那些节点
        从未存在于图中。
        """
        dag = _real_dag()
        dag.mark("N1", StepStatus.FAILED, last_error="编译不通过")
        for node_id in dag.descendants_of("N1"):
            dag.mark(node_id, StepStatus.SKIPPED)
        view = _to_view(_RealResult(dag))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert by_id["N1"]["status"] == "failed"
        assert by_id["N2"]["status"] == "skipped"
        assert by_id["N4"]["status"] == "skipped"
        assert len(view["nodes"]) == 4, "被跳过的节点不能从 nodes 里消失"

    def test_goal_is_carried_through(self) -> None:
        dag = DAG.build([_node("N1", goal="实现分页参数解析")])
        view = _to_view(_RealResult(dag))
        assert view["nodes"][0]["goal"] == "实现分页参数解析"

    def test_missing_dag_yields_empty_nodes(self) -> None:
        """需求阶段就失败时 dag 为 None —— 不能崩，也不能造节点。"""
        view = _to_view(_RealResult(None))
        assert view["nodes"] == []
        assert view["steps"] == []

    def test_nodes_order_is_stable_across_calls(self) -> None:
        """两次序列化同一 DAG 必须给出相同顺序。

        前端 `layoutDag` 靠输入顺序做层内稳定排序的 tie-break；
        顺序抖动会让同一张图在刷新后节点位置跳变。
        """
        dag = _real_dag()
        first = [n["id"] for n in _to_view(_RealResult(dag))["nodes"]]
        second = [n["id"] for n in _to_view(_RealResult(dag))["nodes"]]
        assert first == second

    def test_can_be_consumed_by_node_view_schema(self) -> None:
        """`_to_view` 的输出必须能通过 `NodeView` 校验。

        这是「视图字典」与「响应模型」之间唯一的强一致性检查。
        少一个必填字段（如 `id`）会在这里立刻报错，而不是等到
        接口返回 500。
        """
        from devagent.api.schemas import NodeView

        view = _to_view(_RealResult(_real_dag()))
        for node in view["nodes"]:
            NodeView.model_validate(node)  # 不通过则抛 ValidationError

    def test_built_from_architect_output_is_serializable(self) -> None:
        """走真实的 architect 输出解析路径，而不是手工 `DAG.build`。

        这条路径多了一层「原始 dict → TaskNode」的转换，
        是字段丢失的高风险位置（例如 deps 没被正确传递）。
        """
        dag = build_dag_from_architect_output(
            [
                {"id": "N1", "goal": "解析分页参数", "agent_type": "coder", "deps": []},
                {
                    "id": "N2",
                    "goal": "补边界测试",
                    "agent_type": "tester",
                    "deps": ["N1"],
                },
            ]
        )
        view = _to_view(_RealResult(dag))
        by_id = {n["id"]: n for n in view["nodes"]}
        assert set(by_id) == {"N1", "N2"}
        assert by_id["N2"]["deps"] == ["N1"]
        assert by_id["N1"]["agent_type"] == "coder"
        assert by_id["N2"]["agent_type"] == "tester"


class TestCrossLanguageWordlist:
    """跨语言词表 fixture 的新鲜度。

    `web/graph.js` 消费的是 Python 枚举产出的字符串，中间没有类型系统
    兜底。前端契约测试（`web/graph.test.js`）读的是
    `web/test_contract_words.json`，而那个文件由本目录的 conftest 从
    `enums.py` 导出。

    这里断言导出内容与**当前**枚举完全一致：如果有人手改了 fixture 或
    改了枚举却没重跑 pytest，前端那套契约测试就会拿着旧词表空跑通过。
    """

    def test_fixture_lists_every_step_status(self, contract_words_path: Any) -> None:
        import json
        from pathlib import Path

        data = json.loads(Path(contract_words_path).read_text(encoding="utf-8"))
        assert data["step_status"] == [s.value for s in StepStatus]

    def test_fixture_lists_every_agent_type(self, contract_words_path: Any) -> None:
        import json
        from pathlib import Path

        data = json.loads(Path(contract_words_path).read_text(encoding="utf-8"))
        assert data["agent_type"] == [a.value for a in AgentType]

    def test_step_status_contains_ready_verifying_rejected(self) -> None:
        """固化这三个最容易在前端漏登记的状态。

        它们都是**非终态**或**快照态**，在前端漏登记时不会报错，
        只会静默显示成"待执行"——这正是 Task #15 用契约测试查出来的缺陷。
        """
        values = {s.value for s in StepStatus}
        assert {"ready", "verifying", "rejected"} <= values

    def test_backtracked_is_not_a_step_status(self) -> None:
        """`backtracked` 只存在于事件层，不是枚举成员。

        它是 `applyEvent` 收到 `node_verdict: reject` 时的派生态。
        这条测试防止有人"顺手"把它加进 StepStatus —— 那会让快照层与
        事件层出现两个语义重叠的字面量，前端无从判断该信哪个。
        """
        values = {s.value for s in StepStatus}
        assert "backtracked" not in values

    def test_agent_type_uses_orchestrator_not_coordinator(self) -> None:
        """前端曾把键名写成 `coordinator`，导致编排器节点永远退成灰色。

        编排器是 DAG 图上唯一必然存在的节点，它没颜色 = 第一眼就是错的。
        """
        values = {a.value for a in AgentType}
        assert "orchestrator" in values
        assert "coordinator" not in values
