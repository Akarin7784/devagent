"""向量语义缓存测试。

## 测试策略

**关键决策：不依赖真实 embedding 服务。**

缓存模块的价值在于「判定逻辑」——阈值、分桶、降级、淘汰。
这些全部可以在纯 CPU 上验证。因此这里注入一个**确定性的假 embedder**：
把文本映射成可控的向量，从而精确地构造出「相似」与「不相似」两种情形。

真实 embedding 质量（是否真能把"分页"与"分页支持"判为相似）属于
**模型能力**，不是本模块的责任，也不该让单测依赖外部 API。

假 embedder 的实现方式：用字符袋向量（每个维度对应字符表中的一个字），
让「共享字符多的文本」余弦相似度高。这样测试的**期望值是自解释的**，
而不是一堆魔数。

### 为什么测试里的阈值是 0.8 而不是产品默认的 0.92

字符袋向量的余弦有确定的上界关系：若两条文本的字符集分别为
``A`` 与 ``B``，则相似度约为 ``|A ∩ B| / sqrt(|A| · |B|)``。
例如「用户分页」(4 字) vs「用户分页列表」(6 字) = ``4 / sqrt(24) ≈ 0.816``
——**即使 100% 重合的部分是"核心语义"，也会被新字符稀释**。

这是字符袋模型的固有特性，不是缓存的缺陷。真实 embedding 模型
（如 text-embedding-v3）会把「用户分页」与「用户分页列表」判到 0.95+，
因为它们编码的是语义而非字面。

因此测试用 0.8 作为「相似」的门槛，而"不相似"的对照
（「用户分页」vs「鉴权」= 0.0）与它之间有巨大间隔，
足以验证**判定逻辑本身**的正确性 —— 这确实是本文件要测的东西。
真实模型下的阈值表现属于模型能力，不该由单测断言。
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from devagent.models.provider import ChatMessage, ChatResult
from devagent.models.vector_cache import (
    CacheStats,
    VectorSemanticCache,
    cosine_similarity,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------- #
# 测试替身
# ---------------------------------------------------------------------- #


def _msgs(text: str) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=text)]


def _result(text: str = "ok") -> ChatResult:
    return ChatResult(content=text, model="m", provider="p")


#: 用于构造向量的字符表。取一段常用汉字，保证测试里的文本都能被表达。
_ALPHABET = "分页列表用户接口增加支持能力排序搜索导出缓存鉴权校验重试限流"


def _bag_vector(text: str, size: int = len(_ALPHABET)) -> list[float]:
    """字符袋向量：每个维度对应字符表中的一个字的出现次数。

    这是最朴素的"词袋"模型。它不完美，但**足够可预测**：
    两段文本共享的字越多，向量方向越接近，余弦相似度越高。
    测试里可以直接从文本推出期望的相似关系，无需硬编码数值。
    """
    vec = [0.0] * size
    for ch in text:
        if ch in _ALPHABET:
            vec[_ALPHABET.index(ch)] += 1.0
    if not any(vec):
        vec[0] = 1.0  # 避免零向量导致相似度恒为 0
    return vec


class FakeEmbedder:
    """确定性嵌入器，并记录被调用次数（用于验证"精确命中不触发嵌入"）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    async def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("嵌入服务不可用")
        return [_bag_vector(t) for t in texts]

    @property
    def call_count(self) -> int:
        return len(self.calls)


# ---------------------------------------------------------------------- #
# 余弦相似度（纯函数）
# ---------------------------------------------------------------------- #


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_scale_invariant(self) -> None:
        """模长必须被归一化掉 —— 这正是用余弦而非欧氏距离的原因。

        否则「长文本 vs 短文本」会被判为不相似，而它们语义可以完全一致。
        """
        assert cosine_similarity([1.0, 2.0], [100.0, 200.0]) == pytest.approx(1.0)

    def test_zero_vector_returns_zero_not_error(self) -> None:
        """零向量代表"无法比较"，等价于不相似 —— 不抛异常。"""
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert cosine_similarity([1.0, 1.0], [0.0, 0.0]) == 0.0

    def test_dimension_mismatch_returns_zero(self) -> None:
        """维度不一致时返回 0 而不是 IndexError。

        真实的坑：换 embedding 模型后维度从 1024 变 1536，
        旧缓存条目还在。此时应静默不命中，而不是让请求崩溃。
        """
        assert cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


