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
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from devagent.api.routes import router
from devagent.api.service import TaskService
from devagent.api.store import EventBus, InMemoryTaskStore
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

        from devagent.models.gateway import ModelGateway
        from devagent.orchestration import Orchestrator

        app.state.settings = resolved
        app.state.gateway = ModelGateway(resolved)
        app.state.store = InMemoryTaskStore()
        app.state.bus = EventBus()
        app.state.orchestrator = Orchestrator(resolved, gateway=app.state.gateway)
        app.state.task_service = TaskService(
            app.state.orchestrator,
            store=app.state.store,
            bus=app.state.bus,
        )
        logger.info("api_started")

        try:
            yield
        finally:
            # ---- 关闭 ----
            service: TaskService = app.state.task_service
            await service.shutdown()
            await app.state.orchestrator.aclose()
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

    return app


def _debug_enabled() -> bool:
    import os

    return os.environ.get("DEVAGENT_DEBUG", "").lower() in {"1", "true", "yes"}


__all__ = ["create_app"]
