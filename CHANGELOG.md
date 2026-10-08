# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 与
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]

### 页面间距与信息密度（2026-10-08）

- 统一概览、上下文、评测、可观测性和设置的区块间距，移除叠加外边距；上下文五层卡片与并排卡片对齐，窄屏改为单列。
- 精简重复页头、副标题、教学式说明和字段环境变量提示；指标快照与接入说明按需展开，保留验证错误、费用和重启提示。

### 完整后端设置与供应商接入（2026-10-08）

- 设置页新增模型供应商和后端配置：按后端 schema 展示全部字段，支持搜索、局部保存、放弃修改与重启提示。
- 提供 30 个国内外及本地供应商预设，支持自定义供应商、模型 / 嵌入 / 裁判路由、认证、超时、重试和价格设置；新增 Anthropic 原生 Messages 适配。
- 增加服务端覆盖配置持久化、密钥脱敏、字段校验、revision 冲突检查与本机 / 鉴权访问控制；前端支持会话 API Key 和认证 SSE。
- 启动命令读取配置中的监听地址 / 端口，显式参数优先，默认监听回环地址；修复模型零重试与 5xx 重试行为。
- 更新静态模块版本避免缓存混用，补充接口 / 协议 / 保存回归和配置文档。验证结果见质量文档。

### Agent 工作区重构（2026-10-08）

- 默认进入工作台：任务搜索与状态筛选、需求输入、本地草稿、快捷键提交和需求建议。
- 执行会话分为执行过程、代码改动与验证结果；计划可选中查看步骤，依赖图按需展开，宽屏展示侧面详情。
- 产物与验证按逻辑步骤显示最新尝试，避免回显过期补丁；缺失验证结论不再显示为通过，明确展示补丁未应用与基线测试的边界。
- 统一中性灰与紫色强调的明暗主题、桌面及移动布局；修复顶栏标题更新、移动导航遮罩层级与焦点管理，样式版本更新避免沿用旧缓存。
- 增加工作区回归测试；当前七个前端脚本共 201 条断言，验证结果集中记录于 [质量与已知限制](docs/07-质量与已知限制.md)。

### 代码审查修复与整理（2026-10-08）

- 严格校验 Verifier 的标准覆盖、布尔类型与顶层判定，验证材料只包含结构化代码产物。
- 向 DAG 下游与 Tester 传递代码产物，检查点恢复同步恢复产物；Verifier 解析失败仍计入实际用量、费用和熔断步骤。
- 每次测试使用独立基线快照，避免并发同名覆盖和 conftest 串扰；取消执行时终止进程树或容器并清理快照。
- 修复任务首次调度前取消的状态与事件流收尾。
- 合并审查报告、删除过时工程骨架，统一文档导航与能力边界；参数化重复测试，保留真实执行回归场景。验证结果集中记录于 [质量与已知限制](docs/07-质量与已知限制.md)。

### 新增

**开发时前端热更新（零依赖）**

`web/` 是零构建的原生 ES 模块，没有打包器就没有 HMR：改一个页面模块必须手动
刷新。而"手动刷新"经常还不管用 —— Starlette 的 `StaticFiles` 只发
`Last-Modified` / `ETag`、**不发 `Cache-Control`**，浏览器于是按启发式规则
（经验值是距上次修改时间的 10%）自行决定缓存多久，"刚改完 + F5"照样拿到旧文件。
表现就是那句话：「我明明改了，刷新还是旧的」。

新增 `scripts/dev_reload.py`，`serve_demo.py` 默认启用：

- `DevStaticFiles`：静态资源补 `Cache-Control: no-store` —— 手动刷新必定拿到
  磁盘上的最新内容；
- `LiveReload`：轮询 `web/` 的文件指纹（mtime + 大小，含新增与删除），变化后
  经 SSE `/__dev/events` 推给浏览器，客户端整页重载。同一批保存有 150ms 去抖，
  编辑器一次落多个文件只刷一次；重载保留 hash 与查询参数，当前路由与 `?api=`
  都不会丢；
- 客户端脚本由服务端**注入** `index.html`，`web/` 里不残留开发代码，生产镜像与
  既有前端测试零改动；`--no-reload` 可关闭；URL 上加 `?no-reload=1` 可让单个
  页面不连接那条流 —— 它是**永不结束**的请求，headless 截图/爬虫的
  `networkidle` 与 `--virtual-time-budget` 会因此一直等下去（实测把无头截图卡死，
  这个开关就是被那次卡死逼出来的）。

