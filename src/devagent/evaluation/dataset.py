"""Golden Set 数据集。

评测的地基。没有 golden set 的「评测」只是演示。

数据格式：JSONL，每行一个样本。之所以不用 CSV/YAML：
- JSONL 天然支持嵌套结构（acceptance_criteria 是列表）；
- 逐行追加，适合持续扩充；
- 与 LLM 生态一致，易于让模型生成候选样本再由人工筛。

样本 schema（宽松，允许逐步演进）::

    {
      "id": "req-001",
      "category": "requirement",
      "goal": "为 /users 接口增加分页参数",
      "expected_criteria": ["支持 page 参数", "非法参数返回 400"],
      "forbidden_criteria": ["运行流畅", "代码优雅"],
      "tags": ["api", "validation"]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devagent.logging_config import get_logger

logger = get_logger(__name__)


class DatasetError(ValueError):
    """数据集格式错误。"""


@dataclass(slots=True)
class GoldenSample:
    """一个评测样本。"""

    id: str
    category: str
    goal: str
    expected_criteria: list[str] = field(default_factory=list)
    forbidden_criteria: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GoldenSample:
        missing = [k for k in ("id", "category", "goal") if not raw.get(k)]
        if missing:
            # 不要把整条原始数据回显进错误信息：数据集内容可能来自用户上传，
            # 而错误信息会经 API 返回给调用方（早先的实现会把整行内容带出去）。
            raise DatasetError(
                f"样本缺少必填字段：{missing}（该行字段：{sorted(raw.keys())[:10]}）"
            )
        return cls(
            id=str(raw["id"]),
            category=str(raw["category"]),
            goal=str(raw["goal"]),
            expected_criteria=[str(c) for c in raw.get("expected_criteria", [])],
            forbidden_criteria=[str(c) for c in raw.get("forbidden_criteria", [])],
            tags=[str(t) for t in raw.get("tags", [])],
            metadata=dict(raw.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "category": self.category,
            "goal": self.goal,
        }
        if self.expected_criteria:
            out["expected_criteria"] = list(self.expected_criteria)
        if self.forbidden_criteria:
            out["forbidden_criteria"] = list(self.forbidden_criteria)
        if self.tags:
            out["tags"] = list(self.tags)
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out


@dataclass(slots=True)
class GoldenSet:
    """一组评测样本。"""

    samples: list[GoldenSample] = field(default_factory=list)
    name: str = "default"

    def __len__(self) -> int:
        return len(self.samples)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.samples)

    def by_category(self, category: str) -> list[GoldenSample]:
        return [s for s in self.samples if s.category == category]

    def by_tag(self, tag: str) -> list[GoldenSample]:
        return [s for s in self.samples if tag in s.tags]

    @property
    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.samples:
            counts[s.category] = counts.get(s.category, 0) + 1
        return counts

    def subset(self, ids: set[str]) -> GoldenSet:
        return GoldenSet(samples=[s for s in self.samples if s.id in ids], name=self.name)

    def validate_unique_ids(self) -> None:
        seen: set[str] = set()
        dupes: list[str] = []
        for s in self.samples:
            if s.id in seen:
                dupes.append(s.id)
            seen.add(s.id)
        if dupes:
            raise DatasetError(f"样本 id 重复：{dupes}")

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls, path: str | Path) -> GoldenSet:
        """从 JSONL 加载。跳过空行与 ``#`` 注释行。"""
        p = Path(path)
        if not p.exists():
            raise DatasetError(f"数据集不存在：{p}")
        samples: list[GoldenSample] = []
        errors: list[str] = []
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                errors.append(f"第 {lineno} 行 JSON 解析失败：{exc}")
                continue
            try:
                samples.append(GoldenSample.from_dict(raw))
            except DatasetError as exc:
                errors.append(f"第 {lineno} 行：{exc}")
        if errors:
            raise DatasetError("数据集存在错误：\n" + "\n".join(errors))
        result = cls(samples=samples, name=p.stem)
        result.validate_unique_ids()
        logger.info("golden_set_loaded", path=str(p), count=len(samples))
        return result

    @classmethod
    async def aload(cls, path: str | Path) -> GoldenSet:
        """异步加载：把同步文件读放到线程里。

        为什么需要：``load`` 会做 ``read_text`` + 逐行 ``json.loads``，
        对一个几千行的数据集是**阻塞调用**。它此前被直接在 async 路由里
        调用，会卡住整个事件循环（所有并发请求一起等）。
        """
        import asyncio

        return await asyncio.to_thread(cls.load, path)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(s.to_dict(), ensure_ascii=False) for s in self.samples]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("golden_set_saved", path=str(p), count=len(self.samples))

    @classmethod
    def from_dicts(cls, raw_list: list[dict[str, Any]], *, name: str = "inline") -> GoldenSet:
        gs = cls(samples=[GoldenSample.from_dict(r) for r in raw_list], name=name)
        gs.validate_unique_ids()
        return gs


__all__ = ["DatasetError", "GoldenSample", "GoldenSet"]
