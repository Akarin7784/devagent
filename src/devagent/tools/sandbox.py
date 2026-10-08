"""沙箱执行器。

对齐 ``docs/03-多Agent编排与可靠性.md`` 的安全设计：
代码必须在**不信任环境**中执行。

``DockerSandbox``（生产）落地的措施：
- 容器隔离（Docker，不用宿主环境）
- 资源限制（CPU / 内存 / 进程数）
- 网络隔离（默认禁网）
- 文件系统（只读根 + 独立可写临时区；工作区挂载模式**显式**声明）
- 硬超时强杀（杀 docker CLI 进程树 **并** ``docker rm -f <容器名>``）
- 输出硬上限（超过即停读并终止，不把整条流读进宿主内存）
- 非 root 运行（``--user``）
- 最小能力集（``--cap-drop=ALL`` + ``--security-opt=no-new-privileges``）
- 审计日志

``LocalProcessSandbox``（开发/CI）只提供「硬超时 + 进程组强杀 + 输出硬上限 +
工作目录隔离 + 命令白名单」。它**无法**强制内存 / CPU / 进程数 / 网络 / 只读根
等限制，这一点通过 ``unenforced_limits`` 如实上报——不做「假装隔离」。

编码：Windows 子进程默认按 ANSI 代码页（简体中文系统为 GBK）写 stdout。
两个实现都显式给子进程注入 UTF-8 环境（``PYTHONIOENCODING`` / ``PYTHONUTF8``；
Docker 侧通过 ``-e`` 传给容器），并按
「UTF-8 → 系统首选编码 → ``errors="replace"``」的顺序稳健解码，
避免中文测试证据被替换成 U+FFFD。
"""

from __future__ import annotations

import asyncio
import contextlib
import locale
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from devagent.config import SandboxConfig
from devagent.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_OUTPUT_CHARS = 20_000
"""回传文本的默认字符上限（同时决定读取时的硬字节上限）。"""

DEFAULT_CONTAINER_USER = "10001:10001"
"""沙箱镜像内的非 root 用户（对齐 ``docker/sandbox/Dockerfile`` 的 uid 10001）。"""

_TRUNCATION_SUFFIX = "\n…（输出已截断）"
_OUTPUT_READ_CHUNK = 64 * 1024
_KILL_GRACE_SECONDS = 5.0
_DOCKER_CLEANUP_TIMEOUT = 10.0
_IS_WINDOWS = os.name == "nt"
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
_CONTAINER_UTF8_ENV: dict[str, str] = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


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
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
    allowed_commands: tuple[str, ...] = ("python", "pytest", "python3")
    """允许执行的命令白名单（仅本地实现使用）。"""


@runtime_checkable
class SandboxExecutor(Protocol):
    """沙箱执行器协议。"""

    async def run(
        self, command: list[str], *, workdir: str = "", timeout: int | None = None
    ) -> ExecutionResult: ...

    async def aclose(self) -> None: ...

    @property
    def isolation_level(self) -> str:
        """隔离级别标识（``local-process`` / ``docker-container``）。"""
        ...

    @property
    def is_containerized(self) -> bool:
        """是否运行在容器里（调用方可据此断言拿到了什么级别的隔离）。"""
        ...

    @property
    def unenforced_limits(self) -> tuple[str, ...]:
        """本实现**无法**强制执行的策略项（如实上报，绝不假装隔离）。"""
        ...


