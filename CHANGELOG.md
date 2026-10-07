# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 与
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]

### 新增

**节点级契约回归测试（跨语言边界）**

- `tests/unit/test_node_contract.py`（25 个用例）：用**真实的 `DAG` 对象**
  锁住前端依赖的两层接口
  - `_to_view()` 产出的 `NodeView` 字典（`/tasks/{id}` 的 `nodes`）：
    断言**精确键集合**而非 `in` 逐个检查（多字段同样是契约变更）
  - `Orchestrator` 经 `EventHook` 发出的节点事件 payload（SSE 的 `node_*`）
  - 覆盖 `deps` 是 `list` 非 `tuple`、`agent_type` 是普通 `str`、
    `last_error` 的 `None → ""` 归一化、每条路径 attempt 均 `>= 1`
- `tests/integration/test_orchestrator.py::TestNodeEventContract`（14 个用例）：
  `_EventRecorder` 逐字段校验三类事件的键集合与取值，含
  「钩子抛异常不得中断编排」与「未注册钩子时静默」
- `tests/conftest.py`：新增 `contract_words_path` fixture，把
  `devagent/enums.py` 导出为 `web/test_contract_words.json`
- `web/graph.test.js` 新增 6 个**跨语言契约测试**：逐成员断言
  `StepStatus` / `AgentType` 全集都被前端映射表覆盖，且
  `LEGEND_ITEMS` 解释了每一个用到的视觉分组
- `make web-words`：单独重新导出词表（`web-check` 会自动先跑）

### 修复

**前端状态映射缺口（由契约测试反向查出）**

新增的跨语言契约测试一落地就暴露出 `web/graph.js` 的真实缺陷：
`STATUS_GROUP` 只登记了 6 个状态，而 `StepStatus` 有 8 个成员。
漏登记的后果**不是报错而是静默降级**，比不显示更误导：

- `rejected` → 显示成「待执行」。这是最严重的一个：它正是快照层
  「验证未通过」的唯一表示，却被渲染成"还没开始跑"
- `verifying` → 显示成「待执行」（实际上验证器正在占用资源）
- `ready` → 显示成「待执行」（实际上依赖已满足、马上要调度）
- `AGENT_COLOR` 的键名写成 `coordinator`，而枚举里是 `orchestrator` ——
  编排器是 DAG 图上唯一必然存在的节点，它没有颜色意味着第一眼就是错的；
  `requirement` / `reviewer` 同样缺失

修复：三张映射表补全至覆盖枚举全集，`ready` 新增独立视觉分组
（虚线边框，与「还没轮到」的灰色区分），图例从 5 项扩到 7 项，
并把「未知状态降级为 pending」那条**锁死错误行为的旧测试**重写为
契约驱动（词表来自 Python，而非手抄）。

**测试断言的自我修正**

- `test_backtracked_is_not_a_step_status` 在编写过程中发现：
  `backtracked` 根本不是 `StepStatus` 成员（枚举里是 `rejected`），
  它只是事件层的派生态。原先一条「断言 `pending → pending`」的测试
  与自己的 docstring 无关，属于白写，已替换为三条有意义的断言
- `_FakeState.last_error` 默认 `""` 而真实 `StepState` 默认 `None`，
  被 `_to_view` 的 `str(... or "")` 掩盖 —— 这类假对象与真实类的
  分歧不会让任何测试变红，已用真实类测试锁住

### 变更

- 测试总数 526 → **565**（Python），前端 40 → **46**

**前端 DAG 可视化与 diff 查看器**

- `web/graph.js`：自研层次布局引擎（零依赖，约 200 行），布局与 DOM 解耦
  - `computeLayers`：迭代式拓扑分层（**非递归**，3000 节点深链不爆栈），带环检测
  - 层号取**最长路径**而非最短 —— 取最短会让汇合节点与前驱同层，破坏边的方向感
  - 层内 barycenter 排序减少边交叉；悬空依赖跳过而非崩溃
  - `layoutDag`：回退边（`back=true`）走绕行弧线，不与主链重叠
  - `applyEvent`：SSE 事件 → 图形状态，与服务端 `_emit` 契约逐字段对齐
  - `parseDiff` / `diffStats`：unified diff 解析，输出带类型标注与行号的行
  - `renderDag`：按 id 复用 `<g>` 的**增量更新**，长任务下不重建 DOM
