"""分布式追踪。

设计取舍：**不强绑定 OpenTelemetry**。原因有两个：

1. OTel SDK 对「Agent 轨迹」这种嵌套深、动态性强的结构并不天然友好，
   强行映射成 span 会丢失语义（例如「上下文装配」内部的候选淘汰过程）；
2. 社区开源项目需要「零配置可跑」——clone 下来不装 exporter 也能看到轨迹。

因此这里定义一个极简的 ``Span`` 抽象，并提供两种导出器：

- ``NoopExporter``：默认，仅内存/日志，零依赖；
- ``OTLPExporter``：探测到 ``opentelemetry`` 可用时启用，把 span 转成
  OTel span 上报（属性名保留语义，不强行展平）。

这样做的收益是：**埋点代码在业务侧保持稳定**，未来换后端不改业务。
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from devagent.logging_config import get_logger

logger = get_logger(__name__)

_ATTR_MAX_LEN = 512


@dataclass(slots=True)
class Span:
    """一次可观测操作的记录。

    采用「显式父子指针」而非依赖隐式上下文管理器嵌套，原因是 Agent 编排里
    ``asyncio.gather`` 会并发跑多个节点，contextvar 的隐式栈在并发下容易串线。
    """

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None = None
    start_ns: int = 0
    end_ns: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "unset"
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if not self.end_ns or not self.start_ns:
            return 0.0
        return (self.end_ns - self.start_ns) / 1_000_000

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = _truncate(value)

    def add_event(self, name: str, **attributes: Any) -> None:
        self.events.append(
            {
                "name": name,
                "time_ns": time.time_ns(),
                "attributes": {k: _truncate(v) for k, v in attributes.items()},
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status,
            "error": self.error,
            "attributes": dict(self.attributes),
            "events": list(self.events),
        }


def _truncate(value: Any) -> Any:
    """截断过长的属性值，避免 trace 被单个 prompt 撑爆。

    这是可观测性里的常见坑：把完整 prompt 塞进 span attribute，导致
    collector 侧 OOM。此处统一在写入时截断，而不是在导出时，避免内存里
    先存一份巨大的字符串。
    """
    if isinstance(value, str) and len(value) > _ATTR_MAX_LEN:
        return value[:_ATTR_MAX_LEN] + f"...<truncated {len(value) - _ATTR_MAX_LEN} chars>"
    return value


@runtime_checkable
class SpanExporter(Protocol):
    """Span 导出器协议。"""

    def export(self, span: Span) -> None: ...

    def shutdown(self) -> None: ...


class NoopExporter:
    """默认导出器：仅保留最近 N 条，供本地调试与单测断言。"""

    def __init__(self, capacity: int = 1000) -> None:
        self._capacity = capacity
        self.spans: list[Span] = []

    def export(self, span: Span) -> None:
        self.spans.append(span)
        if len(self.spans) > self._capacity:
            del self.spans[: len(self.spans) - self._capacity]

    def shutdown(self) -> None:
        return None

    def find(self, name: str) -> list[Span]:
        return [s for s in self.spans if s.name == name]


class OTLPExporter:
    """OpenTelemetry 导出器（惰性探测，不可用时降级为内存记录）。

    注意：此类**不在导入期**引入 opentelemetry，只有真正构造时才尝试。
    这样即使环境没装 OTel，``import devagent.observability`` 也不会失败。
    """

    def __init__(self, endpoint: str = "", service_name: str = "devagent") -> None:
        self._endpoint = endpoint
        self._service_name = service_name
        self._tracer: Any = None
        self._fallback = NoopExporter()
        self._otel_available = self._try_init()

    def _try_init(self) -> bool:
        if not self._endpoint:
            return False
        try:  # pragma: no cover - 依赖外部可选包
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(
                resource=Resource.create({"service.name": self._service_name})
            )
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=self._endpoint))
            )
            # 通过 Any 别名调用：opentelemetry-api 与 opentelemetry-sdk 的
            # 类型桩在不同版本间差异较大（是否装了 SDK 会影响可解析属性），
            # 硬依赖类型桩会让「可选依赖」变成「必装 SDK」。
            trace_api: Any = trace
            trace_api.set_tracer_provider(provider)
            self._tracer = trace_api.get_tracer(self._service_name)
            return True
        except Exception as exc:
            logger.warning("otlp_init_failed_fallback_to_memory", error=str(exc))
            return False

    @property
    def available(self) -> bool:
        return self._otel_available

    def export(self, span: Span) -> None:
        if not self._otel_available or self._tracer is None:
            self._fallback.export(span)
            return
        try:  # pragma: no cover - 依赖外部可选包
            with self._tracer.start_as_current_span(span.name) as otel_span:
                for key, value in span.attributes.items():
                    otel_span.set_attribute(key, value)
                if span.error:
                    otel_span.record_exception(Exception(span.error))
        except Exception as exc:
            logger.debug("otlp_export_failed", error=str(exc))
            self._fallback.export(span)

    def shutdown(self) -> None:
        self._fallback.shutdown()


class Tracer:
    """追踪入口。

    用法::

        tracer = Tracer(exporter=NoopExporter())
        with tracer.span("orchestrator.run", task_id="t1") as span:
            with tracer.span("node.execute", node_id="N1"):
                ...
    """

    def __init__(
        self,
        *,
        exporter: SpanExporter | None = None,
        enabled: bool = True,
        service_name: str = "devagent",
    ) -> None:
        self._exporter: SpanExporter = exporter or NoopExporter()
        self._enabled = enabled
        self._service_name = service_name
        self._active: dict[str, Span] = {}

    @property
    def exporter(self) -> SpanExporter:
        return self._exporter

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #
    # 上下文管理器式埋点
    # ------------------------------------------------------------------ #

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent: Span | None = None,
        **attributes: Any,
    ) -> Iterator[Span]:
        """创建一个 span 并保证异常时正确收尾。

        关键点：即使调用方抛异常，span 也会被 ``end`` + ``export``，
        否则一次失败的任务会在 trace 里留下「悬空节点」，反而掩盖了故障点。
        """
        if not self._enabled:
            yield _NULL_SPAN
            return

        active_parent = parent or self._current_span()
        span = Span(
            name=name,
            trace_id=trace_id or (active_parent.trace_id if active_parent else uuid.uuid4().hex),
            span_id=uuid.uuid4().hex[:16],
            parent_id=active_parent.span_id if active_parent else None,
            start_ns=time.time_ns(),
        )
        for key, value in attributes.items():
            span.set_attribute(key, value)

        self._active[span.span_id] = span
        try:
            yield span
        except Exception as exc:
            span.status = "error"
            span.error = f"{type(exc).__name__}: {exc}"
            raise
        else:
            if span.status == "unset":
                span.status = "ok"
        finally:
            span.end_ns = time.time_ns()
            self._active.pop(span.span_id, None)
            self._safe_export(span)

    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent: Span | None = None,
        **attributes: Any,
    ) -> Span:
        """非上下文管理器版本，用于跨协程手工管理生命周期。"""
        active_parent = parent or self._current_span()
        span = Span(
            name=name,
            trace_id=trace_id or (active_parent.trace_id if active_parent else uuid.uuid4().hex),
            span_id=uuid.uuid4().hex[:16],
            parent_id=active_parent.span_id if active_parent else None,
            start_ns=time.time_ns(),
        )
        for key, value in attributes.items():
            span.set_attribute(key, value)
        self._active[span.span_id] = span
        return span

    def end_span(self, span: Span, *, error: str = "") -> None:
        span.end_ns = time.time_ns()
        if error:
            span.status = "error"
            span.error = error
        elif span.status == "unset":
            span.status = "ok"
        self._active.pop(span.span_id, None)
        self._safe_export(span)

    def _current_span(self) -> Span | None:
        """取「最近开始且未结束」的 span 作为父节点。

        并发场景下这是启发式的（可能选到兄弟 span），因此同时提供显式
        ``parent=`` 参数供编排器使用——**显式优先于隐式**。
        """
        if not self._active:
            return None
        return next(reversed(self._active.values()))

    def _safe_export(self, span: Span) -> None:
        try:
            self._exporter.export(span)
        except Exception as exc:
            logger.debug("span_export_failed", error=str(exc))

    def shutdown(self) -> None:
        self._exporter.shutdown()


class _NullSpan(Span):
    """禁用追踪时返回的占位 span：接受所有调用但不记录。"""

    def __init__(self) -> None:
        super().__init__(name="null", trace_id="0" * 32, span_id="0" * 16)

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        return None

    def add_event(self, name: str, **attributes: Any) -> None:  # noqa: ARG002
        return None


_NULL_SPAN = _NullSpan()


__all__ = [
    "NoopExporter",
    "OTLPExporter",
    "Span",
    "SpanExporter",
    "Tracer",
]
