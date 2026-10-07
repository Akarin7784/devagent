# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 与
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]

### 计划中

- 任务持久化（`PostgresTaskStore`，替换当前内存 LRU 实现）
- 语义缓存的向量检索版本（替换当前精确匹配实现）
- 异构模型裁判（消除 LLM-as-Judge 的自我偏好偏差，见 ADR-0005 的局限章节）
- 前端 DAG 可视化与 diff 查看器
- 提示词注入防护（内容信任度分级）

---

## [0.1.0] - 2024-10

首个可运行版本。包含完整的上下文工程内核与端到端交付链路。

### 新增

**上下文工程（核心）**

- 五层模型实现：L1 路由 / L2 隔离 / L3 压缩 / L4 装配 / L5 预算
- 装配算法：加权打分 + 指数冗余惩罚（gamma=4）+ **硬去重** + 位置编排
- 分层压缩（热/温/冷）与结构化摘要
- 按角色的预算模板与动态回流，小预算下按比例压缩输出预留
- 复杂度评分路由与失败升级
- `ContextIsolator`：Verifier 上下文结构上排除上游自我解释

**多 Agent 协作**

- 6 个 Agent：Requirement / Architect / Coder / Tester / Verifier / Reviewer
- 强类型结构化握手（`AgentHandoff`），Agent 间不传自然语言长文
- Verifier 一致性保护：顶层 verdict 与逐条判定冲突时以逐条为准
- Requirement 自动剔除不可验证的验收标准

**编排与可靠性**

- DAG 驱动编排：拓扑序调度、并行执行、**递归失败传播**、回退重置
- Reflexion：教训按 Agent 分桶、FIFO 淘汰、自动去重
- 检查点断点续跑，回退时 `clear_step` 作废检查点
- token + 步数双重熔断
- 循环检测（尝试次数 / 重复失败签名 / 路径环）

**模型层**

- `OpenAICompatibleProvider` 基类，含指数退避重试
- DeepSeek / 通义千问 / 智谱适配与价格表
- `ModelGateway`：档位路由 + 语义缓存(LRU) + 成本账本 + 供应商降级

**工具层**

- `LocalProcessSandbox`（白名单 / 超时 / 输出截断 / 审计日志）
- `DockerSandbox`（`--network none` / 内存与 CPU 限制 / 只读根 / tmpfs）
- `SandboxTestRunner` + `PytestOutputParser`（含路径穿越防护）
- `CodeIndex`：Tree-sitter AST 索引，正则降级

**可观测性**

- 自研 `Span` / `Tracer`，**显式 parent 指针**（`asyncio.gather` 下 contextvar 栈会断裂）
- `MetricsCollector`：Counter / Gauge / Histogram + Prometheus 文本导出 + 水塘采样
- `Observability` 门面 + 进程级单例，默认**关闭**（零开销）
- 埋点覆盖：模型网关三路径、`ContextEngine.build`、Orchestrator 全节点

**评测体系**

- `GoldenSet` JSONL 加载 / 校验 / 切分（错误带行号）
- `LLMJudge`：**双向评估**对冲位置与正向偏差，矛盾检测与置信度降权
- `EvalRunner` + 轨迹级指标（first_pass_rate / context_savings / discrimination）

**API 与前端**

- FastAPI 应用工厂 + lifespan 资源管理
- SSE 事件流：历史回放、心跳保活、慢消费者丢弃最旧事件
- `/tasks` CRUD、`/evaluate`、`/metrics`、`/metrics/prometheus`、`/traces`
- 零构建前端控制台（原生 ES Module，无 npm）

**CLI 与工程化**

- `devagent run|eval|index|serve`
- `scripts/demo_smoke.py`：**无 API Key** 的端到端冒烟
- 315 个测试；ruff + mypy --strict 全绿
- CI（多 Python 版本 × 多平台）、CodeQL、依赖审计
- Makefile、pre-commit、Docker Compose、多阶段 Dockerfile

### 修复

开发过程中发现并修复的 11 个真实缺陷，均已附带回归测试：

1. 装配算法软打分不足以去重（改为软打分 + 硬去重）
2. 压缩标记语义混淆（"装配丢弃"被误报为"压缩"）
3. DAG 失败传播只传一层（下游的下游悬挂在 PENDING）
4. 检查点短路回退：驳回重试读到自己的旧检查点直接成功
5. `AgentMessage.payload` 未携带 tokens 导致记账恒为 0
6. `deque maxlen` 硬编码使 `max_path_length` 配置失效
7. 小预算下装配退化为空集（`reserved_output` 压占全部预算）
8. `TokenUsage.total_tokens` 独立字段导致记账静默为 0（**影响面最大**）
9. requirement / architect 两阶段游离于熔断器之外
10. `PytestOutputParser` 有序正则提前返回导致 `failed` 计数丢失
11. `MetricNames` 未从 `observability/__init__` 再导出

### 已知限制

- 任务存储为内存实现，进程重启后丢失
- 语义缓存是精确匹配，非向量检索
- 提示词注入无专门防护（见 [SECURITY.md](SECURITY.md)）
- 默认沙箱是本地进程，**不是安全边界**（见 [ADR-0004](docs/adr/0004-沙箱默认本地进程docker可选.md)）
- LLM-as-Judge 的自我偏好偏差未处理（见 [ADR-0005](docs/adr/0005-llm-judge-双向评估对冲偏差.md)）

---

[Unreleased]: https://github.com/devagent/devagent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/devagent/devagent/releases/tag/v0.1.0
