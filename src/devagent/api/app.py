"""FastAPI 应用工厂。

用 **工厂函数** 而不是模块级 ``app = FastAPI()``，原因有三：

1. 测试可以构造多个隔离实例，不会互相污染 ``app.state``；
2. 配置在调用时才读取，避免「import 即读环境变量」导致测试难注入；
3. 支持未来多租户（每个租户一个 app 实例）。

生命周期（``lifespan``）中统一初始化/释放重资源（gateway 连接池、任务服务），
保证 uvicorn 优雅退出时不会泄漏连接。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from devagent.api.routes import router
from devagent.api.service import TaskService
from devagent.api.store import EventBus
from devagent.config import Settings, get_settings
from devagent.logging_config import configure_logging, get_logger

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用。"""
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ---- 启动 ----
        configure_logging(resolved.log_level, json_output=resolved.observability.log_json)
        from devagent.observability import configure_observability

        configure_observability(
            enabled=resolved.observability.metrics_enabled,
            otlp_endpoint=resolved.observability.otlp_endpoint,
        )

        from devagent.db.factory import build_store, finalize_store
        from devagent.models.gateway import ModelGateway
        from devagent.orchestration import Orchestrator

        app.state.settings = resolved
        app.state.gateway = ModelGateway(resolved)

        # 存储后端：memory（默认，零依赖）或 sql（持久化）。
        # 装配与释放配对写在同一处，避免 sql 模式下漏掉 engine.dispose()。
        store, database = build_store(resolved)
        app.state.storage = store
        app.state.database = database
        if database is not None:
            # 开发便利：自动建表。生产请用 `alembic upgrade head`
            # （create_all 无法演进已有表结构）。
            await database.init_models()

        app.state.store = store
        app.state.bus = EventBus()
        app.state.orchestrator = Orchestrator(resolved, gateway=app.state.gateway)
        app.state.task_service = TaskService(
            app.state.orchestrator,
            store=store,
            bus=app.state.bus,
        )
        logger.info("api_started", storage=resolved.storage.backend)

        try:
            yield
        finally:
            # ---- 关闭 ----
            service: TaskService = app.state.task_service
            await service.shutdown()
            await app.state.orchestrator.aclose()
            await finalize_store(app.state.storage, app.state.database)
            logger.info("api_stopped")

    app = FastAPI(
        title="DevAgent API",
        description=(
            "多 Agent 协作的软件研发助手。\n\n"
            "核心能力：以**上下文工程**驱动的多 Agent 编排，"
            "含独立验证层、结构化握手协议与幻觉阻断机制。"
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS：前端 dev server 通常跑在 5173（Vite）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix="/api/v1")

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """兜底异常处理：保证任何未捕获异常都返回**统一结构**。

        生产环境不返回堆栈（避免信息泄露），但日志里必须完整记录，
        否则线上问题会变成「只看到一个 500」。
        """
        logger.exception("unhandled_exception", path=str(request.url.path))
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "detail": f"{type(exc).__name__}: {exc}" if _debug_enabled() else "服务器内部错误",
                "code": "INTERNAL",
            },
        )

    @app.get("/", tags=["system"])
    async def root() -> dict[str, Any]:
        return {
            "name": "DevAgent",
            "version": "0.1.0",
            "docs": "/docs",
            "api_prefix": "/api/v1",
        }

    _maybe_mount_frontend(app, resolved)

    return app


def _maybe_mount_frontend(app: FastAPI, settings: Settings) -> Path | None:
    """把前端静态目录挂载到 `/`，实现同源访问。

    为什么需要：前后端分端口运行时，前端必须靠 ``?api=`` 参数才知道后端在哪。
    这个参数一旦丢失（用户直接敲 ``localhost:5173``、或从收藏夹打开），
    前端就会把请求打到自己的静态服务器上，得到 404，界面显示
    「无法连接到服务端」—— 一个**纯配置问题伪装成服务故障**的典型陷阱。
    同源托管后基址恒为空，问题从根上消失。

    挂载点必须在 ``/api/v1`` 已注册**之后**：Starlette 按注册顺序匹配路由，
    先挂 ``/`` 会吞掉所有 API 请求。这里通过「先注册 API、最后挂载前端」的顺序
    保证 API 优先级。

    返回实际挂载的目录；未启用或目录不存在时返回 ``None``。
    """
    from fastapi.staticfiles import StaticFiles

    raw = (settings.web_dir or "").strip()
    if not raw:
        return None

    # 相对路径按「项目根」解析 —— 而项目根是 config.py 往上三层
    # （src/devagent/config.py -> src/devagent -> src -> 项目根），
    # 这样无论从哪个工作目录启动服务都能定位到 web/。
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(__file__).resolve().parents[3] / raw

    if not candidate.is_dir():
        logger.warning("web_dir_not_found", web_dir=str(candidate))
        return None

    index = candidate / "index.html"
    if not index.is_file():
        logger.warning("web_dir_without_index", web_dir=str(candidate))
        return None

    # `GET /` 已在上文注册，Starlette 按注册顺序匹配 —— 若不处理，根路径仍会
    # 命中那个 JSON 端点，而不是 index.html。这里把该路由从表里摘掉，
    # 让后面的 mount 接管。摘除而非「后注册覆盖」是因为路由匹配是**首个命中即返回**，
    # 后注册的同路径路由永远不会被走到。
    app.routes[:] = [
        r
        for r in app.routes
        if not (getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", None) or set()))
    ]

    # html=True 让 `/` 回落到 index.html；SPA 用 hash 路由（#/workbench），
    # 不经过服务端，因此无需额外的 catch-all 重写规则。
    app.mount("/", StaticFiles(directory=str(candidate), html=True), name="web")

    logger.info("frontend_mounted", web_dir=str(candidate))
    return candidate


def _debug_enabled() -> bool:
    import os

    return os.environ.get("DEVAGENT_DEBUG", "").lower() in {"1", "true", "yes"}


__all__ = ["create_app"]
