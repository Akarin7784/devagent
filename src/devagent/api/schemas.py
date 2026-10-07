"""API 数据契约（Pydantic 模型）。

与领域模型分离的原因：领域模型是 ``frozen=True, extra="forbid"`` 的**内部**
契约，而 API 层需要「部分字段可选 + 有默认值 + 可演进」的外部契约。
把两者混在一起会导致内部重构直接破坏 API 兼容性。

命名约定：以 ``Request`` / ``Response`` 结尾，与领域模型明确区分。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------- #
# 通用
# ---------------------------------------------------------------------- #


class HealthResponse(BaseModel):
    """健康检查响应。"""

    status: Literal["ok", "degraded"] = "ok"
    version: str = "0.1.0"
    providers: list[str] = Field(default_factory=list, description="已配置的模型提供商")
    observability_enabled: bool = False


class ErrorResponse(BaseModel):
    """统一错误响应。

    所有失败路径都返回这个结构，前端只需处理一种错误格式。
    """

    error: str
    detail: str = ""
    code: str = ""


# ---------------------------------------------------------------------- #
# 任务
# ---------------------------------------------------------------------- #


class CreateTaskRequest(BaseModel):
    """创建任务请求。"""

    goal: str = Field(min_length=1, max_length=10_000, description="自然语言需求")
    task_id: str = Field(default="", description="可选自定义 id")
    metadata: dict[str, Any] = Field(default_factory=dict)


class StepView(BaseModel):
    """单个步骤的视图。"""

    step_id: str
    agent: str
    output: str = ""
    tokens_used: int = 0
    cost_usd: float = 0.0
    model: str = ""
    attempt: int = 1
    verdict: str = ""
    failed_criteria: list[str] = Field(default_factory=list)


class NodeView(BaseModel):
    """DAG 节点视图。"""

    id: str
    goal: str = ""
    agent_type: str = ""
    deps: list[str] = Field(default_factory=list)
    status: str = "pending"
    attempt: int = 1
    tokens_used: int = 0
    last_error: str = ""


class TaskView(BaseModel):
    """任务详情视图。"""

    task_id: str
    goal: str
    status: str
    succeeded: bool = False
    error: str = ""
    duration_ms: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    steps: list[StepView] = Field(default_factory=list)
    nodes: list[NodeView] = Field(default_factory=list)
    context_metrics: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskListItem(BaseModel):
    """任务列表项（精简视图，不含步骤明细）。"""

    task_id: str
    goal: str
    status: str
    succeeded: bool = False
    duration_ms: int = 0
    total_tokens: int = 0


class TaskListResponse(BaseModel):
    """任务列表响应。"""

    total: int
    items: list[TaskListItem] = Field(default_factory=list)


# ---------------------------------------------------------------------- #
# 上下文
# ---------------------------------------------------------------------- #


class ContextChunkView(BaseModel):
    """上下文片段视图。"""

    kind: str
    source: str = ""
    tokens: int = 0
    age: int = 0
    relevance: float = 0.0
    is_hard: bool = False
    score: float = 0.0
    preview: str = ""


class ContextDecisionView(BaseModel):
    """上下文装配决策视图 —— 本项目最有展示价值的数据。"""

    agent: str
    tokens_before: int = 0
    tokens_after: int = 0
    budget_total: int = 0
    budget_used: int = 0
    compression_applied: bool = False
    routing_tier: str = ""
    dropped: int = 0
    considered: int = 0
    selected: list[ContextChunkView] = Field(default_factory=list)
    dropped_reasons: dict[str, int] = Field(default_factory=dict)

    @property
    def savings_ratio(self) -> float:
        if self.tokens_before <= 0:
            return 0.0
        return 1.0 - (self.tokens_after / self.tokens_before)


# ---------------------------------------------------------------------- #
# 评测
# ---------------------------------------------------------------------- #


class RunEvalRequest(BaseModel):
    """触发评测请求。"""

    dataset_path: str = Field(default="", description="留空则用配置默认值")
    categories: list[str] = Field(default_factory=list)
    max_samples: int | None = Field(default=None, ge=1)
    use_judge: bool = True


class EvalSummaryView(BaseModel):
    """评测摘要视图。"""

    dataset: str
    total: int
    success_rate: float = 0.0
    first_pass_rate: float = 0.0
    judge_pass_rate: float = 0.0
    mean_judge_score: float = 0.0
    inconsistent_judge_rate: float = 0.0
    discrimination: float = 0.0
    mean_context_savings: float = 0.0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    judge_model: str = ""
    categories: dict[str, dict[str, float]] = Field(default_factory=dict)


class MetricsResponse(BaseModel):
    """指标快照响应。

    ★ 注意三层嵌套差异（这是实际踩过的坑）：

    - ``counters`` / ``gauges``：``{指标名: {标签串: 数值}}`` —— 两层；
    - ``histograms``：``{指标名: {标签串: 统计字典}}`` —— 三层，
      最内层是 ``count``/``sum``/``p50``/``p90``... 的统计量字典。

    早期把两者统一声明为 ``dict[str, dict[str, float]]``，导致
    histogram 一旦有数据就抛 ValidationError。
    """

    counters: dict[str, dict[str, float]] = Field(default_factory=dict)
    gauges: dict[str, dict[str, float]] = Field(default_factory=dict)
    histograms: dict[str, dict[str, dict[str, float]]] = Field(default_factory=dict)


class TraceSpanView(BaseModel):
    """Span 视图。"""

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None = None
    duration_ms: float = 0.0
    status: str = "unset"
    error: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)


class TracesResponse(BaseModel):
    """最近的追踪列表。"""

    total: int
    spans: list[TraceSpanView] = Field(default_factory=list)


__all__ = [
    "ContextChunkView",
    "ContextDecisionView",
    "CreateTaskRequest",
    "ErrorResponse",
    "EvalSummaryView",
    "HealthResponse",
    "MetricsResponse",
    "NodeView",
    "RunEvalRequest",
    "StepView",
    "TaskListItem",
    "TaskListResponse",
    "TaskView",
    "TraceSpanView",
    "TracesResponse",
]