为什么轮询而不是 watchdog：本项目对前端零构建、后端零额外依赖是硬约束，而开发
场景下 0.5s 延迟完全够用。只监听前端目录 —— 改 `src/devagent/**` 需要重启进程。

`tests/unit/test_dev_reload.py` 补 14 条用例。其中一条是接线测试，它当场抓到了
一个真实缺陷：`Mount("/")` 的 `path` 会被 Starlette `rstrip("/")` 成**空串**，
按 `=="/"` 找挂载点永远找不到 —— 于是热更新静默失效，页面照常打开，只是"改了
不刷新"，控制台里没有任何线索。端到端另行验证过：改动 `web/` 后浏览器确实自己
重载（服务端访问日志里能看到整页重新加载，含 4 个模块批次；新增/删除/修改/回滚
四种变化都触发过）。

### 修复

**设置页把四个分区挤在一页（前端）**

设置页此前把「后端连接 / 外观 / 数据 / 关于」四张卡片堆在同一页：想切个主题，
也要先滚过一整块连接表单 —— 首屏被**最不常改**的内容占满。改为 Tab 分区，
并顺带修掉改造过程中暴露的两个问题：

- **分区记在 URL 上**（`#/settings?tab=appearance`）：刷新、分享、前进后退都能
  回到同一个分区。复用工作台 `?task=` 的同一套机制 —— 同路由 replace 不派发
  hashchange，因此切分区不会触发外壳拆页重建（`store.js` 里那条回归教训）。
  键盘交互按 ARIA tabs 模式：左右方向键切换、Home/End 到首尾、roving tabindex。
- **`?tab=` 必须归一**：它来自地址栏，是不可信输入。旧书签、手改、拼错都会给出
  一个不存在的分区 id，直接拿去匹配面板的结果是**空白面板且没有任何报错** ——
  看起来像"设置页坏了"，而不是"链接错了"。未知值回落到第一个分区，并把 URL
  就地纠正，否则复制出去又是一个坏链接。
- **标签上的连接状态点会假红**：设置页可能在健康探测返回**之前**就渲染完成，
  那一刻 `connected` 还是 false，于是顶栏写着「服务正常」、分区标签却标红 ——
  同一屏两个相反的结论。现在订阅 `connected`：非输入态整页重绘（卡片里的
  状态与「后端不可达」告警一并刷新），输入态**只就地更新状态点**，避免把用户
  刚敲的地址连同光标一起冲掉（observability 页踩过同一个坑）。

`web/pages.test.js` 补 7 条用例（41 → 48）。其中「任一时刻只渲染一个分区面板、
其余分区的标志文本不在场」直接守住"不再挤在一页"这个诉求本身：断言的是
`tabpanel` 计数与跨分区交叉文本，而不是"分区实现存在"。两条新用例都做过
变异验证 —— 把 `buildActiveCard` 退回"四张卡片全建"、或把订阅退回"渲染时读一次"，
测试分别以"还是挤在一页""顶栏显示服务正常而标签仍是红的"失败。

同时更正 README 里两处早已过期的前端断言计数（73 / 178 → 实测 175）：
数字应当来自 `node web/*.test.js` 的输出，而不是上一次的记忆。

**CI 从未真正跑通（首次推送到 GitHub 才暴露）**

仓库此前没有远端，CI 配置的正确性从未被验证过。首次推送后三个 ubuntu 任务
全红，Windows 任务也红 —— 两个都是**先于本轮改动就存在**的问题：

- `pip install -e ".[dev]"` 少装了 `[db]` extra，而 `tests/unit/test_db.py`
  需要 `sqlalchemy[asyncio]` 的 greenlet 与 aiosqlite → 15 个用例在 setup
  阶段抛 `ModuleNotFoundError`，把真正的失败信号淹没了。现在 lint 与 test
  两个 job 都装 `.[dev,db]`，并让该测试文件在缺依赖时**明确跳过**并给出
  安装提示（跳过 ≠ 不跑：CI 侧依赖是装齐的）
