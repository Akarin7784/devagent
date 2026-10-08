"""Configurable OpenAI-compatible and native Anthropic providers."""

from __future__ import annotations

import json
import time
from typing import Any

from devagent.config import ProviderConfig
from devagent.models.provider import (
    ChatMessage,
    ChatResult,
    ModelError,
    OpenAICompatibleProvider,
    TokenUsage,
    ToolSpec,
)
from devagent.models.providers import DeepSeekProvider, QwenProvider, ZhipuProvider


class ConfiguredProvider(OpenAICompatibleProvider):
    def __init__(self, name: str, config: ProviderConfig, **kwargs: Any) -> None:
        super().__init__(
            api_key=config.api_key.get_secret_value() if config.api_key else "",
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            **kwargs,
        )
        self.name = name
        self.config = config

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.auth_mode == "api-key":
            headers["api-key"] = self._api_key
        elif self.config.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        if path == "/chat/completions":
            if not self.config.send_temperature:
                payload.pop("temperature", None)
            elif self.name == "zhipu" and payload.get("temperature") == 0:
                payload["temperature"] = 0.01
            if "max_tokens" in payload and self.config.max_tokens_field != "max_tokens":
                payload[self.config.max_tokens_field] = payload.pop("max_tokens")
        return await super()._post(path, payload)

    def _extra_payload(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in kwargs.items() if value is not None}

    def estimate_cost(self, usage: TokenUsage, *, model: str) -> float:
        if self.config.input_price_per_million or self.config.output_price_per_million:
            return (
                usage.prompt_tokens * self.config.input_price_per_million
                + usage.completion_tokens * self.config.output_price_per_million
            ) / 1_000_000
        tables = {
            "deepseek": DeepSeekProvider.PRICES,
            "qwen": QwenProvider.PRICES,
            "zhipu": ZhipuProvider.PRICES,
        }
        table = tables.get(self.name, {}).get(model)
        return table.cost(usage) if table else 0.0


class AnthropicProvider(ConfiguredProvider):
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
        }

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
        conversation: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "system":
                continue
            content: list[dict[str, Any]] = []
            if message.role == "tool":
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
            else:
                if message.content:
                    content.append({"type": "text", "text": message.content})
                for call in message.tool_calls or []:
                    function = call.get("function", {})
                    content.append(
                        {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": function["name"],
                            "input": json.loads(function.get("arguments") or "{}"),
                        }
                    )
            role = "assistant" if message.role == "assistant" else "user"
            if conversation and conversation[-1]["role"] == role:
                conversation[-1]["content"].extend(content)
            else:
                conversation.append({"role": role, "content": content})
        payload: dict[str, Any] = {
            "model": model,
            "messages": conversation,
            "max_tokens": max_tokens or 4096,
        }
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        if system:
            payload["system"] = system
        if self.config.send_temperature:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        # OpenAI-only options must not leak into the native Messages API.
        for key in ("top_p", "stop_sequences"):
            if key in kwargs:
                payload[key] = kwargs[key]
        start = time.perf_counter()
        data = await self._post("/messages", payload)
        blocks = data.get("content", [])
        usage = data.get("usage", {})
        return ChatResult(
            content="\n".join(b["text"] for b in blocks if b.get("type") == "text"),
            model=data.get("model", model),
            provider=self.name,
            usage=TokenUsage(
                prompt_tokens=int(usage.get("input_tokens", 0))
                + int(usage.get("cache_creation_input_tokens", 0))
                + int(usage.get("cache_read_input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
            ),
            tool_calls=[
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                }
                for b in blocks
                if b.get("type") == "tool_use"
            ],
            finish_reason={
                "end_turn": "stop",
                "tool_use": "tool_calls",
                "max_tokens": "length",
            }.get(data.get("stop_reason", ""), "stop"),
            latency_ms=int((time.perf_counter() - start) * 1000),
            raw=data,
        )

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        _ = texts, model
        raise ModelError("Anthropic 不提供此嵌入接口，请为 embedding_model 配置其他供应商")
