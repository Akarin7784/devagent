"""持久化层测试（SQLAlchemy + aiosqlite）。

## 为什么这个文件值得单独存在

前面 300+ 个测试全部跑在**内存实现**上，持久化层只在「手工起一次服务」时
被碰过 —— 而项目里最容易出错的恰恰是这类「写了但从未被自动化执行」的代码
（前端那 4 个契约 bug 就是这么来的）。

这里用 ``sqlite+aiosqlite:///:memory:``，兼顾三点：

1. **无外部依赖**：CI 上不需要起 Postgres，测试保持秒级；
2. **真 SQL**：走真实的方言、约束、外键与 JSON 序列化，
   而 ``InMemoryTaskStore`` 是纯 dict，测不出这些问题；
3. **真异步**：验证 ORM 在 ``asyncio`` 下确实能跑通（greenlet 缺失那类
   问题只有在真实 import/调用路径上才暴露）。

同时验证「协议兼容」：``SqlTaskStore`` 与 ``InMemoryTaskStore`` 必须能被
同一段上层代码无差别使用 —— 这是当初先定 ``TaskStore`` 协议的全部意义。
"""

from __future__ import annotations

import time

import pytest

# 持久化层依赖 ``[db]`` extra（``sqlalchemy[asyncio]`` → greenlet、aiosqlite）。
# 缺依赖时**明确跳过**而不是让 15 个用例在 setup 阶段报错：
# 报错会淹没真正的失败信号，而"跳过"至少是诚实的。
# 注意 CI 必须安装该 extra（见 .github/workflows/ci.yml），否则这些用例
# 会在 CI 上静默不跑 —— 那比报错更危险。
pytest.importorskip("aiosqlite", reason="需要 pip install -e '.[db]'")
pytest.importorskip("greenlet", reason="需要 pip install -e '.[db]'")

from devagent.api.store import InMemoryTaskStore, TaskEvent
from devagent.db.session import Database, is_postgres, normalize_url
from devagent.db.task_store import SqlTaskStore

pytestmark = pytest.mark.unit

MEMORY_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------- #
# 夹具
# ---------------------------------------------------------------------- #


@pytest.fixture
async def database() -> Database:
    """每个测试一个全新的内存库（建表 → 用 → 释放）。"""
    db = Database(MEMORY_URL)
    await db.init_models()
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def store(database: Database) -> SqlTaskStore:
    return SqlTaskStore(database)