- Windows 任务在 `demo_smoke.py` 上抛
  `UnicodeEncodeError: 'charmap' codec can't encode characters`：
  英文 Windows 的 stdout 默认是 cp1252，打印中文直接崩溃并以非零码退出。
  本地中文 Windows（cp936）反而看不出问题，所以这个坑一直躺在 CI 里。
  新增 `devagent/console.py::force_utf8_stdio()`（幂等、永不抛异常），
  由 `cli.main()` 与冒烟脚本调用；CI 另加 `PYTHONUTF8=1`

修复后 CI 全绿：Lint & Type Check、Docs Check、5 个测试矩阵
（3.11/3.12/3.13 × ubuntu + Windows + macOS）、Build Package，CodeQL 亦通过。

**对抗性复核查出的 8 项缺陷（含一项 P0：DAG 从未真正执行）**

第一轮修复完成后，又做了一轮**对抗性复核**（独立重写探针、以证伪为目标），
其中一条 P0 是第一轮与**原有整套测试**都没发现的：

- **P0：架构产出的节点从未进入 DAG** —— 编排器读 `payload["nodes"]`，而
  `BaseAgent._to_message` 构造的 payload 只有 content/model/tokens_used/
  cost_usd/raw，Architect 把节点放在 `output.raw["nodes"]`。取的键永远不存在，
  于是**每次任务都静默退回单节点方案**：DAG 并行调度、依赖拓扑、失败传播、
  子图回退、节点级验证全部空转，而任务状态、日志、节点事件看上去都正常。
  最直接的证据是 `scripts/demo_smoke.py`：脚本给出的方案是
  `N1(coder) → N2(coder) → N3(tester)`，修复前它只跑 N1 —— README 甚至把
  这份被截断的输出当成了预期结果（"模型调用 8 次 / token 2011"），
  修复后是 14 次调用 / 8679 token / 三个节点。
  既有测试全绿的原此很典型：架构假回复只给 1 个节点、
  `build_dag_from_architect_output` 是单独测的、集成测试只断言 `result.dag` 存在。
  现补上端到端的节点数与执行顺序断言。
- **P1：步级 token 不含 Tester** —— 开着 Tester 时 `steps[]` 之和比
  `total_tokens` 少一截（实测 800 vs 1000），前端「每步花了多少」与总计矛盾。
- **P1：「没跑到任何测试」被当成测试失败** —— pytest 未收集到测试时打印
  `no tests ran` 并以退出码 5 结束，而非零退出码一律被强制成 `errors=1`，
  于是 Verifier 收到 `0 passed, 0 failed, 1 errors` 这条**看起来像失败**的假证据。
  现在如实标为"未执行"（`raw["nothing_ran"]`）。
- **P2：失败路径漏记模型用量** —— `BaseAgent.run` 在 `parse_output` 抛错时直接
  上抛，那次真实调用既不入 `total_tokens` 也不入熔断器；越是"输出格式不对"
  这种高频失败，预算越不准。现把 usage/cost 挂在异常上，由编排器补记。
- **P2：并发任务被折叠进同一条 trace** —— `Tracer.start_span` 的隐式父节点是
  "最近一个未结束的 span"，并发时会选到别的任务的 span。任务根 span 现在
  显式 `root=True`。
- **P3**：`StepCheckpoint.matches_run` 是死代码（现用于区分"同运行短路"与
  "跨运行续跑"的日志）；`EventBus._closed` 对"只关不发"的 id 无上限
  （现同样 LRU 有界）。
- 缓存命中的 token 计入 `total_tokens` 是有意为之（表示"服务出去的上下文量"），
  成本仍记 0 —— 口径已写进 README，避免被误读为漏记。

**代码审查发现的正确性缺陷（P0/P1）—— 一批"文档承诺了、但实际没生效"的能力**

一次全量审查（含 6 个运行时探针复现）发现：多项被文档当作核心卖点的机制，
在默认部署路径上并未生效，其中两项的失效方向还是反的。以下逐条修复。

*P0 —— 破坏核心正确性*

- **验证失败曾判为「通过」（fail-open）**：`_verify_node` 捕获异常后返回
  `Verdict.PASS`，一次瞬时 503 就能让未经验证的产物被标记为"已验证成功"，
  任务状态还是 `succeeded`、`error` 为空 —— 外部完全观测不到。
  现改为 fail-closed：判 `REJECT`、计入 `verification_unavailable` 指标、
  发出 `verification_unavailable` 事件，任务如实失败
