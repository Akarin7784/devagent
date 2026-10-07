"""数据库引擎与会话管理。

## 设计要点

**1. 惰性导入 + 清晰报错**

``sqlalchemy`` 不是硬依赖（见 ``pyproject.toml`` 的 ``[db]`` extra）。
若未安装就调用，应抛出一条**能直接告诉你怎么办**的错误，
而不是 ``ModuleNotFoundError: No module named 'sqlalchemy'``。

**2. 内存 SQLite 与文件 SQLite 都支持**

测试用 ``sqlite+aiosqlite:///:memory:``（快、隔离）；轻量部署用文件路径；
生产用 Postgres。三者共用同一份 ORM 代码，差异只在 URL。

**3. ``create_all`` 与 Alembic 并存**

- 开发/测试：``init_models()`` 直接 ``create_all``，零迁移文件即可跑通；
- 生产：用 Alembic 迁移（``alembic upgrade head``），因为它能表达
  ``ALTER TABLE``，而 ``create_all`` 只能建新表、无法演进已有 schema。

两条路径都有，但**明确标注各自适用场景**，避免有人在生产上误用 create_all。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from devagent.db.models import Base
from devagent.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

logger = get_logger(__name__)


class DatabaseUnavailableError(RuntimeError):
    """数据库依赖未安装或连接失败。

    单独定义异常类型，让调用方可以区分「数据库不可用」与「SQL 写错了」。
    """


_MISSING_HINT = (
    "未安装数据库依赖。请执行：\n"
    "    pip install -e '.[db]'\n"
    "（或单独安装：pip install 'sqlalchemy>=2.0' 'aiosqlite>=0.20'）\n"
    "若只想跑内存模式，设置 DEVAGENT_STORAGE__BACKEND=memory 即可。"
)


def _require_sqlalchemy() -> tuple[Any, Any, Any]:
    """惰性导入 SQLAlchemy，缺失时给出可操作的提示。"""
    try:
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )
    except ImportError as exc:  # pragma: no cover - 依赖存在时不走这里
        raise DatabaseUnavailableError(_MISSING_HINT) from exc
    return create_async_engine, async_sessionmaker, AsyncSession


def normalize_url(url: str) -> str:
    """把常见写法规整为异步驱动 URL。

    贡献者很容易写成 ``postgresql://...``（同步驱动），然后遇到
    「asyncpg 未被使用」这类令人困惑的错误。这里主动纠正并提示。
    """
    if url.startswith("postgresql://"):
        logger.warning("db_url_normalized", original="postgresql://", fixed="postgresql+asyncpg://")
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


class Database:
    """异步数据库封装：引擎 + 会话工厂。

    用法::

        db = Database("postgresql+asyncpg://...")
        await db.init_models()          # 仅开发/测试
        async with db.session() as sess:
            sess.add(row)
            await sess.commit()
        await db.dispose()
    """

    def __init__(self, url: str, *, echo: bool = False, **engine_kwargs: Any) -> None:
        # 三个返回值都要保留：AsyncSession 必须用于 class_ 绑定。
        # 早期版本这里写成 `create_async_engine, sessionmaker, _ = ...`，
        # 随后引用模块级 AsyncSession —— 但那是 TYPE_CHECKING 下的名字，
        # 运行期不存在，导致 NameError。惰性导入的值必须全程带走。
        create_async_engine, sessionmaker, async_session_cls = _require_sqlalchemy()
        self.url = normalize_url(url)

        # SQLite 不支持连接池参数（pool_size/max_overflow），传了会报错。
        # 这也是为什么内存模式能零配置跑起来：不传池参数即可。
        if not is_postgres(self.url):
            engine_kwargs.pop("pool_size", None)
            engine_kwargs.pop("max_overflow", None)
        else:
            engine_kwargs.setdefault("pool_pre_ping", True)
            """pool_pre_ping：从池里取连接时先探活，避免拿到被中间件
            静默断开的死连接（云数据库常见）。代价是一次额外的 round-trip。"""

        self._engine: AsyncEngine = create_async_engine(self.url, echo=echo, **engine_kwargs)
        self._sessionmaker: async_sessionmaker[AsyncSession] = sessionmaker(
            self._engine, expire_on_commit=False, class_=async_session_cls
        )
        """expire_on_commit=False：提交后仍可读对象属性，避免在响应序列化时
        触发一次隐式的 ``SELECT``（异步下这类懒加载会直接抛错）。"""

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """提供一个自动回滚的会话上下文。"""
        async with self._sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def init_models(self) -> None:
        """建表（``CREATE TABLE IF NOT EXISTS``）。

        仅适用于开发与测试。生产请用 Alembic —— 它只能建表，
        **无法演进已有表结构**（改字段类型、加索引都得靠迁移）。
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("db_models_initialized", url=_redact(self.url))

    async def drop_models(self) -> None:
        """删表。仅供测试清理使用。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def healthcheck(self) -> bool:
        """连通性探测，供 /health 使用。"""
        try:
            from sqlalchemy import text

            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("db_healthcheck_failed", error=str(exc))
            return False


def _redact(url: str) -> str:
    """隐藏 URL 中的密码（日志安全）。"""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if ":" not in rest.split("@", 1)[0]:
        return url
    creds, host = rest.split("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


__all__ = [
    "Database",
    "DatabaseUnavailableError",
    "is_postgres",
    "normalize_url",
]