# ---------------------------------------------------------------------- #
# 构造与校验
# ---------------------------------------------------------------------- #


class TestConstruction:
    def test_rejects_invalid_threshold(self) -> None:
        with pytest.raises(ValueError, match="阈值"):
            VectorSemanticCache(threshold=0.0)
        with pytest.raises(ValueError, match="阈值"):
            VectorSemanticCache(threshold=1.5)

    def test_accepts_boundary_threshold(self) -> None:
        VectorSemanticCache(threshold=1.0)

    def test_starts_empty(self) -> None:
        cache = VectorSemanticCache()
        assert len(cache) == 0
        assert cache.size == 0
        assert cache.vector_size == 0


# ---------------------------------------------------------------------- #
# 精确匹配路径（无 embedder 时的全部能力）
# ---------------------------------------------------------------------- #


class TestExactPath:
    def test_exact_hit_without_embedder(self) -> None:
        """无 embedder 时行为与升级前完全一致 —— 这是可安全上线的前提。"""
        cache = VectorSemanticCache()
        cache.put(_msgs("分页"), "m", 0.2, _result("r"))
        hit = cache.get(_msgs("分页"), "m", 0.2)
        assert hit is not None
        assert hit.content == "r"
        assert cache.stats.exact_hits == 1

    def test_miss_records_stat(self) -> None:
        cache = VectorSemanticCache()
        assert cache.get(_msgs("没缓存过"), "m", 0.2) is None
        assert cache.stats.misses == 1

    def test_different_model_is_miss(self) -> None:
        """同一段文本、不同模型 → 不命中。模型是缓存键的一部分。"""
        cache = VectorSemanticCache()
        cache.put(_msgs("分页"), "model-a", 0.2, _result())
        assert cache.get(_msgs("分页"), "model-b", 0.2) is None

    def test_different_temperature_is_miss(self) -> None:
        """温度不同 → 输出分布不同 → 不命中。

        这是容易被忽略但很重要：temperature=0 与 0.9 的"同一个问题"
        得到的答案质量与风格差异巨大。
        """
        cache = VectorSemanticCache()
        cache.put(_msgs("分页"), "m", 0.0, _result())
        assert cache.get(_msgs("分页"), "m", 0.9) is None

    def test_tool_calls_not_cached(self) -> None:
        """带工具调用的结果有副作用，缓存会跳过实际执行 —— 绝不缓存。"""
        cache = VectorSemanticCache()
        result = ChatResult(
            content="", model="m", provider="p", tool_calls=[{"id": "1", "type": "function"}]
        )
        cache.put(_msgs("分页"), "m", 0.2, result)
        assert len(cache) == 0


# ---------------------------------------------------------------------- #
# 向量检索路径
# ---------------------------------------------------------------------- #


