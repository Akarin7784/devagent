"""上下文压缩（L3）。

对齐 ``docs/02-上下文工程深度设计.md`` 的分层压缩策略：

====== ====================== ================================
层级   对象                    策略
====== ====================== ================================
L1    最近若干轮 / 当前任务    不压缩，保留原文
L2    中期历史（已完成步骤）    滚动摘要（结构化 schema）
L3    远期历史（跨任务）        指针化，按需回捞
====== ====================== ================================

关键设计：
1. **只在步骤边界压缩**，不在推理中途压缩（会丢线索）；
2. 摘要使用**固定 schema**（决策/产物/约束/待办），而非自由文本，
   保证关键信息不丢且可程序化利用；
3. **硬约束永不进入压缩区**。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from devagent.context.tokenizer import HeuristicTokenCounter, TokenCounter
from devagent.enums import ContextKind
from devagent.models.domain import ContextChunk


class Summarizer(Protocol):
    """摘要生成器接口。

    生产环境由 LLM 实现（见 ``devagent.models``）；
    测试中使用 ``EchoSummarizer`` 等确定性实现。
    """

    async def summarize(self, text: str, *, instruction: str) -> str:
        """生成结构化摘要。"""
        ...


@dataclass(slots=True)
class StructuredSummary:
    """结构化摘要（不是自由文本）。

    固定 schema 是「上下文压缩」与「随便总结一下」的分界线：
    它保证压缩后关键信息可被程序化校验与再利用。
    """

    decisions: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def render(self) -> str:
        """渲染为可注入上下文的 Markdown 文本。"""
        parts: list[str] = []
        if self.decisions:
            parts.append("## 已完成的决策\n" + "\n".join(f"- {d}" for d in self.decisions))
        if self.artifacts:
            parts.append("## 已产出的产物\n" + "\n".join(f"- {a}" for a in self.artifacts))
        if self.constraints:
            parts.append("## 已知约束\n" + "\n".join(f"- {c}" for c in self.constraints))
        if self.open_questions:
            parts.append("## 待解决问题\n" + "\n".join(f"- {q}" for q in self.open_questions))
        return "\n\n".join(parts)

    def is_empty(self) -> bool:
        return not (self.decisions or self.artifacts or self.constraints or self.open_questions)

    def merge(self, other: StructuredSummary) -> StructuredSummary:
        """合并两个摘要（滚动摘要场景）。"""
        return StructuredSummary(
            decisions=_dedupe([*self.decisions, *other.decisions]),
            artifacts=_dedupe([*self.artifacts, *other.artifacts]),
            constraints=_dedupe([*self.constraints, *other.constraints]),
            open_questions=_dedupe([*self.open_questions, *other.open_questions]),
            created_at=max(self.created_at, other.created_at),
        )


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


@dataclass(slots=True)
class CompressionResult:
    """压缩结果。"""

    chunks: list[ContextChunk]
    tokens_before: int
    tokens_after: int
    compressed_count: int = 0

    @property
    def ratio(self) -> float:
        if self.tokens_before == 0:
            return 1.0
        return self.tokens_after / self.tokens_before


class ContextCompressor:
    """分层上下文压缩器。

    用法::

        compressor = ContextCompressor(summarizer=llm_summarizer)
        result = await compressor.compress(chunks, current_age=0)
    """

    def __init__(
        self,
        summarizer: Summarizer | None = None,
        *,
        hot_window: int = 3,
        warm_window: int = 10,
        token_counter: TokenCounter | None = None,
        protect_hard: bool = True,
    ) -> None:
        """
        Args:
            summarizer: 摘要生成器；为 None 时退化为「结构化抽取式摘要」。
            hot_window: 热区大小（最近 N 个片段不压缩）。
            warm_window: 温区大小（热区之外的 N 个片段做摘要压缩）。
            token_counter: token 计数器。
            protect_hard: 硬约束是否免于压缩。对应
                ``ContextConfig.hard_constraint_untouchable``；此前这个配置项
                从未被读取（恒为 True），属于「写了开关但接线不存在」。
        """
        self._summarizer = summarizer
        self._hot_window = hot_window
        self._warm_window = warm_window
        self._counter = token_counter or HeuristicTokenCounter()
        self._protect_hard = protect_hard

    async def compress(
        self,
        chunks: Sequence[ContextChunk],
        *,
        current_age: int = 0,  # noqa: ARG002  保留：用于未来的按龄分级压缩策略
    ) -> CompressionResult:
        """执行分层压缩。

        Args:
            chunks: 待压缩片段（通常是完整历史）。
            current_age: 当前步骤的 age，用于划分热/温/冷区。

        Returns:
            ``CompressionResult``，其中冷区片段被替换为指针形式的摘要片段。
        """
        tokens_before = sum(c.tokens for c in chunks)

        # 硬约束是否免压缩由配置决定（``hard_constraint_untouchable``）
        if self._protect_hard:
            hard = [c for c in chunks if c.is_hard]
            soft = [c for c in chunks if not c.is_hard]
        else:
            hard, soft = [], list(chunks)

        # 按 age 升序（新 → 旧），age 越小越新
        soft_sorted = sorted(soft, key=lambda c: c.age)

        hot = soft_sorted[: self._hot_window]
        warm = soft_sorted[self._hot_window : self._hot_window + self._warm_window]
        cold = soft_sorted[self._hot_window + self._warm_window :]

        out: list[ContextChunk] = [*hard, *hot]
        compressed_count = 0

        # 温区：做结构化摘要（保留信息，压缩体积）
        if warm:
            summary_chunk = await self._summarize_group(warm, label="中期历史")
            if summary_chunk is not None:
                out.append(summary_chunk)
                compressed_count += len(warm)

        # 冷区：指针化（只留引用，按需回捞）
        if cold:
            pointer_chunk = self._pointerize(cold, label="远期历史")
            out.append(pointer_chunk)
            compressed_count += len(cold)

        tokens_after = sum(c.tokens for c in out)
        return CompressionResult(
            chunks=out,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            compressed_count=compressed_count,
        )

    async def _summarize_group(
        self, group: Sequence[ContextChunk], *, label: str
    ) -> ContextChunk | None:
        """把一个片段组压缩为单个摘要片段。"""
        if not group:
            return None

        merged_text = "\n\n".join(c.content for c in group)
        if self._summarizer is not None:
            summary_text = await self._summarizer.summarize(
                merged_text,
                instruction=(
                    "把以下开发过程记录压缩为结构化摘要，"
                    "必须包含四个小节：已完成的决策 / 已产出的产物 / 已知约束 / 待解决问题。"
                    "不要遗漏任何约束与决策。"
                ),
            )
        else:
            # 无摘要器时的降级：结构化抽取式摘要（保留小标题/条目/约束），
            # 而不是盲截断 —— 见 _extractive_summary 的说明。
            summary_text = _extractive_summary(merged_text)

        max_age = max(c.age for c in group)
        return ContextChunk(
            id=f"summary_{(group[0].id)}_{max_age}",
            content=f"[{label}摘要]\n{summary_text}",
            kind=ContextKind.HISTORY,
            tokens=self._counter.count(summary_text),
            embedding=None,  # 摘要不参与语义竞争，避免与原文重复计分
            age=max_age + 1,  # 略旧于热区，保证排在尾部之前
            info_units=max(1, len(summary_text.split("\n"))),
            source=f"compressed:{len(group)} chunks",
            meta={"compressed_chunks": len(group), "label": label},
        )

    def _pointerize(self, group: Sequence[ContextChunk], *, label: str) -> ContextChunk:
        """把冷区片段指针化：只保留「发生过什么」的引用。"""
        refs = []
        for c in group:
            src = c.source or c.id
            preview = c.content[:40].replace("\n", " ")
            refs.append(f"- {src}: {preview}…")
        content = f"[{label}指针，共 {len(group)} 项，需要时按 source 回捞]\n" + "\n".join(refs)
        max_age = max(c.age for c in group)
        return ContextChunk(
            id=f"pointer_{group[0].id}_{max_age}",
            content=content,
            kind=ContextKind.HISTORY,
            tokens=self._counter.count(content),
            age=max_age + 100,  # 最旧 → 排在中部靠前
            source=f"pointerized:{len(group)} chunks",
            meta={
                "pointerized_chunks": len(group),
                "label": label,
                "refs": [c.source for c in group],
            },
        )

    def should_compress(self, used_tokens: int, budget_tokens: int, threshold: float) -> bool:
        """判断是否应触发压缩。"""
        if budget_tokens <= 0:
            return False
        return (used_tokens / budget_tokens) >= threshold


class EchoSummarizer:
    """确定性摘要器（测试/降级用）。

    不调用 LLM，仅做关键词抽取式摘要，保证测试可复现。
    """

    def __init__(self, max_chars: int = 400) -> None:
        self._max_chars = max_chars

    async def summarize(self, text: str, *, instruction: str) -> str:  # noqa: ARG002
        # instruction 是 Summarizer 协议的一部分：真实 LLM 摘要器需要它来控制
        # 摘要侧重；此处的 EchoSummarizer 为确定性实现，故不使用该参数。
        # 抽取含关键词的行，模拟结构化摘要
        keywords = ("决定", "选择", "完成", "修改", "约束", "必须", "待", "问题")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        picked = [ln for ln in lines if any(k in ln for k in keywords)]
        if not picked:
            picked = lines[:5]
        rendered = "\n".join(f"- {ln}" for ln in picked[:15])
        return rendered[: self._max_chars]


def _extractive_summary(text: str, max_chars: int = 1200) -> str:
    """无 LLM 摘要器时的降级策略：**结构化抽取**，而不是盲截断。

    早期实现是 ``text[:300] + "…（已截断）"``。温区最多 10 个片段被合并后
    只留前 300 字符 —— 对一份真实的开发过程记录，这等于丢掉 95% 以上内容，
    而文档承诺的是「滚动摘要（结构化 schema）」。既然 ``src/`` 里并不存在
    LLM 摘要器实现（只有测试用的 ``EchoSummarizer``），生产路径实际一直在
    走这条降级分支，所以它必须足够保守：

    1. 优先保留**有结构意义的行**（Markdown 小标题、列表项、含约束/决策
       关键词的行）——这些行承载的信息密度最高；
    2. 其余行按原顺序补齐，直到达到字符预算；
    3. 明确标注这是抽取式降级摘要，且保留原文片段数，便于事后审计。
    """
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""

    keyword_hits = ("约束", "必须", "不得", "决定", "选择", "完成", "修改", "待", "问题", "TODO")
    structural = [
        ln
        for ln in lines
        if ln.lstrip().startswith(("#", "-", "*", "```")) or any(k in ln for k in keyword_hits)
    ]

    picked: list[str] = []
    seen: set[str] = set()
    for ln in [*structural, *lines]:
        key = ln.strip()
        if key in seen:
            continue
        seen.add(key)
        picked.append(ln)

    rendered = "\n".join(picked)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n…（抽取式摘要已达长度上限，完整内容请按 source 回捞）"
    return rendered


def _naive_summary(text: str, max_chars: int = 300) -> str:
    """保留的简单截断实现（供对比实验与外部调用）。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…（已截断，完整内容请按 source 回捞）"


__all__ = [
    "CompressionResult",
    "ContextCompressor",
    "EchoSummarizer",
    "StructuredSummary",
    "Summarizer",
]
