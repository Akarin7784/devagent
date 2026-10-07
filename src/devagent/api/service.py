"""任务编排服务：连接 API 层与 Orchestrator。

## 为什么需要这一层

Orchestrator 是**纯计算**组件：给一个 goal，返回 TaskRunResult。它不关心
HTTP、SSE、存储。但要把它变成「可被前端观测的异步任务」，需要额外：

1. 后台执行（不阻塞请求）；
2. 执行过程中持续发事件；
3. 结果落到 TaskStore；
4. 并发控制（避免一个用户起 1000 个任务打爆 API 配额）。

这些都属于**应用服务层**的职责，放在这里而不是塞进 Orchestrator，
保证编排器的可测试性（现有 272 个测试就依赖它无副作用）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from devagent.api.store import EventBus, TaskEvent, TaskStore
from devagent.enums import TaskStatus
from devagent.logging_config import get_logger

logger = get_logger(__name__)


class TaskService:
    """任务生命周期管理。

    用法::

        service = TaskService(orchestrator, store=InMemoryTaskStore())
        task_id = service.submit("加个分页接口")
        async for event in service.stream(task_id):
            ...
    """

    def __init__(
        self,
        orchestrator: Any,
        *,
        store: TaskStore,
        bus: EventBus | None = None,
        max_concurrent: int = 4,
    ) -> None:
        self._orchestrator = orchestrator
        self._store = store
        self._bus = bus or EventBus()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._running: dict[str, asyncio.Task[None]] = {}

    @property
    def bus(self) -> EventBus:
        return self._bus

    # ------------------------------------------------------------------ #
    # 提交与执行
    # ------------------------------------------------------------------ #

    def submit(
        self, goal: str, *, task_id: str = "", metadata: dict[str, Any] | None = None
    ) -> str:
        """提交任务并立即返回 id（不等待完成）。"""
        tid = task_id or f"task_{uuid.uuid4().hex[:12]}"
        if self._store.get(tid) is not None:
            raise ValueError(f"任务 id 已存在：{tid}")

        self._store.save(
            tid,
            {
                "task_id": tid,
                "goal": goal,
                "status": TaskStatus.PENDING.value,
                "succeeded": False,
                "error": "",
                "duration_ms": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "steps": [],
                "nodes": [],
                "context_metrics": {},
                "metadata": metadata or {},
            },
        )
        self._running[tid] = asyncio.create_task(self._execute(tid, goal))
        logger.info("task_submitted", task_id=tid)
        return tid

    async def _execute(self, task_id: str, goal: str) -> None:
        """后台执行任务，全程发事件。"""
        async with self._semaphore:
            self._publish(task_id, "task_started", {"goal": goal})
            self._update(task_id, {"status": TaskStatus.RUNNING.value})
            started = time.perf_counter()
            try:
                result = await self._orchestrator.run(goal, task_id=task_id)
                view = _to_view(result)
                view["metadata"] = (self._store.get(task_id) or {}).get("metadata", {})
                self._store.save(task_id, view)
                self._publish(
                    task_id,
                    "task_finished",
                    {
                        "status": view["status"],
                        "succeeded": view["succeeded"],
                        "duration_ms": view["duration_ms"],
                        "total_tokens": view["total_tokens"],
                        "error": view["error"],
                    },
                )
            except asyncio.CancelledError:
                self._update(task_id, {"status": TaskStatus.CANCELLED.value})
                self._publish(task_id, "task_cancelled", {})
                raise
            except Exception as exc:
                logger.exception("task_execution_failed", task_id=task_id)
                self._update(
                    task_id,
                    {
                        "status": TaskStatus.FAILED.value,
                        "error": f"{type(exc).__name__}: {exc}",
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                    },
                )
                self._publish(task_id, "task_finished", {"status": "failed", "error": str(exc)})
            finally:
                self._bus.close(task_id)
                self._running.pop(task_id, None)

    def _publish(self, task_id: str, kind: str, payload: dict[str, Any]) -> None:
        self._bus.publish(TaskEvent(kind=kind, task_id=task_id, payload=payload))

    def _update(self, task_id: str, patch: dict[str, Any]) -> None:
        data = self._store.get(task_id)
        if data is None:
            return
        data.update(patch)
        self._store.save(task_id, data)

    # ------------------------------------------------------------------ #
    # 查询与取消
    # ------------------------------------------------------------------ #

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self._store.get(task_id)

    def list(self, *, limit: int = 50, status: str = "") -> list[dict[str, Any]]:
        return self._store.list(limit=limit, status=status)

    def cancel(self, task_id: str) -> bool:
        task = self._running.get(task_id)
        if task is None or task.done():
            return False
        task.cancel()
        logger.info("task_cancel_requested", task_id=task_id)
        return True

    async def wait(self, task_id: str, *, timeout: float | None = None) -> dict[str, Any] | None:
        """等待任务完成（测试与同步场景使用）。"""
        task = self._running.get(task_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except TimeoutError:
                return self._store.get(task_id)
            except asyncio.CancelledError:
                pass
        return self._store.get(task_id)

    async def shutdown(self) -> None:
        """取消所有在跑任务并等待收尾。"""
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()


# ---------------------------------------------------------------------- #
# 领域结果 → API 视图
# ---------------------------------------------------------------------- #


def _to_view(result: Any) -> dict[str, Any]:
    """把 ``TaskRunResult`` 转成可存储/可序列化的字典。"""
    steps: list[dict[str, Any]] = []
    for step in getattr(result, "steps", None) or []:
        agent = getattr(step, "agent", None)
        feedback = getattr(step, "feedback", None)
        steps.append(
            {
                "step_id": str(getattr(step, "step_id", "")),
                "agent": getattr(agent, "value", str(agent or "")),
                "output": str(getattr(step, "output", "")),
                "tokens_used": int(getattr(step, "tokens_used", 0) or 0),
                "cost_usd": float(getattr(step, "cost_usd", 0.0) or 0.0),
                "model": str(getattr(step, "model", "") or ""),
                "attempt": int(getattr(step, "attempt", 1) or 1),
                "verdict": getattr(getattr(feedback, "verdict", None), "value", ""),
                "failed_criteria": list(getattr(feedback, "failed_criteria", []) or []),
            }
        )

    nodes: list[dict[str, Any]] = []
    dag = getattr(result, "dag", None)
    if dag is not None:
        states = getattr(dag, "states", {}) or {}
        for node_id, node in (getattr(dag, "nodes", {}) or {}).items():
            state = states.get(node_id)
            agent_type = getattr(node, "agent_type", None)
            nodes.append(
                {
                    "id": node_id,
                    "goal": str(getattr(node, "goal", "") or ""),
                    "agent_type": getattr(agent_type, "value", str(agent_type or "")),
                    "deps": list(getattr(node, "deps", ()) or ()),
                    "status": getattr(getattr(state, "status", None), "value", "pending"),
                    "attempt": int(getattr(state, "attempt", 1) or 1),
                    "tokens_used": int(getattr(state, "tokens_used", 0) or 0),
                    "last_error": str(getattr(state, "last_error", "") or ""),
                }
            )

    return {
        "task_id": str(getattr(result, "task_id", "")),
        "goal": str(getattr(result, "goal", "")),
        "status": getattr(getattr(result, "status", None), "value", "unknown"),
        "succeeded": bool(getattr(result, "succeeded", False)),
        "error": str(getattr(result, "error", "") or ""),
        "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
        "total_tokens": int(getattr(result, "total_tokens", 0) or 0),
        "total_cost_usd": float(getattr(result, "total_cost_usd", 0.0) or 0.0),
        "steps": steps,
        "nodes": nodes,
        "context_metrics": _context_metrics(),
        "metadata": {},
    }


def _context_metrics() -> dict[str, Any]:
    """从可观测性单例汇总本任务的上下文工程收益。

    这是前端「上下文查看器」的数据源，也是最能体现项目价值的一块。
    """
    from devagent.observability import MetricNames, get_observability

    metrics = get_observability().metrics
    return {
        "tokens_saved": metrics.total(MetricNames.CONTEXT_TOKENS_SAVED),
        "chunks_dropped": metrics.total(MetricNames.CONTEXT_CHUNKS_DROPPED),
        "compression_ratio": metrics.observe_total(MetricNames.CONTEXT_COMPRESSION_RATIO),
        "utilization": metrics.observe_total(MetricNames.CONTEXT_UTILIZATION),
    }


__all__ = ["TaskService", "_to_view"]
