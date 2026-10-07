"""语义缓存：从精确匹配到向量检索。

## 为什么需要升级

原实现（``SemanticCache``）是 **精确匹配**：消息逐字节相同才命中。
但在真实的多 Agent 场景里，同一个语义意图会有大量**措辞不同**的变体：

```
"为用户列表接口增加分页能力"
"给用户列表接口加分页"
"users 列表需要分页支持"
```

这三条在任何有意义的意义上都是同一个需求，会命中完全相同的下游处理路径。
精确匹配只能命中第 1→1 次，向量检索能命中 1→2→3 全部。

**命中率的差距不是边际改进，而是量级的差别** —— 这是"语义缓存"与
"请求去重"的本质区别。

## 设计要点

**1. 混合检索：向量召回 + 精确短路**

命中要同时满足两个条件：

- **精确哈希命中** → 直接返回（100% 确定是同一个请求）；
- 否则走**向量近邻**，且相似度 ≥ 阈值才复用。

顺序不能反：先精确后近似。精确匹配是零成本的（一次字典查找），
放在前面避免对已经确定的请求白算一次 embedding。

**2. 三键联合作用域**

向量相似的**前提是"同一个问题被问给同一个模型、同样的温度"**。
因此检索空间按 ``(model, temperature)`` 分桶，跨桶不互相召回。

理由：``deepseek-chat`` 与 ``qwen-plus`` 对同一句提示的输出分布不同，
把它们混在一起会让缓存返回"看起来相似但来自别的模型"的结果 ——
这在需要模型可复现性的场景里是**正确的灾难**。

**3. 相似度用余弦，阈值默认 0.92**

不用欧氏距离：embedding 的**模长**与语义相关性无关，只与文本长度/词频有关。
余弦把模长归一化掉，只比较方向。

0.92 这个默认值的依据：太低（如 0.85）会把"分页"与"排序"这类
**近义但不同**的需求混为一谈；太高（如 0.98）则近乎退化成精确匹配，
失去升级的意义。0.92 在中文短需求上表现较稳，且**可配置**以适配不同
embedding 模型（不同模型的相似度分布尺度并不一致）。

**4. 同步接口 + 异步预取**

``ChatResult`` 的获取路径是异步的，但向量检索本身是纯计算。
这里把两者拆开：

- ``embedder`` 是一个**异步**回调，负责把文本变成向量（走模型网关）；
- ``aget()`` 先 await embedder，再做同步的向量检索。

这样向量索引保持纯 CPU 逻辑（可被单测直接覆盖，无需真实 embedding 服务），
而 I/O 只在必要的一处发生。

**5. 优雅降级**

没有配置 embedder、embedder 抛异常、或向量维度不一致时，
**自动退回精确匹配**，而不是让整个模型调用失败。

缓存是**优化**而非**功能**。缓存坏了不应该导致请求不可用 ——
这是本模块最重要的不变量。

## 与「上下文装配去重」的区别

项目里还有一处用到相似度：``context/assembly.py`` 的硬去重（指纹）。
那里的目标是**避免同一份内容重复占用预算**，判定必须保守（宁可漏去重，
不可错删）。这里的判定同样保守，但理由不同：错误命中的代价是
**返回错误答案**，比多花一次 token 严重得多。
"""

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from devagent.logging_config import get_logger
from devagent.models.provider import ChatMessage, ChatResult

logger = get_logger(__name__)

