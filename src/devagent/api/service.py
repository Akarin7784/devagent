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
import inspect
import time
import uuid
from typing import Any

from devagent.api.store import EventBus, TaskEvent, TaskStore
from devagent.enums import TaskStatus
from devagent.logging_config import get_logger

logger = get_logger(__name__)


async def _maybe_await(value: Any) -> Any:
    """兼容同步与异步存储。

    ``TaskStore`` 协议被刻意设计为**不声明 async**：内存实现是纯同步的，
    强行加 ``async def`` 会平白引入 200 个协程的调度开销，而且让纯逻辑
    的单测被迫变成 ``async``。

    但 SQL 实现天然是异步的。于是这里做一个薄适配：是 awaitable 就 await。
    代价是一次 ``isawaitable`` 判断（纳秒级），换来两种实现共用同一套调用代码。
    """
    if inspect.isawaitable(value):
        return await value
    return value


def _as_dict(value: Any) -> dict[str, Any] | None:
    """把 ``_maybe_await`` 的结果收窄成任务字典。

    ``_maybe_await`` 的返回类型只能是 ``Any``：它的入参既可能是协程也可能是
    普通值，无法用类型参数表达。不收窄的话每个调用点都会触发
    ``warn_return_any``，最后演变成到处撒 ``cast`` —— 问题只是被藏起来。
    集中在这里做一次显式收窄（顺便复制一份，避免调用方误改存储内部状态），
    调用点就能保持干净。
    """
    if isinstance(value, dict):
        return dict(value)
    return None


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    """把 ``_maybe_await`` 的结果收窄成任务字典列表。"""
    if not value:
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


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
        # 把 Orchestrator 的进度回调接到 EventBus，使 DAG 节点的开始/完成/判定
        # 能实时推给前端。task_id 在回调时需要，因此用「当前执行中的任务 id」
        # 传递 —— 编排器一次 run 只对应一个 task_id，且 _execute 内是串行发起。
        self._current_task_id: str = ""
        """当前执行中的任务 id（供进度回调定位；一次 run 只对应一个）。"""

        self._current_goal: str = ""
        """当前任务的 goal（供 task_started 事件携带）。"""

        orchestrator._on_event = self._on_orchestrator_event

    def _on_orchestrator_event(self, kind: str, payload: dict[str, Any]) -> None:
        """将编排器进度回调转为 SSE 事件。

        注意：这是同步回调（编排器不便 await 每个事件），而 ``bus.publish``
        也是同步的（内部用 put_nowait + 丢最旧策略），因此无需 async。
        """
        tid = self._current_task_id
        if not tid:
            return
        # 把当前 goal 附在第一个事件上，前端据此显示任务标题
        if kind == "task_started":
            payload = {"goal": self._current_goal, **payload}
        self._publish(tid, kind, payload)

    @property
    def bus(self) -> EventBus:
        return self._bus

    # ------------------------------------------------------------------ #
    # 提交与执行
    # ------------------------------------------------------------------ #

    async def submit(
        self, goal: str, *, task_id: str = "", metadata: dict[str, Any] | None = None
    ) -> str:
        """提交任务并立即返回 id（不等待完成）。

        改为 ``async`` 是为了支持 SQL 存储（写库是异步的）。内存实现下
        除了多一次 ``isawaitable`` 判断外没有额外开销。
        """
        tid = task_id or f"task_{uuid.uuid4().hex[:12]}"
        if await _maybe_await(self._store.get(tid)) is not None:
            raise ValueError(f"任务 id 已存在：{tid}")

        await _maybe_await(
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
        )
        self._running[tid] = asyncio.create_task(self._execute(tid, goal))
        logger.info("task_submitted", task_id=tid)
        return tid

    async def _execute(self, task_id: str, goal: str) -> None:
        """后台执行任务，全程发事件。"""
        async with self._semaphore:
            # 供进度回调定位当前任务（见 _on_orchestrator_event）
            self._current_task_id = task_id
            self._current_goal = goal
            self._publish(task_id, "task_started", {"goal": goal})
            await self._update(task_id, {"status": TaskStatus.RUNNING.value})
            started = time.perf_counter()
            try:
                result = await self._orchestrator.run(goal, task_id=task_id)
                view = _to_view(result)
                existing = await _maybe_await(self._store.get(task_id)) or {}
                view["metadata"] = existing.get("metadata", {})
                await _maybe_await(self._store.save(task_id, view))
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
                await self._update(task_id, {"status": TaskStatus.CANCELLED.value})
                self._publish(task_id, "task_cancelled", {})
                raise
            except Exception as exc:
                logger.exception("task_execution_failed", task_id=task_id)
                await self._update(
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
                if self._current_task_id == task_id:
                    self._current_task_id = ""
                    self._current_goal = ""

    def _publish(self, task_id: str, kind: str, payload: dict[str, Any]) -> None:
        self._bus.publish(TaskEvent(kind=kind, task_id=task_id, payload=payload))

    async def _update(self, task_id: str, patch: dict[str, Any]) -> None:
        data = await _maybe_await(self._store.get(task_id))
        if data is None:
            return
        data.update(patch)
        await _maybe_await(self._store.save(task_id, data))

    # ------------------------------------------------------------------ #
    # 查询与取消
    # ------------------------------------------------------------------ #

    async def get(self, task_id: str) -> dict[str, Any] | None:
        return _as_dict(await _maybe_await(self._store.get(task_id)))

    async def list(self, *, limit: int = 50, status: str = "") -> list[dict[str, Any]]:
        return _as_dict_list(await _maybe_await(self._store.list(limit=limit, status=status)))

    async def delete(self, task_id: str) -> bool:
        """删除任务（含事件历史）。"""
        self.cancel(task_id)
        existed = bool(await _maybe_await(self._store.delete(task_id)))
        if existed:
            self._bus.clear(task_id)
        return existed

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
                return _as_dict(await _maybe_await(self._store.get(task_id)))
            except asyncio.CancelledError:
                pass
        return _as_dict(await _maybe_await(self._store.get(task_id)))

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
