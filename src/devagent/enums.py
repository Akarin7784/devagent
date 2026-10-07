"""核心枚举定义。

集中定义跨模块共享的枚举类型，避免循环依赖。
"""

from __future__ import annotations

from enum import StrEnum


class AgentType(StrEnum):
    """系统中的专职 Agent 类型。

    采用 Hierarchical Supervisor 拓扑：Orchestrator 只负责规划与调度，
    各专职 Agent 承担单一职责，Verifier 独立于执行者做交叉验证。
    """

    ORCHESTRATOR = "orchestrator"
    """编排器：任务分解、调度、状态维护。不参与具体执行，避免自评偏差。"""

    REQUIREMENT = "requirement"
    """需求澄清：将模糊需求转为可验收的结构化规格。"""

    ARCHITECT = "architect"
    """方案设计：产出技术方案与任务 DAG。"""

    CODER = "coder"
    """编码：产出补丁与改动理由。"""

    TESTER = "tester"
    """测试：生成测试并在沙箱执行，产出客观证据。"""

    VERIFIER = "verifier"
    """独立验证：以异构模型交叉校验，阻断幻觉传播。"""

    REVIEWER = "reviewer"
    """代码审查：从工程质量维度独立评审。"""


class MessageType(StrEnum):
    """Agent 间结构化消息类型。

    设计原则：Agent 之间不传自然语言长文，只传强类型结构化消息，
    以实现可校验、防歧义、可压缩、可追踪的协作协议。
    """

    TASK_SPEC = "task_spec"
    """派发任务规格。"""

    ARTIFACT = "artifact"
    """提交产出物。"""

    FEEDBACK = "feedback"
    """评审反馈（含拒绝理由与证据）。"""

    QUESTION = "question"
    """请求澄清。"""

    DECISION = "decision"
    """记录架构/实现决策。"""

    ESCALATION = "escalation"
    """升级（当前 Agent 无法完成）。"""


class StepStatus(StrEnum):
    """DAG 节点（步骤）状态。"""

    PENDING = "pending"
    """未就绪：依赖未满足。"""

    READY = "ready"
    """就绪：依赖已全部完成，可调度。"""

    RUNNING = "running"
    """执行中。"""

    VERIFYING = "verifying"
    """校验中（Verifier 处理中）。"""

    SUCCESS = "success"
    """成功。"""

    REJECTED = "rejected"
    """被驳回：需带反馈回退上游。"""

    FAILED = "failed"
    """失败：重试耗尽或不可恢复错误。"""

    SKIPPED = "skipped"
    """跳过：因上游失败或策略取消。"""


class TaskStatus(StrEnum):
    """顶层任务状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PAUSED = "paused"
    """暂停：通常因成本熔断或需人工介入。"""
    CANCELLED = "cancelled"


class ContextKind(StrEnum):
    """上下文片段类型。

    用于装配阶段的差异化处理与位置编排。
    """

    SYSTEM_PROMPT = "system_prompt"
    """系统指令：永不压缩。"""

    TASK_SPEC = "task_spec"
    """任务规格与验收标准：永不压缩。"""

    CODE = "code"
    """源码片段。"""

    TOOL_RESULT = "tool_result"
    """工具执行结果（测试输出、命令回显等）。"""

    HISTORY = "history"
    """历史对话/步骤摘要。"""

    KNOWLEDGE = "knowledge"
    """外部知识（文档、规范、检索结果）。"""

    DECISION = "decision"
    """决策记录。"""


class ModelTier(StrEnum):
    """模型能力档位（用于分级路由）。"""

    SMALL = "small"
    """小模型：格式转换、信息提取、摘要。"""

    MEDIUM = "medium"
    """中模型：常规编码、测试生成。"""

    LARGE = "large"
    """大模型：架构设计、跨文件推理、失败复盘。"""


class Verdict(StrEnum):
    """验证结论。"""

    PASS = "pass"
    REJECT = "reject"


class FailureKind(StrEnum):
    """失败原因分类（用于统计与策略选择）。"""

    TEST_FAILURE = "test_failure"
    """测试未通过。"""

    CRITERIA_UNMET = "criteria_unmet"
    """验收标准未满足。"""

    TOOL_ERROR = "tool_error"
    """工具执行错误。"""

    MODEL_ERROR = "model_error"
    """模型调用错误（超时/限流/内容拒绝）。"""

    BUDGET_EXCEEDED = "budget_exceeded"
    """超出 token 预算。"""

    TIMEOUT = "timeout"
    """超时。"""

    LOOP_DETECTED = "loop_detected"
    """检测到循环。"""

    UNKNOWN = "unknown"
