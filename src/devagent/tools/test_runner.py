"""测试执行器：桥接 Tester Agent 与沙箱。

职责：
1. 把 Tester 生成的测试文件写入工作区；
2. 在沙箱中执行 pytest；
3. 解析 pytest 输出，产出结构化的 ``TestRunOutcome``。

这是「测试即事实」原则的执行载体——系统只采信这里产出的结果。

安全约束（测试文件的内容与路径都由**模型**给出）：

- 写入根目录默认是**本实例专属的一次性临时目录**，绝不是 ``Path.cwd()``：
  否则模型只要给出 ``{"path": "src/devagent/tools/sandbox.py"}`` 就能在宿主上
  静默改写仓库源码；
- 根目录下**已存在**的文件一律拒绝覆盖（本次运行自己写入的除外，便于重试）；
- 只接受安全的相对 ``.py`` 路径，以 ``-`` 开头的一律拒绝
  （``--rootdir=..`` / ``-p`` / ``-c/etc/passwd`` 这类字符串会作为
  pytest **选项**进入 argv）；
- 只采信 pytest 打印在 **stdout** 的摘要：stderr 可以被模型生成的
  ``conftest.py`` 任意伪造（往 stderr 写 ``128 passed in 0.01s``）；
- pytest 退出码非 0 时，outcome 绝不报告成功。
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devagent.agents.tester import TestRunOutcome
from devagent.logging_config import get_logger
from devagent.tools.sandbox import SandboxExecutor

logger = get_logger(__name__)

# pytest 的摘要顺序不固定（``1 failed, 4 passed`` 与 ``4 passed, 1 failed`` 都合法），
# 因此**不能**用「passed 必须在最前」的有序正则去匹配——那会在第一个分组命中后
# 提前返回，导致后续计数丢失。改为逐项独立捕获，再按类别归并。
_COUNT_PATTERN = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed)",
    re.IGNORECASE,
)
# 用于识别「汇总行」。汇总行特征是：包含计数词，且**不含** ``::``（逐条输出形如
# ``tests/test_x.py::test_a PASSED``），也不含进度点阵 ``[ 50%]``。
_SUMMARY_COUNT_RE = re.compile(r"\d+\s+(?:passed|failed|error)", re.IGNORECASE)


@dataclass(slots=True)
class PytestOutputParser:
    """解析 pytest 的输出摘要。

    支持形如::

        3 passed, 1 failed, 2 errors in 0.42s
        5 passed in 1.20s
        no tests ran in 0.01s

    **只解析 stdout**：stderr 的内容可以被被测代码（模型生成的
    ``conftest.py`` / 测试用例）任意写入，据此计数等于把伪造证据当事实。
    """

    @staticmethod
    def parse(stdout: str, stderr: str = "") -> tuple[int, int, int]:
        """返回 (passed, failed, errors)。

        实现要点：pytest 摘要的各项顺序不固定，且同一段文本里可能先出现
        「逐条结果行」再出现「汇总行」。这里只取**最后一行含计数的摘要行**，
        再对其中的各项独立捕获后归并，从而对顺序完全免疫。

        Args:
            stdout: pytest 的标准输出——**唯一**的计数来源。
            stderr: 仅为兼容旧调用方保留，不参与计数（仅记一条 debug 日志）。
        """
        if stderr:
            logger.debug("pytest_stderr_ignored_for_counts", stderr_len=len(stderr))

        # 优先在「最后一条摘要行」上解析；找不到再退回全文最后一次出现。
        summary_line = ""
        for line in reversed(stdout.splitlines()):
            if _SUMMARY_COUNT_RE.search(line):
                summary_line = line
                break
        haystack = summary_line or stdout

        counts: dict[str, int] = {}
        for match in _COUNT_PATTERN.finditer(haystack):
            kind = match.group("kind").lower()
            if kind == "error":
                kind = "errors"
            counts[kind] = counts.get(kind, 0) + int(match.group("count"))

        return counts.get("passed", 0), counts.get("failed", 0), counts.get("errors", 0)

    @staticmethod
    def has_collection_error(stdout: str, stderr: str) -> bool:
        """判断是否发生收集错误（如语法错误导致测试根本无法运行）。"""
        text = f"{stdout}\n{stderr}"
        return "errors during collection" in text or "ERROR collecting" in text


@dataclass(slots=True)
class _WriteReport:
    """测试文件写入结果（写入 / 拒绝覆盖 / 路径非法）。"""

    written: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.refused:
            return "拒绝覆盖已存在的文件（可能是仓库既有文件）：" + ", ".join(self.refused)
        if self.rejected:
            return "测试文件路径不安全，未执行：" + ", ".join(self.rejected)
        return "测试文件内容为空，未执行"


class SandboxTestRunner:
    """基于沙箱的测试执行器。

    用法::

        runner = SandboxTestRunner(sandbox, workspace_root="/tmp/ws")
        outcome = await runner.run_tests(test_files)

    未显式指定 ``workspace_root`` 时使用本实例专属的一次性临时目录
    （见 :attr:`workspace_root`），调用方可用 :meth:`cleanup` 释放。
    """

    def __init__(
        self,
        sandbox: SandboxExecutor,
        *,
        workspace_root: str = "",
        timeout: int = 120,
    ) -> None:
        self._sandbox = sandbox
        self._timeout = timeout
        self._explicit_root = bool(workspace_root)
        self._root: Path | None = Path(workspace_root) if workspace_root else None
        self._owned_files: set[Path] = set()

    @property
    def workspace_root(self) -> Path:
        """写入/执行用的根目录（未指定时按需创建一次性临时目录）。"""
        return self._ensure_root()

    def cleanup(self) -> None:
        """删除本实例创建的一次性临时目录；外部传入的 ``workspace_root`` 不动。"""
        if self._root is None or self._explicit_root:
            return
        shutil.rmtree(self._root, ignore_errors=True)
        self._root = None
        self._owned_files.clear()

    async def run_tests(
        self, test_files: list[dict[str, Any]], *, workdir: str = ""
    ) -> TestRunOutcome:
        """写入测试文件并在沙箱中执行。

        Args:
            test_files: ``[{"path": ..., "content": ...}, ...]``。
            workdir: 工作目录（默认使用本实例的根目录）。

        Returns:
            ``TestRunOutcome``；沙箱不可用或执行失败时如实返回
            ``executed=False``，**绝不伪造结果**。

        说明：``outcome.raw`` 里始终带有 ``written_paths``（本次实际写入的
        相对路径，供调用方清理），以及 ``refused_paths`` / ``rejected_paths``
        （被拒绝覆盖 / 路径非法的条目）与 ``exit_code``。
        """
        if not test_files:
            return TestRunOutcome(executed=False, stderr="没有可执行的测试文件")

        target = Path(workdir) if workdir else self._ensure_root()
        try:
            report = self._write_test_files(test_files, target)
        except OSError as exc:
            logger.error("test_file_write_failed", error=str(exc))
            return TestRunOutcome(executed=False, stderr=f"写入测试文件失败：{exc}")

        audit: dict[str, Any] = {
            "written_paths": list(report.written),
            "workdir": str(target),
        }
        if report.refused:
            audit["refused_paths"] = list(report.refused)
        if report.rejected:
            audit["rejected_paths"] = list(report.rejected)

        if not report.written:
            logger.warning(
                "test_files_not_written",
                refused=len(report.refused),
                rejected=len(report.rejected),
            )
            return TestRunOutcome(executed=False, stderr=report.describe(), raw=audit)

        command = [
            "python",
            "-m",
            "pytest",
            *report.written,
            "-v",
            "--tb=short",
            "-p",
            "no:cacheprovider",
        ]
        logger.debug("pytest_command", argv=command, workdir=str(target))
        try:
            result = await self._sandbox.run(command, workdir=str(target), timeout=self._timeout)
        except (PermissionError, RuntimeError) as exc:
            logger.warning("test_execution_unavailable", error=str(exc))
            return TestRunOutcome(executed=False, stderr=f"沙箱不可用：{exc}", raw=audit)

        audit["exit_code"] = result.exit_code
        audit["timed_out"] = result.timed_out
        audit["truncated"] = result.truncated

        if result.timed_out:
            return TestRunOutcome(
                executed=False,
                stdout=result.stdout,
                stderr=result.stderr or f"测试执行超时（{self._timeout}s）",
                duration_ms=result.duration_ms,
                raw=audit,
            )

        if PytestOutputParser.has_collection_error(result.stdout, result.stderr):
            # 收集失败意味着测试根本没跑起来，必须如实反映
            audit["collection_error"] = True
            return TestRunOutcome(
                executed=True,
                passed=0,
                failed=0,
                errors=1,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_ms=result.duration_ms,
                raw=audit,
            )

        if result.exit_code == 127:
            return TestRunOutcome(
                executed=False,
                stdout=result.stdout,
                stderr=result.stderr or "测试命令不可用（127）",
                duration_ms=result.duration_ms,
                raw=audit,
            )

        # 只信 stdout：stderr 里的计数可能是模型伪造的
        passed, failed, errors = PytestOutputParser.parse(result.stdout)
        if result.exit_code != 0:
            # 退出码非 0 就是「这次运行没有成功」。即使 stdout 写着 N passed
            # （崩溃、被强杀、插件输出等），也不允许 all_passed 为真。
            audit["nonzero_exit"] = True
            if failed == 0 and errors == 0:
                errors = 1
                audit["nonzero_exit_without_failures"] = True

        return TestRunOutcome(
            executed=result.exit_code != 0 or passed + failed + errors > 0,
            passed=passed,
            failed=failed,
            errors=errors,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            raw=audit,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _ensure_root(self) -> Path:
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="devagent-testrun-"))
            logger.info("test_runner_temp_root_created", root=str(self._root))
        return self._root

    def _write_test_files(self, test_files: list[dict[str, Any]], root: Path) -> _WriteReport:
        """写入测试文件，返回写入/拒绝明细。

        安全：拒绝绝对路径与目录穿越；拒绝覆盖已存在的文件（防止模型用
        「测试文件」的路径改写仓库源码）。
        """
        report = _WriteReport()
        root_resolved = root.resolve()
        for spec in test_files:
            rel = str(spec.get("path") or "").strip()
            content = str(spec.get("content") or "")
            if not rel or not content.strip():
                continue
            if not self._is_safe_relative_path(rel):
                logger.warning("unsafe_test_path_rejected", path=rel)
                report.rejected.append(rel)
                continue

            target = (root / rel).resolve()
            try:
                target.relative_to(root_resolved)
            except ValueError:
                logger.warning("path_traversal_rejected", path=rel)
                report.rejected.append(rel)
                continue

            if target.exists() and target not in self._owned_files:
                # 覆盖仓库既有文件是本模块最危险的失败模式：宁可跳过并上报
                logger.warning("existing_file_overwrite_refused", path=rel, root=str(root))
                report.refused.append(rel)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self._owned_files.add(target)
            report.written.append(rel)
        return report

    @staticmethod
    def _is_safe_relative_path(path: str) -> bool:
        """路径白名单：相对、``.py`` 结尾、且不以 ``-`` 开头。

        仅拒绝 ``/`` / ``\\`` / ``:`` / ``..`` 是不够的：
        ``--rootdir=..``、``-p``、``-c/etc/passwd`` 都能通过旧检查，
        随后作为 **pytest 选项**进入 argv（例如 ``-p`` 会让 pytest 加载
        指定插件、``-c`` 会改配置文件路径）。
        """
        if not path or "\x00" in path:
            return False
        if path.startswith(("-", "/", "\\")) or ":" in path:
            return False
        parts = Path(path).parts
        if not parts or ".." in parts:
            return False
        return parts[-1].lower().endswith(".py")


__all__ = ["PytestOutputParser", "SandboxTestRunner"]
