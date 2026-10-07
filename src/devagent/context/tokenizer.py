"""Token 估算与语义相似度工具。

上下文工程依赖两个底层原语：
1. **token 估算**——装配与预算分配都以此为单位；
2. **余弦相似度**——用于相关性打分与冗余判定。

出于可测试性与零外部依赖考虑，本题默认使用启发式估算；
接入真实 tokenizer 时只需替换 ``TokenCounter`` 实现。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

Vector = npt.NDArray[np.float64]


@runtime_checkable
class TokenCounter(Protocol):
    """token 计数器接口。"""

    def count(self, text: str) -> int:
        """返回文本的 token 数。"""
        ...


class HeuristicTokenCounter:
    """启发式 token 估算器。

    规则：
    - CJK 字符按 1 token/字 计（多数中文分词器接近该量级）；
    - 其余按 4 字符 ≈ 1 token 计（BPE 经验值）；
    - 空白与换行折半计入，避免高估。

    典型误差在 ±15% 以内，足以支撑预算分配与装配决策。
    需要精确计数时，替换为本类之外的实现即可（依赖注入）。
    """

    __slots__ = ("chars_per_token", "cjk_tokens_per_char")

    def __init__(self, chars_per_token: float = 4.0, cjk_tokens_per_char: float = 1.0) -> None:
        self.chars_per_token = chars_per_token
        self.cjk_tokens_per_char = cjk_tokens_per_char

    def count(self, text: str) -> int:
        if not text:
            return 0
        cjk = 0
        other = 0
        whitespace = 0
        for ch in text:
            code = ord(ch)
            if _is_cjk(code):
                cjk += 1
            elif ch.isspace():
                whitespace += 1
            else:
                other += 1
        cjk_tokens = cjk * self.cjk_tokens_per_char
        other_tokens = other / self.chars_per_token
        return max(1, math.ceil(cjk_tokens + other_tokens + whitespace * 0.5))


def _is_cjk(code: int) -> bool:
    """判断是否为 CJK 字符（中日韩统一表意文字及常用扩展）。"""
    return (
        0x4E00 <= code <= 0x9FFF  # CJK 统一表意文字
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x20000 <= code <= 0x2A6DF  # 扩展 B
        or 0xF900 <= code <= 0xFAFF  # 兼容表意文字
        or 0x3040 <= code <= 0x30FF  # 日文假名
        or 0xAC00 <= code <= 0xD7AF  # 韩文音节
    )


def _as_vector(v: Vector | Sequence[float] | None) -> Vector | None:
    """把任意序列（tuple / list / ndarray）统一为 ndarray。

    必要性：``ContextChunk`` 是 frozen Pydantic 模型，
    传入的 ndarray 会被规范化成 tuple，因此相似度函数必须兼容两者。
    """
    if v is None:
        return None
    if isinstance(v, np.ndarray):
        return v if v.dtype == np.float64 else v.astype(np.float64)
    if isinstance(v, (tuple, list)):
        return np.asarray(v, dtype=np.float64)
    return None


def cosine_similarity(
    a: Vector | Sequence[float] | None,
    b: Vector | Sequence[float] | None,
) -> float:
    """余弦相似度，取值范围 [-1, 1]。

    任一向量为空、维度不一致或出现零范数时返回 0.0，
    使调用方无需处理异常分支（装配打分中视为「无相关性」）。
    """
    va = _as_vector(a)
    vb = _as_vector(b)
    if va is None or vb is None:
        return 0.0
    if va.size == 0 or vb.size == 0 or va.size != vb.size:
        return 0.0
    norm_a = float(np.linalg.norm(va))
    norm_b = float(np.linalg.norm(vb))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (norm_a * norm_b))


def normalize_vector(vec: Vector) -> Vector:
    """L2 归一化；零向量原样返回（由相似度函数兜底）。"""
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return vec / norm


__all__ = [
    "HeuristicTokenCounter",
    "TokenCounter",
    "Vector",
    "cosine_similarity",
    "normalize_vector",
]