class TestSemanticPath:
    async def test_similar_text_hits(self) -> None:
        """措辞不同但用字高度重叠 → 语义命中。

        这是升级的全部意义：精确匹配会对第 2 条 miss。
        """
        embedder = FakeEmbedder()
        cache = VectorSemanticCache(embedder=embedder, threshold=0.8)

        await cache.aput(_msgs("用户分页"), "m", 0.2, _result("分页答案"))
        hit = await cache.aget(_msgs("用户分页列表"), "m", 0.2)

        assert hit is not None
        assert hit.content == "分页答案"
        assert cache.stats.semantic_hits == 1
        assert cache.stats.exact_hits == 0

    async def test_dissimilar_text_misses(self) -> None:
        embedder = FakeEmbedder()
        cache = VectorSemanticCache(embedder=embedder, threshold=0.8)
        await cache.aput(_msgs("用户分页"), "m", 0.2, _result())
        assert await cache.aget(_msgs("鉴权重试"), "m", 0.2) is None
        assert cache.stats.misses == 1

    async def test_exact_hit_short_circuits_embedding(self) -> None:
        """精确命中时**不能**再算嵌入 —— 否则升级反而变慢。

        精确匹配是零成本的字典查找，放在向量检索前面才合理。
        """
        embedder = FakeEmbedder()
        cache = VectorSemanticCache(embedder=embedder)
        await cache.aput(_msgs("分页"), "m", 0.2, _result())

        before = embedder.call_count
        hit = await cache.aget(_msgs("分页"), "m", 0.2)

        assert hit is not None
        assert cache.stats.exact_hits == 1
        assert embedder.call_count == before, "精确命中不应触发嵌入调用"

    async def test_threshold_is_respected(self) -> None:
        """阈值调高后，原本相似的两条不再命中 —— 阈值必须真正生效。"""
        embedder = FakeEmbedder()
        loose = VectorSemanticCache(embedder=embedder, threshold=0.3)
        strict = VectorSemanticCache(embedder=embedder, threshold=0.999)

        await loose.aput(_msgs("用户分页"), "m", 0.2, _result())
        await strict.aput(_msgs("用户分页"), "m", 0.2, _result())

        assert await loose.aget(_msgs("用户分页列表"), "m", 0.2) is not None
        assert await strict.aget(_msgs("用户分页列表"), "m", 0.2) is None

    async def test_bucket_isolation_by_model(self) -> None:
        """不同模型之间不互相召回。

        这是刻意的保守设计：deepseek 和 qwen 对同一提示的输出不同，
        混在一起会返回「看起来相似但来自别的模型」的结果。
        """
        embedder = FakeEmbedder()
        cache = VectorSemanticCache(embedder=embedder, threshold=0.8)
        await cache.aput(_msgs("用户分页"), "deepseek:chat", 0.2, _result("ds"))
        # 同温度、不同模型 → 不命中
        assert await cache.aget(_msgs("用户分页列表"), "qwen:plus", 0.2) is None
        # 同模型 → 命中，证明差异确实来自分桶而非相似度
        assert await cache.aget(_msgs("用户分页列表"), "deepseek:chat", 0.2) is not None

    async def test_model_filter_can_be_disabled(self) -> None:
        """关闭分桶后跨模型可互相命中（用可复现性换 token）。"""
        embedder = FakeEmbedder()
        cache = VectorSemanticCache(embedder=embedder, threshold=0.8, model_filter=False)
        await cache.aput(_msgs("用户分页"), "deepseek:chat", 0.2, _result("ds"))
        assert await cache.aget(_msgs("用户分页列表"), "qwen:plus", 0.2) is not None

    async def test_bucket_isolation_by_temperature(self) -> None:
        embedder = FakeEmbedder()
        cache = VectorSemanticCache(embedder=embedder, threshold=0.8)
        await cache.aput(_msgs("用户分页"), "m", 0.0, _result())
        assert await cache.aget(_msgs("用户分页列表"), "m", 0.9) is None

    async def test_embedding_uses_last_message(self) -> None:
        """只嵌入最后一条消息：前面的历史会把向量拉向"平均语义"。"""
        embedder = FakeEmbedder()
        cache = VectorSemanticCache(embedder=embedder, threshold=0.99)
        messages = [
            ChatMessage(role="system", content="你是一个资深工程师"),
            ChatMessage(role="user", content="历史无关内容占位"),
            ChatMessage(role="user", content="用户分页"),
        ]
        await cache.aput(messages, "m", 0.2, _result())

        assert embedder.calls[-1] == ["用户分页"]

    async def test_sync_put_creates_exact_only_entry(self) -> None:
        """``put``（同步）只建精确索引，不产生向量。

        这是刻意的：同步路径无法 await 嵌入。条目仍可用（精确命中），
        只是不参与向量召回。
        """
        cache = VectorSemanticCache(embedder=FakeEmbedder())
        cache.put(_msgs("分页"), "m", 0.2, _result())
        assert cache.size == 1
        assert cache.vector_size == 0

    async def test_vector_entries_are_counted(self) -> None:
        cache = VectorSemanticCache(embedder=FakeEmbedder())
        await cache.aput(_msgs("分页"), "m", 0.2, _result())
        assert cache.size == 1
        assert cache.vector_size == 1


