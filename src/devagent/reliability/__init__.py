"""可靠性模块：让长任务不失控。

对齐 ``docs/03-多Agent编排与可靠性.md``。

包含四个组件：
- ``ReflexionMemory``：失败反思的教训存储与检索；
- ``StepCheckpoint`` / ``TaskCheckpointStore``：断点续跑；
- ``CircuitBreaker``：成本与步数熔断；
- ``LoopDetector``：循环与重复失败检测。
"""

from devagent.reliability.breaker import BudgetExceededError, CircuitBreaker
from devagent.reliability.checkpoint import StepCheckpoint, TaskCheckpointStore
from devagent.reliability.loop_detector import LoopDetector
from devagent.reliability.reflexion import ReflexionMemory

__all__ = [
    "BudgetExceededError",
    "CircuitBreaker",
    "LoopDetector",
    "ReflexionMemory",
    "StepCheckpoint",
    "TaskCheckpointStore",
]