- **跨任务上下文污染**：API 层按进程单例持有 `Orchestrator`（`TaskService`
  默认并发 4），而上下文空间/熔断器/反思记忆全挂在 `self` 上 ——
  实测任务 B 的需求提示词里读到了任务 A 的需求原文，且 A 的节点事件被投递到
  B 的 SSE 流（A 收到 0 个、B 收到 2 个）。现引入 `_RunState` + `ContextVar`：
  每次 `run()` 从隔离容器**快照**开始，账本/反思/循环检测/span/run_id 全部按运行隔离；
  `TaskService` 的归属信息同样改为 `ContextVar`（`_current_task_id` 共享字段删除）
- **测试链从未接线**：四个入口（API / CLI / 两个演示脚本）都没传 `test_runner`，
  于是 `TesterAgent.execute` 恒返回 `executed=False`，Verifier 的"客观测试证据"
  是一条字面量 `（测试未执行）`。现新增 `devagent.tools.runtime.build_test_runtime`
  并在 API / CLI / 评测链路接入；测试工作区默认是**一次性临时目录**
  （不再默认指向仓库，避免不受信内容覆写源码）
- **测试证据可伪造**：`PytestOutputParser` 把 stderr 拼在 stdout 之后并倒序扫描，
  模型自写的 `conftest.py` 往 stderr 写一行 `128 passed` 即可覆盖真实结果，
  且 `exit_code` 只存不用。现只解析 stdout，`exit_code != 0` 一律不算通过

*P1 —— 核心承诺未落地*

- **装配算法在生产路径上整体失效**：`src/` 内所有 `make_chunk` 都不带 embedding，
  5 处 `build()` 全传 `task_embedding=None`，而 `cosine_similarity` 遇 `None`
  恒返回 0 —— 相关性恒 0、冗余惩罚恒 0、**硬去重永不触发**（实测两份完全相同的
  片段都进上下文）。且"硬去重行为契约"测试喂的是生产环境不存在的 embedding，
  所以一直全绿。现引入 `chunk_similarity`：有向量用余弦，无向量退化为字符
  n-gram 的 Jaccard；并新增任务文本参数，使相关性在无嵌入模型时也有区分度
- **Verifier 上下文只增不减**：`space.add` 从不清空，重试时上一轮**被驳回**的
  产物与本轮产物同时在场且 `source` 完全相同（`artifact://N1`），Verifier
  无从分辨该审哪一个。现在每次验证前清空 Verifier 空间，`handoff_to` 也会
  先移除上一轮投喂的 handoff 片段（否则重试会让硬约束成倍累积）
- **硬约束静默击穿预算**：硬片段无条件全收且不参与去重，实测预算 2000 时
  投递 13530 token。现按内容去重 + 新增 `hard_overflow_tokens` 决策字段与
  `context_hard_overflow` 指标，超预算时在决策说明里显式标注
- **Tester / Verifier 调用不计账**：每个成功节点实际有 3 次模型调用，
  而账本与熔断器只看到 1 次 —— 可用预算悄悄变成配置值的约 3 倍。
  现已全部计入 `total_tokens`、成本与熔断器（`build_smoke` 的 token 总量
  此此从 2011 变为 4808，这是修复而非回归）
- **需求/架构步骤的 `steps[].tokens_used` 恒为 0**，与 `total_tokens` 自相矛盾
- **检查点写在验证之前**：被驳回、重试耗尽的节点也会留下 `completed=True`，
  同一个 `task_id` 再跑一次会被短路，一步不执行却报成功。现只在**验证通过后**
  写入，失败路径显式清除；新增 `Orchestrator.forget_task()`，评测在同一进程内
  重跑同一样本前会先丢弃检查点
- **位置编排方向写反**：`sorted(reverse=True)` 后取前 N 个当"最新"，而前 N 个
  恰是最旧的片段。现简化为按 age 降序（最新自然落在最尾）
- **前端 XSS**：`alert()` 对字符串正文使用 `html:`，而 `node.lastError` 来自
  Verifier 模型输出的 JSON —— 一句含 `<img onerror>` 的需求即可在控制台执行任意 JS。
  现字符串正文一律走 `text`，并新增全仓"动态数据不得当 HTML 解析"的静态扫描
- **概览成功率恒错**：用 `t.status === 'success'` 比较后端实际发出的 `'succeeded'`，
  全成功时显示"—"、9 成功 1 失败时显示 **900%**
