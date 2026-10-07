"""存储后端装配。

把「配置 → 具体实现」的决策集中在一处，让 ``app.py`` 保持干净，
也让测试可以只替换工厂而不是改应用代码。

## 为什么用工厂函数而不是 if/else 散在 app.py

存储后端的选择会影响**资源生命周期**（SQL 需要 ``dispose``，
内存不需要）。把装配与释放配对写在同一个模块里，才能保证不漏。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from devagent.api.store import InMemoryTaskStore, TaskStore
from devagent.config import Settings
from devagent.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from devagent.db.session import Database

logger = get_logger(__name__)


def build_store(settings: Settings) -> tuple[TaskStore, Database | None]:
    """按配置创建存储后端。

    Returns:
        ``(store, database)``。``database`` 在内存模式下为 ``None``；
        调用方须在关闭时对其调用 ``await database.dispose()``。

    Raises:
        StorageError: 配置为 sql 但依赖未安装。
    """
    backend = settings.storage.backend

    if backend == "memory":
        logger.info("storage_backend", backend="memory", capacity=settings.storage.memory_capacity)
        return InMemoryTaskStore(capacity=settings.storage.memory_capacity), None

    # ---- sql ----
    from devagent.db.session import Database, DatabaseUnavailableError

    try:
        db = Database(
            settings.database.url,
            echo=settings.database.echo,
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
        )
    except DatabaseUnavailableError as exc:
        raise StorageError(
            f"存储后端配置为 sql，但数据库依赖不可用。\n\n{exc}\n\n"
            "或者改用内存模式：DEVAGENT_STORAGE__BACKEND=memory"
        ) from exc

    from devagent.db.task_store import SqlTaskStore

    store = SqlTaskStore(db, event_history_limit=settings.storage.event_history_limit)
    logger.info("storage_backend", backend="sql", url=_safe_url(settings.database.url))
    return store, db


class StorageError(RuntimeError):
    """存储后端装配失败。"""


def _safe_url(url: str) -> str:
    """日志中隐藏密码。"""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    creds, host = rest.split("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}" if scheme else f"{user}:***@{host}"


async def finalize_store(store: Any, database: Database | None) -> None:  # noqa: ARG001
    """关闭存储相关资源。

    ``store`` 参数当前**不被使用**：内存实现无需释放，而 ``SqlTaskStore``
    的会话是「用完即关」（每次操作开一个 ``async with``），没有常驻连接
    需要回收 —— 真正要 ``dispose`` 的是它底下的 engine。

    之所以仍然接收它（而不是删掉参数），是为了让释放与装配的签名保持对称：
    ``build_store`` 返回 ``(store, database)``，``finalize_store`` 就按同样的
    顺序接收。调用点写成 ``await finalize_store(*handles)`` 即可，
    未来若某个实现确实需要 ``close()``，不必去改所有调用点。
    """
    if database is not None:
        await database.dispose()


__all__ = ["StorageError", "build_store", "finalize_store"]
