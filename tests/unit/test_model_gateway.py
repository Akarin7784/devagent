"""模型网关的单元测试。

使用 FakeProvider 注入，不产生真实网络调用，保证 CI 可离线运行。
"""

from __future__ import annotations

from typing import Any

import pytest

from devagent.config import Settings
from devagent.enums import ModelTier
from devagent.models.domain import RoutingSignals
from devagent.models.gateway import ModelGateway, SemanticCache
from devagent.models.provider import (
    ChatMessage,
    ChatResult,
    ModelError,
    TokenUsage,
    ToolSpec,
)
from devagent.models.providers import PriceTable, QwenProvider


class FakeProvider:
    """可编程的假提供商。"""

    def __init__(
        self,
        *,
        reply: str = "ok",
        fail_times: int = 0,
        error: Exception | None = None,
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
        tool_calls: list[dict[str, Any]] | None = None,
        provider_name: str = "fake",
    ) -> None:
        self.reply = reply
        self.fail_times = fail_times
        self.error = error or ModelError("fake failure")
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.tool_calls = tool_calls or []
        self.name = provider_name
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(
            {"model": model, "messages": messages, "temperature": temperature, "tools": tools}
        )
        if len(self.calls) <= self.fail_times:
            raise self.error
        usage = TokenUsage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.prompt_tokens + self.completion_tokens,
        )
        return ChatResult(
            content=self.reply,
            model=model,
            provider=self.name,
            usage=usage,
            tool_calls=self.tool_calls,
            latency_ms=10,
        )

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        return [[float(len(t)), 1.0] for t in texts]


@pytest.fixture
def settings() -> Settings:
    return Settings()


def _msgs() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="hello")]


class TestRouting:
    async def test_routes_by_signals(self, settings: Settings) -> None:
        provider = FakeProvider()
        gateway = ModelGateway(settings, providers={"deepseek": provider, "qwen": provider})

        await gateway.chat(
            _msgs(),
            signals=RoutingSignals(reasoning_depth=0.05, context_size_norm=0.05),
            use_cache=False,
        )
        assert provider.calls[0]["model"] == "qwen-turbo", "简单任务应走小模型"

    async def test_explicit_tier_overrides_signals(self, settings: Settings) -> None:
        provider = FakeProvider()
        gateway = ModelGateway(settings, providers={"deepseek": provider, "qwen": provider})

        await gateway.chat(
            _msgs(),
            signals=RoutingSignals(reasoning_depth=0.0),
            tier=ModelTier.LARGE,
            use_cache=False,
        )
        assert provider.calls[0]["model"] == "deepseek-reasoner"

    async def test_complex_task_routes_to_large(self, settings: Settings) -> None:
        provider = FakeProvider()
        gateway = ModelGateway(settings, providers={"deepseek": provider})
        await gateway.chat(
            _msgs(),
            signals=RoutingSignals(
                reasoning_depth=1.0,
                context_size_norm=1.0,
                tool_call_count_norm=1.0,
                retry_history_norm=1.0,
            ),
            use_cache=False,
        )
        assert provider.calls[0]["model"] == "deepseek-reasoner"


class TestCache:
    async def test_cache_hit_avoids_second_call(self, settings: Settings) -> None:
        provider = FakeProvider()
        gateway = ModelGateway(settings, providers={"deepseek": provider})

        await gateway.chat(_msgs())
        await gateway.chat(_msgs())

        assert len(provider.calls) == 1, "相同请求应命中缓存"
        assert gateway.ledger.cache_hits == 1

    async def test_cache_disabled(self, settings: Settings) -> None:
        provider = FakeProvider()
        gateway = ModelGateway(settings, providers={"deepseek": provider})
        await gateway.chat(_msgs(), use_cache=False)
        await gateway.chat(_msgs(), use_cache=False)
        assert len(provider.calls) == 2

    async def test_tool_calls_not_cached(self, settings: Settings) -> None:
        provider = FakeProvider(tool_calls=[{"id": "1", "type": "function"}])
        gateway = ModelGateway(settings, providers={"deepseek": provider})
        tools = [ToolSpec(name="t", description="d", parameters={})]
        await gateway.chat(_msgs(), tools=tools)
        await gateway.chat(_msgs(), tools=tools)
        # 带工具调用不缓存
        assert len(provider.calls) == 2

    async def test_cache_lru_eviction(self) -> None:
        cache = SemanticCache(max_entries=2)
        from devagent.models.provider import ChatResult

        def res(text: str) -> ChatResult:
            return ChatResult(content=text, model="m", provider="p")

        m1 = [ChatMessage(role="user", content="1")]
        m2 = [ChatMessage(role="user", content="2")]
        m3 = [ChatMessage(role="user", content="3")]
        cache.put(m1, "m", 0.2, res("1"))
        cache.put(m2, "m", 0.2, res("2"))
        cache.put(m3, "m", 0.2, res("3"))  # 淘汰 m1

        assert cache.get(m1, "m", 0.2) is None
        assert cache.get(m2, "m", 0.2) is not None
        assert cache.get(m3, "m", 0.2) is not None


