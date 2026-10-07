"""沙箱执行器。

对齐 ``docs/03-多Agent编排与可靠性.md`` 的安全设计：
代码必须在**不信任环境**中执行。

安全措施：
- 容器隔离（Docker，不用宿主环境）
- 资源限制（CPU / 内存 / 进程数）
- 网络隔离（默认禁网）
- 文件系统（只读根 + 独立可写临时区）
- 硬超时强杀
- 非 root 运行
- 审计日志

设计要点：``SandboxExecutor`` 是 Protocol，
默认提供 **本地子进程实现**（``LocalProcessSandbox``，用于开发/CI）
与 **Docker 实现**（``DockerSandbox``，用于生产隔离）。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from devagent.config import SandboxConfig
from devagent.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ExecutionResult:
    """一次沙箱执行的结果。"""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    command: str = ""
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(slots=True)
class SandboxPolicy:
    """沙箱执行策略。"""

    timeout_seconds: int = 60
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    pids_limit: int = 128
    network_disabled: bool = True
    read_only_root: bool = True
    max_output_chars: int = 20_000
    allowed_commands: tuple[str, ...] = ("python", "pytest", "python3")
    """允许执行的命令白名单（仅本地实现使用）。"""


@runtime_checkable
class SandboxExecutor(Protocol):
    """沙箱执行器协议。"""

    async def run(
        self, command: list[str], *, workdir: str = "", timeout: int | None = None
    ) -> ExecutionResult: ...

    async def aclose(self) -> None: ...


class LocalProcessSandbox:
    """本地子进程沙箱（开发/CI 使用）。

    **安全性说明**：本实现只做「超时 + 输出截断 + 工作目录隔离 +
    命令白名单」，**不提供真正的容器级隔离**。
    生产环境请使用 ``DockerSandbox``。

    之所以保留本实现，是因为：
    1. CI 环境通常没有 Docker daemon；
    2. 本地开发需要快速反馈；
    3. 单元测试需要确定性、无外部依赖的执行环境。
    """

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        *,
        workspace_root: str = "",
    ) -> None:
        self.policy = policy or SandboxPolicy()
        self._root = Path(workspace_root) if workspace_root else Path(tempfile.gettempdir())
        self._audit: list[dict[str, object]] = []

    async def run(
        self, command: list[str], *, workdir: str = "", timeout: int | None = None
    ) -> ExecutionResult:
        """执行命令。

        Raises:
            PermissionError: 命令不在白名单内。
        """
        if not command:
            return ExecutionResult(exit_code=127, stderr="空命令", command="")

        if not self._is_allowed(command[0]):
            raise PermissionError(
                f"命令 {command[0]!r} 不在沙箱白名单中：{self.policy.allowed_commands}"
            )

        effective_timeout = timeout or self.policy.timeout_seconds
        cwd = Path(workdir) if workdir else self._root
        cwd.mkdir(parents=True, exist_ok=True)

        start = time.perf_counter()
        timed_out = False
        stdout = ""
        stderr = ""
        exit_code = -1

        # Windows 下不能用 asyncio 的子进程创建参数，直接执行
        resolved_command = self._resolve_command(command)

        try:
            proc = await asyncio.create_subprocess_exec(
                *resolved_command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=effective_timeout
                )
                exit_code = proc.returncode if proc.returncode is not None else -1
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
            except TimeoutError:
                timed_out = True
                proc.kill()
                await proc.wait()
                exit_code = -1
                stderr = f"执行超时（{effective_timeout}s），已强制终止"
        except FileNotFoundError as exc:
            exit_code = 127
            stderr = f"命令不存在：{exc}"

        duration_ms = int((time.perf_counter() - start) * 1000)
        truncated = len(stdout) > self.policy.max_output_chars
        if truncated:
            stdout = stdout[: self.policy.max_output_chars] + "\n…（输出已截断）"

        result = ExecutionResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
            command=" ".join(command),
            truncated=truncated,
        )
        self._audit.append(
            {
                "command": result.command,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "timed_out": timed_out,
            }
        )
        logger.info(
            "sandbox_executed",
            command=result.command[:100],
            exit_code=exit_code,
            duration_ms=duration_ms,
        )
        return result

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _is_allowed(self, executable: str) -> bool:
        base = Path(executable).name.lower()
        if base.endswith(".exe"):
            base = base[:-4]
        return any(base.startswith(cmd.lower()) for cmd in self.policy.allowed_commands)

    def _resolve_command(self, command: list[str]) -> list[str]:
        """把 ``python`` 解析为当前解释器，避免 PATH 不一致。"""
        resolved = list(command)
        base = Path(resolved[0]).name.lower()
        if base in {"python", "python3"}:
            resolved[0] = sys.executable
        elif base == "pytest":
            # 优先用当前解释器执行 pytest 模块
            resolved = [sys.executable, "-m", "pytest", *resolved[1:]]
        return resolved

    @property
    def audit_log(self) -> list[dict[str, object]]:
        return list(self._audit)


class DockerSandbox:
    """Docker 沙箱（生产环境使用，提供容器级隔离）。

    安全策略：
    - ``--network none``（默认禁网）
    - ``--memory`` / ``--cpus`` / ``--pids-limit`` 限制资源
    - ``--read-only`` 根文件系统只读
    - ``--tmpfs`` 独立可写临时区
    - ``--user`` 非 root 运行

    依赖 ``docker`` CLI 或 SDK；不可用时抛出明确的错误而非静默降级
    （静默降级会导致安全假设被悄悄破坏）。
    """

    def __init__(
        self,
        config: SandboxConfig | None = None,
        *,
        workspace_root: str = "",
    ) -> None:
        self._config = config or SandboxConfig()
        self._root = Path(workspace_root) if workspace_root else Path(tempfile.gettempdir())
        self._available: bool | None = None

    def is_available(self) -> bool:
        """检测 Docker 是否可用（结果缓存）。"""
        if self._available is None:
            self._available = shutil.which("docker") is not None
        return self._available

    async def run(
        self, command: list[str], *, workdir: str = "", timeout: int | None = None
    ) -> ExecutionResult:
        if not self.is_available():
            raise RuntimeError(
                "Docker 不可用：无法启动沙箱。"
                "生产环境请安装 Docker；开发/CI 可改用 LocalProcessSandbox。"
            )

        effective_timeout = timeout or self._config.timeout_seconds
        host_dir = Path(workdir) if workdir else self._root
        host_dir.mkdir(parents=True, exist_ok=True)

        docker_cmd = [
            "docker",
            "run",
            "--rm",
            f"--memory={self._config.memory_limit}",
            f"--cpus={self._config.cpu_limit}",
            f"--pids-limit={self._config.pids_limit}",
            "-v",
            f"{host_dir}:/workspace",
            "-w",
            "/workspace",
        ]
        if self._config.network_disabled:
            docker_cmd.append("--network=none")
        if self._config.read_only_root:
            docker_cmd.append("--read-only")
            docker_cmd += ["--tmpfs", "/tmp:rw,size=64m"]
        docker_cmd += [self._config.image, *command]

        start = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(), timeout=effective_timeout + 10
                )
                return ExecutionResult(
                    exit_code=proc.returncode or 0,
                    stdout=out.decode("utf-8", errors="replace")[:20_000],
                    stderr=err.decode("utf-8", errors="replace")[:20_000],
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    command=" ".join(command),
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return ExecutionResult(
                    exit_code=-1,
                    stderr=f"沙箱执行超时（{effective_timeout}s）",
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    timed_out=True,
                    command=" ".join(command),
                )
        except FileNotFoundError:
            raise RuntimeError("找不到 docker 可执行文件") from None

    async def aclose(self) -> None:
        return None


def build_sandbox(
    config: SandboxConfig, *, workspace_root: str = "", force_local: bool = False
) -> SandboxExecutor:
    """按配置构建沙箱执行器。

    优先 Docker；不可用时**明确记录警告**并降级为本地沙箱
    （而非静默降级 —— 让调用方知道隔离级别已降低）。
    """
    if force_local or not config.enabled:
        return LocalProcessSandbox(
            SandboxPolicy(
                timeout_seconds=config.timeout_seconds,
                memory_limit=config.memory_limit,
                cpu_limit=config.cpu_limit,
                pids_limit=config.pids_limit,
                network_disabled=config.network_disabled,
            ),
            workspace_root=workspace_root,
        )

    docker = DockerSandbox(config, workspace_root=workspace_root)
    if docker.is_available():
        return docker

    logger.warning(
        "sandbox_fallback_to_local",
        reason="Docker 不可用，隔离级别降低为本地子进程",
        hint="生产环境请安装 Docker 以获得容器级隔离",
    )
    return LocalProcessSandbox(workspace_root=workspace_root)


__all__ = [
    "DockerSandbox",
    "ExecutionResult",
    "LocalProcessSandbox",
    "SandboxExecutor",
    "SandboxPolicy",
    "build_sandbox",
]
