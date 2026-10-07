"""异构裁判：消除 LLM-as-Judge 的自我偏好偏差。

## 问题：同源裁判给自己打高分

``LLMJudge`` 的 ``same_source_as_candidate`` 只**标注**了同源风险，
但没有任何实际校正。而默认配置正好落在最糟的情形上：

```
DEVAGENT_EVALUATION__JUDGE_MODEL = "deepseek:deepseek-chat"
DEVAGENT_ROUTING__MEDIUM_MODEL   = "deepseek:deepseek-chat"   ← 候选也用这个
```

也就是说：**在开箱即用的配置下，裁判一直在评自己的输出**。
文献里把这种偏差称为 self-preference / self-enhancement bias，
表现为同源评分系统性偏高（而不是随机噪声）。

这类偏差**不能靠多次采样消除** —— 采样消掉的是方差，而偏差是均值偏移。
100 次采样只会得到 100 个同样偏高的分数。

## 三种对冲手段，本模块都实现

### 1. 异构裁判（heterogeneous judge）

让裁判换一个**不同的模型族**。跨族模型的训练数据、对齐目标、
失败模式都不同，因此"互相客气"的程度显著低于同族。

关键设计：**不是"换个模型名"，而是换到不同的 provider**。
``deepseek-chat`` 与 ``deepseek-reasoner`` 是同族（同一套预训练 + 对齐），
换它等于没换。

### 2. 双向评估（bidirectional）

已在 ``LLMJudge`` 中实现，用于对冲**位置偏差**。
与本模块是互补关系：一个管位置，一个管同源。

### 3. 偏差标定（calibration）

用同一批样本让**同源裁判**与**异构裁判**各评一次，
同源分数相对异构分数的**系统性偏移**就是该裁判的偏差估计。

标定值可以持久化，之后对同源裁判的分数做扣减 —— 这让我们能在
"异构模型不可用"时**量化**地知道同源分数注了多少水，而不是只能
含糊地说"这个分数可能偏高"。

## 一个诚实的限制

标定假设：**异构裁判是无偏的基准**。这不成立 —— 异构裁判也有自己的偏差，
只是**与同源裁判的偏差不相关**。因此标定测的是"两者之差"，
而不是"绝对偏差"。

这比什么都不做要好得多（至少把偏差从"不可知"变成"可比较"），
但不应被宣传成"消除了偏差"。ADR-0008 里记了这一点。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from devagent.evaluation.judge import JudgeResult, LLMJudge
from devagent.logging_config import get_logger

logger = get_logger(__name__)

#: 已知的模型族映射：同族内的模型共享预训练与对齐，互相评审等同同源。
#:
#: 判断"是否异构"必须基于**族**而不是模型名 —— ``deepseek-chat`` 与
#: ``deepseek-reasoner`` 名字不同，但同族；把它们当异构会自欺欺人。
_MODEL_FAMILIES: dict[str, str] = {
    "deepseek-chat": "deepseek",
    "deepseek-reasoner": "deepseek",
    "deepseek-coder": "deepseek",
    "qwen-turbo": "qwen",
    "qwen-plus": "qwen",
    "qwen-max": "qwen",
    "qwen-long": "qwen",
    "text-embedding-v3": "qwen",
    "glm-4-flash": "zhipu",
    "glm-4-air": "zhipu",
    "glm-4-plus": "zhipu",
    "glm-4": "zhipu",
}


def model_family(model_spec: str) -> str:
    """从 ``<provider>:<model>`` 提取模型族。

    优先查表（精确）；查不到则退回 provider 名 —— 因为跨 provider
    几乎必然是跨族，而这是一个安全的保守判断。

    注意 ``str.partition`` 在**没有分隔符**时返回 ``(whole, "", "")``，
    即整个串落到 ``provider`` 而 ``model`` 为空。因此必须显式处理
    「裸模型名」这一情形，否则 ``"deepseek-chat"`` 会被当成 provider 名
    原样返回，导致同族判定失效（这是一个真实踩到的坑）。
    """
    if ":" not in model_spec:
        # 裸模型名：整体就是一个模型标识
        return _MODEL_FAMILIES.get(model_spec, model_spec)
    provider, _, model = model_spec.partition(":")
    if model in _MODEL_FAMILIES:
        return _MODEL_FAMILIES[model]
    return provider or model


def is_heterogeneous(judge_model: str, candidate_model: str) -> bool:
    """判断裁判与候选是否异构（不同模型族）。

    这是本模块的核心判定。注意：**同名同族 → 同源**；
    **同 provider 不同族**（目录里不存在，但理论上）也算异构，
    因为族是从模型名查表的，而 provider 只是兜底。
    """
    if not judge_model or not candidate_model:
        return False
    return model_family(judge_model) != model_family(candidate_model)


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """一次偏差标定的结果。"""

    judge_model: str
    reference_model: str
    sample_count: int
    mean_judge: float
    """同源裁判的平均分。"""

    mean_reference: float
    """异构裁判的平均分（作为比较基准）。"""

    @property
    def bias(self) -> float:
        """偏差估计：正值表示同源裁判**偏高**（self-preference 的方向）。

        这是本模块要产出的核心数字 —— 有了它，"同源评分不可直接采信"
        就从一句定性判断变成了可量化的扣减依据。
        """
        return self.mean_judge - self.mean_reference

    @property
    def relative_bias(self) -> float:
        """相对偏差（占基准的比例），便于跨 1-5 分制比较。"""
        if self.mean_reference == 0:
            return 0.0
        return self.bias / self.mean_reference

    @property
    def trustworth(self) -> bool:
        """标定是否可信。样本太少时偏差估计本身就是噪声。"""
        return self.sample_count >= _MIN_CALIBRATION_SAMPLES

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge_model": self.judge_model,
            "reference_model": self.reference_model,
            "sample_count": self.sample_count,
            "mean_judge": round(self.mean_judge, 4),
            "mean_reference": round(self.mean_reference, 4),
            "bias": round(self.bias, 4),
            "relative_bias": round(self.relative_bias, 4),
            "reliable": self.trustworth,
        }


#: 低于此样本数时，偏差估计的置信度不足，不应用于扣减。
#:
#: 依据：偏差是均值层面的量，其标准误约为 ``sigma / sqrt(n)``。
#: 取 sigma≈0.8（1-5 分制下裁判分数的典型标准差），
#: 要让标准误降到 0.2 以内需要 n ≈ 16。取 20 留出余量。
_MIN_CALIBRATION_SAMPLES = 20


@dataclass(slots=True)
class JudgePanel:
    """异构裁判组：主裁判 + 参考裁判 + 标定出的偏差。

    用法::

        panel = JudgePanel(primary=deepseek_judge, reference=qwen_judge,
                           candidate_model="deepseek:deepseek-chat")
        result = await panel.evaluate(goal=..., criteria=..., candidate=...)
    """

    primary: LLMJudge
    reference: LLMJudge | None = None
    candidate_model: str = ""
    calibration: CalibrationResult | None = None
    _history: list[tuple[float, float]] = field(default_factory=list, repr=False)
    """(primary, reference) 分数对，用于在线累积标定。"""

    @property
    def primary_is_heterogeneous(self) -> bool:
        """主裁判本身是否已经异构（最理想的情形：无需校正）。"""
        return is_heterogeneous(self._primary_model, self.candidate_model)

    @property
    def _primary_model(self) -> str:
        return str(getattr(self.primary, "_model", ""))

    @property
    def _reference_model(self) -> str:
        return str(getattr(self.reference, "_model", "")) if self.reference else ""

    async def evaluate(
        self,
        *,
        goal: str,
        criteria: list[str],
        candidate: str,
        reference_output: str = "",
    ) -> JudgeResult:
        """评估候选输出。

        行为分三种情形：

        1. **主裁判已异构** → 直接用它，无需校正（最理想）。
        2. **主裁判同源 + 有已标定的偏差** → 用主裁判，但按其偏差校正分数。
        3. **主裁判同源 + 无标定** → 如实返回，并在 ``raw`` 中标记
           ``uncalibrated_self_preference``，让报告层能看见这个风险。

        第 3 种情形是**默认配置下的实际路径** —— 因此这里选择"显式标记"
        而不是"静默校正一个猜出来的偏差"。用未经验证的常数去扣分，
        比不扣分更糟：它会让分数看起来已经被修正过。
        """
        result = await self.primary.evaluate(
            goal=goal, criteria=criteria, candidate=candidate, reference=reference_output
        )

        if self.primary_is_heterogeneous:
            return _tag(result, {"judge_relation": "heterogeneous"})

        if self.calibration is not None and self.calibration.trustworth:
            corrected = self._apply_calibration(result)
            return _tag(
                corrected,
                {
                    "judge_relation": "self",
                    "calibrated": True,
                    "bias_applied": round(self.calibration.bias, 4),
                },
            )

        logger.warning(
            "judge_self_preference_uncalibrated",
            judge_model=self._primary_model,
            candidate_model=self.candidate_model,
        )
        return _tag(
            result,
            {
                "judge_relation": "self",
                "calibrated": False,
                "uncalibrated_self_preference": True,
                "hint": "裁判与候选同源且未标定；分数可能系统性偏高，"
                "建议配置 DEVAGENT_EVALUATION__REFERENCE_JUDGE_MODEL 异构裁判",
            },
        )

    async def calibrate(
        self,
        samples: Sequence[tuple[str, list[str], str]],
    ) -> CalibrationResult | None:
        """用一批样本估计主裁判相对参考裁判的系统性偏差。

        Args:
            samples: ``(goal, criteria, candidate)`` 三元组列表。

        Returns:
            标定结果；无参考裁判时返回 ``None``。

        **为什么要用同一批样本**：偏差是均值之差，只有在**相同输入**
        上比较才有意义。分别用不同样本集去比，比的是"样本难度差异"
        而不是"裁判偏差"。
        """
        if self.reference is None:
            return None
        if not samples:
            return None

        primary_scores: list[float] = []
        reference_scores: list[float] = []

        for goal, criteria, candidate in samples:
            p = await self.primary.evaluate(goal=goal, criteria=criteria, candidate=candidate)
            r = await self.reference.evaluate(goal=goal, criteria=criteria, candidate=candidate)
            primary_scores.append(p.overall)
            reference_scores.append(r.overall)
            self._history.append((p.overall, r.overall))

        result = CalibrationResult(
            judge_model=self._primary_model,
            reference_model=self._reference_model,
            sample_count=len(samples),
            mean_judge=sum(primary_scores) / len(primary_scores),
            mean_reference=sum(reference_scores) / len(reference_scores),
        )
        self.calibration = result
        logger.info(
            "judge_calibrated",
            judge_model=result.judge_model,
            bias=round(result.bias, 4),
            samples=result.sample_count,
            reliable=result.trustworth,
        )
        return result

    def _apply_calibration(self, result: JudgeResult) -> JudgeResult:
        """按标定偏差扣减分数。

        扣减后重新判定 ``passed``（用主裁判自己的阈值），
        否则会出现"分数被扣到阈值以下但 passed 仍为 True"的矛盾。
        """
        assert self.calibration is not None
        delta = self.calibration.bias
        if abs(delta) < 1e-6:
            return result

        adjusted = [_clamp5(s.score - delta) for s in result.scores]
        new_overall = _clamp5(result.overall - delta)
        threshold = float(getattr(self.primary, "_pass_threshold", 3.5))
        return JudgeResult(
            scores=tuple(
                type(s)(dimension=s.dimension, score=score, reason=s.reason)
                for s, score in zip(result.scores, adjusted, strict=True)
            ),
            overall=new_overall,
            passed=new_overall >= threshold,
            inconsistent=result.inconsistent,
            raw=result.raw,
        )


def _clamp5(value: float) -> float:
    return max(0.0, min(5.0, value))


def _tag(result: JudgeResult, extra: dict[str, Any]) -> JudgeResult:
    """在 ``raw`` 上附加诊断信息（不改变分数与判定）。"""
    raw = dict(result.raw)
    raw["judge_panel"] = extra
    return JudgeResult(
        scores=result.scores,
        overall=result.overall,
        passed=result.passed,
        inconsistent=result.inconsistent,
        raw=raw,
    )


__all__ = [
    "CalibrationResult",
    "JudgePanel",
    "is_heterogeneous",
    "model_family",
]
