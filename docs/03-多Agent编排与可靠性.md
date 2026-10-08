# 多 Agent 编排与可靠性设计

本文保留拓扑和可靠性设计方案。当前补丁尚未应用、检查点默认在内存中、Reviewer 不会自动追加；设计中的持久化、自动升级等步骤不能当成已实现能力。当前行为见 [架构说明](06-架构说明.md) 和 [质量文档](07-质量与已知限制.md)。

> 本文档解决：「多个 Agent 怎么组织、怎么协作、怎么不出错、错了怎么办」。
> 这是把「demo」变成「系统」的关键篇章。

---

## 1. 为什么是"多 Agent"——先回答质疑

面试官很可能会问：**「多 Agent 是不是过度设计？单 Agent 不行吗？」**
这是高质量反问，必须准备有说服力的回答。

### 1.1 多 Agent 的真实收益

| 收益 | 说明 |
|---|---|
| **能力解耦** | 每个 Agent 专注一件事，prompt 更短更精准，效果好于「万能 prompt」 |
| **上下文隔离** | 天然防止上下文污染与膨胀（见 02 文档） |
| **可验证性** | 独立 Agent 可以交叉验证，阻断幻觉传播 |
| **可观测性** | 每个 Agent 的输入输出可独立追踪，便于定位问题 |
| **可替换性** | 某个 Agent 可以换模型/换实现，不影响其他 |

### 1.2 多 Agent 的代价（要诚实承认）
- token 成本上升（多轮交互）
- 延迟增加（串行步骤）
- 编排复杂度上升
- **可能引入「Agent 间推诿」和「循环」**

### 1.3 你的立场（成熟候选人的回答）
> 「多 Agent 不是必然更好。我做过对比实验：对于简单的单文件修改任务，
> 单 Agent 反而更快更便宜；但对于「需求理解 + 跨文件改动 + 测试 + 审查」这类
> 多阶段任务，多 Agent 的成功率明显更高，因为上下文隔离和独立验证带来的收益
> 超过了 token 成本。所以我保留了**单 Agent 降级模式**，按任务复杂度选择拓扑。」

**这句回答直接展示你不是跟风，而是有工程判断。**

---

## 2. 编排拓扑设计

### 2.1 采用：Hierarchical Supervisor + Verifier 层

```
                    Orchestrator (Supervisor)
                    规划 / 调度 / 状态维护
                            │
      ┌──────────┬──────────┼──────────┬──────────┐
      ▼          ▼          ▼          ▼          ▼
 Requirement Architect   Coder      Tester    Reviewer
   Agent       Agent      Agent      Agent      Agent
                          │
                          └──────▶ Verifier Agent（贯穿式验证）
```

### 2.2 为什么不用 Peer-to-Peer（全互联）
- 全互联会导致**消息组合爆炸**、**责任不清**、**难以调试**
- Supervisor 集中调度 → 状态清晰、可追踪、易恢复

### 2.3 为什么 Verifier 独立
- 干活的人不能同时当裁判（自评有确认偏差）
- Verifier 用**不同模型 + 不同 prompt**，只看客观证据
- 这是**阻断幻觉传播**的核心机制

---

## 3. Orchestrator（编排器）设计

### 3.1 职责边界（严格）
✅ **做**：任务分解、DAG 构建、Agent 调度、状态维护、失败处理、成本管控
❌ **不做**：不写代码、不做设计、不自己验证（避免又当运动员又当裁判）

### 3.2 任务分解为 DAG

```python
@dataclass
class TaskNode:
    id: str
    goal: str
    agent_type: AgentType
    deps: list[str]              # 依赖的前置节点
    acceptance_criteria: list[str]
    status: StepStatus
    attempt: int = 0
```

**DAG 的价值**：
- 无依赖节点可**并行执行**（降延迟）
- 依赖关系显式化 → **可判断能否重跑**
- 失败时只重跑受影响子图，而非全量

### 3.3 编排状态机

```
PENDING → READY → RUNNING → (SUCCESS | FAILED) → VERIFYING
                                  ↑                  │
                                  │              (PASS | REJECT)
                                  └──── 带反馈回退 ───┘
```

- `READY`：所有依赖已完成
- `VERIFYING`：Verifier 校验中
- `REJECT`：打回上游，携带**结构化反馈**（不是简单「重试」）

### 3.4 调度策略
- **DAG 拓扑排序** + 就绪队列
- 无依赖节点并行（受并发上限约束）
- 优先级：关键路径优先（缩短总时长）

---

## 4. Agent 间通信协议（结构化握手）

### 4.1 核心原则
**Agent 之间不传自然语言长文，只传强类型结构化消息。**

理由：可校验、防歧义、可压缩、可追踪、便于程序化处理。

### 4.2 消息类型定义

```python
class MessageType(str, Enum):
    TASK_SPEC      = "task_spec"       # 派发任务
    ARTIFACT       = "artifact"        # 提交产物
    FEEDBACK       = "feedback"        # 反馈（含拒绝理由）
    QUESTION       = "question"        # 请求澄清
    DECISION       = "decision"        # 记录决策
    ESCALATION     = "escalation"      # 升级（搞不定）

@dataclass
class AgentMessage:
    msg_id: str
    from_agent: AgentType
    to_agent: AgentType
    type: MessageType
    task_id: str
    payload: dict                     # Pydantic 校验
    context_refs: list[str]           # 上下文引用（不内联全文）
    created_at: datetime
```

### 4.3 反馈消息的结构化（关键）

