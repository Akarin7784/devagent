"""编排层：DAG 调度与多 Agent 编排。

- ``DAG``：不可变的有向无环图，负责拓扑序、就绪判定、失败传播；
- ``Orchestrator``：编排器，串联需求 → 架构 → 编码 → 测试 → 验证 → 审查。
"""

from devagent.orchestration.dag import (
    DAG,
    DAGError,
    StepState,
    build_dag_from_architect_output,
)
from devagent.orchestration.orchestrator import (
    OrchestrationError,
    Orchestrator,
    OrchestratorConfig,
    TaskRunResult,
)

__all__ = [
    "DAG",
    "DAGError",
    "OrchestrationError",
    "Orchestrator",
    "OrchestratorConfig",
    "StepState",
    "TaskRunResult",
    "build_dag_from_architect_output",
]
