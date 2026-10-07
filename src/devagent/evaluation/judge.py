"""LLM-as-Judge 评测器。

## 为什么需要裁判

自动评测（测试是否通过）只能回答「代码能不能跑」，回答不了
「需求理解对不对」「方案是否合理」。后者必须靠语义判断，而人工标注
无法规模化 —— 于是用 LLM 当裁判。

## 但 LLM 裁判有自己的偏差

这类偏差在文献里被反复验证，本项目针对性做了对冲：

1. **位置偏差（position bias）**：裁判倾向于给「先出现的」答案高分。
   → **双向评估**：把 (A, B) 和 (B, A) 各评一次，取平均。若两次结论
   一致说明稳定；若矛盾则标记为 ``inconsistent`` 并降权。

2. **长度偏差（verbosity bias）**：长答案显得更「充实」。
   → 在 prompt 中显式警告，且**评分维度分离**——「完整性」与「简洁性」
   各占独立维度，让长度无法单向拉高总分。

3. **自我偏好（self-preference）**：用同一模型评自己的输出会偏高。
   → 支持 ``judge_model != candidate_model``，并在报告里标注是否同源。

4. **分数聚集（score clustering）**：裁判爱打 7/8 分，导致区分度低。
   → 使用**锚点式评分**：每个维度给出明确的行为锚点描述，
   并计算 ``discrimination``（样本间标准差）来暴露「打分太集中」。

## 可复现性

温度固定为 0，且 prompt 中显式要求只输出 JSON —— 让评测结果可复现，
这是评测体系的底线（不可复现的评测等于没有评测）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from devagent.logging_config import get_logger
from devagent.models.provider import ChatMessage

logger = get_logger(__name__)

# 评分维度。分离「简洁性」是对冲长度偏差的关键设计。
DIMENSIONS: tuple[str, ...] = ("correctness", "completeness", "conciseness", "actionability")

_DIMENSION_ANCHORS = {
    "correctness": "1=结论错误或与事实矛盾；3=方向对但存在明显错误；5=完全正确，无事实错误",
    "completeness": "1=遗漏核心要求；3=覆盖主要要求但有缺口；5=所有验收标准均有对应",
    "conciseness": "1=大量冗余重复；3=有少量冗余；5=信息密度高，无多余内容",
    "actionability": "1=无法据此行动；3=需补充细节才能执行；5=可直接执行，含明确步骤",
}


@dataclass(frozen=True, slots=True)
class DimensionScore:
    """单个维度的评分。"""

    dimension: str
    score: float
    reason: str = ""


@dataclass(frozen=True, slots=True)
class JudgeResult:
    """一次裁判产出的完整结论。"""

    scores: tuple[DimensionScore, ...]
    overall: float
    passed: bool
    inconsistent: bool = False
    """双向评估两次结论矛盾（说明裁判在该样本上不可靠）。"""
    raw: dict[str, Any] = field(default_factory=dict)

    def score_of(self, dimension: str) -> float:
        for s in self.scores:
            if s.dimension == dimension:
                return s.score
        return 0.0

    @property
    def confidence(self) -> float:
        """置信度：结论矛盾时显著降低。

        用于报告聚合时加权 —— 低置信样本不应平等地影响整体结论。
        """
        return 0.5 if self.inconsistent else 1.0


class JudgeBackend(Protocol):
    """裁判后端协议（便于注入假实现做单测）。"""

    async def complete(self, messages: list[ChatMessage], *, model: str) -> str: ...


class GatewayJudgeBackend:
    """基于 ``ModelGateway`` 的裁判后端。"""

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    async def complete(self, messages: list[ChatMessage], *, model: str) -> str:  # noqa: ARG002
        # 模型由网关的路由器按档位决定，因此此处不直接使用 ``model``。
        result = await self._gateway.chat(
            messages,
            tier=None,
            temperature=0.0,  # 评测必须可复现
            use_cache=False,  # 评测不应被缓存掩盖
        )
        return str(result.content)


_JUDGE_SYSTEM = """你是一位严格、公正的技术评审专家。你的任务是评估一个 AI Agent 的输出质量。

