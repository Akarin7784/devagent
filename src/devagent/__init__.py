"""DevAgent 包根模块。

一个以**上下文工程**为核心竞争力的多 Agent 协作软件研发助手。

架构分层::

    交互层  (api / web)
    编排层  (orchestration)
    Agent 层 (agents)
    上下文工程层 (context)   ← 核心
    模型层  (models)
    工具层  (tools)
    可靠性  (reliability)
    可观测  (observability)
    评测    (evaluation)
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
