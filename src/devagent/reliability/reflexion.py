"""Reflexion：失败反思。

核心思想（对齐 docs/03）：失败不是简单重试，而是**学习**。

流程::

    失败 → 分析根因 → 生成结构化「教训」 → 注入下次尝试的上下文 → 重试

教训按 Agent 类型分桶存储，避免把「编码教训」注入到「验证」环节造成干扰。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from devagent.enums import AgentType
from devagent.models.domain import ReflexionLesson


@dataclass(slots=True)
class ReflexionMemory:
    """教训存储（按 Agent 类型分桶）。

    - **按角色隔离**：Coder 的教训不应污染 Verifier 的判断；
    - **FIFO 上限**：每桶保留最近 N 条，避免上下文无限增长；
    - **去重**：相同 lesson 不重复添加。
    """

    max_per_agent: int = 5
    _buckets: dict[AgentType, deque[ReflexionLesson]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._buckets = defaultdict(lambda: deque(maxlen=self.max_per_agent))

    def add(self, lesson: ReflexionLesson, *, agent_type: AgentType) -> None:
        """记录一条教训。重复（同 root_cause + lesson）会被忽略。"""
        bucket = self._buckets[agent_type]
        for existing in bucket:
            if existing.root_cause == lesson.root_cause and existing.lesson == lesson.lesson:
                return
        bucket.append(lesson)

    def lessons_for(self, agent_type: AgentType) -> list[ReflexionLesson]:
        """获取某 Agent 的历史教训（最新在后）。"""
        return list(self._buckets.get(agent_type, ()))

    def clear(self, agent_type: AgentType | None = None) -> None:
        if agent_type is None:
            self._buckets.clear()
        else:
            self._buckets.pop(agent_type, None)

    def as_context_block(self, agent_type: AgentType) -> str:
        """把教训渲染为可注入上下文的文本块。"""
        lessons = self.lessons_for(agent_type)
        if not lessons:
            return ""
        lines = ["# 历史失败教训（务必避免重复）"]
        for lesson in lessons:
            lines.append(f"- 根因：{lesson.root_cause}")
            lines.append(f"  教训：{lesson.lesson}")
            if lesson.avoid:
                lines.append(f"  避免：{lesson.avoid}")
        return "\n".join(lines)


__all__ = ["ReflexionMemory"]