- **DAG 可视化没有样式**：`graph.js` 用的类名只定义在被孤立的 `web/styles.css` 里，
  节点在两种主题下都是黑底黑字。规则已迁入 `css/components.css` 并改用语义令牌；
  无引用它的 `web/app.js`、`web/styles.css` 已删除。同时补齐了三处
  `marker-end="url(#dag-arrow*)"` 引用却从未定义的箭头 marker
- **按 README 操作无法启动**：`.env.example` 的三个路由值缺 `provider:` 前缀，
  `Settings()` 直接抛 `ValidationError`；`docker-compose.yml` 的 `DEVAGENT_ENV: docker`
  非法；`init_db.sql` 的 `tasks` 表与 ORM 不一致（首次写入即
  `no such column: tasks.succeeded`）；镜像未安装 `[db]` extra 此而跑不了 SQL 模式

*P2/P3 —— 资源、并发与静默失效*

- 沙箱用 UTF-8 硬解码子进程输出，而 Windows 子进程按 GBK 输出 —— 中文证据被
  静默替换为 U+FFFD（这也是仓库自带测试在中文路径下失败的原此）
- 沙箱输出上限在**完全缓冲之后**才生效（200 MiB 输出 → 宿主堆峰值 400 MiB）
- 超时"强杀"杀不掉容器与孙进程；Docker 工作区此前是读写挂载且缺 `--user`
- 模型可控的"测试文件路径"可覆写仓库任意既有文件；`_is_safe_relative_path`
  还接受 `--rootdir=..` 这类 pytest 选项串
- `CodeIndex` 可经目录联接读到工作区之外；且用 tree-sitter 的**字节**偏移去切
  `str`，任何含非 ASCII 的文件符号名/导入/文档串都会被切歪
- `build_sandbox` 失败开放且丢弃调用方策略；`LocalProcessSandbox` 声明了
  memory/cpu/pids/network 限制却一条都没实施
- 取消**排队中**的任务会让状态永久停在 `pending`、SSE 永不结束
  （`try/finally` 原本位于信号量内部）
- `submit()` 的"先查再写"存在 TOCTOU（同一 task_id 可跑两次；SQL 模式下
  撞主键抛 `IntegrityError` → 500 而非 409）
- `EventBus` 历史永不释放，且 SSE 回放取的是**最旧**的 200 条 ——
  晚到订阅者永远看不到 `task_finished`
- `CostLedger.records`、指标序列字典均无上界（`node_id` 来自模型输出，
  取值空间无界）；`context_metrics` 把进程累计值当作"本任务"收益展示
- 语义缓存的近邻检索只按 `(model, temperature)` 分桶，对系统提示/角色完全无感；
  降级结果被写进主模型的桶；未定价模型静默记 $0
- 熔断器预警去重判据失效（判据找 `"tokens"`，文案里是 `"token "`），
  比例过线后每次 `charge()` 都追加一条；节点 span 在**工作结束后**才创建，
  于是 trace 里的节点耗时恒为微秒级
- `InjectionGuard` 用 `str.format` 拼接含来源原文的提示，来源含花括号即抛异常
  （该路径异常会被验证层的 except 兜住 → 静默跳过验证）
- 评测双向合并可能产出 `{"overall": 3.5, "passed": false}` 这类自相矛盾结果；
  重试退避无抖动（并行节点会同步重试，把限流抖动放大成雪崩）
- `docs`/README 里 `DEVAGENT_MODELS__*__ENABLED`、`DEVAGENT_ORCHESTRATION__MAX_ATTEMPTS`
  等**不存在**的配置项已改为真实键名；测试数与 smoke token 数同步更新

### 新增

**回归测试与接线**

- `tests/unit/test_regressions.py`（38 个用例）：每条用例对应一个已复现缺陷，
  命名即说明"以前会怎样"
- `tests/unit/test_deployment_config.py`（12 个用例）：`.env.example` 能构造
  `Settings`、compose 的 env 合法、`init_db.sql` 与 ORM 逐列一致、镜像装了 `[db]`
- `web/pages.test.js`（41）、`web/imports.test.js`（10）：页面纯函数 + 模块图 +
  "动态数据不得当 HTML 解析"的静态防线；`make web-check` 已纳入
