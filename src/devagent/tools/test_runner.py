"""测试执行器：桥接 Tester Agent 与沙箱。

职责：
1. 把 Tester 生成的测试文件写入工作区；
2. 在沙箱中执行 pytest；
3. 解析 pytest 输出，产出结构化的 ``TestRunOutcome``。

这是「测试即事实」原则的执行载体——系统只采信这里产出的结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    """

    @staticmethod
    def parse(stdout: str, stderr: str = "") -> tuple[int, int, int]:
        """返回 (passed, failed, errors)。

        实现要点：pytest 摘要的各项顺序不固定，且同一段文本里可能先出现
        「逐条结果行」再出现「汇总行」。这里只取**最后一行含计数的摘要行**，
        再对其中的各项独立捕获后归并，从而对顺序完全免疫。
        """
        text = f"{stdout}\n{stderr}"

        # 优先在「最后一条摘要行」上解析；找不到再退回全文最后一次出现。
        summary_line = ""
        for line in reversed(text.splitlines()):
            if _SUMMARY_COUNT_RE.search(line):
                summary_line = line
                break
        haystack = summary_line or text

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


class SandboxTestRunner:
    """基于沙箱的测试执行器。

    用法::

        runner = SandboxTestRunner(sandbox, workspace_root="/tmp/ws")
        outcome = await runner.run_tests(test_files)
    """

    def __init__(
        self,
        sandbox: SandboxExecutor,
        *,
        workspace_root: str = "",
        timeout: int = 120,
    ) -> None:
        self._sandbox = sandbox
        self._root = Path(workspace_root) if workspace_root else Path.cwd()
        self._timeout = timeout

    async def run_tests(
        self, test_files: list[dict[str, Any]], *, workdir: str = ""
    ) -> TestRunOutcome:
        """写入测试文件并在沙箱中执行。

        Args:
            test_files: ``[{"path": ..., "content": ...}, ...]``。
            workdir: 工作目录（默认使用构造时的 workspace_root）。

        Returns:
            ``TestRunOutcome``；沙箱不可用或执行失败时如实返回
            ``executed=False``，**绝不伪造结果**。
        """
        if not test_files:
            return TestRunOutcome(executed=False, stderr="没有可执行的测试文件")

        target = Path(workdir) if workdir else self._root
        try:
            written = self._write_test_files(test_files, target)
        except OSError as exc:
            logger.error("test_file_write_failed", error=str(exc))
            return TestRunOutcome(executed=False, stderr=f"写入测试文件失败：{exc}")

        if not written:
            return TestRunOutcome(executed=False, stderr="测试文件内容为空，未执行")

        command = ["python", "-m", "pytest", *written, "-v", "--tb=short", "-p", "no:cacheprovider"]
        try:
            result = await self._sandbox.run(command, workdir=str(target), timeout=self._timeout)
        except (PermissionError, RuntimeError) as exc:
            logger.warning("test_execution_unavailable", error=str(exc))
            return TestRunOutcome(executed=False, stderr=f"沙箱不可用：{exc}")

        if PytestOutputParser.has_collection_error(result.stdout, result.stderr):
            # 收集失败意味着测试根本没跑起来，必须如实反映
            return TestRunOutcome(
                executed=True,
                passed=0,
                failed=0,
                errors=1,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_ms=result.duration_ms,
                raw={"collection_error": True},
            )

        passed, failed, errors = PytestOutputParser.parse(result.stdout, result.stderr)
        executed = passed + failed + errors > 0

        return TestRunOutcome(
            executed=executed,
            passed=passed,
            failed=failed,
            errors=errors,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            raw={"exit_code": result.exit_code, "timed_out": result.timed_out},
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _write_test_files(self, test_files: list[dict[str, Any]], root: Path) -> list[str]:
        """写入测试文件，返回实际写入的相对路径列表。

        安全：拒绝绝对路径与目录穿越，防止越权写入。
        """
        written: list[str] = []
        for spec in test_files:
            rel = str(spec.get("path") or "").strip()
            content = str(spec.get("content") or "")
            if not rel or not content.strip():
                continue
            if not self._is_safe_relative_path(rel):
                logger.warning("unsafe_test_path_rejected", path=rel)
                continue

            target = (root / rel).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                logger.warning("path_traversal_rejected", path=rel)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(rel)
        return written

    @staticmethod
    def _is_safe_relative_path(path: str) -> bool:
        if path.startswith(("/", "\\")) or ":" in path:
            return False
        return ".." not in Path(path).parts


__all__ = ["PytestOutputParser", "SandboxTestRunner"]