失败回退**不能只说「重试」**，必须携带可操作的信息：

```json
{
  "type": "feedback",
  "verdict": "REJECT",
  "failed_criteria": ["非法参数未返回 400"],
  "evidence": {
    "test_case": "test_users_invalid_page",
    "actual": "返回 200 + 空列表",
    "expected": "返回 400"
  },
  "suggestions": ["在参数解析层增加校验", "参考已有校验工具函数"],
  "context_refs": ["file://src/api/users.py#L45-L60"]
}
```

**这是「带反馈的迭代」而非「盲目重试」** —— 面试重点。

---

## 5. 可靠性机制

### 5.1 幻觉传播阻断（四道防线）

| 防线 | 机制 |
|---|---|
| **第一道：证据链** | Coder 必须给出「改动理由 → 对应验收标准」的映射，无法对应则打回 |
| **第二道：测试即事实** | 以沙箱中实际执行结果为准，不采信 Agent 自述「已完成」 |
| **第三道：独立验证** | Verifier 用不同模型交叉验证，且不接收 Coder 的自我辩解 |
| **第四道：交叉审查** | Reviewer 从代码质量维度独立审查，与前两者结论交叉比对 |

### 5.2 Reflexion（失败反思）

失败不是重试，而是**学习**：

```
步骤失败
  → 收集失败证据（测试输出、报错栈、Verifier 反馈）
  → 生成「教训」（结构化）：
      {
        root_cause: "未处理边界值",
        lesson: "分页参数需校验下界，page_size 最小为 1",
        avoid: "不要假设调用方总是传入合法值"
      }
  → 教训注入下次尝试的上下文（放在显眼位置）
  → 重试
  → 若连续失败 → 升级模型 / 换方案 / 请求人工
```

### 5.3 Checkpoint 与断点续跑
- 每个步骤完成即持久化「输入引用 + 输出 + 状态」
- 崩溃恢复：从最后一个成功的 checkpoint 继续，已完成步骤不重跑
- **幂等保证**：重跑不产生副作用（沙箱隔离 + 状态快照）

### 5.4 循环与死锁检测
- 记录 Agent 调用图，检测 A→B→A 循环
- 相同失败连续 N 次 → 判定为「卡死」→ 升级策略
- 全局最大步骤数上限（防无限循环烧钱）

### 5.5 成本熔断
- 单任务 token 超阈值 → 暂停 + 告警
- 单步骤重试超限 → 终止 + 输出中间产物（不白跑）
- 异常模式检测（如 token 消耗速率突增）→ 提前熔断

---

## 6. 沙箱执行安全

代码必须在不信任环境执行，安全设计：

| 措施 | 说明 |
|---|---|
| 容器隔离 | Docker 独立容器，不用宿主环境 |
| 资源限制 | CPU / 内存 / 磁盘 / 进程数限制 |
| 网络隔离 | 默认禁网，仅白名单（如需装依赖） |
| 文件系统 | 只读挂载源码，独立可写临时区 |
| 超时强杀 | 硬超时 + 僵死检测 |
| 非 root 运行 | 最小权限原则 |
| 审计日志 | 记录所有执行的命令与输出 |

**面试加分**：可以聊「为什么不用 microVM（Firecracker）」——安全更强但复杂度与收益在当前场景不匹配，
作为演进方向预留。

---

## 7. 可观测性（全链路追踪）

### 7.1 追踪粒度
每次 **LLM 调用**、**工具调用**、**Agent 步骤** 都生成 span：

```
Task span
 └── Step span (Coder)
      ├── LLM call span (prompt_tokens, completion_tokens, latency, cost, model)
      ├── Tool call span (sandbox exec, duration, exit_code)
      └── Context assembly span (candidates, selected, tokens_before/after)
```

### 7.2 关键埋点指标
- 每步 token 与成本
- 上下文装配的「候选数 / 选中数 / 压缩比」
- 模型路由决策（为什么选这个模型）
- 失败原因分类统计

### 7.3 用途
- 调试（哪一步出的问题）
- 优化（哪个 Agent 最耗 token）
- 演示（前端可视化协作流程）
- 评测（轨迹数据来源）

---

## 8. 端到端时序（一个完整任务的旅程）

```
1. 用户输入需求 → 创建 Task
2. Orchestrator 派发给 Requirement Agent
   → 输出：结构化需求规格（含验收标准）
   → Verifier 检查规格是否可验收
3. Orchestrator 派发给 Architect Agent
   → 输出：技术方案 + 任务 DAG
4. 遍历 DAG，就绪节点派发给 Coder
   → Coder 产出 patch + 改动理由
5. Tester 写测试并在沙箱执行
   → 测试结果作为客观证据
6. Verifier 交叉验证（验收标准 vs 实际产出 vs 测试结果）
   → REJECT：带结构化反馈回退 Coder（含 Reflexion 教训）
   → PASS：进入下一步
7. Reviewer 做代码质量审查
8. 全部通过 → 交付（Patch + 测试报告 + 审查意见 + 决策记录）
```

---

## 9. 当前实现与后续工作

面试回答统一维护于 [面试题库](04-面试题库与简历话术.md)，避免在设计文档中重复维护话术和未经评测的收益数字。

上面的时序是目标方案。当前运行数据流见 [架构说明](06-架构说明.md)，已修复问题和剩余能力边界见 [质量与已知限制](07-质量与已知限制.md)，后续工作见 [项目总纲](01-项目总纲.md)。不再维护与代码脱节的 M2 / M4 勾选清单。
