"""模型网关：统一调用入口。

职责：
1. 按档位路由到具体 Provider + 模型；
2. 语义缓存（相同/近似请求直接命中）；
3. 成本记账（供可观测性与熔断使用）；
4. 供应商故障降级。

这是 Agent 层唯一应接触的模型接口——Agent 不应直接使用 Provider。
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from devagent.config import Settings
from devagent.context.routing import ComplexityRouter, ModelSpec
from devagent.enums import ModelTier
from devagent.logging_config import get_logger
from devagent.models.domain import RoutingSignals
from devagent.models.provider import (
    ChatMessage,
    ChatResult,
    ModelError,
    ModelProvider,
    TokenUsage,
    ToolSpec,
)
from devagent.models.providers import (
    DeepSeekProvider,
    QwenProvider,
    ZhipuProvider,
)
from devagent.observability import get_observability

logger = get_logger(__name__)


@dataclass(slots=True)
class CallRecord:
    """单次模型调用的记账记录。"""

    provider: str
    model: str
    tier: ModelTier | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    cached: bool = False
    error: str | None = None


@dataclass(slots=True)
class CostLedger:
    """成本账本：累计调用次数与花费。"""

    calls: int = 0
    cache_hits: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0
    records: list[CallRecord] = field(default_factory=list)

    def add(self, record: CallRecord) -> None:
        self.calls += 1
        if record.cached:
            self.cache_hits += 1
        self.total_prompt_tokens += record.prompt_tokens
        self.total_completion_tokens += record.completion_tokens
        self.total_cost_usd += record.cost_usd
        self.records.append(record)

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / self.calls if self.calls else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "cost_usd": round(self.total_cost_usd, 6),
        }


class SemanticCache:
    """语义缓存（此处为精确匹配 + LRU 的实现）。

    生产环境可替换为「embedding 相似度 + 向量检索」的语义匹配版本；
    接口保持不变，便于灰度的替换与对照实验。
    """

    def __init__(self, max_entries: int = 512) -> None:
        self._store: OrderedDict[str, ChatResult] = OrderedDict()
        self._max = max_entries
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key(messages: list[ChatMessage], model: str, temperature: float) -> str:
        hasher = hashlib.sha256()
        hasher.update(model.encode())
        hasher.update(f"{temperature:.3f}".encode())
        for m in messages:
            hasher.update(m.role.encode())
            hasher.update(m.content.encode())
        return hasher.hexdigest()

    def get(self, messages: list[ChatMessage], model: str, temperature: float) -> ChatResult | None:
        key = self._key(messages, model, temperature)
        if key in self._store:
            self._store.move_to_end(key)
            self.hits += 1
            return self._store[key]
        self.misses += 1
        return None

    def put(
        self, messages: list[ChatMessage], model: str, temperature: float, result: ChatResult
    ) -> None:
        if result.has_tool_calls:
            return  # 带工具调用的结果不应缓存（有副作用）
        key = self._key(messages, model, temperature)
        self._store[key] = result
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()
        self.hits = self.misses = 0


class ModelGateway:
    """模型网关：路由 + 缓存 + 记账 + 降级。

    用法::

        gateway = ModelGateway(settings)
        result = await gateway.chat(messages, signals=RoutingSignals(...))
        print(gateway.ledger.summary())
    """

    def __init__(
        self,
        settings: Settings,
        *,
        router: ComplexityRouter | None = None,
        cache: SemanticCache | None = None,
        providers: dict[str, ModelProvider] | None = None,
    ) -> None:
        self._settings = settings
        self._router = router or ComplexityRouter(settings.routing)
        self._cache = cache if cache is not None else SemanticCache()
        self._providers = providers if providers is not None else self._build_providers(settings)
        self.ledger = CostLedger()

    # ------------------------------------------------------------------ #
    # 提供商装配
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_providers(settings: Settings) -> dict[str, ModelProvider]:
        m = settings.models
        providers: dict[str, ModelProvider] = {}

        if m.deepseek.enabled:
            providers["deepseek"] = DeepSeekProvider(
                api_key=m.deepseek.api_key.get_secret_value(),  # type: ignore[union-attr]
                base_url=m.deepseek.base_url,
                timeout_seconds=m.deepseek.timeout_seconds,
                max_retries=m.deepseek.max_retries,
            )
        if m.qwen.enabled:
            providers["qwen"] = QwenProvider(
                api_key=m.qwen.api_key.get_secret_value(),  # type: ignore[union-attr]
                base_url=m.qwen.base_url,
                timeout_seconds=m.qwen.timeout_seconds,
                max_retries=m.qwen.max_retries,
            )
        if m.zhipu.enabled:
            providers["zhipu"] = ZhipuProvider(
                api_key=m.zhipu.api_key.get_secret_value(),  # type: ignore[union-attr]
                base_url=m.zhipu.base_url,
                timeout_seconds=m.zhipu.timeout_seconds,
                max_retries=m.zhipu.max_retries,
            )
        return providers

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        signals: RoutingSignals | None = None,
        tier: ModelTier | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: list[ToolSpec] | None = None,
        use_cache: bool = True,
        step_id: str = "",
        **kwargs: Any,
    ) -> ChatResult:
        """执行一次对话调用。

        Args:
            messages: 对话消息。
            signals: 复杂度信号；与 ``tier`` 二选一，优先显式 ``tier``。
            tier: 显式指定档位（绕过路由）。
            temperature: 采样温度。
            max_tokens: 输出上限。
            tools: 可用工具。
            use_cache: 是否使用缓存。
            step_id: 步骤标识（用于追踪）。

        Returns:
            ``ChatResult``。

        Raises:
            ModelError: 所有候选提供商均失败。
        """
        chosen_tier = tier or (self._router.select_tier(signals) if signals else ModelTier.MEDIUM)
        spec = self._router.model_for(chosen_tier)

        # 缓存查询（仅无工具调用时）
        if use_cache and not tools:
            cached = self._cache.get(messages, str(spec), temperature)
            if cached is not None:
                self.ledger.add(
                    CallRecord(
                        provider=cached.provider,
                        model=cached.model,
                        tier=chosen_tier,
                        prompt_tokens=cached.usage.prompt_tokens,
                        completion_tokens=cached.usage.completion_tokens,
                        cost_usd=0.0,
                        latency_ms=0,
                        cached=True,
                    )
                )
                logger.debug("model_cache_hit", step_id=step_id, tier=chosen_tier.value)
                get_observability().record_llm_call(
                    model=cached.model,
                    tier=chosen_tier.value,
                    input_tokens=cached.usage.prompt_tokens,
                    output_tokens=cached.usage.completion_tokens,
                    cost_usd=0.0,
                    latency_ms=0.0,
                    cached=True,
                )
                return cached

        # 按档位尝试；失败则降级到其他已启用的提供商
        candidates = self._candidate_specs(spec)
        last_error: Exception | None = None

        for candidate in candidates:
            provider = self._providers.get(candidate.provider)
            if provider is None:
                continue
            start = time.perf_counter()
            try:
                result = await provider.chat(
                    messages,
                    model=candidate.model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    **kwargs,
                )
            except ModelError as exc:
                last_error = exc
                self.ledger.add(
                    CallRecord(
                        provider=candidate.provider,
                        model=candidate.model,
                        tier=chosen_tier,
                        prompt_tokens=0,
                        completion_tokens=0,
                        cost_usd=0.0,
                        latency_ms=int((time.perf_counter() - start) * 1000),
                        error=str(exc),
                    )
                )
                logger.warning(
                    "model_call_failed",
                    provider=candidate.provider,
                    model=candidate.model,
                    error=str(exc),
                )
                get_observability().record_llm_call(
                    model=candidate.model,
                    tier=chosen_tier.value,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    latency_ms=float(int((time.perf_counter() - start) * 1000)),
                    status="error",
                )
                continue

            cost = self._estimate_cost(provider, result.usage, candidate.model)
            self.ledger.add(
                CallRecord(
                    provider=candidate.provider,
                    model=candidate.model,
                    tier=chosen_tier,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    cost_usd=cost,
                    latency_ms=result.latency_ms,
                )
            )

            if use_cache and not tools:
                self._cache.put(messages, str(spec), temperature, result)

            get_observability().record_llm_call(
                model=result.model,
                tier=chosen_tier.value,
                input_tokens=result.usage.prompt_tokens,
                output_tokens=result.usage.completion_tokens,
                cost_usd=cost,
                latency_ms=float(result.latency_ms),
            )

            if candidate != spec:
                logger.info(
                    "model_fallback_used",
                    requested=str(spec),
                    used=str(candidate),
                    step_id=step_id,
                )
            return result

        raise last_error or ModelError(
            f"没有可用提供商处理档位 {chosen_tier.value}（检查 API Key 配置）"
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成文本向量（用于上下文装配的相关性计算）。"""
        spec = self._router.embedding_model()
        provider = self._providers.get(spec.provider)
        if provider is None:
            raise ModelError(f"嵌入模型提供商不可用：{spec.provider}")
        return await provider.embed(texts, model=spec.model)

    async def aclose(self) -> None:
        for provider in self._providers.values():
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _candidate_specs(self, primary: ModelSpec) -> list[ModelSpec]:
        """构造候选模型列表：首选在前，其他已启用提供商同级模型在后（降级）。"""
        fallback_map = {
            ModelTier.SMALL: ["qwen:qwen-turbo", "zhipu:glm-4-flash"],
            ModelTier.MEDIUM: ["deepseek:deepseek-chat", "qwen:qwen-plus", "zhipu:glm-4-air"],
            ModelTier.LARGE: ["deepseek:deepseek-reasoner", "qwen:qwen-max", "zhipu:glm-4-plus"],
        }
        tier = next(
            (t for t in ModelTier if self._router.model_for(t) == primary), ModelTier.MEDIUM
        )
        specs = [primary]
        for spec_str in fallback_map.get(tier, []):
            spec = ModelSpec.parse(spec_str)
            if spec != primary and spec.provider in self._providers:
                specs.append(spec)
        return specs

    @staticmethod
    def _estimate_cost(provider: ModelProvider, usage: TokenUsage, model: str) -> float:
        estimator = getattr(provider, "estimate_cost", None)
        if estimator is None:
            return 0.0
        result: float = estimator(usage, model=model)
        return result


__all__ = [
    "CallRecord",
    "CostLedger",
    "ModelGateway",
    "SemanticCache",
]
