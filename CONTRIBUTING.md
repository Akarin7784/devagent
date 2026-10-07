# 贡献指南

感谢你有兴趣为 DevAgent 做贡献。本文档说明开发流程、代码规范与提交要求。

---

## 目录

- [行为准则](#行为准则)
- [开发环境](#开发环境)
- [开发工作流](#开发工作流)
- [代码规范](#代码规范)
- [测试要求](#测试要求)
- [提交信息规范](#提交信息规范)
- [Pull Request 流程](#pull-request-流程)
- [报告问题](#报告问题)
- [架构决策](#架构决策)

---

## 行为准则

参与本项目即表示你同意：

- 尊重不同经验水平的贡献者
- 就技术问题讨论技术本身，不针对个人
- 接受建设性批评，也以建设性方式提出批评
- 关注对社区最有利的做法

## 开发环境

### 前置条件

- Python **3.11+**（推荐 3.13）
- Git
- 可选：Docker（仅运行 `DockerSandbox` 时需要）

### 安装

```bash
git clone https://github.com/devagent/devagent.git
cd devagent

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 安装含开发依赖（ruff / mypy / pytest / pre-commit）
pip install -e ".[dev]"

# 或使用 Makefile
make install-dev

# 安装 git hook（提交时自动检查）
pre-commit install
```

### 验证环境

```bash
make check    # ruff + mypy
make test     # 315 个测试
python scripts/demo_smoke.py   # 端到端冒烟，无需 API Key
```

三条都通过，环境就算就绪。

> **不需要配置 API Key 也能开发。** 绝大多数测试使用注入的假 Provider，
> `demo_smoke.py` 也是脚本化的。只有在验证真实模型行为时才需要 Key。

---

## 开发工作流

### 1. 选择或创建 Issue

开始写代码前，请先在 Issue 中确认方向，尤其是：

- 新增功能
- 修改公共接口（`src/devagent/models/domain.py` 中的模型）
- 调整上下文装配算法（会显著影响评测指标）

小的修复（拼写、注释、明显 bug）可以直接提 PR。

### 2. 创建分支

```bash
git checkout -b feat/context-assembly-hybrid-score
git checkout -b fix/checkpoint-short-circuit
git checkout -b docs/adr-0004-sandbox-isolation
```

分支名前缀：`feat/` `fix/` `docs/` `refactor/` `test/` `chore/`。

### 3. 开发

边写边跑测试：

```bash
pytest tests/unit/test_context_engine.py -q
pytest -k "assembly" -q
```

### 4. 提交前自检

```bash
make format     # ruff format + 自动修复
make check      # ruff check + mypy --strict
make test
```

**这三步必须全绿**，CI 会执行同样的检查。

### 5. 提交并推送

```bash
git add -A
git commit -m "feat(context): 引入装配打分的位置权重"
git push origin feat/context-assembly-hybrid-score
```

---

## 代码规范

### 格式化与静态检查

项目使用 **ruff** 做格式化与 lint，**mypy --strict** 做类型检查。配置在 `pyproject.toml`，不要自行调整。

```bash
make format
make check
```

### 类型注解

**所有**函数（含测试）都必须有完整的类型注解。`mypy --strict` 会拒绝：

```python
# 不好
def assemble(chunks):
    return [c for c in chunks if c.score > 0.5]

# 好
def assemble(chunks: list[ContextChunk]) -> list[ContextChunk]:
    return [c for c in chunks if c.score > 0.5]
```

可选依赖用 `try/except ImportError` 惰性导入并优雅降级，同时在 `pyproject.toml`
的 mypy overrides 中声明 `ignore_missing_imports`。

### 不可变领域模型

`src/devagent/models/domain.py` 中的模型全部使用：

```python
model_config = ConfigDict(frozen=True, extra="forbid")
```

- `frozen=True`：Agent 之间传递的数据不可被下游偷偷修改
- `extra="forbid"`：模型输出多出字段时**立即报错**，而不是静默丢弃

新增领域模型请遵循同样的约定。

### 中文使用规范

本项目文档与注释使用中文。以下写法是**正确的**，ruff 已配置忽略对应规则：

- 全角标点：`，。：；（）`（RUF001/002/003 已忽略）
- 中文引号：`「」` `""`

代码标识符、日志 key、配置字段一律使用英文。

### 日志

使用 `structlog` 风格的结构化日志，key 用 `snake_case`，不要把变量拼进 message：

```python
# 不好
logger.info(f"节点 {node_id} 在第 {attempt} 次尝试后成功")

# 好
logger.info("node_succeeded", node_id=node_id, attempt=attempt)
```

### 文档字符串

公共 API 使用 Google 风格的 docstring。重点解释**为什么**，而不是复述代码在做什么：

```python
def _candidate_specs(self, primary: ModelSpec) -> list[ModelSpec]:
    """构造候选模型列表：首选在前，其他已启用提供商同级模型在后。

    降级顺序刻意与档位绑定而非全局固定，因为不同档位的「等价替代」
    并不相同——用 glm-4-plus 顶替 qwen-turbo 是错误的降级方向。
    """
```

---

## 测试要求

### 覆盖率期望

- 新增功能：必须带测试
- Bug 修复：必须带**复现该 bug 的回归测试**（这是硬性要求）
- 重构：不得降低覆盖率

### 测试分层

| 标记 | 说明 | 要求 |
| --- | --- | --- |
| `unit` | 快速、无外部依赖 | 默认 |
| `integration` | 需要 DB / Redis | 可选 |
| `e2e` | 需要真实 API Key | 可选 |
| `slow` | 耗时较长 | 可选 |

```bash
pytest -m unit           # 只跑单元测试
pytest -m "not slow"     # 跳过慢测试
```

### 编写风格

用**注入的假 Provider** 而不是 mock 掉内部方法——这样测试才能覆盖真实的调用链：

```python
class _ScriptedProvider(ModelProvider):
    """按顺序返回预设响应的假模型。"""

    name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages, *, model, **kwargs) -> ChatResult:
        self.calls.append(messages)
        content = self._responses.pop(0)
        return ChatResult(
            content=content,
            model=model,
            provider=self.name,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )
```

注意 `ChatResult` 必须传 `provider=`；`TokenUsage` 的 `total_tokens` 是派生属性，
传不传都会被正确计算（这是为防止记账 bug 而刻意设计的）。

### 回归测试命名

用一个能说明 bug 的测试名：

```python
async def test_dag_failure_propagates_recursively_not_just_one_level() -> None: ...
async def test_checkpoint_is_cleared_on_backtrack_so_retry_reexecutes() -> None: ...
def test_pytest_parser_keeps_failed_count_when_summary_order_varies() -> None: ...
```

---

## 提交信息规范

遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

### type

| type | 用途 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `docs` | 文档 |
| `refactor` | 重构（不改变行为） |
| `test` | 测试 |
| `chore` | 构建/工具/依赖 |
| `perf` | 性能优化 |

### scope

常用：`context` `orchestration` `agents` `models` `api` `eval` `observability` `reliability` `tools` `web`。

### 示例

```
feat(context): 冗余惩罚改用指数衰减 (gamma=4)

线性惩罚在小预算下不足以让重复片段让出位置：相关性满分时，
即使惩罚拉满仍能挤掉低相关但不冗余的关键约束。

改为指数衰减后，明显重复的第二个片段边际价值接近 0，
同时保留对「部分重叠」片段的软性权衡。

Closes #42
```

```
fix(reliability): 回退时清除检查点避免短路重试

验证驳回后重试会读到自己上一轮写入的「已完成」检查点，
直接标记成功，回退机制完全失效。

Fixes #87
```

---

## Pull Request 流程

1. **确保 `make check` 与 `make test` 全绿**
2. 填写 PR 描述，包含：
   - **What**：改了什么
   - **Why**：为什么需要改（关联 Issue）
   - **How**：关键实现思路
   - **Testing**：如何验证的
3. 保持 PR 聚焦——一个 PR 解决一个问题
4. 响应 review 意见；如有分歧，优先讨论设计而非实现细节

### Review 关注点

- 是否引入新的上下文污染路径（Verifier 是否还能被上游影响？）
- 新增模型调用是否已纳入成本账本与熔断
- 失败路径是否已考虑（重试、降级、部分失败）
- 是否可测量（新机制是否带指标？）

### CI

每次推送会执行：

```
ruff check + ruff format --check
mypy --strict
pytest（全量）
python scripts/demo_smoke.py
```

---

## 报告问题

### Bug Report

请包含：

- 复现步骤（最小化）
- 期望行为 vs 实际行为
- 关键日志（`--log-json` 输出更有用）
- 环境：Python 版本、操作系统、供应商

**注意脱敏**：不要贴 API Key，不要贴完整的内网地址。

### Feature Request

说明要解决的问题，而不只是想要的实现。「我需要 X 功能」不如
「我在做 Y 时遇到了 Z 障碍」有价值。

---

## 架构决策

重大设计变更请写 ADR 放入 `docs/adr/`，格式参考已有文档：

```
docs/adr/0001-自研编排层而非使用-langchain.md
docs/adr/0002-上下文装配使用硬去重作为保险.md
docs/adr/0003-可观测性不硬绑-opentelemetry.md
```

ADR 结构：**背景 → 决策 → 备选方案 → 后果**。重点是记录**被否决的方案及原因**，
这是后来者最需要的信息。

---

再次感谢你的贡献。
