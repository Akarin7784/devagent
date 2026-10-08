"""测试运行时装配。

把「沙箱执行器」与「测试执行器」装配成一个可直接注入编排器的对象。

API、CLI 与评测使用同一装配入口。配置目录仅提供基线；Runner 为每次
测试调用创建独立快照，生成文件不写回基线，不在并发任务或重试之间共享。
留空时运行时使用空临时基线；可通过 ``DEVAGENT_SANDBOX__WORKSPACE_MOUNT``
指定已有代码目录。

## 已知限制（不要假装它不存在）

本仓库**还没有实现补丁应用**（Coder 产出的 diff 目前只被渲染成文本）。
因此沙箱里跑的是「未应用本次改动」的代码：测试证据能反映**基线**行为，
不能反映本次改动是否正确。这一点必须在证据文本里如实标注，
否则 Verifier 会把"基线测试通过"误读为"改动已被验证"。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from devagent.config import Settings
from devagent.logging_config import get_logger
from devagent.tools.sandbox import SandboxExecutor, build_sandbox
from devagent.tools.test_runner import SandboxTestRunner

logger = get_logger(__name__)


@dataclass(slots=True)
class TestRuntime:
    """一套可用的测试执行环境。"""

    sandbox: SandboxExecutor
    runner: SandboxTestRunner
    workspace: Path
    owns_workspace: bool
    """True 表示工作区是本进程创建的一次性临时目录，关闭时应删除。"""

    @property
    def isolation(self) -> str:
        """当前隔离级别（容器 / 本地子进程），供日志与自检使用。"""
        return getattr(self.sandbox, "isolation_level", type(self.sandbox).__name__)

    async def aclose(self) -> None:
        await self.sandbox.aclose()
        if self.owns_workspace:
            shutil.rmtree(self.workspace, ignore_errors=True)


def build_test_runtime(settings: Settings, *, workspace_root: str = "") -> TestRuntime:
    """按配置装配测试运行时。

    Args:
        settings: 全局配置。
        workspace_root: 显式指定的工作区（优先于配置）。
    """
    configured = (workspace_root or settings.sandbox.workspace_mount or "").strip()
    if configured:
        workspace = Path(configured)
        workspace.mkdir(parents=True, exist_ok=True)
        owns = False
    else:
        workspace = Path(tempfile.mkdtemp(prefix="devagent-tests-"))
        owns = True

    sandbox = build_sandbox(
        settings.sandbox,
        workspace_root=str(workspace),
        allow_local_fallback=settings.sandbox.allow_local_fallback,
    )
    runner = SandboxTestRunner(
        sandbox,
        workspace_root=str(workspace),
        timeout=settings.sandbox.timeout_seconds,
    )
    logger.info(
        "test_runtime_ready",
        workspace=str(workspace),
        isolation=getattr(sandbox, "isolation_level", type(sandbox).__name__),
        owns_workspace=owns,
    )
    return TestRuntime(
        sandbox=sandbox,
        runner=runner,
        workspace=workspace,
        owns_workspace=owns,
    )


__all__ = ["TestRuntime", "build_test_runtime"]