class LocalProcessSandbox:
    """本地子进程沙箱（开发/CI 使用）。

    **安全性说明**：本实现只做「超时 + 进程组强杀 + 输出硬上限 +
    工作目录隔离 + 命令白名单」，**不提供真正的容器级隔离**。
    配置里的内存 / CPU / 进程数 / 网络 / 只读根限制在这里**不会被强制执行**，
    并可通过 ``unenforced_limits`` 查询。生产环境请使用 ``DockerSandbox``。

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

    @property
    def isolation_level(self) -> str:
        return "local-process"

    @property
    def is_containerized(self) -> bool:
        return False

    @property
    def unenforced_limits(self) -> tuple[str, ...]:
        """本地实现无法强制执行的策略项。"""
        limits = ["memory_limit", "cpu_limit", "pids_limit"]
        if self.policy.network_disabled:
            limits.append("network_disabled")
        if self.policy.read_only_root:
            limits.append("read_only_root")
        return tuple(limits)

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
        truncated = False
        raw_stdout = b""
        raw_stderr = b""
        exit_code = -1

        # Windows 下不能用 asyncio 的子进程创建参数，直接执行
        resolved_command = self._resolve_command(command)

        proc: asyncio.subprocess.Process | None = None
        spawn_error = ""
        try:
            proc = await self._spawn(resolved_command, cwd)
        except FileNotFoundError as exc:
            exit_code = 127
            spawn_error = f"命令不存在：{exc}"
        except OSError as exc:
            exit_code = 126
            spawn_error = f"无法启动命令：{exc}"

        if proc is not None:
            cap_bytes = max(int(self.policy.max_output_chars), 1) * 4
            try:
                drain = await _drain_streams(
                    proc, cap_bytes=cap_bytes, timeout=float(effective_timeout)
                )
            except asyncio.CancelledError:
                await _kill_process_tree(proc, reason="cancelled")
                raise
            except OSError as exc:  # 管道读取异常不应让沙箱崩掉
                logger.warning("sandbox_drain_failed", error=str(exc))
                drain = _DrainResult(stdout=b"", stderr=b"", truncated=False, timed_out=False)
            raw_stdout = drain.stdout
            raw_stderr = drain.stderr
            truncated = drain.truncated
            timed_out = drain.timed_out
            if drain.timed_out or drain.truncated:
                # 输出超限同样要终止：否则子进程会继续占用宿主资源
                await _kill_process_tree(
                    proc, reason="timeout" if drain.timed_out else "output_cap"
                )
            exit_code = await _wait_for_exit(proc)

        stdout = _decode_output(raw_stdout)
        stderr = _decode_output(raw_stderr)
        if spawn_error:
            stderr = spawn_error
        if timed_out:
            stderr = _with_note(stderr, f"执行超时（{effective_timeout}s），已强制终止")

        if len(stdout) > self.policy.max_output_chars:
            stdout = stdout[: self.policy.max_output_chars] + _TRUNCATION_SUFFIX
            truncated = True
        if len(stderr) > self.policy.max_output_chars:
            stderr = stderr[: self.policy.max_output_chars] + _TRUNCATION_SUFFIX
            truncated = True

        duration_ms = int((time.perf_counter() - start) * 1000)
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
                "truncated": truncated,
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

    async def _spawn(self, argv: list[str], cwd: Path) -> asyncio.subprocess.Process:
        """启动子进程：注入 UTF-8 环境，并让它自成进程组/会话。

        自成会话（POSIX ``start_new_session`` / Windows
        ``CREATE_NEW_PROCESS_GROUP``）是「硬超时能杀掉整棵进程树」的前提。
        """
        env = _child_env()
        if _IS_WINDOWS:
            return await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_CREATE_NEW_PROCESS_GROUP,
            )
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

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

    ``run()`` 实际执行的 argv 与 :func:`build_docker_argv` 完全一致（文档即实现）::

        docker run --rm --name=<唯一容器名> \\
            --memory=<cfg.memory_limit> --cpus=<cfg.cpu_limit> \\
            --pids-limit=<cfg.pids_limit> --user=<非 root uid:gid> \\
            --security-opt=no-new-privileges --cap-drop=ALL \\
            [--network=none] [--read-only --tmpfs /tmp:rw,size=64m] \\
            -e PYTHONIOENCODING=utf-8 -e PYTHONUTF8=1 \\
            -v <host_dir>:/workspace[:ro] -w /workspace \\
            <cfg.image> <command...>

    - 非 root：``--user`` 默认取镜像内的 uid（``10001:10001``），可用
      ``user`` 参数覆盖；
    - 工作区挂载模式是**显式**的：``workspace_read_only`` 为 True 时挂载
      ``:ro``；默认（未显式指定时）只有在调用方提供了**专用工作区**
      （构造时给了 ``workspace_root``，或本次传入 ``workdir``）才允许读写挂载，
      否则按只读挂载——不把任意仓库目录以读写方式交给不信任代码；
    - 超时/输出超限时：先杀 docker CLI 的进程树，再 ``docker rm -f <name>``。
      只杀 CLI 并不会停止容器，容器会继续写挂载目录；
    - 依赖 ``docker`` CLI；不可用时抛出明确的错误而非静默降级
      （静默降级会导致安全假设被悄悄破坏）。
    """

    def __init__(
        self,
        config: SandboxConfig | None = None,
        *,
        workspace_root: str = "",
        workspace_read_only: bool | None = None,
        user: str = "",
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        self._config = config or SandboxConfig()
        self._root = Path(workspace_root) if workspace_root else Path(tempfile.gettempdir())
        self._explicit_root = bool(workspace_root)
        self._workspace_read_only = workspace_read_only
        self._user = user
        self._max_output_chars = max_output_chars
        self._available: bool | None = None

    @property
    def isolation_level(self) -> str:
        return "docker-container"

    @property
    def is_containerized(self) -> bool:
        return True

    @property
    def unenforced_limits(self) -> tuple[str, ...]:
        """容器运行时已强制全部策略项，故为空。"""
        return ()

    def is_available(self) -> bool:
        """检测 Docker 是否可用（结果缓存）。"""
        if self._available is None:
            self._available = shutil.which("docker") is not None
        return self._available

    def workspace_read_only_for(self, workdir: str = "") -> bool:
        """解析工作区挂载模式。

        优先级：构造参数 → 配置字段（``SandboxConfig.workspace_read_only``，
        由 config.py 的所有者补充）→ 默认策略（只有专用工作区才可写挂载）。
        """
        if self._workspace_read_only is not None:
            return self._workspace_read_only
        configured = getattr(self._config, "workspace_read_only", None)
        if isinstance(configured, bool):
            return configured
        return not (bool(workdir) or self._explicit_root)

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

        container_name = f"devagent-sandbox-{uuid.uuid4().hex[:12]}"
        docker_cmd = build_docker_argv(
            self._config,
            list(command),
            host_dir=host_dir,
            container_name=container_name,
            user=self._user,
            workspace_read_only=self.workspace_read_only_for(workdir),
        )

        start = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                env=_child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError("找不到 docker 可执行文件") from None

        cap_bytes = max(int(self._max_output_chars), 1) * 4
        try:
            drain = await _drain_streams(
                proc, cap_bytes=cap_bytes, timeout=float(effective_timeout) + 10
            )
        except asyncio.CancelledError:
            await _kill_process_tree(proc, reason="docker_cancelled")
            await self._remove_container(container_name)
            raise
        if drain.timed_out or drain.truncated:
            # 杀 docker CLI ≠ 停止容器：必须显式清理容器，否则它会继续写挂载目录
            await _kill_process_tree(
                proc, reason="docker_timeout" if drain.timed_out else "docker_output_cap"
            )
            await self._remove_container(container_name)

        exit_code = await _wait_for_exit(proc)

        stdout = _decode_output(drain.stdout)
        stderr = _decode_output(drain.stderr)
        truncated = drain.truncated
        if drain.timed_out:
            stderr = _with_note(stderr, f"沙箱执行超时（{effective_timeout}s），容器已强制清理")
            exit_code = -1 if exit_code == 0 else exit_code
        if len(stdout) > self._max_output_chars:
            stdout = stdout[: self._max_output_chars] + _TRUNCATION_SUFFIX
            truncated = True
        if len(stderr) > self._max_output_chars:
            stderr = stderr[: self._max_output_chars] + _TRUNCATION_SUFFIX
            truncated = True

        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.perf_counter() - start) * 1000),
            timed_out=drain.timed_out,
            command=" ".join(command),
            truncated=truncated,
        )

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _remove_container(self, name: str) -> None:
        """``docker rm -f <name>``：尽力而为，docker 不在也不抛异常。"""
        if not name:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.warning("docker_container_cleanup_failed", container=name, error=str(exc))
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_DOCKER_CLEANUP_TIMEOUT)
            return
        logger.warning("docker_container_cleanup_timeout", container=name)