- 前端「任务 DAG」面板：节点按状态着色（成功/执行中/回退/失败），
  执行中节点脉冲提示，`attempt > 1` 显示 `×N` 重跑徽标
- 节点详情面板：点击节点查看角色/依赖/token 与**代码改动 diff**（逐行着色）
- 同步策略：**乐观更新 + 权威覆盖** —— SSE 事件即时反映到图上，
  任务结束时用服务端快照整体覆盖，丢事件也能自愈
- 图渲染失败（如环）降级为提示文案，不让页面白屏
- `scripts/serve_demo.py`：把脚本化假模型注入真实 API，使完整 DAG 能流经
  HTTP + SSE —— 无此工具时，无 Key 环境下 `nodes` 恒为空数组
- `web/graph.test.js`：40 个纯逻辑测试（零依赖，`node` 直跑）
- `make web-check`：前端语法检查 + 逻辑测试

### 修复

**前端 DAG 实现中发现的两个真实缺陷**

- `step_id` 精确匹配失效：真实 `step_id` 形如 `task_xxx:N1`，节点 id 是 `N1`，
  早期实现用 `===` 比较导致 diff 面板恒为空。改为**后缀匹配**。
- diff 文件名取不到：真实 Coder 输出的 diff 围栏**不含** `+++ b/path` 头，
  文件名只在 Markdown 标题 `## 1. src/x.py` 里。改为三级回退；
  并按 `(文件名, 正文)` 去重，避免回退重跑时同一改动重复展示。

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

**提示词注入防护（内容信任度分级）**

- `devagent.context.trust`：按**来源**标注信任等级，与内容长什么样无关
  - `TrustLevel`：SYSTEM / USER / WORKSPACE / EXTERNAL / UNKNOWN
  - 未登记的 scheme 一律落到 UNKNOWN（保守默认）
  - scheme 按 `://` 精确切分查表，避免 `artifact` 误命中 `artifact-content`
- `InjectionGuard`：渲染时给不可信内容加**带随机 nonce** 的边界
  - nonce 每次渲染新生成（`secrets.token_hex(8)`，2^64 空间），
    攻击者无法通过预写闭合标签逃逸
  - 声明写在边界**之上**（模型对边界附近注意力最高）
  - 声明中不嵌标签字面量，避免模型把说明误当边界
- 信任度**参与装配打分**（`ScoringWeights.trust`）：
  外部内容权重 0.25、来源不明 0.0625 —— 需 4/16 倍相关性才能竞争预算
  - **单侧**映射（只惩罚低于 WORKSPACE 的等级）：SYSTEM 权重恒为 1.0。
    若用双侧距离，SYSTEM 会算出 0.0625 反而低于外部内容 ——
    那等于让防护机制把系统约束挤出上下文
- **零影响承诺**：SYSTEM / USER / WORKSPACE 权重全为 1.0，
  升级前所有内部来源片段的打分逐位不变（既有 446 个测试未改一行即全绿）
- **明确不做内容过滤**：注入内容原样保留，只加"这是数据不是指令"的声明。
  有测试断言启发式检测函数**不出现在**渲染路径源码里
- `ContextBundle.render_guarded()` + `trust_summary`；`AgentInvocation.guard`
  由编排器在全部 5 个构造点注入
- `ContextConfig`：`DEVAGENT_CONTEXT__INJECTION_GUARD`（默认 true）、
  `DEVAGENT_CONTEXT__WEIGHT_TRUST`（默认 1.0）
- 79 个新测试（69 个 trust 模块 + 6 个 Agent 接线 + 4 个端到端）
- ADR-0009

### 计划中

- 多裁判投票（当前为「主裁判 + 异构参考裁判」两段式，见 ADR-0008）
- 语义级注入检测（当前只按**来源**分级，见 ADR-0009 的已知限制）
- DAG 布局在节点数 > 100 时改用 Sugiyama 完整三阶段（见 ADR-0010）

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