- `devagent.tools.runtime`：测试运行时装配（沙箱 + 执行器 + 工作区生命周期）
- 新增配置：`sandbox.allow_local_fallback` / `sandbox.user` / `sandbox.workspace_read_only`、
  `security.api_key`（非空时 `/api/v1/**` 要求 `X-API-Key`）、
  `evaluation.dataset_dir`（限定评测接口可读的数据集根目录）
- 新增指标：`context_hard_overflow`、`verification_unavailable`、`llm_unpriced_calls`；
  上下文工程指标统一带 `task_id`，前端"本任务收益"终于可归此
- 跨语言词表新增 `task_status`（后端加枚举 → 前端契约测试立即变红）

### 新增

**整套前端 UI 重设计与实现（可商用级）**

- **设计令牌三层架构**（`web/css/tokens.css`）：`palette`（原始色值）→
  `semantic`（按用途命名，如 `--surface-base` / `--text-primary` /
  `--status-danger`）→ 组件令牌。组件只引用语义层，此此明暗切换时
  组件 CSS **零改动**。共 187 个令牌，无悬空引用（`make web-check` 校验）
- **明暗双主题**：跟随系统 / 手动三态切换，`prefers-color-scheme` 实时监听；
  `index.html` 内置防 FOUC 的主题预置脚本
- **组件库**（`web/js/components.js` + `web/css/components.css`）：
  按钮（5 变体 × 4 尺寸 + loading 态）、表单、徽章/状态点、卡片、
  统计卡、表格、标签页、空/错/骨架态、toast、弹层、抽屉、代码 diff、
  时间线、DAG 画布、KV 列表、图表（donut / 横向条 / 迷你趋势）
- **图标系统**（`web/js/icons.js`）：77 个内联 SVG，24×24 viewBox、
  1.75 描边、继承 `currentColor`。不使用图标字体与外部图标库
- **6 个业务页面**（`web/js/pages/`）：总览、工作台、上下文看板、
  评测中心、可观测性、设置。覆盖后端全部能力接口
- **应用外壳**（`web/js/shell.js`）：hash 路由、字段级订阅 store、
  侧栏/顶栏、连接状态指示、页面 dispose 生命周期
- **响应式**（`web/css/responsive.css`）：1279/1023/767/639/1600 五档断点
  + 横屏手机 + `forced-colors`。≤1023px 侧栏转抽屉，≤767px 表格转卡片
  （`data-label` 与表头一一对应），触摸目标 ≥38px
- **可访问性**：焦点陷阱与焦点恢复（`activateModal`）、`:focus-visible`、
  skip-link、`aria-live` 分级通知（错误 `assertive` / 普通 `polite`）、
  `prefers-reduced-motion` 归零动效、WCAG AA 对比度、表格行键盘可激活、
  标签页方向键交互
- **`web/format.test.js`**（27 个断言）：钉住指标解析与哈希路由。
  这类 bug 不会让页面崩溃，只会**静默显示错误结论**，必须靠测试兜住
- **`make web-check`** 扩展为三段：全模块语法检查 + 相对导入完整性校验
  （防路径写错）+ 73 个纯逻辑断言

### 修复

- **前端指标名全线失配**：早期按 `devagent_` 前缀硬编码，但真实 OTLP
  指标**无前缀**（`llm_calls` / `llm_tokens` / `llm_cost_usd` /
  `context_compression_ratio` / `context_utilization` / `backtracks` /
  `hallucination_blocked` 等）。改为后缀匹配的新 `metric()` 系列
- **直方图取错层级**：结构是三层嵌套（名称 → 标签 → 统计量），此前直接读
  `histograms[name].p90` 得到 `undefined`。新增 `histOverallMean`
  （跨标签按 `count` 加权）与 `histMaxQuantile`（取最坏标签的分位）
- **skip-link 劫持导航**：`#page-root` 被 `parseHash()` 当成未知路由并
  回退到默认页，导致「跳转到主内容」实际跳转首页。现在页内锚点原样放行
- **`observability` 页面渲染崩溃**：`mount(container, ...buildLayout())`
  对单个 DOM 节点做展开，抛 `Spread syntax requires ...iterable`
- **`renderByState` 传字符串崩溃**：`error` 参数为字符串时被当函数调用。
  现在同时接受渲染函数与字符串/`Error`
- **`iconEl('')` 误告警**：未传图标名时调用 `icon(undefined)` 触发
  「未定义图标」警告并返回 `undefined`。现在空名返回空 fragment

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
