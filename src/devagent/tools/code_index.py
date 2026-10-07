"""代码索引（AST 驱动）。

对齐 ``docs/02`` 的设计：为什么关键信息要放首尾、为什么不能「把整个代码库塞进去」
——成本不可控且注意力被稀释。因此需要**精确召回相关文件**。

本模块用 Tree-sitter 解析源码，构建符号表（类/函数/导入），
支持按符号名或关键词召回最相关的文件片段。

降级策略：Tree-sitter 不可用时退化为正则解析
（准确性下降但不影响系统可用性，且明确标注解析模式）。
"""

from __future__ import annotations

import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from devagent.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_FILE_BYTES = 2_000_000
"""单个源码文件的字节上限：超过则跳过（避免把巨型文件读进模型上下文/内存）。"""

try:  # pragma: no cover - 取决于运行环境是否安装
    import tree_sitter_python as tspython
    from tree_sitter import Language, Node, Parser

    _TS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TS_AVAILABLE = False
    Node = object  # type: ignore[assignment,misc]


# 默认排除的目录（避免索引依赖与构建产物）
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".eggs",
    "htmlcov",
)


@dataclass(slots=True)
class Symbol:
    """代码符号（类 / 函数 / 方法）。"""

    name: str
    kind: str  # class / function / method
    file: str
    start_line: int
    end_line: int
    docstring: str = ""
    parent: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


@dataclass(slots=True)
class FileIndex:
    """单个文件的索引结果。"""

    path: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    lines: int = 0
    parse_mode: str = "tree-sitter"

    def symbol_names(self) -> set[str]:
        return {s.qualified_name for s in self.symbols} | {s.name for s in self.symbols}


@dataclass(slots=True)
class SearchHit:
    """检索命中项。"""

    file: str
    score: float
    matched_symbols: list[str] = field(default_factory=list)
    reason: str = ""


