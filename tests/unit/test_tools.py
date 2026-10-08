"""工具层单元测试：沙箱、测试运行器、代码索引。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from devagent.config import SandboxConfig
from devagent.tools import (
    CodeIndex,
    DockerSandbox,
    ExecutionResult,
    LocalProcessSandbox,
    PytestOutputParser,
    SandboxPolicy,
    SandboxTestRunner,
    build_sandbox,
)
from devagent.tools.sandbox import build_docker_argv


class _RecordingSandbox:
    """沙箱替身：记录收到的 argv，不真的执行任何进程。"""

    def __init__(self, result: ExecutionResult | None = None) -> None:
        self.commands: list[list[str]] = []
        self._result = result or ExecutionResult(
            exit_code=0, stdout="1 passed in 0.01s\n", command="pytest"
        )

    async def run(
        self, command: list[str], *, workdir: str = "", timeout: int | None = None
    ) -> ExecutionResult:
        self.commands.append(list(command))
        return self._result

    async def aclose(self) -> None:
        return None

    @property
    def isolation_level(self) -> str:
        return "fake"

    @property
    def is_containerized(self) -> bool:
        return False

    @property
    def unenforced_limits(self) -> tuple[str, ...]:
        return ()


def _make_reader(data: bytes, *, eof: bool) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    if data:
        reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


class _FakeProcess:
    """子进程替身（供 Docker 路径测试使用，不真的起 docker）。"""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        eof: bool = True,
        kill_exit_code: int = -9,
    ) -> None:
        self.stdout = _make_reader(stdout, eof=eof)
        self.stderr = _make_reader(stderr, eof=eof)
        self.pid = 999_999
        self.returncode: int | None = exit_code if eof else None
        self._kill_exit_code = kill_exit_code
        self.killed = False

    async def wait(self) -> int:
        if self.returncode is None:
            # 模拟「被强杀后随即退出」，避免测试等满 grace 时间
            await asyncio.sleep(0.05)
            self.returncode = self._kill_exit_code
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = self._kill_exit_code


class TestSandboxPolicy:
    def test_command_whitelist(self) -> None:
        sandbox = LocalProcessSandbox(SandboxPolicy(allowed_commands=("python",)))

        async def _run() -> None:
            with pytest.raises(PermissionError, match="白名单"):
                await sandbox.run(["rm", "-rf", "/"])

        import asyncio

        asyncio.run(_run())

    def test_empty_command_rejected(self) -> None:
        import asyncio

        sandbox = LocalProcessSandbox()
        result = asyncio.run(sandbox.run([]))
        assert result.exit_code == 127


class TestSandboxExecution:
    async def test_executes_simple_command(self) -> None:
        sandbox = LocalProcessSandbox()
        result = await sandbox.run(["python", "-c", "print('hello')"])
        assert result.exit_code == 0
        assert "hello" in result.stdout

    async def test_captures_nonzero_exit(self) -> None:
        sandbox = LocalProcessSandbox()
        result = await sandbox.run(["python", "-c", "import sys; sys.exit(3)"])
        assert result.exit_code == 3

    async def test_captures_stderr(self) -> None:
        sandbox = LocalProcessSandbox()
        result = await sandbox.run(["python", "-c", "import sys; print('err', file=sys.stderr)"])
        assert "err" in result.stderr

    async def test_timeout_kills_process(self) -> None:
        sandbox = LocalProcessSandbox(SandboxPolicy(timeout_seconds=1))
        result = await sandbox.run(["python", "-c", "import time; time.sleep(10)"], timeout=1)
        assert result.timed_out is True
        assert result.exit_code != 0
        assert "超时" in result.stderr

    @pytest.mark.parametrize("containerized", [False, True], ids=["local", "docker"])
    async def test_cancellation_stops_execution_before_workspace_cleanup(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, containerized: bool
    ) -> None:
        from devagent.tools import sandbox as sandbox_module

        proc = _FakeProcess(eof=False)
        entered = asyncio.Event()
        killed: list[str] = []
        removed: list[str] = []

        async def spawn(*args: Any, **kwargs: Any) -> Any:
            return proc

        async def drain(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()

        async def kill(process: Any, *, reason: str) -> None:
            assert process is proc
            process.kill()
            killed.append(reason)

        async def remove(self: Any, name: str) -> None:
            removed.append(name)

        monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(sandbox_module, "_drain_streams", drain)
        monkeypatch.setattr(sandbox_module, "_kill_process_tree", kill)
        monkeypatch.setattr(DockerSandbox, "is_available", lambda _self: True)
        monkeypatch.setattr(DockerSandbox, "_remove_container", remove)
        sandbox = DockerSandbox() if containerized else LocalProcessSandbox()
        task = asyncio.create_task(sandbox.run(["python", "-c", "pass"], workdir=str(tmp_path)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.killed and len(killed) == 1
        assert bool(removed) is containerized

    async def test_output_truncated(self) -> None:
        sandbox = LocalProcessSandbox(SandboxPolicy(max_output_chars=100))
        result = await sandbox.run(["python", "-c", "print('x' * 5000)"])
        assert result.truncated is True
        assert len(result.stdout) < 200

    async def test_audit_log_recorded(self) -> None:
        sandbox = LocalProcessSandbox()
        await sandbox.run(["python", "-c", "pass"])
        assert len(sandbox.audit_log) == 1
        assert sandbox.audit_log[0]["exit_code"] == 0

    async def test_python_resolved_to_current_interpreter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`python` 应被解析为当前解释器，避免 PATH 不一致。

        缺陷 A：这里也删掉父进程的 UTF-8 变量，确保**沙箱自己**注入的
        UTF-8 环境生效（否则非 ASCII 路径会被 GBK 编码成 ``?``/U+FFFD）。
        """
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        sandbox = LocalProcessSandbox()
        result = await sandbox.run(["python", "-c", "import sys; print(sys.executable)"])
        assert (
            result.stdout.strip().replace("\\", "/").lower()
            == sys.executable.replace("\\", "/").lower()
        )

    # ------------------------------------------------------------------ #
    # 缺陷 A：中文输出必须原样往返（不能被 UTF-8 解码成 U+FFFD）
    # ------------------------------------------------------------------ #

    async def test_chinese_output_round_trips(self) -> None:
        """回归 A：``print("中文测试输出 OK")`` 必须原样返回。"""
        sandbox = LocalProcessSandbox()
        result = await sandbox.run(["python", "-c", "print('中文测试输出 OK')"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "中文测试输出 OK"
        assert "\ufffd" not in result.stdout

    async def test_chinese_output_round_trips_without_parent_utf8_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回归 A：即使父进程没有任何 UTF-8 变量，沙箱也要注入 UTF-8 环境。"""
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        sandbox = LocalProcessSandbox()
        result = await sandbox.run(
            [
                "python",
                "-c",
                "import os, sys; print('中文测试输出 OK'); print(sys.stdout.encoding);"
                " print(os.environ.get('PYTHONIOENCODING', ''))",
            ]
        )
        lines = result.stdout.splitlines()
        assert lines[0] == "中文测试输出 OK"
        assert "\ufffd" not in result.stdout
        assert "utf" in lines[1].lower().replace("-", "").replace("_", "")
        assert lines[2].lower() == "utf-8"

    # ------------------------------------------------------------------ #
    # 缺陷 B：输出上限必须在读取过程中生效（不能先全量缓冲再截断）
    # ------------------------------------------------------------------ #

    async def test_output_cap_is_enforced_while_streaming(self) -> None:
        """回归 B：无限输出的子进程必须被及时终止，且回传文本有界。"""
        sandbox = LocalProcessSandbox(SandboxPolicy(max_output_chars=1_000))
        started = time.monotonic()
        result = await sandbox.run(
            ["python", "-u", "-c", "while True: print('A' * 1000)"], timeout=60
        )
        elapsed = time.monotonic() - started

        assert result.truncated is True, "输出超限必须被标记为 truncated"
        # 上限 1000 字符 + 截断提示
        assert len(result.stdout) <= 1_000 + 64
        assert result.exit_code != 0, "达到输出上限后应终止子进程"
        assert elapsed < 30, f"应在上限处立即返回，而不是等满 60s 超时（实际 {elapsed:.1f}s）"

    # ------------------------------------------------------------------ #
    # 缺陷 C：硬超时必须杀掉整棵进程树（后代进程不能存活）
    # ------------------------------------------------------------------ #

    @pytest.mark.skipif(os.name == "nt", reason="POSIX 进程组语义（Windows 走 taskkill /T）")
    async def test_timeout_kills_descendant_processes(self, tmp_path: Path) -> None:
        """回归 C：只杀直接子进程时，孙进程会继续存活并占用宿主资源。"""
        pid_file = tmp_path / "descendant.pid"
        script = tmp_path / "spawn_descendant.py"
        script.write_text(_descendant_script(), encoding="utf-8")

        sandbox = LocalProcessSandbox(SandboxPolicy(timeout_seconds=5))
        result = await sandbox.run(["python", str(script), str(pid_file)], timeout=5)

        assert result.timed_out is True
        assert pid_file.exists(), "后代进程必须已经启动，否则本条测试无意义"
        pid = int(pid_file.read_text(encoding="utf-8").strip())

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail("后代进程仍然存活：硬超时没有杀掉整棵进程树")


def _descendant_script() -> str:
    """生成「先起一个长命后代、再自己挂起」的测试脚本。

    脚本接收一个参数（后代 pid 文件路径）：后代会把自己的 pid 写进去，
    因此「该文件存在」即证明后代进程确实启动过。
    """
    descendant = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(300)"
    )
    return (
        "import pathlib, subprocess, sys, time\n"
        f"DESCENDANT = {descendant!r}\n"
        "target = sys.argv[1]\n"
        "subprocess.Popen([sys.executable, '-c', DESCENDANT, target])\n"
        "deadline = time.time() + 10\n"
        "while not pathlib.Path(target).exists() and time.time() < deadline:\n"
        "    time.sleep(0.05)\n"
        "time.sleep(300)\n"
    )


class TestDockerHardening:
    """缺陷 D：docstring 承诺的加固参数必须真的出现在 argv 里。"""

    def _argv(self, **kwargs: Any) -> list[str]:
        return build_docker_argv(
            SandboxConfig(), ["python", "-m", "pytest"], host_dir="C:/ws", **kwargs
        )

    def test_docker_argv_contains_isolation_flags(self) -> None:
        argv = self._argv(container_name="devagent-sandbox-abc123")
        assert argv[:3] == ["docker", "run", "--rm"]
        assert "--user=10001:10001" in argv, "缺少非 root 运行（--user）"
        assert "--security-opt=no-new-privileges" in argv
        assert "--cap-drop=ALL" in argv
        assert "--name=devagent-sandbox-abc123" in argv
        assert "--network=none" in argv
        assert "--read-only" in argv
        assert "--tmpfs" in argv
        assert f"--memory={SandboxConfig().memory_limit}" in argv
        assert f"--cpus={SandboxConfig().cpu_limit}" in argv
        assert f"--pids-limit={SandboxConfig().pids_limit}" in argv
        assert "PYTHONIOENCODING=utf-8" in argv, "容器内也要显式 UTF-8 环境（缺陷 A）"
        assert argv[argv.index(SandboxConfig().image) + 1 :] == ["python", "-m", "pytest"]

    def test_docker_argv_workspace_mount_mode_is_explicit(self) -> None:
        rw = self._argv(workspace_read_only=False)
        ro = self._argv(workspace_read_only=True)
        assert rw[rw.index("-v") + 1] == "C:/ws:/workspace"
        assert ro[ro.index("-v") + 1] == "C:/ws:/workspace:ro"

    def test_docker_argv_uses_configured_user(self) -> None:
        """非 root 用户必须来自配置（换镜像也不会悄悄失去该保证）。"""
        argv = build_docker_argv(
            SandboxConfig(user="20002:20002"), ["python", "-c", "pass"], host_dir="C:/ws"
        )
        assert "--user=20002:20002" in argv

    def test_workspace_defaults_to_read_only_without_dedicated_workspace(self) -> None:
        """只有调用方给了专用可写工作区才允许读写挂载。"""
        assert DockerSandbox(SandboxConfig()).workspace_read_only_for("") is True
        assert (
            DockerSandbox(SandboxConfig(), workspace_root="/tmp/dedicated").workspace_read_only_for(
                ""
            )
            is False
        )
        assert DockerSandbox(SandboxConfig()).workspace_read_only_for("/tmp/dedicated") is False
        assert (
            DockerSandbox(
                SandboxConfig(), workspace_root="/tmp/dedicated", workspace_read_only=True
            ).workspace_read_only_for("/tmp/dedicated")
            is True
        )

    async def test_docker_timeout_removes_container(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """回归 C（Docker 侧）：超时后必须 ``docker rm -f``，只杀 CLI 不会停容器。"""
        from devagent.tools import sandbox as sandbox_module

        recorded: list[list[str]] = []

        async def fake_spawn(*argv: Any, **kwargs: Any) -> Any:
            recorded.append([str(item) for item in argv])
            if len(recorded) == 1:
                return _FakeProcess(eof=False)  # docker run：永不 EOF（模拟卡死）
            return _FakeProcess(exit_code=0)

        async def fake_drain(proc: Any, *, cap_bytes: int, timeout: float) -> Any:
            return sandbox_module._DrainResult(
                stdout=b"", stderr=b"", truncated=False, timed_out=True
            )

        monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(sandbox_module, "_drain_streams", fake_drain)
        monkeypatch.setattr(DockerSandbox, "is_available", lambda _self: True)

        docker = DockerSandbox(SandboxConfig(timeout_seconds=1))
        result = await docker.run(["python", "-m", "pytest"], workdir=str(tmp_path))

        assert result.timed_out is True
        run_argv = recorded[0]
        assert run_argv[:2] == ["docker", "run"]
        name = next(item.split("=", 1)[1] for item in run_argv if item.startswith("--name="))
        rm_calls = [argv for argv in recorded if len(argv) > 2 and argv[1] == "rm"]
        assert rm_calls == [["docker", "rm", "-f", name]]


class TestSandboxFactoryIsolation:
    """缺陷 I：构建沙箱时不得丢策略、不得静默降级。"""

    def test_force_local_keeps_configured_policy(self) -> None:
        config = SandboxConfig(
            enabled=False,
            timeout_seconds=7,
            memory_limit="1g",
            cpu_limit=2.0,
            pids_limit=42,
            network_disabled=False,
            read_only_root=False,
        )
        sandbox = build_sandbox(config, force_local=True)
        assert isinstance(sandbox, LocalProcessSandbox)
        assert sandbox.policy.timeout_seconds == 7
        assert sandbox.policy.memory_limit == "1g"
        assert sandbox.policy.cpu_limit == 2.0
        assert sandbox.policy.pids_limit == 42
        assert sandbox.policy.network_disabled is False
        assert sandbox.policy.read_only_root is False

    def test_docker_fallback_keeps_configured_policy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """回归 I：Docker 不可用时降级，但调用方的策略必须完整传递。"""
        monkeypatch.setattr(DockerSandbox, "is_available", lambda _self: False)
        config = SandboxConfig(timeout_seconds=11, memory_limit="256m", cpu_limit=0.5, pids_limit=7)
        sandbox = build_sandbox(config, workspace_root=str(tmp_path))
        assert isinstance(sandbox, LocalProcessSandbox)
        assert sandbox.policy.timeout_seconds == 11
        assert sandbox.policy.memory_limit == "256m"
        assert sandbox.policy.cpu_limit == 0.5
        assert sandbox.policy.pids_limit == 7
        assert sandbox.isolation_level == "local-process"
        assert sandbox.is_containerized is False
        assert "memory_limit" in sandbox.unenforced_limits

    def test_docker_fallback_can_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(DockerSandbox, "is_available", lambda _self: False)
        with pytest.raises(RuntimeError, match="allow_local_fallback"):
            build_sandbox(SandboxConfig(), allow_local_fallback=False)

    def test_config_flag_can_forbid_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``SandboxConfig.allow_local_fallback=False`` 同样生效（与关键字参数取与）。"""
        if "allow_local_fallback" not in SandboxConfig.model_fields:
            pytest.skip(
                "SandboxConfig 尚未提供 allow_local_fallback 字段（由 config.py 所有者补充）"
            )

        monkeypatch.setattr(DockerSandbox, "is_available", lambda _self: False)
        with pytest.raises(RuntimeError):
            build_sandbox(SandboxConfig(allow_local_fallback=False))

    def test_docker_branch_reports_container_isolation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(DockerSandbox, "is_available", lambda _self: True)
        sandbox = build_sandbox(SandboxConfig(), workspace_root=str(tmp_path))
        assert isinstance(sandbox, DockerSandbox)
        assert sandbox.is_containerized is True
        assert sandbox.isolation_level == "docker-container"
        assert sandbox.unenforced_limits == ()

    def test_local_sandbox_reports_unenforced_limits(self) -> None:
        local = LocalProcessSandbox(SandboxPolicy())
        assert local.is_containerized is False
        assert set(local.unenforced_limits) == {
            "memory_limit",
            "cpu_limit",
            "pids_limit",
            "network_disabled",
            "read_only_root",
        }
        permissive = LocalProcessSandbox(
            SandboxPolicy(network_disabled=False, read_only_root=False)
        )
        assert set(permissive.unenforced_limits) == {"memory_limit", "cpu_limit", "pids_limit"}


class TestPytestOutputParser:
    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [
            ("3 passed, 1 failed in 0.42s", (3, 1, 0)),
            ("5 passed in 1.20s", (5, 0, 0)),
            ("2 passed, 1 failed, 3 errors in 0.5s", (2, 1, 3)),
            ("no tests ran in 0.01s", (0, 0, 0)),
            ("1 failed, 4 passed", (4, 1, 0)),
        ],
    )
    def test_parses_summary(self, stdout: str, expected: tuple[int, int, int]) -> None:
        assert PytestOutputParser.parse(stdout) == expected

    def test_detects_collection_error(self) -> None:
        assert PytestOutputParser.has_collection_error("", "ERROR collecting tests/test_x.py")
        assert PytestOutputParser.has_collection_error("errors during collection", "")
        assert not PytestOutputParser.has_collection_error("3 passed", "")

    def test_stderr_counts_are_ignored(self) -> None:
        """回归 G：stderr 是可伪造的，计数只能来自 stdout。"""
        assert PytestOutputParser.parse("1 failed in 0.09s", "128 passed in 0.01s") == (0, 1, 0)


class TestSandboxTestRunner:
    async def test_runs_real_passing_test(self, tmp_path: Path) -> None:
        sandbox = LocalProcessSandbox()
        runner = SandboxTestRunner(sandbox, workspace_root=str(tmp_path), timeout=60)
        outcome = await runner.run_tests(
            [
                {
                    "path": "test_sample.py",
                    "content": "def test_ok():\n    assert 1 + 1 == 2\n",
                }
            ]
        )
        assert outcome.executed is True
        assert outcome.passed == 1
        assert outcome.failed == 0
        assert outcome.all_passed is True

    async def test_runs_real_failing_test(self, tmp_path: Path) -> None:
        sandbox = LocalProcessSandbox()
        runner = SandboxTestRunner(sandbox, workspace_root=str(tmp_path), timeout=60)
        outcome = await runner.run_tests(
            [
                {
                    "path": "test_fail.py",
                    "content": "def test_bad():\n    assert 1 == 2\n",
                }
            ]
        )
        assert outcome.executed is True
        assert outcome.failed == 1
        assert outcome.all_passed is False

    async def test_collection_error_reported(self, tmp_path: Path) -> None:
        """语法错误导致测试无法收集时，必须如实报告为错误。"""
        sandbox = LocalProcessSandbox()
        runner = SandboxTestRunner(sandbox, workspace_root=str(tmp_path), timeout=60)
        outcome = await runner.run_tests(
            [{"path": "test_broken.py", "content": "def test_broken(:\n    pass\n"}]
        )
        assert outcome.executed is True
        assert outcome.all_passed is False

    async def test_empty_test_files_not_executed(self, tmp_path: Path) -> None:
        runner = SandboxTestRunner(LocalProcessSandbox(), workspace_root=str(tmp_path))
        outcome = await runner.run_tests([])
        assert outcome.executed is False

    async def test_blank_content_skipped(self, tmp_path: Path) -> None:
        runner = SandboxTestRunner(LocalProcessSandbox(), workspace_root=str(tmp_path))
        outcome = await runner.run_tests([{"path": "test_x.py", "content": "   "}])
        assert outcome.executed is False

    async def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        """目录穿越必须被拒绝（防止越权写入）。"""
        runner = SandboxTestRunner(LocalProcessSandbox(), workspace_root=str(tmp_path))
        outcome = await runner.run_tests([{"path": "../../evil.py", "content": "print('x')"}])
        assert outcome.executed is False

    async def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        runner = SandboxTestRunner(LocalProcessSandbox(), workspace_root=str(tmp_path))
        outcome = await runner.run_tests([{"path": "C:/evil.py", "content": "print('x')"}])
        assert outcome.executed is False

    # ------------------------------------------------------------------ #
    # 缺陷 F：pytest 选项形态的「路径」必须被拒绝
    # ------------------------------------------------------------------ #

    @pytest.mark.parametrize("path", ["--rootdir=..", "-p", "-c/etc/passwd", "-k", "test.txt"])
    def test_unsafe_paths_rejected_by_whitelist(self, path: str) -> None:
        assert SandboxTestRunner._is_safe_relative_path(path) is False

    @pytest.mark.parametrize("path", ["--rootdir=..", "-p", "-c/etc/passwd"])
    async def test_pytest_option_strings_never_reach_argv(self, tmp_path: Path, path: str) -> None:
        """回归 F：这些字符串会作为 pytest **选项**进入 argv（如 -p 加载插件）。"""
        recorder = _RecordingSandbox()
        runner = SandboxTestRunner(recorder, workspace_root=str(tmp_path))
        outcome = await runner.run_tests(
            [{"path": path, "content": "def test_x():\n    assert True\n"}]
        )
        assert recorder.commands == [], "被拒绝的路径不得进入 pytest argv"
        assert outcome.executed is False
        assert outcome.raw["rejected_paths"] == [path]


class TestTestRunnerWriteIsolation:
    """缺陷 E：模型给出的「测试文件」路径不得改写仓库既有文件。"""

    async def test_existing_file_is_never_overwritten(self, tmp_path: Path) -> None:
        """回归 E：已存在的文件必须保持原样，且拒绝行为要出现在 outcome 里。"""
        victim = tmp_path / "test_victim.py"
        original = "# 仓库原有内容，绝不能被覆盖\n"
        victim.write_text(original, encoding="utf-8")

        runner = SandboxTestRunner(LocalProcessSandbox(), workspace_root=str(tmp_path))
        outcome = await runner.run_tests(
            [{"path": "test_victim.py", "content": "def test_x():\n    assert True\n"}]
        )

        assert victim.read_text(encoding="utf-8") == original
        assert outcome.executed is False
        assert outcome.raw["refused_paths"] == ["test_victim.py"]
        assert "拒绝覆盖" in outcome.stderr

    async def test_repo_source_file_cannot_be_overwritten(self, tmp_path: Path) -> None:
        """回归 E：``src/devagent/tools/sandbox.py`` 这类路径同样被拒绝。"""
        target = tmp_path / "src" / "devagent" / "tools" / "sandbox.py"
        target.parent.mkdir(parents=True)
        target.write_text("ORIGINAL = 1\n", encoding="utf-8")

        runner = SandboxTestRunner(LocalProcessSandbox(), workspace_root=str(tmp_path))
        outcome = await runner.run_tests(
            [{"path": "src/devagent/tools/sandbox.py", "content": "EVIL = 1\n"}]
        )

        assert target.read_text(encoding="utf-8") == "ORIGINAL = 1\n"
        assert outcome.executed is False

    async def test_default_root_is_a_fresh_temp_dir(self) -> None:
        """回归 E：默认根目录是本实例专属临时目录，绝不是 ``Path.cwd()``。"""
        runner = SandboxTestRunner(LocalProcessSandbox(), timeout=60)
        other = SandboxTestRunner(LocalProcessSandbox())
        root = runner.workspace_root
        try:
            assert root != Path.cwd()
            assert root.is_dir()
            assert root.name.startswith("devagent-testrun-")
            assert root.resolve().parent == Path(tempfile.gettempdir()).resolve()
            assert other.workspace_root != root

            outcome = await runner.run_tests(
                [{"path": "test_ok.py", "content": "def test_ok():\n    assert True\n"}]
            )
            assert outcome.all_passed is True
            assert outcome.raw["written_paths"] == ["test_ok.py"]
            assert not (root / "test_ok.py").exists(), "基线不得留下生成文件"
            assert not Path(outcome.raw["workdir"]).exists(), "本次执行目录应自动清理"
        finally:
            runner.cleanup()
            other.cleanup()
        assert not root.exists(), "cleanup 应删除本实例创建的一次性临时目录"

    async def test_workspace_audit_and_cleanup(self, tmp_path: Path) -> None:
        runner = SandboxTestRunner(LocalProcessSandbox(), workspace_root=str(tmp_path))
        outcome = await runner.run_tests(
            [{"path": "tests/test_generated.py", "content": "def test_ok():\n    assert True\n"}]
        )
        assert outcome.raw["written_paths"] == ["tests/test_generated.py"]
        assert outcome.raw["baseline_root"] == str(tmp_path)
        assert outcome.raw["workdir"] != str(tmp_path)
        assert not Path(outcome.raw["workdir"]).exists()
        assert not (tmp_path / "tests" / "test_generated.py").exists()

    async def test_retry_gets_fresh_workspace(self, tmp_path: Path) -> None:
        runner = SandboxTestRunner(LocalProcessSandbox(), workspace_root=str(tmp_path), timeout=60)
        files = [{"path": "test_retry.py", "content": "def test_ok():\n    assert True\n"}]
        first = await runner.run_tests(files)
        second = await runner.run_tests(files)
        assert first.all_passed is True
        assert second.all_passed is True
        assert first.raw["workdir"] != second.raw["workdir"]

    async def test_concurrent_same_path_runs_the_correct_tests(self, tmp_path: Path) -> None:
        class BarrierSandbox(LocalProcessSandbox):
            def __init__(self) -> None:
                super().__init__()
                self.arrivals = 0
                self.ready = asyncio.Event()

            async def run(self, command: list[str], **kwargs: Any) -> ExecutionResult:
                self.arrivals += 1
                if self.arrivals == 2:
                    self.ready.set()
                await asyncio.wait_for(self.ready.wait(), timeout=5)
                return await super().run(command, **kwargs)

        runner = SandboxTestRunner(BarrierSandbox(), workspace_root=str(tmp_path))
        failing, passing = await asyncio.gather(
            runner.run_tests([{"path": "test_shared.py", "content": "def test_a(): assert False"}]),
            runner.run_tests([{"path": "test_shared.py", "content": "def test_b(): assert True"}]),
        )
        assert failing.failed == 1 and not failing.all_passed
        assert passing.passed == 1 and passing.all_passed
        assert "test_b PASSED" not in failing.stdout
        assert failing.raw["workdir"] != passing.raw["workdir"]
        assert list(tmp_path.iterdir()) == []

    async def test_conftest_does_not_leak_to_next_call(self, tmp_path: Path) -> None:
        runner = SandboxTestRunner(LocalProcessSandbox(), workspace_root=str(tmp_path))
        first = await runner.run_tests(
            [
                {
                    "path": "conftest.py",
                    "content": "import pytest\n@pytest.fixture(autouse=True)\ndef fail(): assert False",
                },
                {"path": "test_first.py", "content": "def test_first(): assert True"},
            ]
        )
        second = await runner.run_tests(
            [
                {"path": "test_second.py", "content": "def test_second(): assert True"},
            ]
        )
        assert not first.all_passed
        assert second.all_passed
        assert not (tmp_path / "conftest.py").exists()

    async def test_snapshot_preserves_baseline_code_and_is_removed_on_cancel(
        self, tmp_path: Path
    ) -> None:
        entered = asyncio.Event()
        directories: list[Path] = []
        (tmp_path / "implementation.py").write_text("VALUE = 42", encoding="utf-8")

        class WaitingSandbox(_RecordingSandbox):
            async def run(
                self, command: list[str], *, workdir: str = "", timeout: int | None = None
            ) -> ExecutionResult:
                directory = Path(workdir)
                directories.append(directory)
                assert (directory / "implementation.py").read_text(encoding="utf-8") == "VALUE = 42"
                entered.set()
                await asyncio.Event().wait()
                raise AssertionError("must be cancelled")

        runner = SandboxTestRunner(WaitingSandbox(), workspace_root=str(tmp_path))
        task = asyncio.create_task(
            runner.run_tests(
                [
                    {"path": "test_generated.py", "content": "def test_value(): assert True"},
                ]
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not directories[0].exists()
        assert (tmp_path / "implementation.py").read_text(encoding="utf-8") == "VALUE = 42"


class TestTestRunnerEvidenceIntegrity:
    """缺陷 G：stderr 不可信；退出码非 0 不得报告成功。"""

    async def test_stderr_cannot_forge_a_pass(self, tmp_path: Path) -> None:
        """回归 G：模型用 conftest.py 往 stderr 写假摘要也不能伪造通过。"""
        sandbox = _RecordingSandbox(
            ExecutionResult(
                exit_code=1,
                stdout="1 failed in 0.09s\n",
                stderr="128 passed in 0.01s\n",
                duration_ms=90,
                command="pytest",
            )
        )
        runner = SandboxTestRunner(sandbox, workspace_root=str(tmp_path))
        outcome = await runner.run_tests(
            [{"path": "test_x.py", "content": "def test_x():\n    assert False\n"}]
        )

        assert outcome.passed == 0
        assert outcome.failed == 1
        assert outcome.all_passed is False
        assert outcome.raw["exit_code"] == 1
        assert "128 passed" in outcome.stderr, "stderr 仍保留用于展示"

    async def test_nonzero_exit_code_forbids_success(self, tmp_path: Path) -> None:
        """回归 G：即使 stdout 写着全部通过，退出码非 0 也不能算成功。"""
        sandbox = _RecordingSandbox(
            ExecutionResult(
                exit_code=1,
                stdout="128 passed in 0.01s\n",
                stderr="",
                duration_ms=10,
                command="pytest",
            )
        )
        runner = SandboxTestRunner(sandbox, workspace_root=str(tmp_path))
        outcome = await runner.run_tests(
            [{"path": "test_y.py", "content": "def test_y():\n    assert True\n"}]
        )

        assert outcome.all_passed is False
        assert outcome.errors >= 1
        assert outcome.raw["exit_code"] == 1

    async def test_timeout_is_not_reported_as_executed(self, tmp_path: Path) -> None:
        sandbox = _RecordingSandbox(
            ExecutionResult(exit_code=-1, stderr="执行超时（1s）", timed_out=True, command="pytest")
        )
        runner = SandboxTestRunner(sandbox, workspace_root=str(tmp_path))
        outcome = await runner.run_tests(
            [{"path": "test_z.py", "content": "def test_z():\n    assert True\n"}]
        )
        assert outcome.executed is False
        assert outcome.all_passed is False
        assert outcome.raw["timed_out"] is True


class TestCodeIndex:
    def _make_repo(self, tmp_path: Path) -> Path:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "users.py").write_text(
            '"""User API module."""\n\n\n'
            "class UserService:\n"
            '    """Handles user queries."""\n\n'
            "    def get_users(self, page: int = 1):\n"
            '        """Return paginated users."""\n'
            "        return []\n\n"
            "def parse_pagination(args):\n"
            '    """Parse page and page_size from args."""\n'
            "    return args\n",
            encoding="utf-8",
        )
        (tmp_path / "src" / "auth.py").write_text(
            "import os\n\n\ndef login(token):\n    return True\n",
            encoding="utf-8",
        )
        # 排除目录
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "junk.py").write_text("x = 1", encoding="utf-8")
        return tmp_path

    def test_build_indexes_files(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo).build()
        assert "src/users.py" in index.files
        assert "src/auth.py" in index.files

    def test_excludes_pycache(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo).build()
        assert not any("__pycache__" in p for p in index.files)

    def test_extracts_symbols(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo).build()
        users = index.files["src/users.py"]
        names = {s.name for s in users.symbols}
        assert "UserService" in names
        assert "get_users" in names
        assert "parse_pagination" in names

    def test_identifies_methods_vs_functions(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo).build()
        users = index.files["src/users.py"]
        kinds = {s.name: s.kind for s in users.symbols}
        assert kinds["UserService"] == "class"
        assert kinds["parse_pagination"] == "function"

    def test_extracts_imports(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo).build()
        assert any("import os" in i for i in index.files["src/auth.py"].imports)

    def test_search_ranks_relevant_file(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo).build()
        hits = index.search(["pagination", "users"], top_k=3)
        assert hits
        assert hits[0].file == "src/users.py"

    def test_search_by_symbol_name(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo).build()
        hits = index.search(["UserService"])
        assert hits
        assert "UserService" in hits[0].matched_symbols

    def test_search_empty_keywords(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo).build()
        assert index.search([]) == []
        assert index.search(["   "]) == []

    def test_symbols_named(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo).build()
        found = index.symbols_named("login")
        assert len(found) == 1
        assert found[0].file == "src/auth.py"

    def test_stats(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo).build()
        stats = index.stats()
        assert stats["files"] == 2
        assert stats["classes"] >= 1
        assert stats["parse_mode"] in {"tree-sitter", "regex"}

    def test_auto_build_on_search(self, tmp_path: Path) -> None:
        """未显式 build 时，search 应自动构建索引。"""
        repo = self._make_repo(tmp_path)
        index = CodeIndex(repo)
        hits = index.search(["users"])
        assert hits, "应在首次检索时自动构建索引"

    def test_handles_empty_directory(self, tmp_path: Path) -> None:
        index = CodeIndex(tmp_path).build()
        assert index.files == {}
        assert index.search(["anything"]) == []


class TestCodeIndexSafety:
    """缺陷 H：索引不得越界读文件，也不得切错非 ASCII 源码。"""

    def _write_i18n_repo(self, tmp_path: Path) -> Path:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "i18n.py").write_text(
            '"""模块文档：中文说明。"""\n'
            "\n"
            "import 中文模块\n"
            "\n"
            "\n"
            "def 中文函数():\n"
            '    """中文文档字符串。"""\n'
            "    return 1\n"
            "\n"
            "\n"
            "def parse_pagination(args):\n"
            '    """解析分页参数。"""\n'
            "    return args\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_non_ascii_source_offsets_are_exact(self, tmp_path: Path) -> None:
        """回归 H：tree-sitter 的偏移是 UTF-8 字节偏移，中文文件不得错位。"""
        repo = self._write_i18n_repo(tmp_path)
        index = CodeIndex(repo).build()
        file_index = index.files["src/i18n.py"]

        names = {s.name for s in file_index.symbols}
        assert "中文函数" in names, "含中文的源码按字节偏移切片时符号名被切错"
        assert "parse_pagination" in names, "中文之后的符号同样不能错位"
        assert file_index.imports == ["import 中文模块"]

        if file_index.parse_mode == "tree-sitter":
            docstrings = {s.name: s.docstring for s in file_index.symbols}
            assert docstrings["中文函数"] == "中文文档字符串。"
            assert docstrings["parse_pagination"] == "解析分页参数。"
        assert index.symbols_named("中文函数"), "按名字检索必须命中中文符号"

    def test_oversized_file_is_skipped(self, tmp_path: Path) -> None:
        """回归 H：超过单文件字节上限的文件跳过（不再整文件读入 + 编码翻倍）。"""
        (tmp_path / "big.py").write_text("x = 1\n" + "#" * 4096, encoding="utf-8")
        (tmp_path / "small.py").write_text("x = 1\n", encoding="utf-8")

        index = CodeIndex(tmp_path, max_file_bytes=1024).build()
        assert "small.py" in index.files
        assert "big.py" not in index.files
        assert index.index_file(tmp_path / "big.py") is None

    def test_default_max_file_bytes_is_bounded(self, tmp_path: Path) -> None:
        from devagent.tools.code_index import DEFAULT_MAX_FILE_BYTES

        assert 0 < DEFAULT_MAX_FILE_BYTES <= 8_000_000
        (tmp_path / "huge.py").write_text("#" * (DEFAULT_MAX_FILE_BYTES + 1), encoding="utf-8")
        index = CodeIndex(tmp_path).build()
        assert index.files == {}

    def test_directory_link_target_is_not_indexed(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """回归 H：目录联接/符号链接不得把仓库外的文件拉进模型上下文。"""
        outside = tmp_path_factory.mktemp("outside")
        (outside / "secret.py").write_text("def leaked():\n    return 1\n", encoding="utf-8")

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "inside.py").write_text("def ok():\n    return 1\n", encoding="utf-8")

        link = repo / "linked"
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0 or not link.exists():
                pytest.skip(f"无法创建目录联接：{completed.stderr or completed.stdout}")
        else:
            os.symlink(outside, link, target_is_directory=True)

        index = CodeIndex(repo).build()
        assert "inside.py" in index.files
        assert not any("secret" in path or "linked" in path for path in index.files)
        assert index.symbols_named("leaked") == []
