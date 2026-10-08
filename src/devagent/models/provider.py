"""模型提供商抽象层。

统一不同厂商（DeepSeek / 通义 / 智谱）的 Chat 与 Embedding 接口，
使上层（Agent / 评测）无需关心具体厂商差异。

设计要点：
- ``ModelProvider`` 是 Protocol，便于测试注入 fake 实现；
- ``ChatMessage`` / ``ChatResult`` 是**厂商无关**的中间表示；
- 所有 Provider 都基于 OpenAI 兼容协议（三家均支持），
  因此共享 ``OpenAICompatibleProvider`` 基类，仅需覆盖差异点。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import httpx

from devagent.logging_config import get_logger
from devagent.observability import MetricNames, get_observability

logger = get_logger(__name__)

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ChatMessage:
    """厂商无关的对话消息。"""

    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    def to_openai(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            out["name"] = self.name
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            out["tool_calls"] = self.tool_calls
        return out


@dataclass(slots=True)
class ToolSpec:
    """工具（Function Calling）规格。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True, init=False)
class TokenUsage:
    """token 用量。

    ★ 设计要点：``total_tokens`` 是**派生属性**而不是独立字段。

    早期实现把它做成可写字段，结果出现了一个隐蔽 bug：很多构造点只填
    ``prompt_tokens``/``completion_tokens``（因为成本估算只需要这两项），
    忘了填 ``total_tokens``，于是熔断器读到的永远是 0 —— 表现为
    「token 预算形同虚设，任务永远不会因超支而暂停」。

    改成派生属性后，``total`` 在构造上就不可能不一致：

    - 若提供了 total 且大于分项之和（服务端上报更完整），采信 total；
    - 否则一律以 prompt + completion 为准。
    """

    prompt_tokens: int
    completion_tokens: int
    _total_tokens: int

    def __init__(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        computed = prompt_tokens + completion_tokens
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        # 只在服务端给了更大的值时采信；否则以分项之和为准，保证一致性
        self._total_tokens = total_tokens if total_tokens > computed else computed

    @property
    def total_tokens(self) -> int:
        """总 token 数：恒等于 max(分项之和, 服务端上报值)。"""
        return self._total_tokens

    @property
    def cost_usd(self) -> float:
        """**占位实现**：真实成本请由 Provider 覆盖 ``estimate_cost``。"""
        return 0.0


@dataclass(slots=True)
class ChatResult:
    """一次模型调用的结果。"""

    content: str
    model: str
    provider: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class ModelError(RuntimeError):
    """模型调用错误基类。"""


class ModelTimeoutError(ModelError):
    """调用超时。"""


class ModelRateLimitError(ModelError):
    """被限流。"""


class ModelContentFilterError(ModelError):
    """内容被拒绝。"""


@runtime_checkable
class ModelProvider(Protocol):
    """模型提供商协议。

    所有实现必须保证：
    - ``chat`` 在失败时抛出 ``ModelError`` 的子类（而非静默返回空）；
    - 不吞掉异常，便于上层做重试与降级决策。
    """

    name: str

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> ChatResult: ...

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]: ...


class OpenAICompatibleProvider:
    """OpenAI 兼容协议提供商基类。

    DeepSeek / 通义（兼容模式）/ 智谱 均支持该协议，
    差异仅在 base_url、模型名与少量参数。

    说明：**不继承 ABC**。因为该基类自身已实现全部方法（``chat``/``embed``
    都通过共享的 HTTP 逻辑完成），子类只是填配置。强行声明 ABC 会触发
    「抽象基类却没有抽象方法」的告警，且没有实际收益。
    """

    name: str = "unknown"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 120.0,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ #
    # HTTP 基础设施
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """带重试的 POST 请求。

        重试策略：指数退避；仅对超时与 5xx/429 重试，
        4xx（除 429）立即失败（避免无效重试）。
        """
        client = await self._get_client()
        url = f"{self._base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 2):
            try:
                resp = await client.post(url, headers=self._headers(), json=payload)
                if resp.status_code == 429:
                    raise ModelRateLimitError(f"限流（{resp.status_code}）")
                if resp.status_code >= 500:
                    raise ModelError(f"服务端错误 {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    body = resp.text[:500]
                    if "content" in body and "filter" in body.lower():
                        raise ModelContentFilterError(body)
                    raise ModelError(f"客户端错误 {resp.status_code}: {body}")
                data: dict[str, Any] = resp.json()
                return data
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = ModelTimeoutError(str(exc))
            except (ModelRateLimitError, ModelError) as exc:
                last_error = exc
                # 5xx / 429 可重试；其它客户端错误立即抛出。
                if not isinstance(exc, ModelRateLimitError) and resp.status_code < 500:
                    raise
            if attempt <= self._max_retries:
                # ★ 退避必须带抖动。
                # 纯指数退避会让**所有**并发调用在同一时刻重试：编排器默认
                # 并行 4 个节点，遇到 429 时它们会在 1s / 2s / 4s 同时打回来，
                # 把一个限流抖动放大成一次限流雪崩。抖动把重试打散。
                base = min(2.0 ** (attempt - 1), 8.0)
                backoff = base * (0.5 + random.random() * 0.5)
                logger.warning(
                    "model_call_retry",
                    provider=self.name,
                    attempt=attempt,
                    backoff=round(backoff, 3),
                    error=str(last_error),
                )
                get_observability().inc(
                    MetricNames.LLM_RETRIES, 1, model=self.name, reason="transport_or_ratelimit"
                )
                await asyncio.sleep(backoff)

        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #

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
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_openai() for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"
        payload.update(self._extra_payload(kwargs))

        start = time.perf_counter()
        data = await self._post("/chat/completions", payload)
        latency_ms = int((time.perf_counter() - start) * 1000)

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage_raw = data.get("usage") or {}

        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            total_tokens=int(usage_raw.get("total_tokens", 0)),
        )

        return ChatResult(
            content=message.get("content") or "",
            model=data.get("model", model),
            provider=self.name,
            usage=usage,
            tool_calls=list(message.get("tool_calls") or []),
            finish_reason=choice.get("finish_reason", "stop"),
            latency_ms=latency_ms,
            raw=data,
        )

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        data = await self._post(
            "/embeddings", {"model": model, "input": texts, "encoding_format": "float"}
        )
        items = data.get("data") or []
        # 按 index 排序，保证与输入顺序一致
        items_sorted = sorted(items, key=lambda d: d.get("index", 0))
        return [list(item["embedding"]) for item in items_sorted]

    # ------------------------------------------------------------------ #
    # 子类可覆盖的差异点
    # ------------------------------------------------------------------ #

    def _extra_payload(self, kwargs: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """厂商特有参数注入点（默认无额外参数，子类覆盖）。"""
        return {}

    def estimate_cost(self, usage: TokenUsage, *, model: str) -> float:  # noqa: ARG002
        """按厂商价格表估算成本（USD）。

        默认返回 0；具体 Provider 覆盖。价格随厂商调整，
        因此实现中把单价作为可配置项，不硬编码在逻辑里。
        """
        return 0.0


__all__ = [
    "ChatMessage",
    "ChatResult",
    "ModelContentFilterError",
    "ModelError",
    "ModelProvider",
    "ModelRateLimitError",
    "ModelTimeoutError",
    "OpenAICompatibleProvider",
    "Role",
    "TokenUsage",
    "ToolSpec",
]