def _task_payload(task_id: str, **overrides: object) -> dict[str, object]:
    """一份最小的合法任务数据。

    单独抽出来是因为 ``TaskRow`` 的多数列都非空，测试里逐个补齐会
    把断言淹没在样板里。
    """
    payload: dict[str, object] = {
        "task_id": task_id,
        "goal": f"目标 {task_id}",
        "status": "pending",
        "succeeded": False,
        "error": "",
        "duration_ms": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "steps": [],
        "nodes": [],
        "context_metrics": {},
        "metadata": {},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------- #
# URL 规整（纯函数，最容易在真实部署里坑人）
# ---------------------------------------------------------------------- #


class TestUrlNormalization:
    def test_plain_postgresql_upgraded_to_asyncpg(self) -> None:
        """``postgresql://`` 是同步驱动写法，必须自动纠正。

        否则报错是「asyncpg 未被使用」这类含混信息，贡献者很难自己定位。
        """
        assert normalize_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"

    def test_sqlite_upgraded_to_aiosqlite(self) -> None:
        assert normalize_url("sqlite:///tmp/x.db") == "sqlite+aiosqlite:///tmp/x.db"

    def test_already_async_url_untouched(self) -> None:
        """已正确的 URL 不能被二次改写（否则会变成 +asyncpg+asyncpg）。"""
        url = "postgresql+asyncpg://u:p@h/db"
        assert normalize_url(url) == url

    def test_memory_sqlite_untouched(self) -> None:
        assert normalize_url(MEMORY_URL) == MEMORY_URL

    def test_is_postgres_detects_both_forms(self) -> None:
        assert is_postgres("postgresql://h/db") is True
        assert is_postgres("postgresql+asyncpg://h/db") is True
        assert is_postgres(MEMORY_URL) is False


# ---------------------------------------------------------------------- #
# 会话与建表
# ---------------------------------------------------------------------- #


class TestDatabaseLifecycle:
    async def test_init_models_is_idempotent(self, database: Database) -> None:
        """重复建表必须安全 —— 服务每次启动都会调它。"""
        await database.init_models()
        await database.init_models()

    async def test_healthcheck_true_on_live_db(self, database: Database) -> None:
        assert await database.healthcheck() is True

    async def test_healthcheck_false_on_unreachable_db(self) -> None:
        """健康检查的契约是「返回布尔」而不是「抛异常」。

        编排/监控会周期性调用它，抛异常会让整个 /health 端点 500。

        用「指向不存在目录的 SQLite 文件」来构造失败：
        - 引擎创建是惰性的，真正的失败发生在第一次连接，正好覆盖 healthcheck；
        - 不依赖 asyncpg 这类外部驱动是否安装（CI 上装的是最小依赖集）。
        注意不要用 ``dispose()``：SQLite 是本地文件型，dispose 之后下一次
        连接会直接重建，healthcheck 依然返回 True。
        """
        db = Database("sqlite+aiosqlite:///no/such/dir/devagent.db")
        try:
            assert await db.healthcheck() is False
        finally:
            await db.dispose()

    async def test_session_rolls_back_on_error(self, database: Database) -> None:
        """上下文里抛异常必须回滚，避免脏数据留在事务里。"""
        from devagent.db.models import TaskRow

        with pytest.raises(RuntimeError, match="boom"):
            async with database.session() as session:
                session.add(TaskRow(id="x", goal="g", created_at=time.time()))
                await session.flush()
                raise RuntimeError("boom")

        async with database.session() as session:
            assert await session.get(TaskRow, "x") is None


# ---------------------------------------------------------------------- #
# SqlTaskStore：任务 CRUD
# ---------------------------------------------------------------------- #


class TestSqlTaskStoreCrud:
    async def test_save_then_get_roundtrip(self, store: SqlTaskStore) -> None:
        await store.save("t1", _task_payload("t1", status="running"))
        got = await store.get("t1")
        assert got is not None
        assert got["task_id"] == "t1"
        assert got["goal"] == "目标 t1"
        assert got["status"] == "running"

    async def test_get_missing_returns_none(self, store: SqlTaskStore) -> None:
        assert await store.get("nope") is None

    async def test_save_is_upsert_not_insert(self, store: SqlTaskStore) -> None:
        """同一 id 二次 save 必须是更新，而不是抛主键冲突。"""
        await store.save("t1", _task_payload("t1"))
        await store.save("t1", _task_payload("t1", status="succeeded", succeeded=True))
        assert await store.count() == 1
        got = await store.get("t1")
        assert got is not None
        assert got["status"] == "succeeded"
        assert got["succeeded"] is True

    async def test_metadata_survives_roundtrip(self, store: SqlTaskStore) -> None:
        """metadata 存进 extra 列再读出来，键名不能串。

        ORM 列叫 ``extra`` 而 API 契约叫 ``metadata``，这个映射只发生在
        ``_row_to_dict`` / ``save`` 两处，一旦写错就是静默丢数据。
        """
        await store.save("t1", _task_payload("t1", metadata={"user": "alice", "n": 3}))
        got = await store.get("t1")
        assert got is not None
        assert got["metadata"] == {"user": "alice", "n": 3}
        assert "extra" not in got

    async def test_nested_json_columns_roundtrip(self, store: SqlTaskStore) -> None:
        """steps / nodes / context_metrics 是嵌套 JSON，必须原样往返。"""
        steps = [{"step_id": "s1", "agent": "coder", "tokens_used": 100}]
        nodes = [{"id": "N1", "deps": ["N0"], "nested": {"deep": [1, 2, 3]}}]
        metrics = {"tokens_saved": 1234, "utilization": [0.1, 0.9]}
        await store.save(
            "t1", _task_payload("t1", steps=steps, nodes=nodes, context_metrics=metrics)
        )
        got = await store.get("t1")
        assert got is not None
        assert got["steps"] == steps
        assert got["nodes"] == nodes
        assert got["context_metrics"] == metrics

    async def test_numeric_types_preserved(self, store: SqlTaskStore) -> None:
        """``total_cost_usd`` 是 float，不能被读成 int/str。"""
        await store.save(
            "t1", _task_payload("t1", total_cost_usd=0.0123, total_tokens=1500, duration_ms=42)
        )
        got = await store.get("t1")
        assert got is not None
        assert got["total_cost_usd"] == pytest.approx(0.0123)
        assert got["total_tokens"] == 1500
        assert got["duration_ms"] == 42

    async def test_delete(self, store: SqlTaskStore) -> None:
        await store.save("t1", _task_payload("t1"))
        assert await store.delete("t1") is True
        assert await store.get("t1") is None
        assert await store.delete("t1") is False

    async def test_count(self, store: SqlTaskStore) -> None:
        assert await store.count() == 0
        for i in range(3):
            await store.save(f"t{i}", _task_payload(f"t{i}"))
        assert await store.count() == 3


class TestSqlTaskStoreList:
    async def test_list_newest_first(self, store: SqlTaskStore) -> None:
        """列表页默认按创建时间倒序（新任务在最上面）。"""
        for i in range(3):
            await store.save(f"t{i}", _task_payload(f"t{i}"))
            # created_at 用 time.time()，同毫秒内会并列，稍微错开保证顺序稳定
            import asyncio

            await asyncio.sleep(0.01)
        ids = [t["task_id"] for t in await store.list()]
        assert ids == ["t2", "t1", "t0"]

    async def test_list_filters_by_status(self, store: SqlTaskStore) -> None:
        await store.save("a", _task_payload("a", status="succeeded"))
        await store.save("b", _task_payload("b", status="running"))
        assert [t["task_id"] for t in await store.list(status="succeeded")] == ["a"]

    async def test_list_respects_limit(self, store: SqlTaskStore) -> None:
        for i in range(5):
            await store.save(f"t{i}", _task_payload(f"t{i}"))
        assert len(await store.list(limit=2)) == 2

    async def test_list_empty(self, store: SqlTaskStore) -> None:
        assert await store.list() == []


# ---------------------------------------------------------------------- #
# 事件持久化（内存版没有的能力，也是 SSE 历史回放的可靠性来源）
# ---------------------------------------------------------------------- #


class TestSqlTaskStoreEvents:
    async def test_append_and_replay_in_order(self, store: SqlTaskStore) -> None:
        await store.save("t1", _task_payload("t1"))
        for i in range(5):
            await store.append_event(
                TaskEvent(kind=f"e{i}", task_id="t1", payload={"i": i}, timestamp=float(i))
            )
        events = await store.replay_events("t1")
        assert [e.kind for e in events] == [f"e{i}" for i in range(5)]
        assert [e.payload["i"] for e in events] == [0, 1, 2, 3, 4]

    async def test_seq_is_per_task(self, store: SqlTaskStore) -> None:
        """序列号在任务内自增，不同任务互不干扰。"""
        await store.save("t1", _task_payload("t1"))
        await store.save("t2", _task_payload("t2"))
        await store.append_event(TaskEvent(kind="a", task_id="t1"))
        await store.append_event(TaskEvent(kind="b", task_id="t2"))
        assert [e.kind for e in await store.replay_events("t1")] == ["a"]
        assert [e.kind for e in await store.replay_events("t2")] == ["b"]

    async def test_replay_isolated_per_task(self, store: SqlTaskStore) -> None:
        await store.save("t1", _task_payload("t1"))
        await store.save("t2", _task_payload("t2"))
        for i in range(3):
            await store.append_event(TaskEvent(kind="x", task_id="t1", payload={"i": i}))
        await store.append_event(TaskEvent(kind="y", task_id="t2", payload={"i": 99}))
        t1 = await store.replay_events("t1")
        assert len(t1) == 3
        assert all(e.kind == "x" for e in t1)

    async def test_replay_respects_limit(self, store: SqlTaskStore) -> None:
        await store.save("t1", _task_payload("t1"))
        for i in range(10):
            await store.append_event(TaskEvent(kind=f"e{i}", task_id="t1"))
        assert len(await store.replay_events("t1", limit=4)) == 4

    async def test_purge_events(self, store: SqlTaskStore) -> None:
        await store.save("t1", _task_payload("t1"))
        for i in range(4):
            await store.append_event(TaskEvent(kind=f"e{i}", task_id="t1"))
        assert await store.purge_events("t1") == 4
        assert await store.replay_events("t1") == []

    async def test_payload_and_timestamp_survive(self, store: SqlTaskStore) -> None:
        await store.save("t1", _task_payload("t1"))
        await store.append_event(
            TaskEvent(
                kind="node_verdict", task_id="t1", payload={"verdict": "reject"}, timestamp=1.5
            )
        )
        event = (await store.replay_events("t1"))[0]
        assert event.payload == {"verdict": "reject"}
        assert event.timestamp == pytest.approx(1.5)
        assert event.task_id == "t1"

    async def test_deleting_task_cascades_events(self, store: SqlTaskStore) -> None:
        """删任务必须连带清掉事件，否则事件表会无限膨胀。

        这里依赖 ORM 的 ``cascade="all, delete-orphan"`` 而不是数据库
        ``ON DELETE CASCADE`` —— SQLite 默认不开 ``PRAGMA foreign_keys``，
        只靠 DDL 级联在测试环境下会静默失效。
        """
        await store.save("t1", _task_payload("t1"))
        for i in range(3):
            await store.append_event(TaskEvent(kind=f"e{i}", task_id="t1"))
        await store.delete("t1")
        assert await store.replay_events("t1") == []


# ---------------------------------------------------------------------- #
# 协议兼容：两种实现可互换
# ---------------------------------------------------------------------- #


class TestStoreProtocolCompatibility:
    """同一段上层代码必须能无差别地跑在两种实现上。

    这是「先定 TaskStore 协议、再写实现」这个决策能否成立的**唯一验证点**。
    只要这里绿灯，API/Service 层就永远不需要知道底层是内存还是 SQL。

    注意：内存实现是**同步**的，SQL 实现是**异步**的（这是刻意设计，
    见 ``devagent.api.service._maybe_await``）。因此上层不能直接 ``await``，
    必须先过适配器 —— 这个测试正是在验证适配器对两种实现都成立。
    """

    @pytest.mark.parametrize("kind", ["memory", "sql"])
    async def test_same_sequence_of_operations_with_adapter(
        self, kind: str, database: Database
    ) -> None:
        from devagent.api.service import _maybe_await

        store: InMemoryTaskStore | SqlTaskStore = (
            InMemoryTaskStore() if kind == "memory" else SqlTaskStore(database)
        )

        await _maybe_await(store.save("t1", _task_payload("t1")))
        await _maybe_await(store.save("t2", _task_payload("t2", status="succeeded")))

        got = await _maybe_await(store.get("t1"))
        assert got is not None
        assert got["goal"] == "目标 t1"
        assert got["metadata"] == {}

        listed = await _maybe_await(store.list())
        assert {t["task_id"] for t in listed} == {"t1", "t2"}

        succeeded = await _maybe_await(store.list(status="succeeded"))
        assert [t["task_id"] for t in succeeded] == ["t2"]

        assert await _maybe_await(store.delete("t1")) is True
        assert await _maybe_await(store.get("t1")) is None

    async def test_memory_store_is_synchronous_by_design(self, database: Database) -> None:
        """内存实现返回的是**普通值**而非协程。

        这条断言存在的意义是「锁住设计意图」：一旦有人图省事把
        ``InMemoryTaskStore`` 也改成 ``async def``，纯逻辑单测会集体被迫
        变成协程，且平白多出每次调用的协程调度开销。改动时这条会红。
        """
        import inspect

        memory = InMemoryTaskStore()
        assert not inspect.iscoroutinefunction(memory.save)
        assert not inspect.iscoroutinefunction(memory.get)

    async def test_sql_store_methods_are_coroutines(self, store: SqlTaskStore) -> None:
        """SQL 实现必须是协程，否则 ``_maybe_await`` 会当成同步返回值用。"""
        import inspect

        for name in ("save", "get", "list", "delete"):
            assert inspect.iscoroutinefunction(getattr(store, name)), f"{name} 必须是协程"

    async def test_both_implementations_satisfy_protocol(self, store: SqlTaskStore) -> None:
        """两种实现都必须通过运行期协议检查。

        ``TaskStore`` 声明为 ``runtime_checkable``，因此这句话能真正
        拦住「实现漏了某个方法」这类错误 —— 否则只有 CI 里的 mypy 会发现，
        而 mypy 只检查被实际调用的路径。
        """
        from devagent.api.store import TaskStore

        assert isinstance(store, TaskStore)
        assert isinstance(InMemoryTaskStore(), TaskStore)
