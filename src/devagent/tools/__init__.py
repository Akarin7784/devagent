"""工具层：沙箱执行、测试运行、代码索引。

安全原则：代码必须在**不信任环境**中执行。
``build_sandbox`` 优先使用 Docker（容器级隔离），
不可用时降级为本地子进程并**明确告警**（不静默降级）。
"""

from devagent.tools.code_index import CodeIndex, FileIndex, SearchHit, Symbol
from devagent.tools.sandbox import (
    DockerSandbox,
    ExecutionResult,
    LocalProcessSandbox,
    SandboxExecutor,
    SandboxPolicy,
    build_sandbox,
)
from devagent.tools.test_runner import PytestOutputParser, SandboxTestRunner

__all__ = [
    "CodeIndex",
    "DockerSandbox",
    "ExecutionResult",
    "FileIndex",
    "LocalProcessSandbox",
    "PytestOutputParser",
    "SandboxExecutor",
    "SandboxPolicy",
    "SandboxTestRunner",
    "SearchHit",
    "Symbol",
    "build_sandbox",
]
