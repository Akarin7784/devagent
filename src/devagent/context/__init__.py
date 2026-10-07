"""上下文工程层（本项目核心竞争力）。

五层能力模型（自底向上）::

    L5  Budget      预算分配     —— 决定「总量」
    L4  Assembly    装配         —— 决定「选什么」
    L3  Compression 压缩         —— 决定「怎么变短」
    L2  Isolation   隔离         —— 决定「谁能看到」
    L1  Routing     路由         —— 决定「谁来消费」

对外主要入口是 :class:`ContextEngine`，它把五层能力串联为单一门面。
"""

from devagent.context.assembly import (
    AssemblyResult,
    ContextAssembler,
    ScoreBreakdown,
    ScoringWeights,
    estimate_tokens,
    make_chunk,
)
from devagent.context.budget import (
    DEFAULT_TEMPLATE,
    TEMPLATES,
    BudgetAllocator,
    BudgetTemplate,
)
from devagent.context.compression import (
    CompressionResult,
    ContextCompressor,
    EchoSummarizer,
    StructuredSummary,
    Summarizer,
)
from devagent.context.isolation import (
    AgentContextSpace,
    ContextBundle,
    ContextEngine,
    ContextIsolator,
    ContextPolicyError,
)
from devagent.context.routing import (
    ComplexityRouter,
    ModelSpec,
    estimate_signals,
)
from devagent.context.tokenizer import (
    HeuristicTokenCounter,
    TokenCounter,
    cosine_similarity,
)
from devagent.context.trust import (
    KNOWN_INJECTION_PATTERNS,
    InjectionGuard,
    InjectionRisk,
    TrustAssessment,
    TrustLevel,
    assess_trust,
    looks_like_injection,
)

__all__ = [
    "DEFAULT_TEMPLATE",
    "KNOWN_INJECTION_PATTERNS",
    "TEMPLATES",
    "AgentContextSpace",
    "AssemblyResult",
    "BudgetAllocator",
    "BudgetTemplate",
    "ComplexityRouter",
    "CompressionResult",
    "ContextAssembler",
    "ContextBundle",
    "ContextCompressor",
    "ContextEngine",
    "ContextIsolator",
    "ContextPolicyError",
    "EchoSummarizer",
    "HeuristicTokenCounter",
    "InjectionGuard",
    "InjectionRisk",
    "ModelSpec",
    "ScoreBreakdown",
    "ScoringWeights",
    "StructuredSummary",
    "Summarizer",
    "TokenCounter",
    "TrustAssessment",
    "TrustLevel",
    "assess_trust",
    "cosine_similarity",
    "estimate_signals",
    "estimate_tokens",
    "looks_like_injection",
    "make_chunk",
]
