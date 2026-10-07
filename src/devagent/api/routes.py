"""FastAPI 路由。

分为四组：

- ``/health``  健康检查（无需认证）
- ``/tasks``   任务 CRUD + SSE 事件流
- ``/context`` 上下文装配决策查询（前端「上下文查看器」数据源）
- ``/metrics`` 与 ``/traces`` 可观测性查询

设计原则：路由函数只做「参数校验 → 调 service → 转视图」，不写业务逻辑。
保持路由薄，是让 API 层可被替换（换成 gRPC/GraphQL）的前提。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from sse_starlette.sse import EventSourceResponse

from devagent.api.schemas import (
    CreateTaskRequest,
    ErrorResponse,
    EvalSummaryView,
    HealthResponse,
    MetricsResponse,
    RunEvalRequest,
    TaskListItem,
    TaskListResponse,
    TaskView,
    TraceSpanView,
    TracesResponse,
)

if TYPE_CHECKING:
    from devagent.api.service import TaskService

router = APIRouter()


def _service(request: Request) -> TaskService:
    service: TaskService = request.app.state.task_service
    return service


def _to_task_view(data: dict[str, Any]) -> TaskView:
    """把存储中的字典转成响应模型。

    用 ``model_validate`` 而非手工构造：任何字段不匹配都会**立即报错**，
    而不是静默丢字段导致前端拿到残缺数据。
    """
    return TaskView.model_validate(
        {
            "task_id": data.get("task_id", ""),
            "goal": data.get("goal", ""),
            "status": data.get("status", "pending"),
            "succeeded": data.get("succeeded", False),
            "error": data.get("error", ""),
            "duration_ms": data.get("duration_ms", 0),
            "total_tokens": data.get("total_tokens", 0),
            "total_cost_usd": data.get("total_cost_usd", 0.0),
            "steps": data.get("steps", []),
            "nodes": data.get("nodes", []),
            "context_metrics": data.get("context_metrics", {}),
            "metadata": data.get("metadata", {}),
        }
    )


# ---------------------------------------------------------------------- #
# 健康检查
# ---------------------------------------------------------------------- #


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    from devagent.config import get_settings
    from devagent.observability import get_observability

    settings = get_settings()
    providers = [name for name, cfg in settings.models.__dict__.items() if _enabled(cfg)]
    return HealthResponse(
        status="ok",
        providers=providers,
        observability_enabled=get_observability().enabled,
    )


def _enabled(provider_cfg: Any) -> bool:
    return bool(getattr(provider_cfg, "enabled", False))


# ---------------------------------------------------------------------- #
# 任务
# ---------------------------------------------------------------------- #


@router.post(
    "/tasks",
    response_model=TaskView,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["tasks"],
    responses={409: {"model": ErrorResponse}},
)
async def create_task(payload: CreateTaskRequest, request: Request) -> TaskView:
    """提交任务（异步）。返回 202 与任务视图，前端随后订阅 ``/tasks/{id}/events``。"""
    service = _service(request)
    try:
        task_id = await service.submit(
            payload.goal, task_id=payload.task_id, metadata=payload.metadata
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    data = await service.get(task_id)
    if data is None:  # pragma: no cover - 竞态兜底
        raise HTTPException(status_code=500, detail="任务创建后立即丢失")
    return _to_task_view(data)


@router.get("/tasks", response_model=TaskListResponse, tags=["tasks"])
async def list_tasks(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    status_filter: str = Query(default="", alias="status"),
) -> TaskListResponse:
    service = _service(request)
    items = await service.list(limit=limit, status=status_filter)
    return TaskListResponse(
        total=len(items),
        items=[
            TaskListItem(
                task_id=i.get("task_id", ""),
                goal=i.get("goal", ""),
                status=i.get("status", ""),
                succeeded=i.get("succeeded", False),
                duration_ms=i.get("duration_ms", 0),
                total_tokens=i.get("total_tokens", 0),
            )
            for i in items
        ],
    )


@router.get(
    "/tasks/{task_id}",
    response_model=TaskView,
    tags=["tasks"],
    responses={404: {"model": ErrorResponse}},
)
async def get_task(task_id: str, request: Request) -> TaskView:
    data = await _service(request).get(task_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return _to_task_view(data)


@router.delete(
    "/tasks/{task_id}",
    tags=["tasks"],
    responses={404: {"model": ErrorResponse}},
)
async def delete_task(task_id: str, request: Request) -> dict[str, Any]:
    service = _service(request)
    if not await service.delete(task_id):
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return {"deleted": True, "task_id": task_id}


@router.post("/tasks/{task_id}/cancel", tags=["tasks"])
async def cancel_task(task_id: str, request: Request) -> dict[str, Any]:
    service = _service(request)
    if await service.get(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    cancelled = service.cancel(task_id)
    return {"cancelled": cancelled, "task_id": task_id}


@router.get("/tasks/{task_id}/events", tags=["tasks"])
async def stream_events(task_id: str, request: Request) -> EventSourceResponse:
    """SSE 事件流。

    实现要点：
    - 先检查任务是否存在，避免客户端订阅一个不存在的流却永久挂住；
    - 订阅时**回放历史事件**，让晚到的客户端也能看到完整过程；
    - 任务结束且队列排空后主动关闭流，避免客户端一直等。
    """
    service = _service(request)
    if await service.get(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        queue = service.bus.subscribe(task_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    # 心跳：既保活连接，也让客户端能感知服务端仍在工作
                    yield {"event": "ping", "data": json.dumps({"task_id": task_id})}
                    if service.bus.is_closed(task_id) and queue.empty():
                        break
                    continue

                yield {
                    "event": event.kind,
                    "data": json.dumps(event.to_sse_data(), ensure_ascii=False),
                }
                if event.kind in {"task_finished", "task_cancelled"} and queue.empty():
                    break
        finally:
            service.bus.unsubscribe(task_id, queue)

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------- #
# 上下文
# ---------------------------------------------------------------------- #


@router.get("/tasks/{task_id}/context", tags=["context"])
async def get_task_context(task_id: str, request: Request) -> dict[str, Any]:
    """返回该任务的上下文工程指标。

    前端「上下文查看器」用这个接口展示：压缩前/后 token、丢弃片段数、
    各 Agent 的预算利用率分布 —— 这是本项目最有说服力的可视化。
    """
    data = await _service(request).get(task_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return {
        "task_id": task_id,
        "metrics": data.get("context_metrics", {}),
    }


# ---------------------------------------------------------------------- #
# 评测
# ---------------------------------------------------------------------- #


@router.post("/evaluate", response_model=EvalSummaryView, tags=["evaluation"])
async def run_evaluation(payload: RunEvalRequest, request: Request) -> EvalSummaryView:
    """同步运行一次评测（样本量小时适用；大批量请用 CLI 或后台任务）。"""
    from devagent.api.eval_routes import execute_evaluation

    summary = await execute_evaluation(request.app, payload)
    return EvalSummaryView.model_validate(summary)


# ---------------------------------------------------------------------- #
# 可观测性
# ---------------------------------------------------------------------- #


@router.get("/metrics", response_model=MetricsResponse, tags=["observability"])
async def get_metrics() -> MetricsResponse:
    from devagent.observability import get_observability

    return MetricsResponse.model_validate(get_observability().metrics.snapshot())


@router.get("/metrics/prometheus", tags=["observability"])
async def get_metrics_prometheus() -> Any:
    """Prometheus 文本格式（供外部采集器抓取）。"""
    from fastapi.responses import PlainTextResponse

    from devagent.observability import get_observability

    text = get_observability().metrics.to_prometheus_text()
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4")


@router.get("/traces", response_model=TracesResponse, tags=["observability"])
async def get_traces(limit: int = Query(default=100, ge=1, le=1000)) -> TracesResponse:
    """返回最近的 span（需启用了内存导出器）。"""
    from devagent.observability import get_observability
    from devagent.observability.tracing import NoopExporter

    exporter = get_observability().tracer.exporter
    spans: list[Any] = []
    if isinstance(exporter, NoopExporter):
        spans = [s.to_dict() for s in exporter.spans[-limit:]]
    return TracesResponse(
        total=len(spans),
        spans=[TraceSpanView.model_validate(s) for s in spans],
    )


__all__ = ["router"]
