"""具体模型提供商实现。

三家国内厂商（DeepSeek / 通义千问 / 智谱 GLM）均兼容 OpenAI 协议，
差异集中在 base_url、模型名与价格表。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from devagent.models.provider import OpenAICompatibleProvider, TokenUsage


@dataclass(frozen=True, slots=True)
class PriceTable:
    """价格表（USD / 1M tokens）。

    单价随厂商调整，故集中在此处以便于更新，
    不散落在业务逻辑中。
    """

    input_per_million: float
    output_per_million: float

    def cost(self, usage: TokenUsage) -> float:
        return (
            usage.prompt_tokens / 1_000_000 * self.input_per_million
            + usage.completion_tokens / 1_000_000 * self.output_per_million
        )


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek 提供商。

    文档：https://platform.deepseek.com/api-docs
    """

    name = "deepseek"

    PRICES: ClassVar[dict[str, PriceTable]] = {
        "deepseek-chat": PriceTable(input_per_million=0.27, output_per_million=1.10),
        "deepseek-reasoner": PriceTable(input_per_million=0.55, output_per_million=2.19),
    }

    def estimate_cost(self, usage: TokenUsage, *, model: str) -> float:
        table = self.PRICES.get(model)
        return table.cost(usage) if table else 0.0

    def _extra_payload(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        # DeepSeek 支持 response_format 等，透传即可
        return {k: v for k, v in kwargs.items() if v is not None}


class QwenProvider(OpenAICompatibleProvider):
    """通义千问（阿里云 DashScope 兼容模式）提供商。

    文档：https://help.aliyun.com/zh/model-studio/
    """

    name = "qwen"

    PRICES: ClassVar[dict[str, PriceTable]] = {
        "qwen-turbo": PriceTable(input_per_million=0.05, output_per_million=0.20),
        "qwen-plus": PriceTable(input_per_million=0.40, output_per_million=1.20),
        "qwen-max": PriceTable(input_per_million=2.40, output_per_million=9.60),
    }

    def estimate_cost(self, usage: TokenUsage, *, model: str) -> float:
        table = self.PRICES.get(model)
        return table.cost(usage) if table else 0.0


class ZhipuProvider(OpenAICompatibleProvider):
    """智谱 GLM 提供商。

    文档：https://open.bigmodel.cn/dev/api
    """

    name = "zhipu"

    PRICES: ClassVar[dict[str, PriceTable]] = {
        "glm-4-flash": PriceTable(input_per_million=0.015, output_per_million=0.015),
        "glm-4-air": PriceTable(input_per_million=0.14, output_per_million=0.14),
        "glm-4-plus": PriceTable(input_per_million=7.0, output_per_million=7.0),
    }

    def estimate_cost(self, usage: TokenUsage, *, model: str) -> float:
        table = self.PRICES.get(model)
        return table.cost(usage) if table else 0.0

    def _extra_payload(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        # GLM 部分模型不支持 temperature=0，做最小保护
        payload = dict(kwargs)
        if payload.get("temperature") == 0:
            payload["temperature"] = 0.01
        return {k: v for k, v in payload.items() if v is not None}


__all__ = [
    "DeepSeekProvider",
    "PriceTable",
    "QwenProvider",
    "ZhipuProvider",
]
