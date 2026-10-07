"""Agent 层：专职 Agent 实现。

采用 Hierarchical Supervisor 拓扑，各 Agent 单一职责：
- ``RequirementAgent``：需求澄清 → 可验收规格
- ``ArchitectAgent``：方案设计 → 任务 DAG
- ``CoderAgent``：编码 → 改动 + 验收标准映射
- ``TesterAgent``：测试生成与执行 → 客观证据
- ``VerifierAgent``：独立验证 → 通过/驳回（防幻觉传播）
- ``ReviewerAgent``：工程质量审查（与 Verifier 正交）
"""

from devagent.agents.architect import ArchitectAgent
from devagent.agents.base import (
    AgentContextError,
    AgentInvocation,
    AgentOutput,
    BaseAgent,
)
from devagent.agents.coder import CoderAgent
from devagent.agents.requirement import RequirementAgent
from devagent.agents.reviewer import ReviewerAgent
from devagent.agents.tester import TesterAgent, TestRunner, TestRunOutcome
from devagent.agents.verifier import VerifierAgent

__all__ = [
    "AgentContextError",
    "AgentInvocation",
    "AgentOutput",
    "ArchitectAgent",
    "BaseAgent",
    "CoderAgent",
    "RequirementAgent",
    "ReviewerAgent",
    "TestRunOutcome",
    "TestRunner",
    "TesterAgent",
    "VerifierAgent",
]