class TestFallback:
    async def test_falls_back_to_other_provider(self, settings: Settings) -> None:
        primary = FakeProvider(fail_times=1)  # 首次失败
        fallback = FakeProvider(reply="fallback", provider_name="fallback")
        gateway = ModelGateway(
            settings,
            providers={"deepseek": primary, "qwen": fallback, "zhipu": fallback},
        )
        result = await gateway.chat(_msgs(), tier=ModelTier.MEDIUM, use_cache=False)
        assert result.content == "fallback"
        assert len(primary.calls) == 1
        assert len(fallback.calls) >= 1

    async def test_raises_when_all_providers_fail(self, settings: Settings) -> None:
        failing = FakeProvider(fail_times=99)
        gateway = ModelGateway(
            settings, providers={"deepseek": failing, "qwen": failing, "zhipu": failing}
        )
        with pytest.raises(ModelError):
            await gateway.chat(_msgs(), tier=ModelTier.MEDIUM, use_cache=False)

    async def test_no_provider_configured_raises(self, settings: Settings) -> None:
        gateway = ModelGateway(settings, providers={})
        with pytest.raises(ModelError, match="没有可用提供商"):
            await gateway.chat(_msgs(), use_cache=False)


class TestLedger:
    async def test_ledger_records_usage(self, settings: Settings) -> None:
        provider = FakeProvider(prompt_tokens=1000, completion_tokens=500)
        gateway = ModelGateway(settings, providers={"deepseek": provider})
        await gateway.chat(_msgs(), use_cache=False)

        assert gateway.ledger.calls == 1
        assert gateway.ledger.total_prompt_tokens == 1000
        assert gateway.ledger.total_completion_tokens == 500

    async def test_ledger_records_errors(self, settings: Settings) -> None:
        failing = FakeProvider(fail_times=99)
        gateway = ModelGateway(settings, providers={"deepseek": failing})
        with pytest.raises(ModelError):
            await gateway.chat(_msgs(), tier=ModelTier.LARGE, use_cache=False)
        errors = [r for r in gateway.ledger.records if r.error]
        assert errors, "失败调用也应记账（便于统计失败率）"

    async def test_summary_shape(self, settings: Settings) -> None:
        provider = FakeProvider()
        gateway = ModelGateway(settings, providers={"deepseek": provider})
        await gateway.chat(_msgs())
        summary = gateway.ledger.summary()
        assert set(summary) == {
            "calls",
            "cache_hits",
            "cache_hit_rate",
            "prompt_tokens",
            "completion_tokens",
            "cost_usd",
        }


class TestCostEstimation:
    def test_price_table_cost(self) -> None:
        table = PriceTable(input_per_million=1.0, output_per_million=2.0)
        usage = TokenUsage(prompt_tokens=1_000_000, completion_tokens=500_000)
        assert table.cost(usage) == pytest.approx(2.0)

    def test_known_provider_cost(self) -> None:
        p = QwenProvider(api_key="x", base_url="http://x")
        usage = TokenUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
        cost = p.estimate_cost(usage, model="qwen-turbo")
        assert cost > 0

    def test_unknown_model_cost_zero(self) -> None:
        p = QwenProvider(api_key="x", base_url="http://x")
        assert p.estimate_cost(TokenUsage(), model="nonexistent") == 0.0


class TestEmbed:
    async def test_embed_delegates_to_provider(self, settings: Settings) -> None:
        provider = FakeProvider()
        gateway = ModelGateway(settings, providers={"qwen": provider})
        vecs = await gateway.embed(["hello", "world!"])
        assert len(vecs) == 2
        assert vecs[0][0] == 5.0  # len("hello")
        assert vecs[1][0] == 6.0  # len("world!")

    async def test_embed_without_provider_raises(self, settings: Settings) -> None:
        gateway = ModelGateway(settings, providers={})
        with pytest.raises(ModelError, match="嵌入模型提供商不可用"):
            await gateway.embed(["x"])
