"""任务存储与事件总线。

## 为什么先做内存版

真正的生产实现应落 Postgres（``db`` 包已有配置），但 API 层不应被数据库
可用性绑架 —— 否则贡献者 clone 下来必须先起一个 Postgres 才能跑通 demo。

因此定义 ``TaskStore`` 协议 + 两个实现：

- ``InMemoryTaskStore``：默认，零依赖，进程内；
- （后续）``SqlTaskStore``：持久化，用于多实例部署。

这体现了「先定接口再定实现」的依赖倒置，也让 API 层可被单元测试完全覆盖。

## 事件总线

SSE 需要「任务执行过程中持续推送事件」。这里用 ``asyncio.Queue`` 实现
每个任务一条事件流。

关键设计：**带缓冲的历史回放**。客户端可能晚于任务启动才连上 SSE，
若不回放历史，用户会看到「一片空白直到下一个事件」。因此每个订阅者
连接时会先收到已产生的全部事件。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from devagent.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class TaskEvent:
    """一条任务事件。"""

    kind: str
    """事件类型：task_started / node_started / node_finished / task_finished 等。"""

    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_sse_data(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            **self.payload,
        }


class EventBus:
    """任务事件的发布/订阅。

    实现要点：
    - 每个任务维护一个**历史环形缓冲**（供晚到的订阅者回放）与该任务的
      活跃订阅者队列；
    - 发布时写入历史并推给所有活跃队列；
    - 队列满时丢弃最旧事件而不是阻塞发布者 —— **绝不能让慢客户端
      拖慢任务执行**（这是 SSE 实现里最常见的坑）。
    """

    def __init__(self, *, history_size: int = 500, queue_size: int = 200) -> None:
        self._history: dict[str, deque[TaskEvent]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[TaskEvent]]] = {}
        self._closed: set[str] = set()
        self._history_size = history_size
        self._queue_size = queue_size
        self._dropped: dict[str, int] = {}

    def publish(self, event: TaskEvent) -> None:
        hist = self._history.setdefault(event.task_id, deque(maxlen=self._history_size))
        hist.append(event)

        for queue in self._subscribers.get(event.task_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 慢消费者：丢最旧的，保住最新的。丢事件比阻塞任务好得多。
                self._dropped[event.task_id] = self._dropped.get(event.task_id, 0) + 1
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    logger.debug("event_dropped", task_id=event.task_id, kind=event.kind)

    def subscribe(self, task_id: str) -> asyncio.Queue[TaskEvent]:
        """订阅某任务的事件流，并**回放已有历史**。"""
        queue: asyncio.Queue[TaskEvent] = asyncio.Queue(maxsize=self._queue_size)
        for event in self._history.get(task_id, ()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                break
        self._subscribers.setdefault(task_id, []).append(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue[TaskEvent]) -> None:
        subs = self._subscribers.get(task_id)
        if not subs:
            return
        with contextlib.suppress(ValueError):
            subs.remove(queue)
        if not subs:
            self._subscribers.pop(task_id, None)

    def close(self, task_id: str) -> None:
        """标记任务事件流结束（订阅者收到此标记后应停止等待）。"""
        self._closed.add(task_id)

    def is_closed(self, task_id: str) -> bool:
        return task_id in self._closed

    def history(self, task_id: str) -> list[TaskEvent]:
        return list(self._history.get(task_id, ()))

    def dropped_count(self, task_id: str) -> int:
        return self._dropped.get(task_id, 0)

    def clear(self, task_id: str) -> None:
        self._history.pop(task_id, None)
        self._subscribers.pop(task_id, None)
        self._closed.discard(task_id)
        self._dropped.pop(task_id, None)


@runtime_checkable
class TaskStore(Protocol):
    """任务存储协议。

    刻意**不声明** ``async``：内存实现是纯同步的（见 ``InMemoryTaskStore``），
    而 SQL 实现是异步的。上层的兼容由 ``devagent.api.service._maybe_await``
    承担 —— 这样纯逻辑单测不必被迫变成协程，也不会为每次调用引入协程开销。

    声明为 ``runtime_checkable`` 是为了让「实现是否满足协议」这件事**可被
    测试断言**。否则类型错误只会在 CI 的 mypy 里出现（而且只覆盖被真正
    用到的调用路径），运行时完全没有防线。
    """

    def save(self, task_id: str, data: dict[str, Any]) -> Any: ...

    def get(self, task_id: str) -> Any: ...

    def list(self, *, limit: int = 50, status: str = "") -> Any: ...

    def delete(self, task_id: str) -> Any: ...


class InMemoryTaskStore:
    """进程内任务存储（带容量上限的 LRU）。"""

    def __init__(self, *, capacity: int = 200) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._order: deque[str] = deque()
        self._capacity = capacity

    def save(self, task_id: str, data: dict[str, Any]) -> None:
        if task_id not in self._data:
            self._order.append(task_id)
        self._data[task_id] = data
        while len(self._order) > self._capacity:
            oldest = self._order.popleft()
            self._data.pop(oldest, None)

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self._data.get(task_id)

    def list(self, *, limit: int = 50, status: str = "") -> list[dict[str, Any]]:
        items = [self._data[tid] for tid in reversed(self._order) if tid in self._data]
        if status:
            items = [i for i in items if i.get("status") == status]
        return items[:limit]

    def delete(self, task_id: str) -> bool:
        existed = self._data.pop(task_id, None) is not None
        with contextlib.suppress(ValueError):
            self._order.remove(task_id)
        return existed

    def __len__(self) -> int:
        return len(self._data)


__all__ = [
    "EventBus",
    "InMemoryTaskStore",
    "TaskEvent",
    "TaskStore",
]