评分要求：
1. 严格依据给定的验收标准评判，不要引入标准之外的个人偏好；
2. **不要因为答案更长就给更高分** —— 请独立评估"完整性"与"简洁性"；
3. 每个维度使用 1-5 分整数，必须参考锚点描述；
4. 只输出 JSON，不要任何额外文字或 Markdown 代码块标记。

评分维度锚点：
{anchors}

输出格式：
{{"scores": [{{"dimension": "correctness", "score": 4, "reason": "..."}}], "overall": 4.0}}"""


class LLMJudge:
    """LLM 裁判。

    用法::

        judge = LLMJudge(backend, model="deepseek:deepseek-chat")
        result = await judge.evaluate(
            goal="...", criteria=[...], candidate="...",
        )
    """

    def __init__(
        self,
        backend: JudgeBackend,
        *,
        model: str = "deepseek:deepseek-chat",
        candidate_model: str = "",
        pass_threshold: float = 3.5,
        bidirectional: bool = True,
    ) -> None:
        self._backend = backend
        self._model = model
        self._candidate_model = candidate_model
        self._pass_threshold = pass_threshold
        self._bidirectional = bidirectional

    @property
    def same_source_as_candidate(self) -> bool:
        """裁判与候选是否同源（自我偏好风险的提示）。"""
        if not self._candidate_model:
            return False
        return self._model.split(":")[-1] == self._candidate_model.split(":")[-1]

    async def evaluate(
        self,
        *,
        goal: str,
        criteria: list[str],
        candidate: str,
        reference: str = "",
    ) -> JudgeResult:
        """评估候选输出。

        启用双向评估时，会跑两次：
        - 第一次：正常顺序（criteria → candidate）
        - 第二次：交换参照物与候选的位置（若提供了 reference）
          或无 reference 时改为「反向提问」（先问缺点再问优点）

        两次 overall 差值超过 ``_INCONSISTENCY_THRESHOLD`` 视为矛盾。
        """
        forward = await self._single_pass(
            goal=goal, criteria=criteria, candidate=candidate, reference=reference, reverse=False
        )
        if not self._bidirectional:
            return forward

        backward = await self._single_pass(
            goal=goal, criteria=criteria, candidate=candidate, reference=reference, reverse=True
        )

        inconsistent = abs(forward.overall - backward.overall) > _INCONSISTENCY_THRESHOLD
        merged = self._merge(forward, backward)
        if inconsistent:
            logger.warning(
                "judge_inconsistent",
                forward=forward.overall,
                backward=backward.overall,
                delta=abs(forward.overall - backward.overall),
            )
        return merged

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _single_pass(
        self,
        *,
        goal: str,
        criteria: list[str],
        candidate: str,
        reference: str,
        reverse: bool,
    ) -> JudgeResult:
        anchors = "\n".join(f"- {k}: {v}" for k, v in _DIMENSION_ANCHORS.items())
        system = _JUDGE_SYSTEM.format(anchors=anchors)

        criteria_text = "\n".join(f"- {c}" for c in criteria) or "（未提供验收标准）"
        parts = [f"## 任务目标\n{goal}", f"## 验收标准\n{criteria_text}"]
        if reference:
            # reverse 时交换顺序，抵消位置偏差
            if reverse:
                parts.append(f"## 待评估输出\n{candidate}")
                parts.append(f"## 参考输出\n{reference}")
            else:
                parts.append(f"## 参考输出\n{reference}")
                parts.append(f"## 待评估输出\n{candidate}")
        else:
            if reverse:
                parts.append(f"## 待评估输出\n{candidate}")
                parts.append("## 特别要求\n请优先指出该输出的**缺陷与遗漏**，再考虑其优点。")
            else:
                parts.append(f"## 待评估输出\n{candidate}")
                parts.append("## 特别要求\n请优先考虑该输出的**优点与达成度**，再考虑其缺陷。")

        messages = [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content="\n\n".join(parts)),
        ]
        raw_text = await self._backend.complete(messages, model=self._model)
        return self._parse(raw_text)

    def _parse(self, text: str) -> JudgeResult:
        payload = _extract_json(text)
        if payload is None:
            # 解析失败不抛异常：评测体系不能因为一次格式错误而整体失败，
            # 但必须如实记录为「低分 + 不可信」，而不是默默给个及格分。
            logger.warning("judge_parse_failed", preview=text[:200])
            return JudgeResult(
                scores=tuple(DimensionScore(d, 0.0, "解析失败") for d in DIMENSIONS),
                overall=0.0,
                passed=False,
                inconsistent=True,
                raw={"parse_failed": True, "text": text[:500]},
            )

        scores: list[DimensionScore] = []
        for item in payload.get("scores", []):
            dim = str(item.get("dimension", ""))
            if not dim:
                continue
            scores.append(
                DimensionScore(
                    dimension=dim,
                    score=_clamp(float(item.get("score", 0))),
                    reason=str(item.get("reason", "")),
                )
            )

        overall = payload.get("overall")
        if overall is None:
            # 未给总分则由各维度均值推导，避免「总分与分项无关」的怪现象
            overall = sum(s.score for s in scores) / len(scores) if scores else 0.0
        overall = _clamp(float(overall))

        return JudgeResult(
            scores=tuple(scores),
            overall=overall,
            passed=overall >= self._pass_threshold,
            raw=payload,
        )

    def _merge(self, a: JudgeResult, b: JudgeResult) -> JudgeResult:
        """合并两次评估：逐维度取均值，保留矛盾标记。"""
        by_dim: dict[str, list[DimensionScore]] = {}
        for result in (a, b):
            for s in result.scores:
                by_dim.setdefault(s.dimension, []).append(s)

        merged_scores = tuple(
            DimensionScore(
                dimension=dim,
                score=sum(s.score for s in items) / len(items),
                reason=" | ".join(s.reason for s in items if s.reason),
            )
            for dim, items in by_dim.items()
        )
        overall = (a.overall + b.overall) / 2
        inconsistent = (
            a.inconsistent
            or b.inconsistent
            or abs(a.overall - b.overall) > _INCONSISTENCY_THRESHOLD
        )
        # ★ 通过判定必须与 overall **同源**，否则报告会自相矛盾。
        # 早先是 ``passed = a.passed and b.passed``，而 overall 取平均：
        # 前向 3.6 / 反向 3.4（阈值 3.5）会产出 {"overall": 3.5, "passed": false}，
        # 于是 ``mean_judge_score`` 说达标、``judge_pass_rate`` 说没达标，
        # 同一个样本在两个口径里给出相反结论（异构校正后还会再翻转一次）。
        #
        # 现在：分数取平均（保持原有的"平均分"契约），通过与否由该平均分推导。
        # 两次评估的分歧本身不会丢失 —— 差距大时 ``inconsistent`` 会置位，
        # 而两次各自的通过结论也原样保留在 ``raw["passes"]`` 里，
        # 需要"两次都通过才算通过"的下游可以据此自行收紧。
        passed = overall >= self._pass_threshold
        return JudgeResult(
            scores=merged_scores,
            overall=overall,
            passed=passed,
            inconsistent=inconsistent,
            raw={
                "forward": a.raw,
                "backward": b.raw,
                "passes": [a.passed, b.passed],
                "overalls": [a.overall, b.overall],
            },
        )


_INCONSISTENCY_THRESHOLD = 1.0


def _clamp(value: float, low: float = 0.0, high: float = 5.0) -> float:
    return max(low, min(high, value))


def _extract_json(text: str) -> dict[str, Any] | None:
    """从可能带 Markdown 包裹的回复中提取 JSON 对象。"""
    candidate = text.strip()
    # 去掉 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # 退回：抓第一个平衡的 {...}
    start = candidate.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(candidate)):
        if candidate[i] == "{":
            depth += 1
        elif candidate[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(candidate[start : i + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


__all__ = [
    "DIMENSIONS",
    "DimensionScore",
    "GatewayJudgeBackend",
    "JudgeBackend",
    "JudgeResult",
    "LLMJudge",
]