# ---------------------------------------------------------------------- #
# 降级（缓存坏了不能让请求不可用）
# ---------------------------------------------------------------------- #


class TestGracefulDegradation:
    async def test_embedder_failure_falls_back_to_miss(self) -> None:
        """嵌入抛异常 → 返回 miss，而不是把异常抛给调用方。

        缓存是**优化**不是**功能**。这是本模块最重要的不变量。
        """
        cache = VectorSemanticCache(embedder=FakeEmbedder(fail=True), threshold=0.8)
        assert await cache.aget(_msgs("分页"), "m", 0.2) is None
        assert cache.stats.embed_failures == 1
        assert cache.stats.misses == 1

    async def test_put_failure_does_not_break_put(self) -> None:
        """写入时嵌入失败 → 仍然建立精确索引，条目不至于完全丢失。"""
        cache = VectorSemanticCache(embedder=FakeEmbedder(fail=True))
        await cache.aput(_msgs("分页"), "m", 0.2, _result("r"))
        assert cache.size == 1
        assert cache.vector_size == 0
        assert cache.get(_msgs("分页"), "m", 0.2) is not None

    async def test_embedder_returning_empty_degrades(self) -> None:
        async def empty_embedder(texts: Sequence[str]) -> list[list[float]]:
            return []

        cache = VectorSemanticCache(embedder=empty_embedder)
        await cache.aput(_msgs("分页"), "m", 0.2, _result())
        assert cache.vector_size == 0
        assert cache.stats.embed_failures == 1

    async def test_empty_text_is_not_embedded(self) -> None:
        """空文本没有语义，嵌入它毫无意义 —— 直接降级。"""
        embedder = FakeEmbedder()
        cache = VectorSemanticCache(embedder=embedder)
        assert await cache.aget([ChatMessage(role="user", content="")], "m", 0.2) is None
        assert embedder.call_count == 0

    async def test_dimension_mismatch_does_not_crash(self) -> None:
        """换 embedding 模型后旧条目维度不同 —— 必须静默不命中。"""
        calls = {"n": 0}

        async def shifting_embedder(texts: Sequence[str]) -> list[list[float]]:
            calls["n"] += 1
            dim = 3 if calls["n"] == 1 else 5
            return [[1.0] * dim for _ in texts]

        cache = VectorSemanticCache(embedder=shifting_embedder, threshold=0.9)
        await cache.aput(_msgs("分页"), "m", 0.2, _result())
        # 不应抛异常，只是不命中
        assert await cache.aget(_msgs("用户分页列表"), "m", 0.2) is None


# ---------------------------------------------------------------------- #
# 淘汰与统计
# ---------------------------------------------------------------------- #


