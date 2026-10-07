# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 与
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]

### 新增

**任务持久化（`SqlTaskStore`）**

- `devagent.db` 包：SQLAlchemy 2.0 异步 ORM（`Mapped[...]` / `mapped_column` 新式声明）
- `TaskRow` / `TaskEventRow` 两张表；任务 JSON 列整体存储，避免无谓拆表
- `TaskStore` 协议保持不变，内存与 SQL 实现**可直接互换**（由 `_maybe_await` 适配）
- 事件落库使 SSE 的「历史回放」跨进程重启依然可靠
- `StorageConfig`：`DEVAGENT_STORAGE__BACKEND=memory|sql`，向后兼容、默认 `memory`
- `pyproject.toml` 新增 `[db]` extra；`scripts/init_db.sql`（pgvector / HNSW / pg_trgm）
- 33 个持久化层测试（`sqlite+aiosqlite:///:memory:`，无需外部数据库）

**语义缓存（向量检索）**

- `VectorSemanticCache`：**精确短路 + 向量近邻**两级检索
- 余弦相似度（模长无关），可配阈值（默认 0.92）
- 按 `(model, temperature)` 分桶，避免跨模型返回"看起来相似"的结果
- 只嵌入最后一条消息（拼接历史会让向量向"平均语义"塌缩）
- 嵌入失败**自动降级**为精确匹配 —— 缓存是优化而非功能
- 分别统计 `exact_hits` / `semantic_hits`，升级收益可度量
- `CacheConfig`：`DEVAGENT_CACHE__SEMANTIC`（默认 **false**，行为与升级前一致）
- `GET /api/v1/cache` 端点 + 前端缓存看板（会明确提示"语义命中为 0"需关闭）
- 43 个新测试（37 个判定逻辑 + 6 个网关接线）

**异构裁判与偏差标定**

- `JudgePanel`：主裁判 + 可选**异构参考裁判** + 标定出的偏差，三者在同一对象内闭环
- **按模型族**判定异构（`deepseek-chat` 与 `deepseek-reasoner` 属同族，
  名字不同不算异构）—— 误判为异构会让"异构裁判"变成自欺欺人
- `model_family()` 显式处理裸模型名（无 `provider:` 前缀）的输入
- `CalibrationResult`：`bias` / `relative_bias` / `trustworth`，样本数 < 20 时判为不可靠
- 三种情形**刻意不同**地处理：
  - 主裁判异构 → 直接用，标 `judge_relation=heterogeneous`（不浪费算力校正）
  - 同源 + 已标定 → 按偏差扣减，并**用主裁判自己的阈值重算 `passed`**
  - 同源 + 未标定 → **如实返回**并标 `uncalibrated_self_preference=true`，
    绝不用猜出来的常数扣分（那会让分数看起来已被修正）
- `build_judge()` 工厂：CLI 与 API 共用，保证两个入口的结论可比较；
  配了同族参考裁判时直接拒绝（`reference_judge_same_family`）
- `EvalReport.calibration` 字段 + `summary()` 输出 —— 分数是否被校正过
  直接决定读者怎么解读 `mean_judge_score`，不写出来就是误导
- 标定用**真实 golden set 的验收标准文本**作探针：内容中性、分布一致，
  且能在跑昂贵的编排之前完成
- CLI `devagent eval` 输出裁判偏差行，并区分三种"没有值"：
  未标定（同族风险提示）/ 样本不足（明确说明**分数未校正**）/ 已校正（带符号的 bias）
- 49 个新测试（35 个判定逻辑 + 8 个运行器接线 + 6 个 CLI 输出）
- ADR-0008

### 计划中

- 前端 DAG 可视化与 diff 查看器
- 提示词注入防护（内容信任度分级）

### 修复

持久化层引入时发现并修复的 5 个缺陷：

12. `pyproject.toml` 缺少 `[db]` extra，但报错文案让用户执行 `pip install -e '.[db]'`
    —— **一条照着做必然失败的建议**。已补齐，并明确写 `sqlalchemy[asyncio]`
    （裸 `sqlalchemy` 不拉 `greenlet`，而 asyncio 扩展在 import 期就硬性依赖它）
13. `Database.__init__` 丢弃 `_require_sqlalchemy()` 返回的 `AsyncSession`，
    随后引用仅在 `TYPE_CHECKING` 下存在的模块级名字 → 运行期 `NameError`
    （即：**该实现从未被真正执行过**）
14. `TaskStore` 未声明 `@runtime_checkable`，"实现是否满足协议"无法在运行期断言，
    只能依赖 mypy 覆盖到实际调用路径
15. `SqlTaskStore.list` 方法遮蔽内置 `list`，使 `list[TaskEvent]` 注解
    被 mypy 判定为非法类型（`Function ... is not valid as a type`）
16. `purge_events` 依赖 `Result.rowcount`，异步 `Result` 无此属性；
    改为先取主键再按主键删，跨方言行为一致且行数准确

另修复 2 个测试自身的缺陷（`await` 误写在生成器表达式内导致 `async_generator`、
未 `await` 的 `submit` 协程），以及 `SqlTaskStore` 新增测试暴露出的
`test_concurrency_limit` / `test_shutdown_cancels_running` 调用点问题。

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

- 语义缓存默认**关闭**：开启后请求差异大时只增加嵌入成本而无收益，
  需关注 `/api/v1/cache` 的 `semantic_share`
- 缓存是**进程内**的，多实例部署各自独立（不做持久化是有意为之）
- 向量检索在条目数超过 10⁴ 时需改造成 ANN（当前 512 条上限下线性扫描约 0.5ms）
- 提示词注入无专门防护（见 [SECURITY.md](SECURITY.md)）
- 默认沙箱是本地进程，**不是安全边界**（见 [ADR-0004](docs/adr/0004-沙箱默认本地进程docker可选.md)）
- LLM-as-Judge 的自我偏好偏差未处理（见 [ADR-0005](docs/adr/0005-llm-judge-双向评估对冲偏差.md)）
- `SqlTaskStore` 不自动清理历史任务：留保留策略给外部（cron / 定期任务），
  硬编码 TTL 会让需要长期留存审计记录的部署很难受

---

[Unreleased]: https://github.com/devagent/devagent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/devagent/devagent/releases/tag/v0.1.0
