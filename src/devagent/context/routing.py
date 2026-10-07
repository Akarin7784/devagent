"""上下文路由 / 模型分级路由（L1）。

对齐 ``docs/02-上下文工程深度设计.md`` 的 L1 路由设计。

核心思想：不是所有任务都值得用最强模型。
按**复杂度评分**把任务分档，简单任务走小模型，从而显著降本。

复杂度公式::

    complexity = w1·reasoning_depth
               + w2·context_size_norm
               + w3·tool_call_count_norm
               + w4·retry_history_norm

并支持**失败升级**：重试次数越多，复杂度评分越高，最终升级到更强的模型。
"""

from __future__ import annotations

from dataclasses import dataclass

from devagent.config import RoutingConfig
from devagent.enums import ModelTier
from devagent.models.domain import RoutingSignals, RoutingWeights


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """模型标识：``provider`` + ``model`` 名。"""

    provider: str
    model: str

    @classmethod
    def parse(cls, spec: str) -> ModelSpec:
        provider, _, model = spec.partition(":")
        if not provider or not model:
            raise ValueError(f"非法模型标识（应为 '<provider>:<model>'）：{spec!r}")
        return cls(provider=provider, model=model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


class ComplexityRouter:
    """基于复杂度评分的模型分级路由器。

    用法::

        router = ComplexityRouter(settings.routing)
        tier = router.select_tier(RoutingSignals(reasoning_depth=0.9, ...))
        spec = router.model_for(tier)
    """

    def __init__(self, config: RoutingConfig | None = None) -> None:
        self._config = config or RoutingConfig()

    # ------------------------------------------------------------------ #
    # 档位选择
    # ------------------------------------------------------------------ #

    def weights(self) -> RoutingWeights:
        c = self._config
        return RoutingWeights(
            reasoning_depth=c.weight_reasoning_depth,
            context_size=c.weight_context_size,
            tool_calls=c.weight_tool_calls,
            retry_history=c.weight_retry_history,
        )

    def thresholds(self) -> tuple[float, float]:
        return (self._config.threshold_small_upper, self._config.threshold_medium_upper)

    def select_tier(self, signals: RoutingSignals) -> ModelTier:
        """按复杂度评分选择模型档位。

        Args:
            signals: 复杂度信号。

        Returns:
            对应的 ``ModelTier``。
        """
        complexity = signals.complexity(self.weights())
        small_upper, medium_upper = self.thresholds()

        if complexity < small_upper:
            return ModelTier.SMALL
        if complexity < medium_upper:
            return ModelTier.MEDIUM
        return ModelTier.LARGE

    def select_tier_for_retry(self, signals: RoutingSignals, attempt: int) -> ModelTier:
        """重试场景的档位选择（失败升级策略）。

        每次重试把 ``retry_history_norm`` 上调，从而推动档位升级：
        小模型连续失败 → 中模型 → 大模型。
        """
        if attempt <= 1:
            return self.select_tier(signals)
        boosted = signals.model_copy(
            update={
                "retry_history_norm": min(1.0, signals.retry_history_norm + 0.15 * (attempt - 1))
            }
        )
        return self.select_tier(boosted)

    # ------------------------------------------------------------------ #
    # 模型解析
    # ------------------------------------------------------------------ #

    def model_for(self, tier: ModelTier) -> ModelSpec:
        """把档位映射为具体模型标识。"""
        mapping = {
            ModelTier.SMALL: self._config.small_model,
            ModelTier.MEDIUM: self._config.medium_model,
            ModelTier.LARGE: self._config.large_model,
        }
        return ModelSpec.parse(mapping[tier])

    def embedding_model(self) -> ModelSpec:
        return ModelSpec.parse(self._config.embedding_model)

    # ------------------------------------------------------------------ #
    # 解释性
    # ------------------------------------------------------------------ #

    def explain(self, signals: RoutingSignals) -> dict[str, object]:
        """生成路由决策的可解释说明（用于调试与可观测性）。"""
        weights = self.weights()
        complexity = signals.complexity(weights)
        tier = self.select_tier(signals)
        small_upper, medium_upper = self.thresholds()
        return {
            "complexity": round(complexity, 4),
            "tier": tier.value,
            "model": str(self.model_for(tier)),
            "thresholds": {"small_upper": small_upper, "medium_upper": medium_upper},
            "components": {
                "reasoning_depth": round(weights.reasoning_depth * signals.reasoning_depth, 4),
                "context_size": round(weights.context_size * signals.context_size_norm, 4),
                "tool_calls": round(weights.tool_calls * signals.tool_call_count_norm, 4),
                "retry_history": round(weights.retry_history * signals.retry_history_norm, 4),
            },
        }


def estimate_signals(
    *,
    prompt_tokens: int,
    max_context: int = 32_000,
    tool_calls: int = 0,
    max_tool_calls: int = 10,
    attempt: int = 1,
    max_attempts: int = 3,
    reasoning_depth: float = 0.5,
) -> RoutingSignals:
    """从原始指标构造 ``RoutingSignals``（归一化）。

    便捷函数，避免调用方到处手写归一化逻辑。
    """
    return RoutingSignals(
        reasoning_depth=max(0.0, min(1.0, reasoning_depth)),
        context_size_norm=max(0.0, min(1.0, prompt_tokens / max(max_context, 1))),
        tool_call_count_norm=max(0.0, min(1.0, tool_calls / max(max_tool_calls, 1))),
        retry_history_norm=max(0.0, min(1.0, (attempt - 1) / max(max_attempts - 1, 1))),
    )


__all__ = ["ComplexityRouter", "ModelSpec", "estimate_signals"]
