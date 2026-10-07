"""断点续跑（Checkpoint）。

对齐 ``docs/03``：每个步骤完成即持久化，崩溃后从最后成功点继续，
已完成步骤不重跑。

存储抽象：``CheckpointStore`` 是 Protocol，默认提供内存实现；
生产环境接 Redis / Postgres 只需实现同一接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class StepCheckpoint:
    """单个步骤的检查点。

    ``run_id`` 用于区分「同一次运行内的重试」与「跨运行的恢复」：
    - 同一次运行内因验证驳回而重试时，**不应**被检查点短路，
      否则回退机制失效（曾出现该缺陷：驳回后直接读检查点标记为成功）；
    - 只有跨运行恢复时才应跳过已完成的步骤。
    """

    task_id: str
    step_id: str
    completed: bool = False
    node_id: str = ""
    run_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def matches_run(self, run_id: str) -> bool:
        """判断该检查点是否属于指定的运行实例。"""
        return bool(run_id) and self.run_id == run_id


@runtime_checkable
class CheckpointStore(Protocol):
    """检查点存储协议。"""

    def save(self, checkpoint: StepCheckpoint) -> None: ...

    def load(self, task_id: str, step_id: str) -> StepCheckpoint | None: ...

    def list_for_task(self, task_id: str) -> list[StepCheckpoint]: ...

    def clear(self, task_id: str) -> None: ...


@dataclass(slots=True)
class TaskCheckpointStore:
    """内存检查点存储（开发/测试默认实现）。

    生产环境可替换为 Redis/Postgres 实现；接口保持一致，
    使断点续跑能力不依赖具体存储。
    """

    _data: dict[str, dict[str, StepCheckpoint]] = field(default_factory=dict)

    def save(self, checkpoint: StepCheckpoint) -> None:
        self._data.setdefault(checkpoint.task_id, {})[checkpoint.step_id] = checkpoint

    def load(self, task_id: str, step_id: str) -> StepCheckpoint | None:
        return self._data.get(task_id, {}).get(step_id)

    def list_for_task(self, task_id: str) -> list[StepCheckpoint]:
        return list(self._data.get(task_id, {}).values())

    def completed_step_ids(self, task_id: str) -> set[str]:
        """返回某任务已完成的步骤 id 集合（用于快速跳过）。"""
        return {cp.step_id for cp in self._data.get(task_id, {}).values() if cp.completed}

    def clear(self, task_id: str) -> None:
        self._data.pop(task_id, None)

    def clear_step(self, task_id: str, step_id: str) -> None:
        """清除单个步骤的检查点。

        用于**回退重试**场景：若不清除，重试时读取到旧的「已完成」记录
        会直接跳过执行，导致回退机制失效（该缺陷曾真实发生）。
        """
        steps = self._data.get(task_id)
        if steps is not None:
            steps.pop(step_id, None)

    def clear_all(self) -> None:
        self._data.clear()


__all__ = [
    "CheckpointStore",
    "StepCheckpoint",
    "TaskCheckpointStore",
]