class CodeIndex:
    """代码库索引。

    用法::

        index = CodeIndex("/path/to/repo")
        index.build()
        hits = index.search(["pagination", "users"], top_k=5)
    """

    def __init__(
        self,
        root: str | Path,
        *,
        excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
        max_files: int = 2000,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.root = Path(root).resolve()
        self.excludes = set(excludes)
        self.max_files = max_files
        self.max_file_bytes = max(int(max_file_bytes), 1)
        self.files: dict[str, FileIndex] = {}
        self._built = False
        self._parser: object | None = None

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #

    def build(self) -> CodeIndex:
        """遍历仓库并索引所有 Python 文件。"""
        count = 0
        for path in self._iter_files():
            if count >= self.max_files:
                logger.warning("index_file_limit_reached", limit=self.max_files)
                break
            try:
                fi = self.index_file(path)
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug("index_file_skipped", path=str(path), error=str(exc))
                continue
            if fi is not None:
                self.files[fi.path] = fi
                count += 1
        self._built = True
        logger.info("code_index_built", files=len(self.files), root=str(self.root))
        return self

    def index_file(self, path: Path) -> FileIndex | None:
        """索引单个文件。

        越界路径（符号链接 / Windows 目录联接指向仓库外）、链接本身与超大
        文件一律跳过——索引的内容会进入模型上下文，不能越界读取。
        """
        if not self._is_inside_root(path) or _is_link_or_reparse_point(path):
            logger.debug("index_file_outside_root_skipped", path=str(path))
            return None
        if self._is_oversized(path):
            logger.info("index_file_too_large_skipped", path=str(path), limit=self.max_file_bytes)
            return None
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        rel = str(path.resolve().relative_to(self.root)).replace("\\", "/")
        if _TS_AVAILABLE:
            symbols, imports = self._parse_with_tree_sitter(source)
            mode = "tree-sitter"
        else:
            symbols, imports = self._parse_with_regex(source)
            mode = "regex"

        for symbol in symbols:
            symbol.file = rel

        return FileIndex(
            path=rel,
            symbols=symbols,
            imports=imports,
            lines=source.count("\n") + 1,
            parse_mode=mode,
        )

    # ------------------------------------------------------------------ #
    # 解析
    # ------------------------------------------------------------------ #

    def _get_parser(self) -> object | None:
        if not _TS_AVAILABLE:
            return None
        if self._parser is None:
            language = Language(tspython.language())
            parser = Parser(language)
            self._parser = parser
        return self._parser

    def _parse_with_tree_sitter(self, source: str) -> tuple[list[Symbol], list[str]]:
        parser = self._get_parser()
        if parser is None:
            return self._parse_with_regex(source)

        # tree-sitter 的 start_byte/end_byte 是 **UTF-8 字节偏移**，不是 str 的
        # 字符下标。含中文的源码必须按字节切片再解码，否则符号名/导入/文档
        # 字符串会整体错位（例如期望 '中文' 却得到 "中文'\n"）。
        source_bytes = source.encode("utf-8")
        tree = parser.parse(source_bytes)  # type: ignore[attr-defined]
        symbols: list[Symbol] = []
        imports: list[str] = []

        def visit(node: object, parent: str | None = None) -> None:
            ntype = getattr(node, "type", "")
            if ntype == "class_definition":
                name = _node_text(node, "name", source_bytes)
                if name:
                    symbols.append(
                        Symbol(
                            name=name,
                            kind="class",
                            file="",
                            start_line=node.start_point[0] + 1,  # type: ignore[attr-defined]
                            end_line=node.end_point[0] + 1,  # type: ignore[attr-defined]
                            docstring=_extract_docstring(node, source_bytes),
                        )
                    )
                    parent = name
            elif ntype == "function_definition":
                name = _node_text(node, "name", source_bytes)
                if name:
                    symbols.append(
                        Symbol(
                            name=name,
                            kind="method" if parent else "function",
                            file="",
                            start_line=node.start_point[0] + 1,  # type: ignore[attr-defined]
                            end_line=node.end_point[0] + 1,  # type: ignore[attr-defined]
                            docstring=_extract_docstring(node, source_bytes),
                            parent=parent,
                        )
                    )
            elif ntype in {"import_statement", "import_from_statement"}:
                text = _slice_utf8(
                    source_bytes,
                    node.start_byte,  # type: ignore[attr-defined]
                    node.end_byte,  # type: ignore[attr-defined]
                )
                imports.append(text.strip())

            for child in getattr(node, "children", []):
                visit(child, parent)

        visit(tree.root_node)
        return symbols, imports

    def _parse_with_regex(self, source: str) -> tuple[list[Symbol], list[str]]:
        """正则降级解析（Tree-sitter 不可用时）。

        只识别顶层类/函数定义，准确性低于 AST 解析，
        但足以支撑「粗召回」这一使用场景。
        """
        symbols: list[Symbol] = []
        imports: list[str] = []
        lines = source.splitlines()
        current_class: str | None = None
        class_indent = 0

        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                imports.append(stripped)
                continue

            match = re.match(r"^(\s*)class\s+(\w+)", line)
            if match:
                indent = len(match.group(1))
                current_class = match.group(2)
                class_indent = indent
                symbols.append(
                    Symbol(
                        name=current_class,
                        kind="class",
                        file="",
                        start_line=i,
                        end_line=i,
                    )
                )
                continue

            match = re.match(r"^(\s*)(?:async\s+)?def\s+(\w+)", line)
            if match:
                indent = len(match.group(1))
                name = match.group(2)
                is_method = current_class is not None and indent > class_indent
                if not is_method:
                    current_class = None
                symbols.append(
                    Symbol(
                        name=name,
                        kind="method" if is_method else "function",
                        file="",
                        start_line=i,
                        end_line=i,
                        parent=current_class if is_method else None,
                    )
                )
        return symbols, imports

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #

    def search(self, keywords: list[str], *, top_k: int = 5) -> list[SearchHit]:
        """按关键词召回最相关的文件。

        打分规则（简单可解释）：
        - 符号名精确命中：+3.0
        - 符号名包含关键词：+2.0
        - 文档字符串命中：+1.0
        - 文件名命中：+1.5
        - 导入命中：+0.5

        Args:
            keywords: 查询关键词（通常来自需求/任务描述）。
            top_k: 返回数量上限。
        """
        if not self._ensure_built():
            return []

        normalized = [k.lower().strip() for k in keywords if k.strip()]
        if not normalized:
            return []

        hits: list[SearchHit] = []
        for rel, fi in self.files.items():
            score = 0.0
            matched: list[str] = []
            path_lower = rel.lower()

            for keyword in normalized:
                if keyword in path_lower:
                    score += 1.5
                    matched.append(f"file:{rel}")

                for symbol in fi.symbols:
                    sname = symbol.name.lower()
                    qname = symbol.qualified_name.lower()
                    if sname == keyword or qname == keyword:
                        score += 3.0
                        matched.append(symbol.qualified_name)
                    elif keyword in sname or keyword in qname:
                        score += 2.0
                        matched.append(symbol.qualified_name)

                    if symbol.docstring and keyword in symbol.docstring.lower():
                        score += 1.0

                for imp in fi.imports:
                    if keyword in imp.lower():
                        score += 0.5

            if score > 0:
                hits.append(
                    SearchHit(
                        file=rel,
                        score=score,
                        matched_symbols=sorted(set(matched)),
                        reason=_describe_match(matched),
                    )
                )

        hits.sort(key=lambda h: (-h.score, h.file))
        return hits[:top_k]

    def symbols_named(self, name: str) -> list[Symbol]:
        """按符号名精确查找（用于「谁定义了 X」这类问题）。"""
        if not self._ensure_built():
            return []
        target = name.lower()
        return [
            s
            for fi in self.files.values()
            for s in fi.symbols
            if s.name.lower() == target or s.qualified_name.lower() == target
        ]

    def stats(self) -> dict[str, object]:
        if not self._ensure_built():
            return {"built": False}
        return {
            "built": True,
            "files": len(self.files),
            "symbols": sum(len(fi.symbols) for fi in self.files.values()),
            "classes": sum(
                1 for fi in self.files.values() for s in fi.symbols if s.kind == "class"
            ),
            "functions": sum(
                1 for fi in self.files.values() for s in fi.symbols if s.kind != "class"
            ),
            "parse_mode": ("tree-sitter" if _TS_AVAILABLE else "regex"),
        }

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _ensure_built(self) -> bool:
        if not self._built:
            try:
                self.build()
            except OSError:
                return False
        return self._built

    def _iter_files(self) -> Iterator[Path]:
        """遍历 Python 文件，跳过排除目录、越界路径、链接与超大文件。"""
        for path in self.root.rglob("*.py"):
            if any(part in self.excludes for part in path.parts):
                continue
            if not self._is_inside_root(path) or _is_link_or_reparse_point(path):
                # 目录联接 / 符号链接可能把仓库外的文件拉进模型上下文
                logger.debug("index_path_outside_root_skipped", path=str(path))
                continue
            if self._is_oversized(path):
                logger.info(
                    "index_file_too_large_skipped", path=str(path), limit=self.max_file_bytes
                )
                continue
            yield path

    def _is_inside_root(self, path: Path) -> bool:
        """解析后必须仍位于仓库根目录内（防链接逃逸）。"""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return False
        return True

    def _is_oversized(self, path: Path) -> bool:
        try:
            return path.stat().st_size > self.max_file_bytes
        except OSError:
            return True


def _is_link_or_reparse_point(path: Path) -> bool:
    """符号链接或 Windows 重解析点（目录联接）返回 True。

    无法确认时按「是」处理：索引宁可少一个文件，也不能越界读文件。
    """
    try:
        st = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & reparse_flag)