def build_docker_argv(
    config: SandboxConfig,
    command: list[str],
    *,
    host_dir: str | Path,
    container_name: str = "",
    user: str = "",
    workspace_read_only: bool = False,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """构造 ``docker run`` 的 argv。

    单独抽成函数是为了可测试：无需 Docker daemon 即可断言
    「文档承诺的加固参数」确实出现在命令行里。
    """
    mount_mode = ":ro" if workspace_read_only else ""
    argv = [
        "docker",
        "run",
        "--rm",
        f"--memory={config.memory_limit}",
        f"--cpus={config.cpu_limit}",
        f"--pids-limit={config.pids_limit}",
        f"--user={user or _configured_user(config) or DEFAULT_CONTAINER_USER}",
        "--security-opt=no-new-privileges",
        "--cap-drop=ALL",
    ]
    if container_name:
        argv.append(f"--name={container_name}")
    if config.network_disabled:
        argv.append("--network=none")
    if config.read_only_root:
        argv += ["--read-only", "--tmpfs", "/tmp:rw,size=64m"]
    for key, value in (_CONTAINER_UTF8_ENV if env is None else env).items():
        argv += ["-e", f"{key}={value}"]
    argv += ["-v", f"{host_dir}:/workspace{mount_mode}", "-w", "/workspace"]
    argv += [config.image, *command]
    return argv


def build_sandbox(
    config: SandboxConfig,
    *,
    workspace_root: str = "",
    force_local: bool = False,
    allow_local_fallback: bool = True,
    workspace_read_only: bool | None = None,
) -> SandboxExecutor:
    """按配置构建沙箱执行器。

    隔离级别：
    - Docker 可用 → ``DockerSandbox``（容器级隔离，``is_containerized`` 为 True）；
    - Docker 不可用 → 由 ``allow_local_fallback`` 决定：为 True（默认，保持
      开发/CI 流程可用）时降级为 ``LocalProcessSandbox`` 并打 WARNING，明确
      记录隔离级别与无法强制执行的限制；为 False 时**快速失败**（抛
      ``RuntimeError``），绝不静默降级。

    无论走哪个分支，``config`` 里的超时/内存/CPU/进程数/网络/只读根策略都会
    传递下去（本地实现会在 ``unenforced_limits`` 中如实标注哪些它做不到）。

    注意：``SandboxConfig`` 目前没有 ``allow_local_fallback`` 字段，
    因此该开关以关键字参数形式提供；config.py 的所有者补上字段后
    ``config.allow_local_fallback=False`` 也会被识别（两者取逻辑与）。
    """
    policy = _policy_from_config(config)

    if force_local or not config.enabled:
        local = LocalProcessSandbox(policy, workspace_root=workspace_root)
        logger.warning(
            "sandbox_local_forced",
            reason="force_local" if force_local else "sandbox.enabled=False",
            isolation_level=local.isolation_level,
            unenforced_limits=",".join(local.unenforced_limits),
        )
        return local

    docker = DockerSandbox(
        config, workspace_root=workspace_root, workspace_read_only=workspace_read_only
    )
    if docker.is_available():
        return docker

    fallback_allowed = allow_local_fallback and bool(getattr(config, "allow_local_fallback", True))
    if not fallback_allowed:
        raise RuntimeError(
            "Docker 不可用且 allow_local_fallback=False："
            "拒绝降级为本地子进程沙箱（避免隔离级别被悄悄降低）。"
        )

    local = LocalProcessSandbox(policy, workspace_root=workspace_root)
    logger.warning(
        "sandbox_fallback_to_local",
        reason="Docker 不可用，隔离级别降低为本地子进程",
        isolation_level=local.isolation_level,
        unenforced_limits=",".join(local.unenforced_limits),
        hint="生产环境请安装 Docker 以获得容器级隔离",
    )
    return local


def _policy_from_config(config: SandboxConfig) -> SandboxPolicy:
    """把 ``SandboxConfig`` 映射为 ``SandboxPolicy``（两个分支共用，避免丢策略）。"""
    return SandboxPolicy(
        timeout_seconds=config.timeout_seconds,
        memory_limit=config.memory_limit,
        cpu_limit=config.cpu_limit,
        pids_limit=config.pids_limit,
        network_disabled=config.network_disabled,
        read_only_root=config.read_only_root,
    )


def _configured_user(config: SandboxConfig) -> str:
    """读取配置里的非 root 用户（config.py 增加该字段后自动生效）。"""
    for field in ("run_as_user", "user"):
        value = getattr(config, field, "")
        if isinstance(value, str) and value:
            return value
    return ""


def _child_env() -> dict[str, str]:
    """子进程环境变量：显式要求 UTF-8 输出（Windows 默认是 ANSI 代码页）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _decode_output(data: bytes) -> str:
    """稳健解码子进程输出：UTF-8 → 系统首选编码 → 替换解码。

    直接 ``decode("utf-8", errors="replace")`` 会把 GBK 编码的中文
    静默变成 U+FFFD，从而污染交给 Verifier 的测试证据。
    """
    if not data:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _with_note(text: str, note: str) -> str:
    """在已有输出后追加说明（没有输出时只返回说明）。"""
    return f"{text.rstrip()}\n{note}" if text.strip() else note


class _StreamCapture:
    """带硬字节上限的流缓冲：超过上限的字节**直接丢弃**，不进内存。"""

    __slots__ = ("_cap", "_chunks", "_size", "truncated")

    def __init__(self, cap: int) -> None:
        self._cap = max(int(cap), 1)
        self._chunks: list[bytes] = []
        self._size = 0
        self.truncated = False

    @property
    def full(self) -> bool:
        return self.truncated

    def feed(self, chunk: bytes) -> None:
        remaining = self._cap - self._size
        if remaining <= 0:
            if chunk:
                self.truncated = True
            return
        take = chunk[:remaining]
        self._chunks.append(take)
        self._size += len(take)
        if len(take) < len(chunk):
            self.truncated = True

    def data(self) -> bytes:
        return b"".join(self._chunks)


@dataclass(slots=True)
class _DrainResult:
    stdout: bytes
    stderr: bytes
    truncated: bool
    timed_out: bool


async def _pump_stream(stream: asyncio.StreamReader | None, capture: _StreamCapture) -> None:
    """读取一条流直到 EOF 或达到硬上限。"""
    if stream is None:
        return
    while not capture.full:
        chunk = await stream.read(_OUTPUT_READ_CHUNK)
        if not chunk:
            break
        capture.feed(chunk)


async def _drain_streams(
    proc: asyncio.subprocess.Process, *, cap_bytes: int, timeout: float
) -> _DrainResult:
    """在「硬字节上限 + 超时」约束下读取子进程输出。

    与 ``proc.communicate()`` 的区别：任一输出流超过 ``cap_bytes`` 立即停止
    读取并返回（由调用方终止进程），因此宿主内存不随子进程输出无限增长。
    """
    out_capture = _StreamCapture(cap_bytes)
    err_capture = _StreamCapture(cap_bytes)
    tasks: set[asyncio.Task[None]] = {
        asyncio.ensure_future(_pump_stream(proc.stdout, out_capture)),
        asyncio.ensure_future(_pump_stream(proc.stderr, err_capture)),
    }
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(float(timeout), 0.0)
    timed_out = False
    try:
        while tasks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            done, pending = await asyncio.wait(
                tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                timed_out = True
                break
            tasks = set(pending)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    logger.debug("sandbox_stream_read_failed", error=str(exc))
            if out_capture.full or err_capture.full:
                break
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    return _DrainResult(
        stdout=out_capture.data(),
        stderr=err_capture.data(),
        truncated=out_capture.truncated or err_capture.truncated,
        timed_out=timed_out,
    )


async def _wait_for_exit(proc: asyncio.subprocess.Process, *, timeout: float = 1.0) -> int:
    """等待子进程退出并返回退出码（等不到就返回 -1，绝不假装成功）。"""
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            return -1
    except (ProcessLookupError, PermissionError, OSError):
        return -1
    return code if code is not None else -1


async def _kill_process_tree(proc: asyncio.subprocess.Process, *, reason: str) -> None:
    """尽力终止子进程**及其后代**；平台不支持时也不抛异常。

    只对直接子进程 ``proc.kill()`` 会漏掉孙进程（例如测试脚本里再起的
    服务进程），它们会继续占用宿主资源。
    """
    if proc.returncode is not None:
        return
    pid = proc.pid
    if _IS_WINDOWS:
        await _taskkill_tree(pid, reason=reason)
    else:
        _kill_process_group(pid, reason=reason)

    try:
        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
        return
    except TimeoutError:
        logger.warning("sandbox_kill_wait_timeout", pid=pid, reason=reason)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        proc.kill()
    with contextlib.suppress(TimeoutError, ProcessLookupError, OSError):
        await asyncio.wait_for(proc.wait(), timeout=1.0)


def _kill_process_group(pid: int, *, reason: str) -> None:
    """POSIX：杀掉整个进程组（子进程以 ``start_new_session`` 启动）。"""
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    if callable(killpg) and callable(getpgid):
        try:
            killpg(getpgid(pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("sandbox_killpg_failed", pid=pid, reason=reason, error=str(exc))
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, sig)


async def _taskkill_tree(pid: int, *, reason: str) -> None:
    """Windows：``taskkill /T /F`` 杀整棵进程树（尽力而为）。"""
    taskkill = shutil.which("taskkill")
    if taskkill is None:
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        candidate = Path(system_root) / "System32" / "taskkill.exe"
        taskkill = str(candidate) if candidate.exists() else None
    if taskkill is None:
        logger.debug("sandbox_taskkill_unavailable", pid=pid, reason=reason)
        return
    try:
        killer = await asyncio.create_subprocess_exec(
            taskkill,
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.debug("sandbox_taskkill_failed", pid=pid, error=str(exc))
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(killer.wait(), timeout=_KILL_GRACE_SECONDS)


__all__ = [
    "DEFAULT_CONTAINER_USER",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DockerSandbox",
    "ExecutionResult",
    "LocalProcessSandbox",
    "SandboxExecutor",
    "SandboxPolicy",
    "build_docker_argv",
    "build_sandbox",
]
