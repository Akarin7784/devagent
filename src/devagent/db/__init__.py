"""db 子包：数据库持久化。

``sqlalchemy`` 是**可选依赖**（``pip install -e '.[db]'``）。
本包的所有导入都被设计为惰性的：仅 import 本包不会触发 SQLAlchemy 导入，
因此内存模式下未安装数据库依赖也能正常启动。

导出的符号在 sqlalchemy 缺失时访问会抛出带操作指引的
``DatabaseUnavailableError``（见 ``session._require_sqlalchemy``），
而不是裸的 ``ModuleNotFoundError``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from devagent.db.models import Base, TaskEventRow, TaskRow
    from devagent.db.session import Database, DatabaseUnavailableError
    from devagent.db.task_store import SqlTaskStore

__all__ = [
    "Base",
    "Database",
    "DatabaseUnavailableError",
    "SqlTaskStore",
    "TaskEventRow",
    "TaskRow",
]


def __getattr__(name: str) -> Any:
    """惰性导出：只有真正访问时才导入 SQLAlchemy 相关模块。

    这样做的好处：``import devagent.db`` 在未安装 sqlalchemy 的环境里
    也不会炸 —— 而这是内存模式的正常场景。
    """
    if name in {"Base", "TaskEventRow", "TaskRow"}:
        from devagent.db import models

        return getattr(models, name)
    if name in {"Database", "DatabaseUnavailableError"}:
        from devagent.db import session

        return getattr(session, name)
    if name == "SqlTaskStore":
        from devagent.db.task_store import SqlTaskStore

        return SqlTaskStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