def _slice_utf8(source_bytes: bytes, start: int, end: int) -> str:
    """按 UTF-8 字节偏移切片并解码（tree-sitter 的偏移语义）。"""
    return source_bytes[start:end].decode("utf-8", errors="replace")


def _node_text(node: object, field_name: str, source_bytes: bytes) -> str:
    """提取节点某字段的源码文本（按字节偏移，避免中文错位）。"""
    try:
        child = node.child_by_field_name(field_name)  # type: ignore[attr-defined]
    except AttributeError:
        return ""
    if child is None:
        return ""
    return _slice_utf8(source_bytes, child.start_byte, child.end_byte)


def _extract_docstring(node: object, source_bytes: bytes) -> str:
    """提取节点的文档字符串（若首个子语句是字符串字面量）。"""
    try:
        body = node.child_by_field_name("body")  # type: ignore[attr-defined]
    except AttributeError:
        return ""
    if body is None:
        return ""
    for child in getattr(body, "children", []):
        if getattr(child, "type", "") == "expression_statement":
            for sub in getattr(child, "children", []):
                if getattr(sub, "type", "") == "string":
                    text = _slice_utf8(source_bytes, sub.start_byte, sub.end_byte)
                    return text.strip("\"'").strip()[:200]
        break
    return ""


def _describe_match(matched: list[str]) -> str:
    if not matched:
        return ""
    symbols = [m for m in matched if not m.startswith("file:")]
    files = [m for m in matched if m.startswith("file:")]
    parts = []
    if symbols:
        parts.append(f"符号：{', '.join(sorted(set(symbols))[:3])}")
    if files:
        parts.append("文件名匹配")
    return "；".join(parts)


__all__ = [
    "DEFAULT_EXCLUDES",
    "DEFAULT_MAX_FILE_BYTES",
    "CodeIndex",
    "FileIndex",
    "SearchHit",
    "Symbol",
]
