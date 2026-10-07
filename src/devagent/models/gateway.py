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
from collections import OrderedDict, deque
from collections.abc import Sequence
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
from devagent.models.vector_cache import VectorSemanticCache
from devagent.observability import MetricNames, get_observability

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
    records: deque[Any] = field(default_factory=lambda: deque(maxlen=1000))
    """明细环形缓冲。

    刻意用**有界** deque：网关是进程级单例，长跑服务里每个模型调用都会
    追加一条记录，而只有测试会读它 —— 无界列表等于给服务埋一个稳定增长
    的内存泄漏。聚合数字（``calls`` / ``total_*``）不受影响。
    """

    def add(self, record: Any) -> None:
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
    """精确匹配 + LRU 的响应缓存。

    这是**默认实现**，零依赖、零额外调用。语义（向量）匹配版本见
    ``devagent.models.vector_cache.VectorSemanticCache`` —— 它需要嵌入模型，
    且每次未命中都要多一次嵌入调用。

    两者都暴露 ``get`` / ``put`` / ``clear``，但向量版额外有 ``aget`` /
    ``aput``（异步）。网关通过 ``_cache_get`` / ``_cache_put`` 适配差异。

    **为什么保留这个实现而不是直接换掉**：

    1. 嵌入调用本身有成本与延迟，"更聪明但更慢"不总是更优 —— 需要对照实验；
    2. 它不依赖任何外部服务，是 `DEVAGENT_CACHE__SEMANTIC=false`（默认）时的路径，
       保证 clone 下来即可零配置运行；
    3. 向量版在嵌入失败时会降级到它，因此它是**必须存在**的兜底。
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

    def __len__(self) -> int:
        return len(self._store)


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
        cache: SemanticCache | VectorSemanticCache | None = None,
        providers: dict[str, ModelProvider] | None = None,
    ) -> None:
        self._settings = settings
        self._router = router or ComplexityRouter(settings.routing)
        cache_cfg = getattr(settings, "cache", None)
        self._cache_enabled = bool(cache_cfg.enabled) if cache_cfg is not None else True
        if cache is not None:
            self._cache: SemanticCache | VectorSemanticCache = cache
        elif cache_cfg is not None and cache_cfg.semantic:
            # 需要嵌入模型 → 用自身 embed 作为 embedder。
            # 这里传 self.embed（而非 self.embed 的绑定结果前先判断可用性），
            # 让缺失提供商的情形在**首次调用时**降级为精确匹配，
            # 而不是在构造网关时就把服务起不来。
            self._cache = VectorSemanticCache(
                embedder=self.embed,
                threshold=cache_cfg.similarity_threshold,
                max_entries=cache_cfg.max_entries,
                model_filter=cache_cfg.model_filter,
            )
        else:
            self._cache = SemanticCache(max_entries=cache_cfg.max_entries if cache_cfg else 512)
        self._providers = providers if providers is not None else self._build_providers(settings)
        self.ledger = CostLedger()
        self._unpriced_models: set[str] = set()
        """已告警过的"价格表未命中"模型，避免每次调用都刷一条日志。"""

    @property
    def provider_names(self) -> list[str]:
        """网关**实际可用**的模型提供商名字。

        为什么需要它，而不是让调用方去读配置：

        `/health` 早先直接读 ``settings.models``（配置），于是演示模式下
        会报告「未配置任何模型」—— 而实际上 ``DemoProvider`` 已经注入网关，
        任务跑得好好的。前端据此弹出「未配置模型提供商，提交的任务会失败」，
        是一条**彻头彻尾的假警报**：它吓退用户，而用户试一下会发现任务照样成功。

        判断「能不能跑」必须看**运行时实际注册了什么**，而不是配置里写了什么。
        这两者在以下场景会分叉：
          - 演示/冒烟模式：注入脚本化假模型，配置为空；
          - 测试：注入 stub provider；
          - 生产降级：某家 Key 失效后被摘除。
        """
        return sorted(self._providers.keys())

    # ------------------------------------------------------------------ #
    # 缓存适配
    # ------------------------------------------------------------------ #

    async def _cache_get(
        self, messages: list[ChatMessage], model: str, temperature: float
    ) -> ChatResult | None:
        """读缓存，兼容同步（精确匹配）与异步（向量检索）两种实现。

        这里用 ``isinstance`` 而不是鸭子类型：两种实现的**语义不同**
        （向量版会额外触发一次嵌入调用 + 计算，精确版是纯字典查找），
        调用方需要知道自己在走哪条路径。显式判断比"看起来一样"更诚实。
        """
        if isinstance(self._cache, VectorSemanticCache):
            return await self._cache.aget(messages, model, temperature)
        return self._cache.get(messages, model, temperature)

    async def _cache_put(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float,
        result: ChatResult,
    ) -> None:
        if isinstance(self._cache, VectorSemanticCache):
            await self._cache.aput(messages, model, temperature, result)
        else:
            self._cache.put(messages, model, temperature, result)

    @property
    def cache(self) -> SemanticCache | VectorSemanticCache:
        """当前缓存实现（供诊断与统计读取）。"""
        return self._cache

    def cache_stats(self) -> dict[str, Any]:
        """缓存统计。向量版会多出 exact/semantic 的区分 —— 这是升级的度量依据。"""
        if isinstance(self._cache, VectorSemanticCache):
            return {"mode": "vector", **self._cache.stats.as_dict(), "size": self._cache.size}
        return {
            "mode": "exact",
            "hits": self._cache.hits,
            "misses": self._cache.misses,
            "size": len(self._cache),
        }

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
        if use_cache and self._cache_enabled and not tools:
            cached = await self._cache_get(messages, str(spec), temperature)
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

            cost = self._cost_or_warn(provider, result.usage, candidate.model)
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

            if use_cache and self._cache_enabled and not tools:
                # ★ 只在「不是因为主模型故障而降级」时写缓存。
                # 早先无条件写入、且用的是请求的 spec：某次 deepseek 5xx 之后
                # qwen 的答案被存进 deepseek 的桶，等 deepseek 恢复，
                # 同一个请求会命中那次降级产生的答案 —— 与模块文档承诺的
                # 「跨桶不互相召回」正好相反。
                #
                # 注意判据是「主模型**可用**却没用上」，而不是「candidate != spec」：
                # 主模型压根没配置（路由默认指向一个没有 Key 的提供商）时，
                # 降级是常态而非异常，此时缓存它是正确且必要的 ——
                # 唯一的替代方案是把同一个请求再打一遍。
                degraded_from_healthy_primary = (
                    candidate != spec and spec.provider in self._providers
                )
                if degraded_from_healthy_primary:
                    logger.info(
                        "cache_skip_fallback_result",
                        requested=str(spec),
                        used=str(candidate),
                        reason="主模型可用但本次失败，降级结果不入缓存",
                    )
                else:
                    await self._cache_put(messages, str(spec), temperature, result)

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

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """生成文本向量（用于上下文装配的相关性计算与语义缓存）。

        入参声明为 ``Sequence[str]`` 而非 ``list[str]``：``Callable`` 的参数是
        **逆变**的，缓存侧的 ``Embedder`` 协议声明的是 ``Sequence[str]``，
        若这里收窄成 ``list[str]`` 就无法作为 embedder 传入（mypy 会拒绝）。
        放宽到 ``Sequence`` 既满足类型系统，也确实更宽容 —— 调用方不必先
        把元组或生成器转成列表。
        """
        spec = self._router.embedding_model()
        provider = self._providers.get(spec.provider)
        if provider is None:
            raise ModelError(f"嵌入模型提供商不可用：{spec.provider}")
        return await provider.embed(list(texts), model=spec.model)

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

    def _cost_or_warn(self, provider: ModelProvider, usage: TokenUsage, model: str) -> float:
        """估算成本；价格表未命中时**必须留痕**。

        为什么：``Providers.estimate_cost`` 在 ``PRICES`` 找不到模型时直接
        ``return 0.0``。于是任何配了价格表外模型名的部署（换型号、厂商返回
        带日期的快照 id）都会把真实花费静默记成 $0 —— 成本账本、任务成本、
        评测报告一起偏低，而且没有任何地方能看出来。
        """
        cost = self._estimate_cost(provider, usage, model)
        tokens = usage.prompt_tokens + usage.completion_tokens
        if cost == 0.0 and tokens > 0 and model not in self._unpriced_models:
            self._unpriced_models.add(model)
            logger.warning(
                "model_price_unknown",
                provider=getattr(provider, "name", "?"),
                model=model,
                hint="该模型不在价格表中，成本将记为 0；请补充价格表或改用已定价模型",
            )
            get_observability().inc(MetricNames.LLM_UNPRICED_CALLS, 1, model=model)
        return cost


__all__ = [
    "CallRecord",
    "CostLedger",
    "ModelGateway",
    "SemanticCache",
    "VectorSemanticCache",
]
