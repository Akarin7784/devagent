"""指标采集。

设计取舍：**不引入 prometheus_client**。原因是本项目的核心指标是「任务级」
而非「进程级」——例如「本次任务上下文压缩节省了多少 token」这种指标，
Prometheus 的 counter/histogram 模型表达起来很别扭（需要造大量动态 label）。

因此这里实现一个轻量的**聚合器**：

- ``Counter``：单调累加（token、成本、调用次数）；
- ``Gauge``：瞬时值（当前并发节点数）；
- ``Histogram``：分布统计（延迟、压缩比），自带分位数计算。

同时提供 ``PrometheusTextFormatter``，在需要接入标准监控时导出文本格式，
从而**既能内嵌展示，又能对接生态**。
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any

# 自定义分位数：不依赖 numpy，保证零依赖可跑
_QUANTILES = (0.5, 0.9, 0.95, 0.99)


@dataclass(slots=True)
class _Series:
    """一个带标签的指标序列。"""

    labels: tuple[tuple[str, str], ...] = ()
    value: float = 0.0
    count: int = 0
    observations: list[float] = field(default_factory=list)

    @property
    def label_key(self) -> str:
        if not self.labels:
            return ""
        return ",".join(f'{k}="{v}"' for k, v in self.labels)


def _label_tuple(labels: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


class MetricsCollector:
    """线程安全的指标聚合器。

    为什么需要线程安全：Agent 编排用 ``asyncio``，但沙箱执行与可选的
    并行工具调用可能落到线程池里，累加时存在竞争。用一个粗粒度锁远比
    之后 debug 「指标偶尔少算几千 token」划算。
    """

    def __init__(
        self,
        *,
        max_observations: int = 10_000,
        max_series_per_metric: int = 2_000,
    ) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, OrderedDict[tuple[tuple[str, str], ...], _Series]] = defaultdict(
            OrderedDict
        )
        self._gauges: dict[str, OrderedDict[tuple[tuple[str, str], ...], _Series]] = defaultdict(
            OrderedDict
        )
        self._histograms: dict[str, OrderedDict[tuple[tuple[str, str], ...], _Series]] = (
            defaultdict(OrderedDict)
        )
        self._max_observations = max_observations
        self._max_series_per_metric = max_series_per_metric
        self._evicted: dict[str, int] = {}
        """因超上限被淘汰的序列数（按指标名），用于判断上限是否定得过小。"""

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def _bounded(
        self,
        bucket: OrderedDict[tuple[tuple[str, str], ...], _Series],
        name: str,
        key: tuple[tuple[str, str], ...],
    ) -> _Series:
        """取（或创建）序列，并对**序列数量**做 LRU 上限保护。

        为什么必须限：写入方会拿 ``node_id`` / ``task_id`` 之类来自模型输出
        或用户输入的值当标签，取值空间无界。此前只对「单个序列的观测数」
        做了蓄水池采样，序列字典本身**从不淘汰** —— 长跑进程里
        ``snapshot()`` 会序列化全部历史序列，内存与响应体一起膨胀。
        这里按最近使用顺序淘汰最久未写入的序列，保证内存有界。
        """
        series = bucket.get(key)
        if series is None:
            series = _Series(labels=key)
            bucket[key] = series
            if len(bucket) > self._max_series_per_metric:
                # 淘汰最久未写入的一条；被淘汰的序列若再次写入会自动重建。
                bucket.popitem(last=False)
                self._evicted[name] = self._evicted.get(name, 0) + 1
        else:
            bucket.move_to_end(key)
        return series

    def inc_counter(self, name: str, value: float = 1.0, **labels: Any) -> None:
        key = _label_tuple(labels)
        with self._lock:
            series = self._bounded(self._counters[name], name, key)
            series.value += value
            series.count += 1

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        key = _label_tuple(labels)
        with self._lock:
            series = self._bounded(self._gauges[name], name, key)
            series.value = value
            series.count = 1

    def observe(self, name: str, value: float, **labels: Any) -> None:
        key = _label_tuple(labels)
        with self._lock:
            series = self._bounded(self._histograms[name], name, key)
            series.value += value
            series.count += 1
            # 容量保护：超高基数的长尾会吃掉内存，超过上限后转为水塘采样
            if len(series.observations) < self._max_observations:
                series.observations.append(value)
            else:
                # 蓄水池采样：以 count 为分母的概率替换，保持分布无偏
                import random

                idx = random.randrange(series.count)
                if idx < self._max_observations:
                    series.observations[idx] = value

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    def counter(self, name: str, **labels: Any) -> float:
        key = _label_tuple(labels)
        with self._lock:
            bucket = self._counters.get(name)
            series = bucket.get(key) if bucket is not None else None
            return series.value if series else 0.0

    def gauge(self, name: str, **labels: Any) -> float:
        key = _label_tuple(labels)
        with self._lock:
            bucket = self._gauges.get(name)
            series = bucket.get(key) if bucket is not None else None
            return series.value if series else 0.0

    def histogram_stats(self, name: str, **labels: Any) -> dict[str, float]:
        key = _label_tuple(labels)
        with self._lock:
            bucket = self._histograms.get(name)
            series = bucket.get(key) if bucket is not None else None
            if not series or not series.observations:
                return {"count": 0, "sum": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
            obs = sorted(series.observations)
            stats: dict[str, float] = {
                "count": float(series.count),
                "sum": series.value,
                "mean": series.value / series.count if series.count else 0.0,
                "min": obs[0],
                "max": obs[-1],
            }
            for q in _QUANTILES:
                stats[f"p{int(q * 100)}"] = _quantile(obs, q)
            return stats

    def snapshot(self) -> dict[str, Any]:
        """导出全部指标，供 API 层 / 报告消费。"""
        with self._lock:
            return {
                "counters": {
                    name: {s.label_key: s.value for s in series.values()}
                    for name, series in self._counters.items()
                },
                "gauges": {
                    name: {s.label_key: s.value for s in series.values()}
                    for name, series in self._gauges.items()
                },
                "histograms": {
                    name: {s.label_key: _series_summary(s) for s in series.values()}
                    for name, series in self._histograms.items()
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._evicted.clear()

    def total(self, name: str, **label_filter: Any) -> float:
        """按标签**前缀过滤**求和，忽略未指定的标签。

        为什么需要：``counter()`` 是精确标签匹配，读的时候必须把写入时的
        标签一个不差地重复一遍，非常脆弱。而报告层几乎总是想问
        「所有 agent 加起来省了多少 token」，这时需要的是聚合而不是精确命中。
        """
        wanted = {str(k): str(v) for k, v in label_filter.items()}
        with self._lock:
            series_map = self._counters.get(name)
            if series_map is None:
                return 0.0
            total = 0.0
            for labels, series in series_map.items():
                as_dict = dict(labels)
                if all(as_dict.get(k) == v for k, v in wanted.items()):
                    total += series.value
            return total

    def observe_total(self, name: str, **label_filter: Any) -> dict[str, float]:
        """``histogram_stats`` 的多序列聚合版本。"""
        wanted = {str(k): str(v) for k, v in label_filter.items()}
        merged: list[float] = []
        count = 0
        total = 0.0
        with self._lock:
            for labels, series in self._histograms.get(name, {}).items():
                as_dict = dict(labels)
                if all(as_dict.get(k) == v for k, v in wanted.items()):
                    merged.extend(series.observations)
                    count += series.count
                    total += series.value
        if not merged:
            return {"count": float(count), "sum": total}
        obs = sorted(merged)
        stats: dict[str, float] = {
            "count": float(count),
            "sum": total,
            "mean": total / count if count else 0.0,
            "min": obs[0],
            "max": obs[-1],
        }
        for q in _QUANTILES:
            stats[f"p{int(q * 100)}"] = _quantile(obs, q)
        return stats

    # ------------------------------------------------------------------ #
    # 生态对接
    # ------------------------------------------------------------------ #

    def to_prometheus_text(self, prefix: str = "devagent") -> str:
        """导出 Prometheus exposition 文本格式。

        注意 histogram 只导出 ``_sum``/``_count``/分位数 gauge，而非标准
        bucket 形式——因为我们的观测值不是预先分桶的，硬造 bucket 反而失真。
        分位数以 ``_quantile`` 形式暴露，语义清晰且可被 Grafana 直接画图。
        """
        lines: list[str] = []
        with self._lock:
            for name, series in self._counters.items():
                metric = f"{prefix}_{name}_total"
                lines.append(f"# TYPE {metric} counter")
                for s in series.values():
                    lines.append(f"{metric}{_fmt_labels(s.labels)} {s.value}")
            for name, series in self._gauges.items():
                metric = f"{prefix}_{name}"
                lines.append(f"# TYPE {metric} gauge")
                for s in series.values():
                    lines.append(f"{metric}{_fmt_labels(s.labels)} {s.value}")
            for name, series in self._histograms.items():
                metric = f"{prefix}_{name}"
                lines.append(f"# TYPE {metric} summary")
                for s in series.values():
                    summary = _series_summary(s)
                    lines.append(f"{metric}_sum{_fmt_labels(s.labels)} {summary['sum']}")
                    lines.append(f"{metric}_count{_fmt_labels(s.labels)} {summary['count']}")
                    for q in _QUANTILES:
                        key = f"p{int(q * 100)}"
                        ql = _fmt_labels((*s.labels, ("quantile", str(q))))
                        lines.append(f"{metric}{ql} {summary.get(key, 0.0)}")
        return "\n".join(lines) + "\n"


def _quantile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（与 numpy.percentile 默认行为一致）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return sorted_values[int(pos)]
    weight = pos - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _series_summary(series: _Series) -> dict[str, float]:
    if not series.observations:
        return {"count": float(series.count), "sum": series.value}
    obs = sorted(series.observations)
    out: dict[str, float] = {"count": float(series.count), "sum": series.value}
    for q in _QUANTILES:
        out[f"p{int(q * 100)}"] = _quantile(obs, q)
    return out


def _fmt_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"


# ---------------------------------------------------------------------- #
# 指标名常量：集中定义，避免各处硬编码字符串导致 typo 后指标静默丢失
# ---------------------------------------------------------------------- #


class MetricNames:
    """标准指标名。"""

    # 模型层
    LLM_CALLS = "llm_calls"
    """LLM 调用次数。labels: model, tier, status"""

    LLM_TOKENS = "llm_tokens"
    """LLM token 消耗。labels: model, direction(input/output)"""

    LLM_COST_USD = "llm_cost_usd"
    """累计成本（美元）。labels: model"""

    LLM_LATENCY_MS = "llm_latency_ms"
    """LLM 调用延迟分布。labels: model"""

    LLM_CACHE_HITS = "llm_cache_hits"
    """语义缓存命中次数。labels: model"""

    LLM_RETRIES = "llm_retries"
    """重试次数。labels: model, reason"""

    LLM_UNPRICED_CALLS = "llm_unpriced_calls"
    """价格表未命中的调用次数。labels: model

    非 0 就意味着成本账本偏低（这些调用的花费被记为 0），
    属于"数字看起来很正常、但其实是错的"那类问题，必须显式暴露。
    """

    # 上下文工程
    CONTEXT_TOKENS_SAVED = "context_tokens_saved"
    """上下文压缩/去重节省的 token。labels: agent, reason, task_id"""

    CONTEXT_COMPRESSION_RATIO = "context_compression_ratio"
    """压缩比分布。labels: agent, task_id"""

    CONTEXT_CHUNKS_DROPPED = "context_chunks_dropped"
    """装配阶段淘汰的片段数。labels: agent, reason, task_id"""

    CONTEXT_UTILIZATION = "context_utilization"
    """预算利用率分布。labels: agent, task_id"""

    CONTEXT_HARD_OVERFLOW = "context_hard_overflow"
    """硬约束击穿输入预算的 token 数。labels: agent, task_id

    为什么需要单独一个指标：硬约束「永不丢弃」是刻意设计，但**超预算本身
    必须可见**。此前它是静默的——实测预算 2000 时投递了 13530 token。
    """

    # 编排
    NODE_EXECUTIONS = "node_executions"
    """节点执行次数。labels: node_type, status"""

    NODE_DURATION_MS = "node_duration_ms"
    """节点耗时分布。labels: node_type"""

    TASK_TOTAL = "task_total"
    """任务总数。labels: status"""

    TASK_DURATION_MS = "task_duration_ms"
    """任务端到端耗时分布。"""

    RETRIES = "retries"
    """重试次数。labels: node_type, reason

    刻意**不用 node_id 当标签**：节点 id 来自模型的 JSON 输出，取值无界，
    当标签会让指标基数随任务数无限增长（每个节点一条序列），
    而按 Agent 类型聚合才是有决策价值的口径。
    """

    BACKTRACKS = "backtracks"
    """回退次数（Verifier 驳回触发）。labels: node_type"""

    ESCALATIONS = "escalations"
    """模型升档次数。labels: from_tier, to_tier"""

    # 可靠性
    BUDGET_EXCEEDED = "budget_exceeded"
    """熔断触发次数。labels: kind"""

    LOOP_DETECTED = "loop_detected"
    """检测到循环次数。labels: node_type"""

    REFLEXION_LESSONS = "reflexion_lessons"
    """沉淀的复盘教训数。labels: agent"""

    # 验证
    VERDICT_TOTAL = "verdict_total"
    """验证结论计数。labels: verdict, node_type"""

    HALLUCINATION_BLOCKED = "hallucination_blocked"
    """被 Verifier 拦截的不可验证断言数。labels: node_type"""

    VERIFICATION_UNAVAILABLE = "verification_unavailable"
    """验证器不可用导致的「无法验证」次数。labels: node_type

    这个数字**必须为 0 才敢说验证有效**：一旦大于 0，说明有产出在
    「没有通过验证」的情况下进入了流程，此时任务状态应体现出来。
    """


__all__ = ["MetricNames", "MetricsCollector"]
