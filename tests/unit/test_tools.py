"""工具层单元测试：沙箱、测试运行器、代码索引。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from devagent.tools import (
    CodeIndex,
    LocalProcessSandbox,
    PytestOutputParser,
    SandboxPolicy,
    SandboxTestRunner,
)


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

    async def test_python_resolved_to_current_interpreter(self) -> None:
        """`python` 应被解析为当前解释器，避免 PATH 不一致。"""
        sandbox = LocalProcessSandbox()
        result = await sandbox.run(["python", "-c", "import sys; print(sys.executable)"])
        assert (
            result.stdout.strip().replace("\\", "/").lower()
            == sys.executable.replace("\\", "/").lower()
        )


class TestPytestOutputParser:
    def test_parses_passed_failed(self) -> None:
        p, f, e = PytestOutputParser.parse("3 passed, 1 failed in 0.42s")
        assert (p, f, e) == (3, 1, 0)

    def test_parses_pass_only(self) -> None:
        p, f, e = PytestOutputParser.parse("5 passed in 1.20s")
        assert (p, f, e) == (5, 0, 0)

    def test_parses_with_errors(self) -> None:
        p, f, e = PytestOutputParser.parse("2 passed, 1 failed, 3 errors in 0.5s")
        assert (p, f, e) == (2, 1, 3)

    def test_parses_no_tests(self) -> None:
        p, f, e = PytestOutputParser.parse("no tests ran in 0.01s")
        assert (p, f, e) == (0, 0, 0)

    def test_parses_unordered_counts(self) -> None:
        p, f, e = PytestOutputParser.parse("1 failed, 4 passed")
        assert (p, f, e) == (4, 1, 0)

    def test_detects_collection_error(self) -> None:
        assert PytestOutputParser.has_collection_error("", "ERROR collecting tests/test_x.py")
        assert PytestOutputParser.has_collection_error("errors during collection", "")
        assert not PytestOutputParser.has_collection_error("3 passed", "")


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