class TestEvictionAndStats:
    async def test_lru_eviction(self) -> None:
        cache = VectorSemanticCache(max_entries=2)
        cache.put(_msgs("一"), "m", 0.2, _result("1"))
        cache.put(_msgs("二"), "m", 0.2, _result("2"))
        cache.put(_msgs("三"), "m", 0.2, _result("3"))

        assert cache.get(_msgs("一"), "m", 0.2) is None
        assert cache.get(_msgs("二"), "m", 0.2) is not None
        assert len(cache) == 2

    async def test_eviction_also_removes_vector_entry(self) -> None:
        """淘汰必须同时清掉向量索引 —— 否则向量桶会无限增长（内存泄漏）。

        只清精确索引是个很隐蔽的 bug：功能上看不出问题，
        但向量桶会一直留着重叠的悬垂条目。
        """
        cache = VectorSemanticCache(embedder=FakeEmbedder(), max_entries=2)
        for i in range(5):
            await cache.aput(_msgs(f"分页{i}"), "m", 0.2, _result())

        assert cache.size == 2
        assert cache.vector_size == 2, "向量索引必须与精确索引同步淘汰"

    async def test_overwrite_does_not_duplicate_vector(self) -> None:
        """同键重复写入不能产生重复向量条目。"""
        cache = VectorSemanticCache(embedder=FakeEmbedder(), max_entries=10)
        for _ in range(3):
            await cache.aput(_msgs("分页"), "m", 0.2, _result())
        assert cache.size == 1
        assert cache.vector_size == 1

    async def test_stats_separate_exact_and_semantic(self) -> None:
        """分开统计是判断"升级是否有效"的唯一依据。

        如果只记一个总命中数，就无法回答"向量检索到底贡献了多少"。
        """
        cache = VectorSemanticCache(embedder=FakeEmbedder(), threshold=0.8)
        await cache.aput(_msgs("用户分页"), "m", 0.2, _result())
        await cache.aget(_msgs("用户分页"), "m", 0.2)  # exact
        await cache.aget(_msgs("用户分页列表"), "m", 0.2)  # semantic
        await cache.aget(_msgs("鉴权"), "m", 0.2)  # miss

        assert cache.stats.exact_hits == 1
        assert cache.stats.semantic_hits == 1
        assert cache.stats.misses == 1
        assert cache.stats.hits == 2
        assert cache.stats.hit_rate == pytest.approx(2 / 3)
        assert cache.stats.semantic_share == pytest.approx(0.5)

    async def test_stats_dict_is_serializable(self) -> None:
        cache = VectorSemanticCache()
        cache.get(_msgs("x"), "m", 0.2)
        data = cache.stats.as_dict()
        assert set(data) == {
            "exact_hits",
            "semantic_hits",
            "misses",
            "embed_failures",
            "hits",
            "hit_rate",
            "semantic_share",
        }
        assert isinstance(data["hit_rate"], float)

    def test_cache_stats_no_division_by_zero(self) -> None:
        stats = CacheStats()
        assert stats.hit_rate == 0.0
        assert stats.semantic_share == 0.0

    async def test_clear_resets_everything(self) -> None:
        cache = VectorSemanticCache(embedder=FakeEmbedder())
        await cache.aput(_msgs("分页"), "m", 0.2, _result())
        await cache.aget(_msgs("分页"), "m", 0.2)

        cache.clear()
        assert cache.size == 0
        assert cache.vector_size == 0
        assert cache.stats.hits == 0
        assert cache.stats.misses == 0


# ---------------------------------------------------------------------- #
# 与新旧实现的一致性（升级的可验证前提）
# ---------------------------------------------------------------------- #


class TestUpgradeEquivalence:
    async def test_no_embedder_behaves_exactly_like_old_cache(self) -> None:
        """无 embedder → 精确匹配语义与旧实现完全一致。

        这是"可以安全灰度替换"的形式化验证：两种配置在精确路径上
        必须给出**逐条相同**的结论。
        """
        from devagent.models.gateway import SemanticCache

        old = SemanticCache(max_entries=2)
        new = VectorSemanticCache(max_entries=2)

        samples = [("分页", "m", 0.2), ("排序", "m", 0.2), ("缓存", "other", 0.2)]
        for text, model, temp in samples:
            old.put(_msgs(text), model, temp, _result(text))
            new.put(_msgs(text), model, temp, _result(text))

        for text, model, temp in [*samples, ("不存在", "m", 0.2)]:
            o = old.get(_msgs(text), model, temp)
            n = new.get(_msgs(text), model, temp)
            assert (o is None) == (n is None), f"不一致：{text}"
            if o is not None:
                assert o.content == n.content
