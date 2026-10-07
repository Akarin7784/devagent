"""基于 SQL 的 ``TaskStore`` 实现。

## 为什么要有这一层（而不是让 API 层直接写 SQL）

``TaskStore`` 是 Protocol（见 ``devagent.api.store``），API 层只依赖它。
因此这里可以自由替换底层，而上层零改动 —— 这正是当初先定协议的价值。

## 与内存版的行为差异（必须知道的）

| 维度 | InMemoryTaskStore | SqlTaskStore |
| --- | --- | --- |
| 生命周期 | 进程内，重启即失 | 持久，跨重启/跨实例 |
| 容量 | LRU 上限 200 | 无上限（由 DB 管理） |
| 事件历史 | 内存环形缓冲 500 条 | 落库，可回放任意历史 |
| 依赖 | 无 | sqlalchemy（可选 extra） |

**事件持久化的必要性**：任务可能跑几分钟，用户晚 30 分钟才打开页面。
内存缓冲里最早的 500 条或许还在，但进程重启后就彻底没了。
落库让 SSE 的「历史回放」能力变成真正可靠的，而不是「只要进程没重启就还行」。

## 一个刻意的取舍：不自动清理

任务数据只增不删。生产环境应有保留策略（如 30 天），但这属于**运维策略**
而非应用逻辑 —— 硬编码一个 TTL 会让需要长期留存审计记录的部署很难受。
因此这里只提供 ``delete``，把策略留给外部（cron / 定期任务）。
"""

from __future__ import annotations

import builtins
import time
from typing import Any

from sqlalchemy import delete, func, select

from devagent.api.store import TaskEvent
from devagent.db.models import TaskEventRow, TaskRow
from devagent.db.session import Database
from devagent.logging_config import get_logger

logger = get_logger(__name__)

# 本类有 ``list`` 方法（TaskStore 协议要求），因此在类体内部 ``list[...]``
# 会被解析成那个方法而非内置类型，mypy 报 "Function ... is not valid as a type"。
# 显式取别名叫 ``List`` 绕开这一层遮蔽，比在每个注解里写 ``builtins.list`` 干净。
List = builtins.list


class SqlTaskStore:
    """持久化任务存储。

    实现 ``TaskStore`` 协议，与 ``InMemoryTaskStore`` 可直接互换。
    """

    def __init__(self, database: Database, *, event_history_limit: int = 2000) -> None:
        self._db = database
        self._event_limit = event_history_limit

    # ------------------------------------------------------------------ #
    # TaskStore 协议
    # ------------------------------------------------------------------ #

    async def save(self, task_id: str, data: dict[str, Any]) -> None:
        """写入或更新任务（upsert）。

        用「先查后改」而不是数据库方言的 ``ON CONFLICT``：后者在
        Postgres / SQLite 上语法与支持度有差异，而这里写入频率很低
        （一个任务通常只落库个位数次），多一次查询完全可接受，
        换来的是跨方言一致性。
        """
        now = time.time()
        async with self._db.session() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                row = TaskRow(
                    id=task_id,
                    goal=str(data.get("goal", "")),
                    created_at=now,
                )
                session.add(row)

            row.goal = str(data.get("goal", row.goal))
            row.status = str(data.get("status", row.status or "pending"))
            row.succeeded = bool(data.get("succeeded", row.succeeded))
            row.error = str(data.get("error", "") or "")
            row.duration_ms = int(data.get("duration_ms", 0) or 0)
            row.total_tokens = int(data.get("total_tokens", 0) or 0)
            row.total_cost_usd = float(data.get("total_cost_usd", 0.0) or 0.0)
            row.steps = list(data.get("steps") or [])
            row.nodes = list(data.get("nodes") or [])
            row.context_metrics = dict(data.get("context_metrics") or {})
            row.extra = dict(data.get("metadata") or {})
            row.updated_at = now

            await session.commit()

    async def get(self, task_id: str) -> dict[str, Any] | None:
        async with self._db.session() as session:
            row = await session.get(TaskRow, task_id)
            return _row_to_dict(row) if row is not None else None

    async def list(self, *, limit: int = 50, status: str = "") -> list[dict[str, Any]]:
        stmt = select(TaskRow).order_by(TaskRow.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(TaskRow.status == status)
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dict(r) for r in rows]

    async def delete(self, task_id: str) -> bool:
        async with self._db.session() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        return True

    async def count(self) -> int:
        async with self._db.session() as session:
            return int((await session.execute(select(func.count(TaskRow.id)))).scalar() or 0)

    # ------------------------------------------------------------------ #
    # 事件持久化（内存版没有的能力）
    # ------------------------------------------------------------------ #

    async def append_event(self, event: TaskEvent) -> None:
        """追加一条事件。序列号在任务内自增，保证顺序与去重。"""
        async with self._db.session() as session:
            current_max = (
                await session.execute(
                    select(func.max(TaskEventRow.seq)).where(TaskEventRow.task_id == event.task_id)
                )
            ).scalar()
            seq = int(current_max or 0) + 1
            session.add(
                TaskEventRow(
                    task_id=event.task_id,
                    seq=seq,
                    kind=event.kind,
                    payload=dict(event.payload),
                    timestamp=event.timestamp,
                )
            )
            await session.commit()

    async def replay_events(self, task_id: str, *, limit: int | None = None) -> List[TaskEvent]:
        """按顺序回放某任务的历史事件。"""
        stmt = (
            select(TaskEventRow)
            .where(TaskEventRow.task_id == task_id)
            .order_by(TaskEventRow.seq)
            .limit(limit or self._event_limit)
        )
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            TaskEvent(
                kind=r.kind, task_id=r.task_id, payload=dict(r.payload or {}), timestamp=r.timestamp
            )
            for r in rows
        ]

    async def purge_events(self, task_id: str) -> int:
        """清除某任务的事件历史，返回删除条数。

        先从对象层取 ``id`` 再按主键删，而不是直接 ``DELETE ... WHERE``：
        前者能拿到准确的受影响行数（``CursorResult.rowcount``），
        且 SQLite 与 Postgres 行为一致。事件表按 task_id 有索引，
        多这一趟 ``SELECT`` 的成本可以忽略。
        """
        async with self._db.session() as session:
            ids = (
                (
                    await session.execute(
                        select(TaskEventRow.id).where(TaskEventRow.task_id == task_id)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                return 0
            await session.execute(delete(TaskEventRow).where(TaskEventRow.id.in_(ids)))
            await session.commit()
            return len(ids)


def _row_to_dict(row: TaskRow) -> dict[str, Any]:
    """ORM 行 → API 层使用的普通 dict。

    这是持久化模型与 API 契约之间的**唯一**转换点。
    """
    return {
        "task_id": row.id,
        "goal": row.goal,
        "status": row.status,
        "succeeded": row.succeeded,
        "error": row.error or "",
        "duration_ms": row.duration_ms,
        "total_tokens": row.total_tokens,
        "total_cost_usd": row.total_cost_usd,
        "steps": list(row.steps or []),
        "nodes": list(row.nodes or []),
        "context_metrics": dict(row.context_metrics or {}),
        "metadata": dict(row.extra or {}),
    }


__all__ = ["SqlTaskStore"]