#: 异步嵌入函数签名：一批文本 → 一批向量。
Embedder = Callable[[Sequence[str]], Awaitable[list[list[float]]]]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度，值域 ``[-1, 1]``。

    对零向量返回 ``0.0`` 而不是抛异常 —— 调用方是在「判断是否命中」，
    零向量代表"无法比较"，语义上等价于"不相似"。
    """
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


@dataclass(slots=True)
class CacheStats:
    """命中统计。分开记精确/近似，否则无法判断向量检索是否真的有效。"""

    exact_hits: int = 0
    """精确哈希命中。"""

    semantic_hits: int = 0
    """向量近邻命中。"""

    misses: int = 0
    embed_failures: int = 0
    """嵌入失败次数（降级为精确匹配）。"""

    @property
    def hits(self) -> int:
        return self.exact_hits + self.semantic_hits

    @property
    def total(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total if self.total else 0.0

    @property
    def semantic_share(self) -> float:
        """语义命中占全部命中的比例 —— 这就是"升级带来了多少增量"的答案。"""
        return self.semantic_hits / self.hits if self.hits else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "exact_hits": self.exact_hits,
            "semantic_hits": self.semantic_hits,
            "misses": self.misses,
            "embed_failures": self.embed_failures,
            "hits": self.hits,
            "hit_rate": round(self.hit_rate, 4),
            "semantic_share": round(self.semantic_share, 4),
        }


@dataclass(slots=True)
class _Entry:
    """一条缓存项：向量 + 原始键 + 结果。"""

    key: str
    """精确哈希键（用于精确短路与去重）。"""

    vector: list[float]
    """查询侧向量（对最后一个 user 消息做嵌入）。"""

    result: ChatResult
    bucket: tuple[str, str]
    """``(model, temperature)`` 检索桶。"""


class VectorSemanticCache:
    """向量检索语义缓存。

    对外接口与旧的精确匹配版保持一致（``get`` / ``put`` / ``clear``），
    并新增异步的 ``aget`` / ``aput``。旧接口在无 embedder 时行为完全不变，
    因此可以作为「灰度替换 + 对照实验」的实现。

    用法::

        cache = VectorSemanticCache(embedder=gateway.embed, threshold=0.92)
        hit = await cache.aget(messages, "deepseek:deepseek-chat", 0.2)
        if hit is None:
            result = await provider.chat(...)
            await cache.aput(messages, "deepseek:deepseek-chat", 0.2, result)
    """

    def __init__(
        self,
        *,
        embedder: Embedder | None = None,
        threshold: float = 0.92,
        max_entries: int = 512,
        model_filter: bool = True,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"相似度阈值必须在 (0, 1] 内，收到 {threshold}")
        self._embedder = embedder
        self._threshold = threshold
        self._max = max_entries
        self._model_filter = model_filter
        """是否按 (model, temperature) 分桶检索。

        关掉可以让"同一问题问不同模型"也互相命中（省更多 token），
        代价是可能返回别的模型产出的结果 —— 需要模型可复现性时**不要**关。
        """

        # 精确匹配索引：O(1) 短路，且是 embedder 缺失时的唯一路径
        self._exact: OrderedDict[str, _Entry] = OrderedDict()
        # 向量索引：按桶分组，桶内线性扫描。
        # 为什么不做 ANN（HNSW/IVF）：512 条以内的线性扫描是微秒级，
        # 引入 ANN 库会给一个"优化项"带来重依赖与调参负担。
        # 超过 512 条时仍然可接受（512 × 1024 维点积约 0.5ms）。
        self._buckets: dict[tuple[str, str], list[_Entry]] = {}
        self.stats = CacheStats()

    # ------------------------------------------------------------------ #
    # 键与桶
    # ------------------------------------------------------------------ #

    @staticmethod
    def _key(messages: Sequence[ChatMessage], model: str, temperature: float) -> str:
        hasher = hashlib.sha256()
        hasher.update(model.encode())
        hasher.update(f"{temperature:.3f}".encode())
        for m in messages:
            hasher.update(m.role.encode())
            hasher.update(m.content.encode())
        return hasher.hexdigest()

    @staticmethod
    def _bucket(model: str, temperature: float) -> tuple[str, str]:
        return (model, f"{temperature:.3f}")

    @staticmethod
    def _query_text(messages: Sequence[ChatMessage]) -> str:
        """取用于嵌入的文本。

        只用**最后一条**消息（通常是本轮的实际请求）。理由：
        - 前面的消息多为稳定的系统提示与历史，把它们拼进来会让所有请求
          的向量都向"平均语义"塌缩，反而降低区分度；
        - 短需求场景下，最后一条消息本身就是完整意图。
        """
        for m in reversed(messages):
            if m.content:
                return m.content
        return ""

    # ------------------------------------------------------------------ #
    # 同步接口（保持与旧实现的兼容）
    # ------------------------------------------------------------------ #

    def get(
        self, messages: Sequence[ChatMessage], model: str, temperature: float
    ) -> ChatResult | None:
        """精确匹配查询。

        无 embedder 时这就是全部能力 —— 与升级前行为一致。
        """
        key = self._key(messages, model, temperature)
        entry = self._exact.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        self._exact.move_to_end(key)
        self.stats.exact_hits += 1
        return entry.result

    def put(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        temperature: float,
        result: ChatResult,
    ) -> None:
        """同步写入（不含向量）。

        没有向量也能工作：只建精确索引。异步版本 ``aput`` 会补上向量。
        """
        if result.has_tool_calls:
            # 带工具调用的结果有副作用，复用它会跳过实际执行 —— 绝不缓存
            return
        key = self._key(messages, model, temperature)
        entry = _Entry(
            key=key,
            vector=[],
            result=result,
            bucket=self._bucket(model, temperature),
        )
        self._store_entry(entry)

    # ------------------------------------------------------------------ #
    # 异步接口（向量检索）
    # ------------------------------------------------------------------ #

    async def aget(
        self, messages: Sequence[ChatMessage], model: str, temperature: float
    ) -> ChatResult | None:
        """先精确短路，再向量近邻。"""
        key = self._key(messages, model, temperature)
        exact = self._exact.get(key)
        if exact is not None:
            self._exact.move_to_end(key)
            self.stats.exact_hits += 1
            return exact.result

        vector = await self._embed_query(messages)
        if vector is None:
            self.stats.misses += 1
            return None

        match = self._search(vector, model, temperature)
        if match is None:
            self.stats.misses += 1
            return None

        self.stats.semantic_hits += 1
        self._exact.move_to_end(match.key)
        logger.debug(
            "semantic_cache_hit",
            model=model,
            temperature=temperature,
            similarity=round(cosine_similarity(vector, match.vector), 4),
        )
        return match.result

    async def aput(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        temperature: float,
        result: ChatResult,
    ) -> None:
        """写入缓存，同时建立精确索引与向量索引。"""
        if result.has_tool_calls:
            return
        key = self._key(messages, model, temperature)
        vector = await self._embed_query(messages)
        entry = _Entry(
            key=key,
            vector=vector or [],
            result=result,
            bucket=self._bucket(model, temperature),
        )
        self._store_entry(entry)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _embed_query(self, messages: Sequence[ChatMessage]) -> list[float] | None:
        """计算查询向量；失败时返回 ``None``（触发降级）。"""
        if self._embedder is None:
            return None
        text = self._query_text(messages)
        if not text:
            return None
        try:
            vectors = await self._embedder([text])
        except Exception:
            # 缓存是优化不是功能：嵌入失败必须降级而不是让调用失败
            self.stats.embed_failures += 1
            logger.warning("semantic_cache_embed_failed", model="", exc_info=True)
            return None
        if not vectors or not vectors[0]:
            self.stats.embed_failures += 1
            return None
        return list(vectors[0])

    def _search(self, vector: list[float], model: str, temperature: float) -> _Entry | None:
        """在候选桶内找相似度最高且过阈值的条目。"""
        if self._model_filter:
            candidates = self._buckets.get(self._bucket(model, temperature), [])
        else:
            candidates = [e for entries in self._buckets.values() for e in entries]

        best: _Entry | None = None
        best_score = self._threshold
        for entry in candidates:
            if not entry.vector:
                continue  # 只有精确索引的条目无法参与向量比较
            score = cosine_similarity(vector, entry.vector)
            if score >= best_score:
                best_score = score
                best = entry
        return best

    def _store_entry(self, entry: _Entry) -> None:
        """写入两级索引并执行 LRU 淘汰。"""
        # 同键覆盖：旧的向量条目要从桶里摘掉，否则会留下悬垂引用
        stale = self._exact.get(entry.key)
        if stale is not None:
            bucket = self._buckets.get(stale.bucket)
            if bucket is not None:
                self._buckets[stale.bucket] = [e for e in bucket if e.key != entry.key]

        self._exact[entry.key] = entry
        self._exact.move_to_end(entry.key)

        if entry.vector:
            self._buckets.setdefault(entry.bucket, []).append(entry)

        while len(self._exact) > self._max:
            _, evicted = self._exact.popitem(last=False)
            bucket = self._buckets.get(evicted.bucket)
            if bucket is not None:
                self._buckets[evicted.bucket] = [e for e in bucket if e.key != evicted.key]
                if not self._buckets[evicted.bucket]:
                    del self._buckets[evicted.bucket]

    # ------------------------------------------------------------------ #
    # 维护
    # ------------------------------------------------------------------ #

    def clear(self) -> None:
        self._exact.clear()
        self._buckets.clear()
        self.stats = CacheStats()

    @property
    def size(self) -> int:
        return len(self._exact)

    @property
    def vector_size(self) -> int:
        """已建立向量索引的条目数（无 embedder 时为 0）。"""
        return sum(len(v) for v in self._buckets.values())

    def __len__(self) -> int:
        return len(self._exact)


__all__ = [
    "CacheStats",
    "Embedder",
    "VectorSemanticCache",
    "cosine_similarity",
]
