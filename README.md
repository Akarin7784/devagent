# DevAgent

> 一个以**上下文工程（Context Engineering）**为内核的多 Agent 协作软件研发助手。

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-353%20passing-brightgreen)](#测试)
[![mypy](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/badge/code%20style-ruff-000000)](https://github.com/astral-sh/ruff)

DevAgent 把一句自然语言需求，变成「需求规格 → 技术方案 → 代码改动 → 测试 → 独立验证」的端到端交付，
并且把每一步的**上下文构造过程**作为一等公民进行量化与优化。

它不是为了再做一个「Agent 框架」，而是为了回答一个具体问题：

> **当多 Agent 协作时，如何保证每个 Agent 看到的信息既充分、又不冗余、且不从上游继承幻觉？**

---

## 目录

- [为什么是上下文工程](#为什么是上下文工程)
- [核心设计](#核心设计)
- [快速开始](#快速开始)
- [架构总览](#架构总览)
- [上下文工程五层模型](#上下文工程五层模型)
- [配置](#配置)
- [CLI 与 API](#cli-与-api)
- [测试](#测试)
- [项目结构](#项目结构)
- [设计文档](#设计文档)
- [常见问题](#常见问题)
- [贡献](#贡献)
- [License](#license)

---

## 为什么是上下文工程

过去两年 Agent 工程的重心发生了转移：**提示词工程**关心「怎么问」，
而**上下文工程**关心「在有限的窗口里放什么、不放什么、以什么顺序放」。

在多 Agent 系统里这不是优化项，而是正确性问题。三个具体的失效模式：

| 失效模式 | 现象 | DevAgent 的对策 |
| --- | --- | --- |
| **上下文污染** | Coder 的自我辩解被 Verifier 读到，验证形同虚设 | 隔离层：Verifier 的上下文**结构上**无法引用上游自我解释 |
| **幻觉传播** | 上游编造的函数名被下游当成事实继续使用 | 结构化握手：只传强类型字段，不传自然语言长文 |
| **预算挤占** | 高相关但重复的片段吃掉全部预算，关键约束被挤掉 | 加权打分 + **硬去重** + 非线性冗余惩罚 |

DevAgent 对这三点的处理都有**可测量的指标**与**对应的回归测试**，而不是停留在理念层面。

---

## 核心设计

### 1. 独立验证层（Independent Verifier）

验证 Agent **不共享** Coder 的对话上下文。它拿到的是：

- 需求规格中的验收标准原文
- Code diff（客观产物）
- 测试执行的真实输出

拿不到的是：Coder 说「我已经实现了 X」。

```
Coder 上下文:  requirements + constraints + reflexion_lessons + 相关代码
Verifier 上下文: requirements.验收标准 + diff + 测试stdout   ← 无 Coder 自述
```

这条边界由 `ContextIsolator` 在代码层面强制，并有专门测试守护。

### 2. DAG 驱动的编排与回退

Architect 把任务分解为 DAG，编排器按拓扑序并行调度无依赖节点。
失败时**递归传播**到所有下游，只重跑受影响的子图：

```
需求 → 架构 → ┌─ N1 分页参数 ──┐
              └─ N2 仓储查询 ──┴→ N3 测试 → 验证
                                     ↑
                          驳回时回退到 N1，清空其检查点
```

### 3. Reflexion：失败即资产

验证驳回时产出的 `lesson` 会被注入到同一 Agent 的下一次尝试，
按 Agent 分桶、FIFO 淘汰、自动去重。经验在任务内持续积累。

### 4. 预算熔断与成本账本

token 与步数双重熔断，**所有**模型调用（含必经的需求/架构阶段）都记账。
成本账本支持按模型、按档位聚合。

### 5. 可观测性（零依赖、可降级）

自研 `Span`/`Tracer` + Counter/Gauge/Histogram，**不硬绑 OpenTelemetry**：
检测到 OTel SDK 就走 OTLP 导出，没有就静默降级为 Noop。默认**关闭**，零开销。

---

## 快速开始

### 环境要求

- Python **3.11+**（开发使用 3.13）
- 无需数据库、无需 Docker 即可跑通冒烟（内存模式）
- 真实运行需要至少一个国内模型 API Key

### 三步跑通

```bash
# 1. 安装（可编辑模式）
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. 无需 API Key 验证全链路（脚本化假模型）
python scripts/demo_smoke.py
# → 状态=succeeded，展示 DAG 节点、执行步骤、token 总量

# 3. 配置真实模型后执行
cp .env.example .env               # 填入 DEEPSEEK_API_KEY 等
devagent run "为用户列表接口增加分页能力"
```

`demo_smoke.py` 的预期输出：

```
任务 task_1791360851430  状态=succeeded
模型调用次数 : 8
DAG 节点     : {"N1": "success"}
执行步骤     : requirement:ok -> architect:ok -> coder:ok -> verifier:reject -> coder:ok -> verifier:pass
token 总量   : 2011
SMOKE OK
```

> 注意其中 `verifier:reject → coder:ok` 这一段：脚本故意让验证器第一次驳回，
> 用来覆盖**回退重试**路径。真实运行时这条路径由模型自行触发。

### 启动控制台

```bash
devagent serve --port 8000
# 浏览器打开 web/index.html（零构建，原生 ES Module）
# 或直接访问 API 文档 http://localhost:8000/docs
```

---

## 架构总览

```
                    ┌──────────────────────────────────────────┐
                    │            Orchestrator                  │
                    │   DAG 调度 · 并行执行 · 失败传播 · 回退   │
                    └───────────────┬──────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌───────────────┐          ┌───────────────┐          ┌───────────────┐
│  Requirement  │          │   Architect   │          │     Coder     │
│  需求澄清      │          │  方案 + DAG   │          │  代码改动     │
└───────────────┘          └───────────────┘          └───────────────┘
                                                              │
                       ┌──────────────────────────────────────┤
                       ▼                                      ▼
              ┌───────────────┐                    ┌─────────────────┐
              │    Tester     │                    │    Verifier     │
              │  生成并执行测试 │                    │  独立验收（隔离）│
              └───────────────┘                    └─────────────────┘

        ┌───────────────────────────────────────────────────────┐
        │            Context Engineering Layer                  │
        │  L1 路由 · L2 隔离 · L3 压缩 · L4 装配 · L5 预算       │
        └───────────────────────────────────────────────────────┘
        ┌───────────────────────────────────────────────────────┐
        │  ModelGateway: 路由 · 语义缓存 · 成本账本 · 供应商降级  │
        └───────────────────────────────────────────────────────┘
        ┌───────────────────────────────────────────────────────┐
        │  Observability: Tracing · Metrics · Prometheus 导出    │
        └───────────────────────────────────────────────────────┘
```

### 模块地图

| 模块 | 路径 | 职责 |
| --- | --- | --- |
| 领域模型 | `src/devagent/models/domain.py` | 全部 frozen Pydantic 模型，`extra="forbid"` |
| 模型层 | `src/devagent/models/` | Provider 抽象、网关、缓存、账本 |
| 上下文工程 | `src/devagent/context/` | **核心竞争力**：装配/压缩/预算/隔离/路由 |
| Agent 层 | `src/devagent/agents/` | 6 个 Agent 的提示词与解析 |
| 编排层 | `src/devagent/orchestration/` | DAG、主循环、状态机 |
| 可靠性 | `src/devagent/reliability/` | Reflexion、检查点、熔断、循环检测 |
| 工具层 | `src/devagent/tools/` | 沙箱、pytest 解析、AST 代码索引 |
| 可观测性 | `src/devagent/observability/` | 自研 Span/Metrics，可降级 OTLP |
| 评测 | `src/devagent/evaluation/` | Golden set、双向 LLM-as-Judge、轨迹指标 |
| API | `src/devagent/api/` | FastAPI + SSE 流式进度 |
| 前端 | `web/` | 零构建控制台（原生 ES Module） |

---

## 上下文工程五层模型

这是本项目的核心，也是面试中最值得展开的部分。

### L1 · 路由（Routing）

不是所有任务都值得用大模型。复杂度评分将请求分派到 `SMALL` / `MEDIUM` / `LARGE` 档位，
失败后自动升级档位重试。

### L2 · 隔离（Isolation）

每个 Agent 一个 `ContextSpace`，空间之间通过 `ContextIsolator` 管控访问。
最关键的一条规则：**Verifier 空间禁止借用 Coder 的自述内容**。

### L3 · 压缩（Compression）

分层压缩：热数据（当前相关的）保留原文，温数据转结构化摘要，冷数据只留引用。
压缩标记与「装配丢弃」严格区分，避免指标虚高。

### L4 · 装配（Assembly）

这是最核心的算法。目标：在有限预算内选出**高相关且低冗余**的片段集合，并按位置摆放。

```
score(chunk) = w_rel · relevance
             + w_rec · recency
             + w_auth · authority          # 来源权威度
             - w_red · redundancy_penalty  # 非线性，gamma=4

选择过程：贪心 + 硬去重（双保险）
位置编排：高价值片段放头尾（对抗 lost-in-the-middle）
```

**非线性冗余惩罚**的意义：两个高度相似的片段，第二个的边际价值远低于线性衰减的估计。
指数 `gamma=4` 让「明显重复」的片段几乎失去竞争力，但仍保留软性权衡空间。

**硬去重**是第二道保险：即便软打分因配置失误让冗余片段胜出，去重也会在最终集合上兜住。
这个设计来自一个真实 bug（见 [docs/05](docs/05-工程落地指南.md)）。

### L5 · 预算（Budget）

按角色的预算模板 + 动态回流：验证阶段预算更高，编码阶段输入预算更高。
小预算下按比例压缩输出预留（上限 25%），避免可用输入被压到 0。

---

## 配置

配置通过环境变量注入，嵌套字段用 `__` 分隔（`pydantic-settings` 约定）。

```bash
# 模型供应商（至少配一个）
DEEPSEEK_API_KEY=sk-xxx
QWEN_API_KEY=sk-xxx
ZHIPU_API_KEY=xxx

# 嵌套配置示例
DEVAGENT_MODELS__DEEPSEEK__TIMEOUT_SECONDS=120
DEVAGENT_OBSERVABILITY__METRICS_ENABLED=true
DEVAGENT_ORCHESTRATION__MAX_ATTEMPTS=3
```

完整清单见 [`.env.example`](.env.example)。

### 关键开关

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DEVAGENT_MODELS__*__ENABLED` | 按 Key 自动 | 供应商开关 |
| `DEVAGENT_OBSERVABILITY__METRICS_ENABLED` | `false` | 指标采集（默认关闭，零开销） |
| `DEVAGENT_OBSERVABILITY__OTLP_ENDPOINT` | `""` | 设置后走 OTLP 导出 |
| `DEVAGENT_ORCHESTRATION__MAX_ATTEMPTS` | `3` | 单节点最大尝试次数 |
| `DEVAGENT_RELIABILITY__MAX_TASK_TOKENS` | `500_000` | 任务级 token 熔断阈值 |
| `DEVAGENT_STORAGE__BACKEND` | `memory` | 任务存储后端：`memory` 或 `sql` |
| `DEVAGENT_DATABASE__URL` | 本地 Postgres | `sql` 模式下的连接串；测试用 `sqlite+aiosqlite:///./devagent.db` |

### 任务持久化（可选）

默认走内存存储 —— clone 下来**不需要任何数据库**就能跑通 demo。
需要在进程重启后保留任务与事件流时，切到 SQL 后端：

```bash
pip install -e '.[db]'                      # sqlalchemy[asyncio] + aiosqlite + asyncpg
export DEVAGENT_STORAGE__BACKEND=sql
export DEVAGENT_DATABASE__URL=postgresql+asyncpg://user:pass@localhost/devagent
psql "$DEVAGENT_DATABASE__URL" -f scripts/init_db.sql   # 扩展与索引（Postgres）
```

`TaskStore` 是协议，内存与 SQL 两种实现**可直接互换**，上层零改动。
事件落库后，SSE 的「历史回放」在进程重启后依然可靠（见
[ADR-0002](docs/adr/0002-上下文装配使用硬去重作为保险.md) 同类取舍记录）。

---

## CLI 与 API

### CLI

```bash
devagent run "需求描述"                    # 执行任务
devagent run "需求" --json                 # JSON 输出
devagent eval --category requirement      # 跑 golden set 评测
devagent index src/devagent --query "assemble budget"   # 检查 AST 索引
devagent serve --port 8000                # 启动 API
```

### 主要端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/tasks` | 提交任务 |
| `GET` | `/api/v1/tasks/{id}` | 查询任务与 DAG 节点状态 |
| `GET` | `/api/v1/tasks/{id}/events` | **SSE** 实时执行事件流 |
| `GET` | `/api/v1/tasks/{id}/context` | 查看上下文装配决策 |
| `POST` | `/api/v1/evaluate` | 运行评测 |
| `GET` | `/api/v1/metrics` | 指标快照 |
| `GET` | `/api/v1/metrics/prometheus` | Prometheus 文本格式 |
| `GET` | `/api/v1/traces` | 近期 trace |

### SSE 事件流

```bash
curl -N http://localhost:8000/api/v1/tasks/<id>/events
```

事件类型：`task.started` / `node.started` / `node.verdict` / `node.finished` / `task.finished`。
支持**历史回放**（晚订阅者也能拿到完整时间线），慢消费者采用丢弃最旧事件策略，绝不阻塞主流程。

---

## 测试

```bash
make test            # 全量（353 个）
make test-unit       # 仅单元测试
make check           # ruff + mypy --strict
```

当前状态：

```
353 passed in 21.3s
ruff check .......... 通过
ruff format --check . 通过
mypy --strict ....... 52 个源文件，0 错误
```

测试覆盖了若干**回归场景**，每一个都对应一个曾经真实存在的 bug：

- `TokenUsage.total_tokens` 恒为 0 导致熔断失效
- `PytestOutputParser` 有序正则提前返回导致 `failed` 计数丢失
- DAG 失败传播只传一层，下游下游悬挂在 `PENDING`
- 检查点短路回退：驳回后重试读到自己的旧检查点直接成功
- `MetricsResponse.histograms` schema 层级不匹配（一有数据就 500）

这些不是构造的测试用例，是开发过程中真的踩到的坑。

---

## 项目结构

```
.
├── src/devagent/
│   ├── agents/           # 6 个 Agent
│   ├── api/              # FastAPI + SSE
│   ├── context/          # ★ 上下文工程
│   ├── evaluation/       # 评测体系
│   ├── models/           # 领域模型 + 模型网关
│   ├── observability/    # 追踪与指标
│   ├── orchestration/    # DAG 编排
│   ├── reliability/      # 可靠性机制
│   ├── tools/            # 沙箱 / 测试执行 / 代码索引
│   ├── db/               # 持久化（SQLAlchemy 异步 ORM，可选依赖）
│   ├── cli.py
│   └── config.py
├── tests/unit/           # 全量单元测试
├── datasets/             # golden set（13 样本 4 类）
├── docs/                 # 设计文档 + ADR
├── web/                  # 零构建前端
└── scripts/              # 冒烟与辅助脚本
```

---

## 设计文档

深入的设计取舍记录在 `docs/`：

| 文档 | 内容 |
| --- | --- |
| [01-项目总纲](docs/01-项目总纲.md) | 定位、架构、数据模型、里程碑 |
| [02-上下文工程深度设计](docs/02-上下文工程深度设计.md) | ★ 五层模型与装配算法 |
| [03-多Agent编排与可靠性](docs/03-多Agent编排与可靠性.md) | 拓扑、握手协议、幻觉阻断 |
| [04-面试题库与简历话术](docs/04-面试题库与简历话术.md) | 深挖题与表述方式 |
| [05-工程落地指南](docs/05-工程落地指南.md) | 目录结构、关键代码、踩坑记录 |
| [ADR](docs/adr/) | 架构决策记录 |

---

## 常见问题

**为什么不用 LangChain / LangGraph？**

需要精确控制上下文预算、重试策略与全链路埋点。框架的抽象层会让「这个 token 为什么被放进去了」
变得难以回答——而这恰恰是本项目要解决的问题。自研编排层约 600 行，代价可控。

**多 Agent 是不是过度设计？**

对简单任务确实是。因此保留了**单 Agent 降级模式**：复杂度评分低于阈值时直接走单 Coder，
不启动完整 DAG。这也是一个可被配置开关控制的显式设计，不是一个被忽略的缺陷。

**前端为什么不用 React？**

为了让「clone 下来就能跑」成立。`web/` 使用原生 ES Module + 无构建步骤，
打开 `index.html` 即可。零 `npm install` 意味着零依赖地狱。

**支持哪些模型？**

任何 OpenAI 兼容接口。内置 DeepSeek / 通义千问 / 智谱的适配与价格表，
通过 `ModelProvider` 协议可以接入任意供应商。

**能离线跑吗？**

可以。`scripts/demo_smoke.py` 使用脚本化假模型，不需要任何 API Key 或网络。

---

## 贡献

欢迎提交 Issue 与 PR。开始前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

```bash
make install-dev    # 安装依赖 + pre-commit hook
make check          # 提交前必须通过
make test
```

## License

[Apache-2.0](LICENSE)
