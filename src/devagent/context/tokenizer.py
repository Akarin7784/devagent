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


# --------------------------------------------------------------------------- #
# 词法相似度：向量不可用时的**兜底**语义近似
# --------------------------------------------------------------------------- #

_SHINGLE_SIZE = 2
"""字符 n-gram 的 n。

为什么是字符而不是词：本项目语料以中文与代码为主。中文没有空格分词，
代码里的标识符也会被空格切碎，唯有**字符 n-gram** 对两者都不需要分词器，
且对「同一段文字只差几个字」这类近似重复足够敏感。
"""


def _shingles(text: str) -> set[str]:
    """把文本切成字符 n-gram 集合（空白归一化后）。

    归一化的理由：上下文片段里换行/缩进的差异不代表语义差异，
    不做归一化会让「同一段话的两种缩进」被判为不相似。
    """
    normalized = "".join(text.split())
    if not normalized:
        return set()
    if len(normalized) <= _SHINGLE_SIZE:
        return {normalized}
    return {normalized[i : i + _SHINGLE_SIZE] for i in range(len(normalized) - _SHINGLE_SIZE + 1)}


def lexical_similarity(a: str, b: str) -> float:
    """基于字符 n-gram 的 Jaccard 相似度，取值 ``[0, 1]``。

    存在意义：装配算法的**相关性与冗余判定原本只依赖 embedding**，
    而 embedding 是可选能力（需要额外的嵌入模型调用）。一旦没有向量，
    ``cosine_similarity`` 恒返回 0，于是「硬去重」永不触发、
    「冗余惩罚」恒为 0 —— 整套打分退化成按插入顺序取片段。

    这是最危险的一类退化：**功能静默失效，而测试仍然全绿**
    （因为单测都手工构造了向量）。因此这里提供一个不依赖外部服务的
    确定性兜底，保证「没有嵌入模型」时算法依然按设计意图工作。

    空文本与空文本的相似度定义为 0（而不是 1）：两个空片段不构成
    「互为冗余」的理由，否则会把它们互相去重掉。
    """
    sa = _shingles(a)
    sb = _shingles(b)
    if not sa or not sb:
        return 0.0
    intersection = len(sa & sb)
    union = len(sa | sb)
    return intersection / union if union else 0.0


def estimate_info_units(text: str) -> int:
    """估算文本的「有效信息单元」数（供信息密度打分使用）。

    早先的实现是 ``len(content.split(". "))``：那对英文尚可，
    对**中文恒为 1**（中文句号是 ``。`` 且后面不跟空格），
    于是密度项 ``info_units / tokens`` 对中文语料几乎恒为 0 —— 又一个
    「看起来在工作、实际不起作用」的指标。

    这里按中英文句子边界切分，并给代码块内常见的 ``;`` ``{`` ``}``
    换行留出计数，使中文与代码两条路径都有区分度。
    """
    if not text.strip():
        return 0
    boundaries = "。！？；!?;\n"
    units = 1
    for ch in text:
        if ch in boundaries:
            units += 1
    return max(1, units)


__all__ = [
    "HeuristicTokenCounter",
    "TokenCounter",
    "Vector",
    "cosine_similarity",
    "estimate_info_units",
    "lexical_similarity",
    "normalize_vector",
]
