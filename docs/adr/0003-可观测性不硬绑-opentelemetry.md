# ADR-0003：可观测性自研实现，不硬绑 OpenTelemetry

- **状态**：已采纳
- **日期**：2024-10
- **决策者**：核心开发

---

## 背景

项目需要追踪（tracing）与指标（metrics）能力，用于：

1. 定位「哪一步、哪个 Agent 慢 / 贵」
2. 量化上下文工程的效果（**这是项目的核心卖点，必须有数据支撑**）
3. 生产环境接入现有观测平台

`opentelemetry-api` 与 `opentelemetry-sdk` 已在依赖列表中。

## 问题

直接把 OTel 作为唯一实现会带来三个具体问题。

### 问题 1：测试与离线场景无法运行

OTel SDK 的 `TracerProvider` 与 exporter 有全局状态。测试中反复创建/销毁 provider
会产生「provider already set」警告，且 exporter 的网络调用让测试变慢甚至挂起。

更关键的是：**一个 clone 下来就应该能跑的项目，不应该要求用户先起一个 collector。**

### 问题 2：`asyncio.gather` 破坏 contextvar 隐式栈

OTel 的 span 父子关系依赖 `contextvars`。但编排器用 `asyncio.gather` 并行执行
无依赖节点，每个子任务有独立的 context 副本，导致：

```python
with tracer.start_as_current_span("task"):        # 父
    await asyncio.gather(
        run_node("N1"),   # 这里的 current span 不是 task！是 context 副本里的旧值
        run_node("N2"),
    )
```

结果的 trace 树是**断开的**：节点 span 挂不到任务 span 下面。

### 问题 3：接口耦合导致降级困难

如果全项目直接调用 `opentelemetry.trace.get_tracer()`，那么「不装 OTel」
就变成不可能——每次调用都要判空，或依赖一个 Noop 实现的存在。

## 决策

**自研轻量 `Span` / `Tracer` / `MetricsCollector`，通过 Protocol 定义导出接口；
OTel 作为可选后端惰性探测、优雅降级。**

分层：

```
业务代码
   │  只依赖本项目的接口
   ▼
devagent.observability（自研门面 + 单例）
   │  SpanExporter Protocol
   ▼
┌──────────────────┬──────────────────┐
│  NoopExporter    │  OTLPExporter    │
│  （默认，零开销） │  （惰性探测 OTel）│
└──────────────────┴──────────────────┘
```

### 关键设计点

**1. `Span` 携带显式 parent 指针，而非依赖 contextvar**

```python
@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None = None   # ← 显式，不靠 contextvar
    ...
```

`Tracer.start_span(name, parent=some_span)` 直接传父 span。
并行执行时每个节点拿到的是**同一个** task span 引用，trace 树正确。

这是对问题 2 的直接回应，代价是调用方要显式传递 parent——但这比 trace 树断裂要好。

**2. 默认关闭，零开销**

`Observability` 门面默认 `enabled=False`。关闭时所有 `record_*` 调用立即返回：

```python
def record_llm_call(self, **kwargs: Any) -> None:
    if not self._enabled:
        return          # 无字典查找、无锁、无分配
    ...
```

测试、CLI 单次运行、离线冒烟都不需要承担观测开销。

**3. 导出器失败不影响主流程**

```python
try:
    self._exporter.export(span)
except Exception:
    logger.warning("span_export_failed", span_name=span.name)
    # 绝不向上抛：观测失败不应导致业务失败
```

这条有专门的测试守护（`test_exporter_failure_is_isolated`）。

**4. OTel 检测惰性且宽容**

```python
try:
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace import TracerProvider
    ...
except ImportError:
    return None      # 静默降级，不报错
```

`pyproject.toml` 中把 `opentelemetry.*` 加入 mypy 的 `ignore_missing_imports`，
承认它是可选依赖。

## 备选方案

### 方案 A：直接用 OTel，不做抽象

**否决**：问题 1、2、3 全部无法解决。测试会依赖全局 provider 状态，
并行 trace 树断裂，且无法「不装 OTel」。

### 方案 B：用 OTel 但自己包一层（保留 OTel 的 span 语义）

**否决**：仍然继承 contextvar 的父子关系模型，问题 2 无法在包装层修复
（除非完全绕过 OTel 的 context 机制，那就不是"用 OTel"了）。

### 方案 C：用 structlog + Prometheus client

**否决**：structlog 已在用（日志），但日志无法表达 span 的层级与时序；
`prometheus_client` 引入额外依赖，而我们的指标需求（Counter/Gauge/Histogram
+ 文本导出）用标准库约 200 行即可满足。

## 后果

### 正面

- 测试零观测开销，无误导性全局状态
- 并行执行的 trace 树正确（显式 parent 的直接收益）
- 不装 OTel 也能完整使用，装了则自动导出
- 指标可直接以 Prometheus 文本格式暴露（`/api/v1/metrics/prometheus`）
- 量化上下文效果：`context_compression_ratio` 按 Agent 分桶

### 负面

- 不是 OTel 标准实现，生态工具（Jaeger UI 的某些特性）可能不完全兼容
- 需要自己维护约 500 行观测代码
- 水塘采样（reservoir sampling）限制了直方图精度（换取内存上界）

### 可观测性自身的收益（有价值的旁证）

指标埋点接入后**立刻暴露了一个真实 bug**：`TokenUsage.total_tokens` 恒为 0
（多处构造只填 prompt/completion）。熔断器读到的永远是 0，token 预算形同虚设。

这个 bug 在单元测试中不可见（每个组件单独看都正确），
只有端到端指标才暴露出来。**观测能力的投入自己就付了账。**

## 相关

- `src/devagent/observability/`
- `tests/unit/test_observability.py`
- [ADR-0001](0001-自研编排层而非使用-langchain.md)
