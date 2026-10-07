"""API 层测试。

用假 Orchestrator 注入，避免测试依赖真实 LLM。覆盖：

- 健康检查与可观测性端点；
- 任务 CRUD（含 404/409 边界）；
- **SSE 事件流**（含历史回放、任务结束自动关闭）；
- 并发控制与取消；
- 统一错误结构。

SSE 是最容易出错的部分：晚订阅、快任务、客户端断开，
这几种情况都单独测。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from devagent.api.app import create_app
from devagent.api.service import TaskService, _to_view
from devagent.api.store import EventBus, InMemoryTaskStore, TaskEvent
from devagent.config import Settings
from devagent.enums import TaskStatus

# ---------------------------------------------------------------------- #
# 测试替身
# ---------------------------------------------------------------------- #


class _FakeState:
    def __init__(self, status: Any, attempt: int = 1, tokens: int = 0) -> None:
        self.status = status
        self.attempt = attempt
        self.tokens_used = tokens
        self.last_error = ""


class _FakeNode:
    def __init__(self, goal: str, deps: tuple[str, ...] = ()) -> None:
        from devagent.enums import AgentType

        self.goal = goal
        self.agent_type = AgentType.CODER
        self.deps = deps


class _FakeDAG:
    def __init__(self) -> None:
        from devagent.enums import StepStatus

        self.nodes = {"N1": _FakeNode("实现分页"), "N2": _FakeNode("补测试", ("N1",))}
        self.states = {
            "N1": _FakeState(StepStatus.SUCCESS, tokens=100),
            "N2": _FakeState(StepStatus.SUCCESS, tokens=50),
        }


class _FakeStep:
    def __init__(self, step_id: str, agent: str, output: str = "done") -> None:
        from devagent.enums import AgentType

        self.step_id = step_id
        self.agent = AgentType(agent)
        self.output = output
        self.tokens_used = 100
        self.cost_usd = 0.001
        self.model = "fake:model"
        self.attempt = 1
        self.feedback = None


class _FakeResult:
    def __init__(self, task_id: str, goal: str, *, succeed: bool = True) -> None:
        from devagent.enums import StepStatus

        self.task_id = task_id
        self.goal = goal
        self.status = TaskStatus.SUCCEEDED if succeed else TaskStatus.FAILED
        self.succeeded = succeed
        self.error = "" if succeed else "存在失败节点"
        self.duration_ms = 42
        self.total_tokens = 150
        self.total_cost_usd = 0.002
        self.steps = [_FakeStep(f"{task_id}:N1", "coder")]
        self.dag = _FakeDAG() if succeed else None
        _ = StepStatus


class _FakeOrchestrator:
    """假编排器：可控耗时与成败。"""

    def __init__(self, *, delay: float = 0.0, succeed: bool = True) -> None:
        self.delay = delay
        self.succeed = succeed
        self.closed = False
        self.calls: list[str] = []

    async def run(self, goal: str, *, task_id: str | None = None) -> _FakeResult:
        self.calls.append(goal)
        if self.delay:
            await asyncio.sleep(self.delay)
        return _FakeResult(task_id or "t", goal, succeed=self.succeed)

    async def aclose(self) -> None:
        self.closed = True


class _ExplodingOrchestrator:
    async def run(self, goal: str, *, task_id: str | None = None) -> Any:
        raise RuntimeError("模拟编排崩溃")

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------- #
# EventBus / Store（单元）
# ---------------------------------------------------------------------- #


class TestEventBus:
    def test_publish_and_subscribe_replays_history(self) -> None:
        """晚订阅者必须收到历史事件，否则前端会看到一段空白。"""
        bus = EventBus()
        bus.publish(TaskEvent(kind="a", task_id="t"))
        bus.publish(TaskEvent(kind="b", task_id="t"))
        queue = bus.subscribe("t")
        assert queue.qsize() == 2

    def test_subscriber_isolation(self) -> None:
        bus = EventBus()
        q1 = bus.subscribe("t")
        q2 = bus.subscribe("t")
        bus.publish(TaskEvent(kind="x", task_id="t"))
        assert q1.qsize() == 1
        assert q2.qsize() == 1

    def test_unsubscribe_stops_delivery(self) -> None:
        bus = EventBus()
        q = bus.subscribe("t")
        bus.unsubscribe("t", q)
        bus.publish(TaskEvent(kind="x", task_id="t"))
        assert q.qsize() == 0

    def test_slow_consumer_drops_oldest_not_block(self) -> None:
        """队列满时丢最旧事件，绝不阻塞发布者（慢客户端不能拖慢任务）。"""
        bus = EventBus(queue_size=2)
        q = bus.subscribe("t")
        for i in range(5):
            bus.publish(TaskEvent(kind=f"e{i}", task_id="t"))
        assert q.qsize() == 2
        assert bus.dropped_count("t") > 0

    def test_history_bounded(self) -> None:
        bus = EventBus(history_size=3)
        for i in range(10):
            bus.publish(TaskEvent(kind=f"e{i}", task_id="t"))
        assert len(bus.history("t")) == 3

    def test_close_flag(self) -> None:
        bus = EventBus()
        assert bus.is_closed("t") is False
        bus.close("t")
        assert bus.is_closed("t") is True

    def test_clear(self) -> None:
        bus = EventBus()
        bus.publish(TaskEvent(kind="a", task_id="t"))
        bus.close("t")
        bus.clear("t")
        assert bus.history("t") == []
        assert bus.is_closed("t") is False


class TestInMemoryTaskStore:
    def test_save_get_delete(self) -> None:
        store = InMemoryTaskStore()
        store.save("t1", {"task_id": "t1", "status": "running"})
        assert store.get("t1") is not None
        assert len(store) == 1
        assert store.delete("t1") is True
        assert store.get("t1") is None
        assert store.delete("t1") is False

    def test_list_is_newest_first(self) -> None:
        store = InMemoryTaskStore()
        for i in range(3):
            store.save(f"t{i}", {"task_id": f"t{i}", "status": "ok"})
        assert [t["task_id"] for t in store.list()] == ["t2", "t1", "t0"]

    def test_list_status_filter_and_limit(self) -> None:
        store = InMemoryTaskStore()
        store.save("a", {"task_id": "a", "status": "running"})
        store.save("b", {"task_id": "b", "status": "done"})
        assert [t["task_id"] for t in store.list(status="done")] == ["b"]
        assert len(store.list(limit=1)) == 1

    def test_capacity_evicts_oldest(self) -> None:
        store = InMemoryTaskStore(capacity=2)
        for i in range(4):
            store.save(f"t{i}", {"task_id": f"t{i}"})
        assert store.get("t0") is None
        assert store.get("t3") is not None


# ---------------------------------------------------------------------- #
# TaskService（单元）
# ---------------------------------------------------------------------- #


class TestTaskService:
    async def test_submit_and_wait(self) -> None:
        service = TaskService(_FakeOrchestrator(), store=InMemoryTaskStore())
        tid = service.submit("加个分页")
        data = await service.wait(tid)
        assert data is not None
        assert data["status"] == "succeeded"
        assert data["succeeded"] is True
        assert data["total_tokens"] == 150

    async def test_duplicate_task_id_rejected(self) -> None:
        service = TaskService(_FakeOrchestrator(), store=InMemoryTaskStore())
        service.submit("g", task_id="dup")
        with pytest.raises(ValueError, match="已存在"):
            service.submit("g2", task_id="dup")

    async def test_emits_start_and_finish_events(self) -> None:
        service = TaskService(_FakeOrchestrator(), store=InMemoryTaskStore())
        tid = service.submit("g")
        await service.wait(tid)
        kinds = [e.kind for e in service.bus.history(tid)]
        assert kinds[0] == "task_started"
        assert kinds[-1] == "task_finished"

    async def test_failure_recorded_not_lost(self) -> None:
        """编排器抛异常时任务状态必须落库为 failed，且带错误信息。"""
        service = TaskService(_ExplodingOrchestrator(), store=InMemoryTaskStore())
        tid = service.submit("g")
        data = await service.wait(tid)
        assert data is not None
        assert data["status"] == "failed"
        assert "模拟编排崩溃" in data["error"]

    async def test_cancel(self) -> None:
        service = TaskService(_FakeOrchestrator(delay=5.0), store=InMemoryTaskStore())
        tid = service.submit("g")
        await asyncio.sleep(0.05)
        assert service.cancel(tid) is True
        data = await service.wait(tid, timeout=2)
        assert data is not None
        assert data["status"] == "cancelled"

    async def test_cancel_unknown_returns_false(self) -> None:
        service = TaskService(_FakeOrchestrator(), store=InMemoryTaskStore())
        assert service.cancel("nope") is False

    async def test_concurrency_limit(self) -> None:
        """信号量必须限制同时执行的任务数。"""
        service = TaskService(
            _FakeOrchestrator(delay=0.1), store=InMemoryTaskStore(), max_concurrent=2
        )
        tids = [service.submit(f"g{i}") for i in range(6)]
        await asyncio.gather(*(service.wait(t) for t in tids))
        assert all(service.get(t)["status"] == "succeeded" for t in tids)

    async def test_shutdown_cancels_running(self) -> None:
        service = TaskService(_FakeOrchestrator(delay=5.0), store=InMemoryTaskStore())
        service.submit("g")
        await asyncio.sleep(0.05)
        await service.shutdown()
        assert service._running == {}

    async def test_metadata_preserved(self) -> None:
        service = TaskService(_FakeOrchestrator(), store=InMemoryTaskStore())
        tid = service.submit("g", metadata={"user": "alice"})
        data = await service.wait(tid)
        assert data["metadata"] == {"user": "alice"}


class TestToView:
    def test_converts_dag_and_steps(self) -> None:
        view = _to_view(_FakeResult("t1", "goal"))
        assert view["task_id"] == "t1"
        assert len(view["steps"]) == 1
        assert view["steps"][0]["agent"] == "coder"
        assert {n["id"] for n in view["nodes"]} == {"N1", "N2"}
        assert view["nodes"][1]["deps"] == ["N1"]

    def test_handles_missing_dag(self) -> None:
        """DAG 为 None（需求阶段就失败）时不能崩。"""
        view = _to_view(_FakeResult("t1", "goal", succeed=False))
        assert view["nodes"] == []
        assert view["succeeded"] is False


# ---------------------------------------------------------------------- #
# HTTP 集成
# ---------------------------------------------------------------------- #


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """构造注入了假编排器的 TestClient。"""
    app = create_app(Settings())
    orchestrator = _FakeOrchestrator()
    service = TaskService(orchestrator, store=InMemoryTaskStore())

    with TestClient(app) as c:
        # 覆盖 lifespan 中构造的真实组件，避免测试触网
        app.state.orchestrator = orchestrator
        app.state.task_service = service
        yield c


class TestHealthEndpoint:
    def test_health_ok(self, client: Any) -> None:
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "providers" in body

    def test_root(self, client: Any) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["name"] == "DevAgent"

    def test_openapi_generated(self, client: Any) -> None:
        r = client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json()["paths"]
        assert "/api/v1/tasks" in paths
        assert "/api/v1/tasks/{task_id}/events" in paths


class TestTaskEndpoints:
    def test_create_and_get(self, client: Any) -> None:
        r = client.post("/api/v1/tasks", json={"goal": "加个分页"})
        assert r.status_code == 202
        task_id = r.json()["task_id"]

        r = client.get(f"/api/v1/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json()["goal"] == "加个分页"

    def test_create_rejects_empty_goal(self, client: Any) -> None:
        r = client.post("/api/v1/tasks", json={"goal": ""})
        assert r.status_code == 422

    def test_duplicate_id_conflict(self, client: Any) -> None:
        client.post("/api/v1/tasks", json={"goal": "g", "task_id": "fixed"})
        r = client.post("/api/v1/tasks", json={"goal": "g2", "task_id": "fixed"})
        assert r.status_code == 409

    def test_get_unknown_404(self, client: Any) -> None:
        r = client.get("/api/v1/tasks/nope")
        assert r.status_code == 404
        assert "nope" in r.json()["detail"]

    def test_list_tasks(self, client: Any) -> None:
        client.post("/api/v1/tasks", json={"goal": "a"})
        client.post("/api/v1/tasks", json={"goal": "b"})
        r = client.get("/api/v1/tasks")
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_delete_task(self, client: Any) -> None:
        tid = client.post("/api/v1/tasks", json={"goal": "g"}).json()["task_id"]
        assert client.delete(f"/api/v1/tasks/{tid}").status_code == 200
        assert client.get(f"/api/v1/tasks/{tid}").status_code == 404

    def test_delete_unknown_404(self, client: Any) -> None:
        assert client.delete("/api/v1/tasks/nope").status_code == 404

    def test_cancel_unknown_404(self, client: Any) -> None:
        assert client.post("/api/v1/tasks/nope/cancel").status_code == 404


class TestSseStream:
    def test_events_include_start_and_finish(self, client: Any) -> None:
        tid = client.post("/api/v1/tasks", json={"goal": "g"}).json()["task_id"]
        with client.stream("GET", f"/api/v1/tasks/{tid}/events") as resp:
            assert resp.status_code == 200
            kinds: list[str] = []
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    kinds.append(line.split(":", 1)[1].strip())
                if "task_finished" in line:
                    break
        assert "task_started" in kinds
        assert "task_finished" in kinds

    def test_stream_unknown_task_404(self, client: Any) -> None:
        r = client.get("/api/v1/tasks/nope/events")
        assert r.status_code == 404

    def test_late_subscriber_gets_history(self, client: Any) -> None:
        """任务已完成后再订阅，仍应收到历史事件（否则前端空白）。"""
        tid = client.post("/api/v1/tasks", json={"goal": "g"}).json()["task_id"]
        # 等任务跑完
        for _ in range(50):
            if client.get(f"/api/v1/tasks/{tid}").json()["status"] != "pending":
                break

        with client.stream("GET", f"/api/v1/tasks/{tid}/events") as resp:
            body = "".join(resp.iter_lines())
        assert "task_started" in body


class TestObservabilityEndpoints:
    def test_metrics_shape(self, client: Any) -> None:
        r = client.get("/api/v1/metrics")
        assert r.status_code == 200
        assert set(r.json()) == {"counters", "gauges", "histograms"}

    def test_metrics_serializes_nested_histograms(self) -> None:
        """回归：histogram 是三层嵌套结构，曾被误声明为两层导致 500。

        这里直接产生真实 histogram 数据再取快照，确保序列化不报错。
        """
        from devagent.observability import MetricNames, get_observability

        app = create_app(Settings())
        with TestClient(app) as c:
            metrics = get_observability().metrics
            metrics.observe(MetricNames.TASK_DURATION_MS, 12.5)
            metrics.observe(MetricNames.CONTEXT_COMPRESSION_RATIO, 0.6, agent="coder")
            metrics.inc_counter(MetricNames.LLM_TOKENS, 100, model="m", direction="input")
            metrics.set_gauge(MetricNames.CONTEXT_TOKENS_SAVED, 42, agent="coder")

            r = c.get("/api/v1/metrics")
            assert r.status_code == 200
            body = r.json()
            assert "task_duration_ms" in body["histograms"]
            stats = body["histograms"]["task_duration_ms"][""]
            assert stats["count"] == 1
            assert "p50" in stats
            # counters 是两层，histograms 是三层 —— 这个差异必须被 schema 正确表达
            assert isinstance(body["counters"]["llm_tokens"], dict)

    def test_prometheus_text(self, client: Any) -> None:
        r = client.get("/api/v1/metrics/prometheus")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]

    def test_traces_returns_list(self, client: Any) -> None:
        r = client.get("/api/v1/traces")
        assert r.status_code == 200
        assert isinstance(r.json()["spans"], list)

    def test_context_metrics_for_task(self, client: Any) -> None:
        tid = client.post("/api/v1/tasks", json={"goal": "g"}).json()["task_id"]
        r = client.get(f"/api/v1/tasks/{tid}/context")
        assert r.status_code == 200
        assert r.json()["task_id"] == tid

    def test_context_unknown_task_404(self, client: Any) -> None:
        assert client.get("/api/v1/tasks/nope/context").status_code == 404


class TestSseEventFormat:
    def test_event_payload_is_valid_json(self) -> None:
        event = TaskEvent(kind="node_finished", task_id="t", payload={"node": "N1"})
        data = event.to_sse_data()
        json.dumps(data)  # 不应抛异常
        assert data["kind"] == "node_finished"
        assert data["node"] == "N1"
        assert "task_id" in data
