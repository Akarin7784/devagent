"""Orchestrator：编排器（系统的大脑）。

职责边界（严格遵守）：
✅ 做：任务分解、DAG 构建、Agent 调度、状态维护、失败处理、成本管控
❌ 不做：不写代码、不做设计、不自己验证
（避免「又当运动员又当裁判」——验证交给独立的 Verifier）

执行模型：DAG 驱动的循环推进——
1. 取就绪节点；
2. 为每个节点装配上下文并派发给对应 Agent；
3. 处理产出：成功则标记并解冻下游，失败则带反馈回退；
4. 直到 DAG 完成或触发熔断。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from devagent.agents import (
    AgentInvocation,
    AgentOutput,
    ArchitectAgent,
    BaseAgent,
    CoderAgent,
    RequirementAgent,
    ReviewerAgent,
    TesterAgent,
    TestRunOutcome,
    VerifierAgent,
)
from devagent.config import Settings
from devagent.context import ContextEngine, make_chunk
from devagent.context.isolation import ContextIsolator
from devagent.enums import AgentType, ContextKind, StepStatus, TaskStatus, Verdict
from devagent.logging_config import get_logger
from devagent.models.domain import (
    AgentHandoff,
    AgentMessage,
    ContextRef,
    FeedbackPayload,
    ReflexionLesson,
    RoutingSignals,
    StepResult,
)
from devagent.models.gateway import ModelGateway
from devagent.observability import MetricNames, get_observability
from devagent.observability.tracing import Span
from devagent.orchestration.dag import DAG, DAGError, build_dag_from_architect_output
from devagent.reliability import (
    BudgetExceededError,
    CircuitBreaker,
    LoopDetector,
    ReflexionMemory,
    StepCheckpoint,
    TaskCheckpointStore,
)

logger = get_logger(__name__)

# 进度事件回调签名：(kind, payload)。kind 如 node_started / node_finished。
# 用普通 Callable 而不是 Protocol，是为了让调用方可以直接传一个 sync 函数
# （API 层内部转成 EventBus publish），无需定义类。
EventHook = Callable[[str, dict[str, Any]], None]


class OrchestrationError(RuntimeError):
    """编排过程中的不可恢复错误。"""


@dataclass(slots=True)
class _RunState:
    """**单次运行**的全部可变状态。

    为什么要显式抽出来：Orchestrator 全进程只有一个实例（API 层在
    ``create_app`` 里构造一次），而 ``TaskService`` 默认允许 4 个任务并发。
    此前这些字段全都挂在 ``self`` 上，于是并发任务**共用同一份**上下文空间、
    熔断器、反思记忆与运行 id —— 实测任务 B 的需求提示词里出现了任务 A 的
    需求原文，且节点事件被发到了别人的 SSE 流里。

    现在每次 ``run()`` 造一个状态对象并放进 ``ContextVar``（asyncio 的每个
    Task 有独立的 context 拷贝，因此天然按任务隔离），运行期间的读写一律走它。
    """

    task_id: str
    run_id: str
    isolator: ContextIsolator
    breaker: CircuitBreaker
    reflexion: ReflexionMemory
    loop_detector: LoopDetector
    outputs: dict[str, AgentOutput] = field(default_factory=dict)
    task_span: Span | None = None


@dataclass(slots=True)
class _TestEvidence:
    """Tester 产出的客观证据 + 它的用量（用量必须回传给调用方计账）。"""

    text: str = ""
    tokens: int = 0
    cost: float = 0.0


@dataclass(slots=True)
class TaskRunResult:
    """一次任务编排的最终结果。"""

    task_id: str
    status: TaskStatus
    goal: str
    dag: DAG | None = None
    steps: list[StepResult] = field(default_factory=list)
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    duration_ms: int = 0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is TaskStatus.SUCCEEDED


@dataclass(slots=True)
class OrchestratorConfig:
    """编排器行为配置。"""

    max_steps: int = 50
    max_tokens: int = 500_000
    max_retries: int = 3
    max_parallel: int = 4
    enable_tester: bool = True
    enable_reviewer: bool = True
    """是否启用 Tester/Reviewer。关闭可加速，但会削弱证据链。"""

    max_backtrack_depth: int = 5
    """回退链最大深度（对齐 ``reliability.max_backtrack_depth``）。

    此前该配置项从未被读取：``LoopDetector`` 用构造默认值，阈值与
    ``max_retries`` 重叠，导致「循环检测」那条分支实际不可达。
    """

    same_failure_threshold: int = 3
    """相同失败连续出现多少次判为卡死（对齐 ``reliability.same_failure_threshold``）。"""

    enable_checkpoints: bool = True
    """是否启用断点续跑（对齐 ``reliability.checkpoint_enabled``）。"""


class Orchestrator:
    """多 Agent 编排器。

    用法::

        orch = Orchestrator(settings)
        result = await orch.run("为 /users 接口增加分页参数")
    """

    def __init__(
        self,
        settings: Settings,
        *,
        gateway: ModelGateway | None = None,
        context_engine: ContextEngine | None = None,
        config: OrchestratorConfig | None = None,
        checkpoint_store: TaskCheckpointStore | None = None,
        test_runner: Any = None,
        on_event: EventHook | None = None,
    ) -> None:
        self._settings = settings
        self._gateway = gateway or ModelGateway(settings)
        self._context = context_engine or ContextEngine(settings.context)
        # 注入防护器：从上下文引擎取，保证与装配打分用的是同一份配置。
        # 不在这里新建实例 —— 两处独立构造会在配置变更时悄悄分叉。
        self._guard = self._context.guard
        # 进度回调：可选、纯观测性，失败绝不影响编排（见 _emit 的注释）。
        self._on_event = on_event
        self._config = config or OrchestratorConfig(
            max_steps=settings.reliability.max_task_steps,
            max_tokens=settings.reliability.max_task_tokens,
            max_retries=settings.reliability.max_retries,
            max_backtrack_depth=settings.reliability.max_backtrack_depth,
            same_failure_threshold=settings.reliability.same_failure_threshold,
            enable_checkpoints=settings.reliability.checkpoint_enabled,
        )
        self._checkpoints = checkpoint_store or TaskCheckpointStore()
        self._test_runner = test_runner

        # 各专职 Agent
        self._agents: dict[AgentType, BaseAgent] = {
            AgentType.REQUIREMENT: RequirementAgent(self._gateway),
            AgentType.ARCHITECT: ArchitectAgent(self._gateway),
            AgentType.CODER: CoderAgent(self._gateway),
            AgentType.TESTER: TesterAgent(self._gateway, test_runner=test_runner),
            AgentType.VERIFIER: VerifierAgent(self._gateway),
            AgentType.REVIEWER: ReviewerAgent(self._gateway),
        }

        # ---- 运行级状态 ----
        # 运行期一律通过 _state() 读取 ContextVar；下面这几个同名属性是
        # 「最近一次运行」的镜像，只用于事后自省（测试与排障会读它们），
        # **不要**在编排逻辑里使用它们 —— 并发下它们指向的是别的任务。
        self._run_state: ContextVar[_RunState | None] = ContextVar(
            "devagent_run_state", default=None
        )
        self._breaker = CircuitBreaker(
            max_tokens=self._config.max_tokens,
            max_steps=self._config.max_steps,
        )
        self._reflexion = ReflexionMemory()
        self._loop_detector = self._make_loop_detector()
        self._run_id: str = ""
        """最近一次运行的标识。"""
        self._task_span: Span | None = None
        """最近一次运行的任务根 span。"""

    # ------------------------------------------------------------------ #
    # 运行级状态
    # ------------------------------------------------------------------ #

    def _make_loop_detector(self) -> LoopDetector:
        """按配置构造循环检测器（把此前未接线的两个阈值接上）。"""
        return LoopDetector(
            max_attempts=self._config.max_backtrack_depth,
            same_failure_threshold=self._config.same_failure_threshold,
        )

    def _state(self) -> _RunState:
        """当前运行的状态；不在运行中时抛错而不是静默返回脏数据。"""
        state = self._run_state.get()
        if state is None:
            raise OrchestrationError("编排器运行状态未初始化：请通过 run() 启动任务")
        return state

    def _new_run_state(self, task_id: str) -> _RunState:
        return _RunState(
            task_id=task_id,
            # 加随机后缀：毫秒级时间戳不足以区分同时启动的两个任务
            # （实测两个并发任务拿到同一个 run_<ms>）。
            run_id=f"run_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
            # 从引擎的容器**快照**开始：既保留调用方预置的片段，
            # 又保证本次运行的写入不会泄漏给其他任务。
            isolator=self._context.isolator.snapshot(),
            breaker=CircuitBreaker(
                max_tokens=self._config.max_tokens,
                max_steps=self._config.max_steps,
            ),
            reflexion=ReflexionMemory(),
            loop_detector=self._make_loop_detector(),
        )

    # ------------------------------------------------------------------ #
    # 进度事件
    # ------------------------------------------------------------------ #

    def _emit(self, kind: str, **payload: Any) -> None:
        """发出一个进度事件（可选能力，默认没有任何订阅者）。

        设计要点：**回调异常绝不能影响编排**。订阅方（如 SSE 推送）
        可能因为客户端断连、队列满等原因抛异常，而任务本身必须继续跑完
        —— 「观测失败导致业务失败」是最不该出现的一类耦合。
        因此这里吞掉异常并降级为一条日志。
        """
        hook = self._on_event
        if hook is None:
            return
        try:
            hook(kind, payload)
        except Exception:
            logger.warning("event_hook_failed", kind=kind, exc_info=True)

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #

    async def run(self, goal: str, *, task_id: str | None = None) -> TaskRunResult:
        """执行一个完整的研发任务。

        Args:
            goal: 自然语言需求。
            task_id: 可选任务 id（默认自动生成）。

        Returns:
            ``TaskRunResult``。

        并发语义：本方法可被**并发调用**（同一实例）。所有运行期可变状态都
        放在本次调用自己的 ``_RunState`` 里，通过 ``ContextVar`` 存取 ——
        asyncio 会给每个 Task 复制一份 context，因此两个并发任务互不可见。
        """
        # 自动生成的 task_id 同样加随机后缀：检查点按 (task_id, step_id) 归档，
        # 若两个并发运行恰好落在同一毫秒，它们会共享检查点 —— 第二次运行
        # 可能"恢复"到第一次的中间状态。API 层总是显式传入 id，
        # 这里兜住的是直接使用库的场景。
        tid = task_id or f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}"
        start = time.perf_counter()
        logger.info("task_started", task_id=tid, goal=goal[:100])

        # 建立本次运行的状态并安装到当前 Task 的 context。
        state = self._new_run_state(tid)
        token = self._run_state.set(state)
        # 事后自省用的镜像（并发下**仅供查看**，逻辑一律读 _state()）。
        self._run_id = state.run_id
        self._breaker = state.breaker
        self._reflexion = state.reflexion
        self._loop_detector = state.loop_detector

        # 整个任务包一个根 span。注意这里用显式 ``task_span`` 贯穿全程而不是
        # with 语句，因为下面有多个 return 分支（提前失败返回），用 with 会
        # 让「返回」与「关闭 span」两个语义耦合在缩进里，容易漏掉某个分支。
        obs = get_observability()
        # root=True：任务 span 必须是根节点，不能继承"最近一个未结束的 span"。
        # 并发时那个启发式会选到**别的任务**的 span，把两个任务折叠进同一条
        # trace（实测 B 的 task.run 成了 A 的子节点）。
        task_span = obs.start_span("task.run", root=True, task_id=tid, goal=goal[:200])
        state.task_span = task_span
        self._task_span = task_span

        result = TaskRunResult(task_id=tid, status=TaskStatus.RUNNING, goal=goal)

        try:
            # 阶段 1：需求澄清
            handoff = await self._run_requirement(tid, goal, result)
            if handoff is None:
                return self._finalize(result, start, TaskStatus.FAILED, "需求澄清失败")

            # 阶段 2：架构设计 + DAG 构建
            dag = await self._run_architect(tid, handoff, result)
            if dag is None:
                return self._finalize(result, start, TaskStatus.FAILED, "架构设计失败")
            result.dag = dag

            # 阶段 3：DAG 驱动的执行循环
            await self._execute_dag(tid, dag, handoff, result)

            if result.dag is not None and result.dag.has_failure():
                return self._finalize(result, start, TaskStatus.FAILED, "存在失败节点")

            return self._finalize(result, start, TaskStatus.SUCCEEDED, None)

        except BudgetExceededError as exc:
            logger.warning("task_budget_exceeded", task_id=tid, error=str(exc))
            return self._finalize(result, start, TaskStatus.PAUSED, str(exc))
        except OrchestrationError as exc:
            return self._finalize(result, start, TaskStatus.FAILED, str(exc))
        except Exception as exc:
            logger.exception("task_unexpected_error", task_id=tid)
            return self._finalize(result, start, TaskStatus.FAILED, f"未预期错误：{exc}")
        finally:
            # 无论走哪条返回分支，都必须把 context 恢复原状，
            # 否则同一个 Task 里后续调用会读到已结束运行的状态。
            self._run_state.reset(token)

    def forget_task(self, task_id: str) -> None:
        """丢弃某任务的检查点。

        供「同一 task_id 需要重新完整执行」的场景使用（例如评测用
        ``eval-{sample.id}`` 这种确定性 id 重复跑同一个样本）。不清的话，
        第二次运行会命中上一次的成功检查点，**一步都不执行**却报成功。
        """
        self._checkpoints.clear(task_id)

    # ------------------------------------------------------------------ #
    # 阶段 1：需求澄清
    # ------------------------------------------------------------------ #

    async def _run_requirement(
        self, task_id: str, goal: str, result: TaskRunResult
    ) -> AgentHandoff | None:
        state = self._state()
        step_id = f"{task_id}:requirement"
        state.isolator.space_for(AgentType.REQUIREMENT).add(
            make_chunk(
                f"# 用户需求\n{goal}",
                ContextKind.TASK_SPEC,
                is_hard=True,
                source="user://input",
            )
        )

        bundle = await self._context.build(
            agent=AgentType.REQUIREMENT,
            task_embedding=None,
            current_step=step_id,
            isolator=state.isolator,
            task_id=task_id,
            task_text=goal,
        )
        invocation = AgentInvocation(
            task_id=task_id, step_id=step_id, bundle=bundle, attempt=1, guard=self._guard
        )
        agent = self._agents[AgentType.REQUIREMENT]

        try:
            message = await agent.run(invocation)
        except Exception as exc:
            logger.error("requirement_failed", task_id=task_id, error=str(exc))
            self._charge_failed_call(state, result, exc)
            return None

        # ★ 记账修复：需求阶段此前 charge(tokens=0) 且不计步数，导致两个
        # 必经阶段的大模型调用完全游离于熔断器之外。现改为按实际用量记账，
        # 并把该阶段的 token 计入任务总账（否则 total_tokens 会漏掉这两步）。
        used_tokens = int(message.payload.get("tokens_used") or 0)
        used_cost = float(message.payload.get("cost_usd") or 0.0)
        result.total_tokens += used_tokens
        result.total_cost_usd += used_cost
        state.breaker.charge(tokens=used_tokens, steps=1)

        if message.handoff is None:
            return AgentHandoff(task_id=task_id, goal=goal)

        # 把需求规格同步到架构师空间
        state.isolator.handoff_to(AgentType.ARCHITECT, message.handoff)
        self._record_step(
            result,
            StepResult(
                step_id=step_id,
                agent=AgentType.REQUIREMENT,
                output=str(message.payload.get("content", "")),
                handoff=message.handoff,
                # 步级 token/成本必须与任务总账一致：此前 steps[] 里这两步恒为 0，
                # 前端展示「每步花了多少」时与 total_tokens 自相矛盾。
                tokens_used=used_tokens,
                cost_usd=used_cost,
            ),
        )
        return message.handoff

    # ------------------------------------------------------------------ #
    # 阶段 2：架构设计
    # ------------------------------------------------------------------ #

    async def _run_architect(
        self, task_id: str, handoff: AgentHandoff, result: TaskRunResult
    ) -> DAG | None:
        state = self._state()
        step_id = f"{task_id}:architect"
        bundle = await self._context.build(
            agent=AgentType.ARCHITECT,
            task_embedding=None,
            current_step=step_id,
            isolator=state.isolator,
            task_id=task_id,
            task_text=handoff.goal,
        )
        invocation = AgentInvocation(
            task_id=task_id,
            step_id=step_id,
            bundle=bundle,
            handoff=handoff,
            attempt=1,
            guard=self._guard,
        )
        agent = self._agents[AgentType.ARCHITECT]

        try:
            message = await agent.run(invocation)
        except Exception as exc:
            # 早先写的是 ``except (AgentContextError, Exception)``：元组里前一项
            # 被后一项完全覆盖，属于误导性死写法，只会让人以为这里区分了两类错误。
            logger.error("architect_failed", task_id=task_id, error=str(exc))
            self._charge_failed_call(state, result, exc)
            return None

        # 与需求阶段对称：按实际用量记账并计入任务总账。
        # 架构阶段通常是最贵的一次调用（需要跨文件推理），漏记会更危险。
        used_tokens = int(message.payload.get("tokens_used") or 0)
        used_cost = float(message.payload.get("cost_usd") or 0.0)
        result.total_tokens += used_tokens
        result.total_cost_usd += used_cost
        state.breaker.charge(tokens=used_tokens, steps=1)

        # ★ 取架构产出的节点列表。
        #
        # 这里曾经只读 `payload["nodes"]`，而 `BaseAgent._to_message` 构造的
        # payload 只有 content/model/tokens_used/cost_usd/raw 五个键 ——
        # Architect 是把 nodes 放在 `output.raw` 里的。于是 `payload["nodes"]`
        # **永远是 None**，每一次任务都静默退回单节点方案：
        # DAG 并行调度、依赖拓扑、失败传播、子图回退、节点级验证全部不生效，
        # 而日志与结果看上去完全正常（状态 success、节点 N1）。
        # 这是最隐蔽的一类缺陷：实现与测试都在，只有"接线"是断的。
        raw = message.payload.get("raw")
        raw_nodes = (raw or {}).get("nodes") if isinstance(raw, dict) else None
        if not raw_nodes:
            # 兼容自定义 Agent：仍接受直接放在 payload 顶层的 nodes
            raw_nodes = message.payload.get("nodes")
        if not raw_nodes:
            logger.warning("architect_produced_no_nodes", task_id=task_id, fallback="single_node")
            dag = DAG.build([_single_node(handoff)])
        else:
            try:
                dag = build_dag_from_architect_output(list(raw_nodes))
            except DAGError as exc:
                logger.error("dag_build_failed", task_id=task_id, error=str(exc))
                return None
        logger.info("dag_built", task_id=task_id, nodes=len(dag.nodes))

        self._record_step(
            result,
            StepResult(
                step_id=step_id,
                agent=AgentType.ARCHITECT,
                output=str(message.payload.get("content", "")),
                handoff=message.handoff or handoff,
                tokens_used=used_tokens,
                cost_usd=used_cost,
            ),
        )
        return dag

    # ------------------------------------------------------------------ #
    # 阶段 3：DAG 执行
    # ------------------------------------------------------------------ #

    async def _execute_dag(
        self,
        task_id: str,
        dag: DAG,
        handoff: AgentHandoff,
        result: TaskRunResult,
    ) -> None:
        """推进 DAG 直到完成或熔断。

        每一轮：取就绪节点 → 并行执行（受并发上限约束）→ 更新状态。
        """
        while not dag.is_complete():
            self._state().breaker.charge(tokens=0, steps=0)  # 触发上限检查

            ready = dag.ready_nodes()
            if not ready:
                if dag.blocked_nodes():
                    # 存在阻塞但无就绪 → 死锁（通常因失败传播未收敛）
                    raise OrchestrationError(
                        f"调度死锁：{len(dag.blocked_nodes())} 个节点被阻塞但无可执行节点"
                    )
                break

            batch = ready[: self._config.max_parallel]
            outcomes = await asyncio.gather(
                *(self._execute_node(task_id, dag, nid, handoff, result) for nid in batch),
                return_exceptions=True,
            )
            for nid, outcome in zip(batch, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    # 熔断异常必须向上传播以暂停整个任务，
                    # 否则会被当作普通节点失败吞掉（任务无法正确进入 PAUSED）。
                    if isinstance(outcome, BudgetExceededError):
                        raise outcome
                    logger.error(
                        "node_execution_error", task_id=task_id, node=nid, error=str(outcome)
                    )
                    dag.mark(nid, StepStatus.FAILED, last_error=str(outcome))

        # 未就绪且未终态的节点统一标记为跳过
        for nid in list(dag.nodes):
            if dag.states[nid].status is StepStatus.PENDING:
                dag.mark(nid, StepStatus.SKIPPED)

    async def _execute_node(
        self,
        task_id: str,
        dag: DAG,
        node_id: str,
        upstream_handoff: AgentHandoff,
        result: TaskRunResult,
    ) -> None:
        """执行单个 DAG 节点（含重试与回退）。"""
        node = dag.nodes[node_id]
        obs = get_observability()
        run_state = self._state()
        node_start = time.perf_counter()
        # attempt 在进入 _execute_node_inner 后才自增，因此这里预告「本次是第几次」。
        self._emit(
            "node_started",
            node_id=node_id,
            goal=node.goal[:200],
            agent_type=node.agent_type.value,
            deps=list(node.deps),
            attempt=dag.states[node_id].attempt + 1,
            total_nodes=len(dag.nodes),
        )
        # 节点 span 必须在**工作开始前**创建。早先的写法是在 finally 里
        # ``with obs.span(...)`` 包一个空块，于是 Span.duration_ms（由
        # start_ns/end_ns 计算）只有微秒级，而真实耗时被塞进 attribute ——
        # 任何读 trace 的人都会以为节点瞬间完成。
        node_span = obs.start_span(
            "node.execute",
            parent=run_state.task_span,
            node_id=node_id,
            node_type=node.agent_type.value,
        )
        try:
            await self._execute_node_inner(task_id, dag, node_id, upstream_handoff, result)
        finally:
            # 无论成功/失败/回退，都记录一次节点级观测。
            # 放在 finally 而不是各分支里，是因为 _execute_node_inner 有 6 个
            # return 点，逐个埋点必然漏 —— 「一个 finally」胜过「六处复制」。
            duration = (time.perf_counter() - node_start) * 1000
            node_state = dag.states[node_id]
            status = node_state.status.value
            agent_name = node.agent_type.value
            obs.inc(MetricNames.NODE_EXECUTIONS, 1, node_type=agent_name, status=status)
            obs.observe(MetricNames.NODE_DURATION_MS, duration, node_type=agent_name)
            node_span.set_attribute("attempt", node_state.attempt)
            node_span.set_attribute("status", status)
            node_span.set_attribute("duration_ms", round(duration, 3))
            node_span.set_attribute("tokens_used", node_state.tokens_used)
            obs.end_span(node_span, error=node_state.last_error or "")
            self._emit(
                "node_finished",
                node_id=node_id,
                agent_type=agent_name,
                # 被驳回后重置为 PENDING 表示「将回退重跑」。对前端而言这不是
                # 「完成」，而是「判定未通过 → 进入下一轮」，因此显式标注为
                # backtracked，避免 UI 上显示成一次莫名其妙的 pending 完成。
                status="backtracked" if node_state.status is StepStatus.PENDING else status,
                attempt=node_state.attempt,
                duration_ms=round(duration, 1),
                tokens_used=node_state.tokens_used,
                last_error=node_state.last_error or "",
            )

    async def _execute_node_inner(
        self,
        task_id: str,
        dag: DAG,
        node_id: str,
        upstream_handoff: AgentHandoff,
        result: TaskRunResult,
    ) -> None:
        node = dag.nodes[node_id]
        run_state = self._state()
        agent = self._agents.get(node.agent_type)
        if agent is None:
            dag.mark(node_id, StepStatus.FAILED, last_error=f"无对应 Agent：{node.agent_type}")
            return

        # 循环检测（第 1 维）：同一节点的回退链过深视为死循环
        if run_state.loop_detector.detect(node_id, dag.states[node_id].attempt):
            dag.mark(node_id, StepStatus.FAILED, last_error="检测到循环/重复失败")
            get_observability().inc(MetricNames.LOOP_DETECTED, 1, node_type=node.agent_type.value)
            logger.warning("loop_detected", task_id=task_id, node=node_id)
            return

        step_id = f"{task_id}:{node_id}"
        dag.mark(node_id, StepStatus.RUNNING)
        dag.mark_attempt(node_id)
        attempt = dag.states[node_id].attempt

        # 构造该节点的握手信息（融合上游规格与本节点目标）
        handoff = AgentHandoff(
            task_id=task_id,
            goal=node.goal or upstream_handoff.goal,
            acceptance_criteria=list(
                node.acceptance_criteria or tuple(upstream_handoff.acceptance_criteria)
            ),
            constraints=list(upstream_handoff.constraints),
            relevant_files=list(upstream_handoff.relevant_files),
            context_refs=[
                *upstream_handoff.context_refs,
                ContextRef(uri=f"node://{node_id}", note=node.goal),
            ],
            budget_tokens=upstream_handoff.budget_tokens,
        )

        # 注入该 Agent 的独立上下文空间（handoff_to 会先移除上一轮投喂的
        # handoff 片段，因此重试不会让硬约束在空间里重复累积）
        node_isolator = run_state.isolator.snapshot()
        node_space = node_isolator.handoff_to(node.agent_type, handoff)
        for dependency in node.deps:
            dependency_output = run_state.outputs.get(dependency)
            if dependency_output is None:
                raise OrchestrationError(f"依赖节点 {dependency} 的产物不可用")
            node_space.add(
                make_chunk(
                    _artifact_text(dependency_output),
                    ContextKind.CODE,
                    source=f"artifact://{dependency}",
                )
            )

        # 恢复断点（仅"上次运行遗留的、**已通过验证**的步骤"才跳过）。
        # 注意：检查点现在只在 Verifier 判定通过后才写入（见下方 PASS 分支），
        # 因此 completed=True 就意味着「这一步确实做过且被验证过」。
        checkpoint = (
            self._checkpoints.load(task_id, step_id) if self._config.enable_checkpoints else None
        )
        if (
            checkpoint is not None
            and checkpoint.completed
            and isinstance(saved_output := checkpoint.payload.get("output"), dict)
        ):
            run_state.outputs[node_id] = AgentOutput(
                content=str(saved_output.get("content", "")),
                raw=dict(saved_output.get("raw") or {}),
            )
            dag.mark(node_id, StepStatus.SUCCESS)
            # 跨运行恢复是**设计意图**（检查点只在通过验证后写入），
            # 但"是不是同一次运行"必须可观测：同一次运行内被检查点短路
            # 意味着回退机制失效，那是 bug；跨运行短路才是断点续跑。
            logger.info(
                "node_resumed_from_checkpoint",
                task_id=task_id,
                node=node_id,
                same_run=checkpoint.matches_run(run_state.run_id),
            )
            return

        # 历史教训注入（Reflexion）
        lessons: tuple[ReflexionLesson, ...] = tuple(
            run_state.reflexion.lessons_for(node.agent_type)
        )

        signals = RoutingSignals(
            reasoning_depth=_reasoning_depth_for(node.agent_type),
            context_size_norm=0.0,
            tool_call_count_norm=0.0,
            retry_history_norm=min(1.0, (attempt - 1) / max(self._config.max_retries, 1)),
        )

        bundle = await self._context.build(
            agent=node.agent_type,
            task_embedding=None,
            current_step=step_id,
            routing_signals=signals,
            budget_total=handoff.budget_tokens,
            isolator=node_isolator,
            task_id=task_id,
            task_text=handoff.goal,
        )
        invocation = AgentInvocation(
            task_id=task_id,
            step_id=step_id,
            bundle=bundle,
            handoff=handoff,
            lessons=lessons,
            attempt=attempt,
            routing_signals=signals,
            guard=self._guard,
        )

        # 执行 Agent
        try:
            output, outcome = await self._run_agent_node(agent, invocation)
        except Exception as exc:
            dag.mark(node_id, StepStatus.FAILED, last_error=str(exc))
            # 失败也要计一步，避免「失败不消耗预算」导致无限重试
            run_state.breaker.charge(tokens=0, steps=1)
            # 若这次失败发生在模型调用**之后**（例如输出格式解析失败），
            # 补记那次真实用量（见 base.py 把 usage 挂在异常上）。
            self._charge_failed_call(run_state, result, exc)
            return

        # 记账：先把产出计入结果，再触发熔断检查。
        # 顺序很重要——若先 charge（可能抛 BudgetExceededError），
        # 则本次已消耗的 token 会丢失，导致暂停时的账目不完整。
        result.total_tokens += output.tokens_used
        result.total_cost_usd += output.cost_usd

        step_result = agent.to_step_result(output, invocation)
        # 把测试执行结果并入证据
        if outcome is not None:
            step_result = step_result.model_copy(
                update={
                    "output": f"{step_result.output}\n\n## 测试执行\n{outcome.summary()}",
                }
            )
        self._record_step(result, step_result)

        run_state.breaker.charge(tokens=output.tokens_used, steps=1)

        # 验证（Tester 与 Verifier 参与时）
        verdict, feedback = await self._verify_node(task_id, node_id, output, handoff, result)

        # 验证结论是本项目最值得展示的时刻（尤其是驳回 + 回退），
        # 因此单独发一个事件，而不是让前端从 node_finished 反推。
        self._emit(
            "node_verdict",
            node_id=node_id,
            agent_type=node.agent_type.value,
            verdict=verdict.value,
            attempt=attempt,
            failed_criteria=list(feedback.failed_criteria) if feedback else [],
            suggestions=list(feedback.suggestions) if feedback else [],
            lesson=(feedback.lesson if feedback else "") or "",
        )

        if verdict is Verdict.PASS:
            # ★ 检查点只在**通过验证之后**才写入。
            # 早先的实现在验证**之前**就写 completed=True，于是：
            # ① 被驳回的尝试也会留下"已完成"记录；② 重试耗尽后标记 FAILED 时
            # 那条记录仍然存在 —— 同一个 task_id 再跑一次就会被检查点短路，
            # 一步不执行却报成功。写入点后移，从根上消除这一类脏记录。
            run_state.outputs[node_id] = output
            self._save_checkpoint(task_id, step_id, node_id, attempt, output)
            dag.mark(node_id, StepStatus.SUCCESS, tokens_used=output.tokens_used)
            logger.info("node_succeeded", task_id=task_id, node=node_id, attempt=attempt)
            return

        # 未通过：记录教训并决定是否回退
        if feedback is not None and feedback.lesson:
            run_state.reflexion.add(
                ReflexionLesson(
                    root_cause=feedback.suggestions[0]
                    if feedback.suggestions
                    else "未满足验收标准",
                    lesson=feedback.lesson,
                    source_step=step_id,
                ),
                agent_type=node.agent_type,
            )

        obs = get_observability()
        node_type = node.agent_type.value

        # 循环检测（第 2、3 维）：重复失败模式 + 回退路径。
        # 这三条此前只有第 1 维（深度）被调用，另两个方法从未接线 ——
        # 于是文档里写的「相同失败连续出现 N 次升级策略」实际不存在。
        signature = "|".join(sorted(feedback.failed_criteria)) if feedback else "unknown"
        repeated_failure = run_state.loop_detector.record_failure(node_id, signature)
        cyclic_path = run_state.loop_detector.record_path(node_id)
        if cyclic_path:
            obs.inc(MetricNames.LOOP_DETECTED, 1, node_type=node_type)

        if attempt >= self._config.max_retries or repeated_failure or cyclic_path:
            if repeated_failure:
                reason = f"相同失败连续出现 {self._config.same_failure_threshold} 次（无收敛迹象）"
            elif cyclic_path:
                reason = "回退路径成环（在最近若干次回退中反复出现同一节点）"
            else:
                detail = feedback.failed_criteria if feedback else "未知"
                reason = f"重试 {attempt} 次仍未通过：{detail}"
            if self._config.enable_checkpoints:
                # 终止路径也必须清掉本步骤的检查点：节点最终是失败的，
                # 任何"已完成"记录都会在下次同 id 运行时被误信。
                self._checkpoints.clear_step(task_id, step_id)
            dag.mark(
                node_id,
                StepStatus.FAILED,
                last_error=reason,
                tokens_used=output.tokens_used,
            )
            logger.warning(
                "node_failed_after_retries",
                task_id=task_id,
                node=node_id,
                reason=reason,
            )
            return

        # 回退：连同下游子图一起重置为 PENDING，并清掉它们的检查点。
        # 用 reset_subgraph 而不是只重置自己：一旦某个下游已经成功过，
        # 上游的重跑会让它的产出建立在过期的输入上，必须一并失效。
        affected = dag.reset_subgraph(node_id)
        for affected_id in affected:
            run_state.outputs.pop(affected_id, None)
            if self._config.enable_checkpoints:
                self._checkpoints.clear_step(task_id, f"{task_id}:{affected_id}")
        dag.mark(node_id, StepStatus.PENDING, tokens_used=output.tokens_used)
        obs.inc(MetricNames.BACKTRACKS, 1, node_type=node_type)
        obs.inc(MetricNames.RETRIES, 1, node_type=node_type, reason="verifier_reject")
        lesson_count = len(run_state.reflexion.lessons_for(node.agent_type))
        if lesson_count:
            obs.gauge(
                MetricNames.REFLEXION_LESSONS,
                float(lesson_count),
                agent=node_type,
            )
        logger.info("node_rejected_retrying", task_id=task_id, node=node_id, attempt=attempt)

    def _charge_failed_call(
        self, state: _RunState, result: TaskRunResult, exc: BaseException, *, steps: int = 0
    ) -> None:
        """补记「模型调用发生了、但后续步骤失败」的用量。

        ``BaseAgent.run`` 在 ``parse_output`` 抛错时会把 usage/cost_usd 挂在
        异常对象上（见 agents/base.py）。不补记的话，**越是高频的失败路径，
        预算越不准** —— 模型输出格式不对是很常见的失败，而它每次都真实计费。
        """
        usage = getattr(exc, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0)
        cost = float(getattr(exc, "cost_usd", 0.0) or 0.0)
        result.total_tokens += tokens
        result.total_cost_usd += cost
        state.breaker.charge(tokens=tokens, steps=steps)
        logger.warning("failed_call_charged", tokens=tokens, cost_usd=cost)

    def _save_checkpoint(
        self, task_id: str, step_id: str, node_id: str, attempt: int, output: AgentOutput
    ) -> None:
        """记录「该节点本轮**已通过验证**」。"""
        if not self._config.enable_checkpoints:
            return
        self._checkpoints.save(
            StepCheckpoint(
                task_id=task_id,
                step_id=step_id,
                completed=True,
                node_id=node_id,
                # 取本次运行自己的 run_id，而不是实例上的镜像字段 ——
                # 并发下镜像指向的是**最后一个**启动的运行，
                # 会让 A 任务的检查点带上 B 的 run_id。
                run_id=self._state().run_id,
                payload={
                    "tokens": output.tokens_used,
                    "attempt": attempt,
                    "output": {"content": output.content, "raw": output.raw},
                },
            )
        )

    # ------------------------------------------------------------------ #
    # 验证
    # ------------------------------------------------------------------ #

    async def _verify_node(
        self,
        task_id: str,
        node_id: str,
        coder_output: AgentOutput,
        handoff: AgentHandoff,
        result: TaskRunResult,
    ) -> tuple[Verdict, FeedbackPayload | None]:
        """对节点产出执行验证。

        ★ 关键设计：构造 Verifier 上下文时**刻意排除 Coder 的自我解释**，
        只提供验收标准 + 实际产出 + 客观证据（测试结果）。
        """
        run_state = self._state()
        # Tester 参与：先跑测试产出客观证据
        evidence = _TestEvidence()
        if self._config.enable_tester:
            evidence = await self._collect_test_evidence(
                task_id, node_id, handoff, result, artifact=coder_output
            )
            # ★ 记账：Tester 的模型调用此前完全游离于账本之外 ——
            # 每个成功节点实际有 3 次模型调用（coder + tester + verifier），
            # 而熔断器和 total_tokens 只看到 1 次，实际可用预算约为配置值的 3 倍。
            result.total_tokens += evidence.tokens
            result.total_cost_usd += evidence.cost
            run_state.breaker.charge(tokens=evidence.tokens, steps=0)

        step_id = f"{task_id}:{node_id}:verify"
        verifier_isolator = run_state.isolator.snapshot()
        space = verifier_isolator.space_for(AgentType.VERIFIER)
        # ★ 每次验证都以**空空间**开始。
        # 早先只 add 不清理，于是同一次运行内：重试时上一轮被驳回的产物与
        # 本轮产物同时在场且 source 完全相同（artifact://N1），Verifier 无法
        # 分辨该审哪一个；多节点任务里 N1 的标准与产物也会污染 N2 的验证。
        # 「验证只看这一次的证据」本就是设计意图，这里用 clear 强制它成立。
        space.clear()

        # 只注入：验收标准 + 实际产出 + 客观证据
        criteria_text = "\n".join(f"- [ ] {c}" for c in handoff.acceptance_criteria)
        space.add(
            make_chunk(
                f"# 待验证的验收标准\n{criteria_text}",
                ContextKind.TASK_SPEC,
                is_hard=True,
                source=f"verify://{node_id}",
            )
        )
        space.add(
            make_chunk(
                f"# 实际产出（不含作者解释）\n{_artifact_text(coder_output)}",
                ContextKind.CODE,
                source=f"artifact://{node_id}",
            )
        )
        if evidence.text:
            space.add(
                make_chunk(
                    f"# 客观测试证据\n{evidence.text}",
                    ContextKind.TOOL_RESULT,
                    is_hard=True,
                    source="sandbox://tests",
                )
            )

        verifier_handoff = AgentHandoff(
            task_id=task_id,
            goal=handoff.goal,
            acceptance_criteria=list(handoff.acceptance_criteria),
            constraints=list(handoff.constraints),
        )
        bundle = await self._context.build(
            agent=AgentType.VERIFIER,
            task_embedding=None,
            current_step=step_id,
            budget_total=handoff.budget_tokens,
            isolator=verifier_isolator,
            task_id=task_id,
            task_text=handoff.goal,
        )
        invocation = AgentInvocation(
            task_id=task_id,
            step_id=step_id,
            bundle=bundle,
            handoff=verifier_handoff,
            attempt=1,
            guard=self._guard,
        )

        try:
            message = await self._agents[AgentType.VERIFIER].run(invocation)
        except BudgetExceededError:
            raise
        except Exception as exc:
            # ★ fail-closed：验证器不可用时**绝不算通过**。
            # 早先这里返回 Verdict.PASS 并注释为"保守判为通过"，方向正好相反：
            # 一次瞬时 503 就能让未经验证的产物被标记为"已验证成功"，
            # 任务状态还是 succeeded、error 为空 —— 外部完全观测不到。
            # 对一个以"独立验证"为核心卖点的系统，这是最不能接受的失效方式。
            obs = get_observability()
            obs.inc(MetricNames.VERIFICATION_UNAVAILABLE, 1, node_type="verifier")
            logger.warning("verification_error", task_id=task_id, node=node_id, error=str(exc))
            self._emit(
                "verification_unavailable",
                node_id=node_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            unavailable = FeedbackPayload(
                verdict=Verdict.REJECT,
                failed_criteria=list(handoff.acceptance_criteria),
                evidence={"verification_error": f"{type(exc).__name__}: {exc}"},
                suggestions=["验证器不可用：需恢复验证能力后重试，不得视为通过"],
                lesson=(
                    f"验证器调用失败（{type(exc).__name__}）：本轮产出**未经验证**，"
                    "绝不能当作已满足验收标准。"
                ),
            )
            usage = getattr(exc, "usage", None)
            self._record_step(
                result,
                StepResult(
                    step_id=step_id,
                    agent=AgentType.VERIFIER,
                    output=f"验证失败：{exc}",
                    feedback=unavailable,
                    tokens_used=int(getattr(usage, "total_tokens", 0) or 0) + evidence.tokens,
                    cost_usd=float(getattr(exc, "cost_usd", 0.0) or 0.0) + evidence.cost,
                ),
            )
            self._charge_failed_call(run_state, result, exc, steps=1)
            return Verdict.REJECT, unavailable

        # 验证调用同样要计账（此前漏记，见上面的说明）。
        used_tokens = int(message.payload.get("tokens_used") or 0)
        used_cost = float(message.payload.get("cost_usd") or 0.0)
        result.total_tokens += used_tokens
        result.total_cost_usd += used_cost
        run_state.breaker.charge(tokens=used_tokens, steps=1)

        feedback = message.feedback
        verdict = feedback.verdict if feedback is not None else Verdict.REJECT
        obs = get_observability()
        node_type = self._agents[AgentType.VERIFIER].agent_type.value
        obs.inc(MetricNames.VERDICT_TOTAL, 1, node_type=node_type, verdict=verdict.value)
        if verdict is Verdict.REJECT:
            # 「被驳回的验收条目数」是幻觉拦截的代理指标：条数越多说明
            # Coder 越倾向于声称完成但实际未达标。
            blocked = len(feedback.failed_criteria) if feedback is not None else 0
            obs.inc(MetricNames.HALLUCINATION_BLOCKED, max(1, blocked), node_type=node_type)
        self._record_step(
            result,
            StepResult(
                step_id=step_id,
                agent=AgentType.VERIFIER,
                output=str(message.payload.get("content", "")),
                feedback=feedback,
                # ★ 把 Tester 的用量并入本步骤。
                # 验证阶段实际有两次模型调用（Tester 生成测试 + Verifier 判定），
                # 若只记 Verifier 的用量，steps[] 之和会比 total_tokens 少一截，
                # 前端「每步花了多少」与总账自相矛盾（实测 800 vs 1000）。
                tokens_used=used_tokens + evidence.tokens,
                cost_usd=used_cost + evidence.cost,
            ),
        )
        return verdict, feedback

    async def _collect_test_evidence(
        self,
        task_id: str,
        node_id: str,
        handoff: AgentHandoff,
        result: TaskRunResult,
        *,
        artifact: AgentOutput | None = None,
    ) -> _TestEvidence:
        """运行 Tester 生成并执行测试，返回（证据文本, token, 成本）。

        返回值从 ``str`` 改成小结构体：调用方需要把 Tester 的用量计账，
        而"只返回字符串"的签名让这件事在类型层面就不可能做到。
        """
        tester = self._agents.get(AgentType.TESTER)
        if not isinstance(tester, TesterAgent):
            return _TestEvidence()
        run_state = self._state()
        step_id = f"{task_id}:{node_id}:test"
        tester_isolator = run_state.isolator.snapshot()
        space = tester_isolator.handoff_to(AgentType.TESTER, handoff)
        if artifact is not None:
            space.add(
                make_chunk(
                    _artifact_text(artifact), ContextKind.CODE, source=f"artifact://{node_id}"
                )
            )
        bundle = await self._context.build(
            agent=AgentType.TESTER,
            task_embedding=None,
            current_step=step_id,
            budget_total=handoff.budget_tokens,
            isolator=tester_isolator,
            task_id=task_id,
            task_text=handoff.goal,
        )
        invocation = AgentInvocation(
            task_id=task_id,
            step_id=step_id,
            bundle=bundle,
            handoff=handoff,
            attempt=1,
            guard=self._guard,
        )
        try:
            output, outcome = await tester.generate_and_run(invocation)
        except Exception as exc:
            logger.warning("tester_error", task_id=task_id, node=node_id, error=str(exc))
            self._charge_failed_call(run_state, result, exc)
            return _TestEvidence()
        text = self._describe_evidence(output, outcome)
        return _TestEvidence(text=text, tokens=output.tokens_used, cost=output.cost_usd)

    @staticmethod
    def _describe_evidence(output: AgentOutput, outcome: TestRunOutcome) -> str:
        """把测试结果转成给 Verifier 看的证据文本。

        ★ 核心要求：**「没能执行」与「执行失败」必须区分**。
        早先两者都被压成一条占位符或一段原始输出，Verifier 无从判断
        「测试跑了但挂了」与「环境根本跑不了测试」—— 后者不构成功能缺陷的
        证据。把这两者混为一谈，会让 Verifier 在环境问题上错误驳回，
        或者在真的失败时以为只是环境噪音。
        """
        if not outcome.executed:
            reason = outcome.stderr or "没有可执行的测试文件"
            return (
                f"（测试未执行：{reason}）\n"
                "说明：本轮没有获得可执行的测试证据，这**不构成**功能缺陷的证据，"
                "也不代表功能已通过验证。"
            )
        if outcome.raw.get("collection_error"):
            return (
                "（测试无法执行：pytest 收集阶段失败）\n"
                f"{output.content}\n"
                "说明：收集失败通常源于被测代码缺失或无法导入，"
                "**不构成**功能缺陷的证据。"
            )
        return (
            f"{output.content}\n\n"
            "（说明：测试运行在沙箱工作区内。当前版本**尚未实现补丁应用**，"
            "因此该结果反映的是基线行为，不能单独作为「改动已满足验收标准」的证明。）"
        )

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    async def _run_agent_node(
        self, agent: BaseAgent, invocation: AgentInvocation
    ) -> tuple[AgentOutput, TestRunOutcome | None]:
        """调用 Agent；Tester 走专用路径（生成 + 执行）。"""
        if isinstance(agent, TesterAgent):
            return await agent.generate_and_run(invocation)
        message: AgentMessage = await agent.run(invocation)
        payload = message.payload
        output = AgentOutput(
            content=str(payload.get("content", "")),
            handoff=message.handoff,
            feedback=message.feedback,
            model=str(payload.get("model", "")),
            tokens_used=int(payload.get("tokens_used", 0) or 0),
            cost_usd=float(payload.get("cost_usd", 0.0) or 0.0),
            raw=dict(payload.get("raw") or {}),
        )
        return output, None

    async def aclose(self) -> None:
        """释放底层模型连接等资源。

        服务器/CLI 在任务结束后必须调用，否则 httpx 连接池会一直挂着，
        长跑进程会慢慢泄漏文件描述符。
        """
        close = getattr(self._gateway, "aclose", None)
        if close is not None:
            await close()

    def _record_step(self, result: TaskRunResult, step: StepResult) -> None:
        result.steps.append(step)

    def _finalize(
        self,
        result: TaskRunResult,
        start: float,
        status: TaskStatus,
        error: str | None,
    ) -> TaskRunResult:
        result.status = status
        result.error = error
        result.duration_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "task_finished",
            task_id=result.task_id,
            status=status.value,
            steps=len(result.steps),
            tokens=result.total_tokens,
            duration_ms=result.duration_ms,
        )
        obs = get_observability()
        obs.inc(MetricNames.TASK_TOTAL, 1, status=status.value)
        obs.observe(MetricNames.TASK_DURATION_MS, float(result.duration_ms))
        # 用本次运行自己的 span（并发下 self._task_span 可能已被别的任务改写）
        own_state = self._run_state.get()
        task_span = own_state.task_span if own_state is not None else None
        if task_span is not None:
            task_span.set_attribute("status", status.value)
            task_span.set_attribute("steps", len(result.steps))
            task_span.set_attribute("tokens", result.total_tokens)
            obs.end_span(task_span, error=error or "")
        return result


def _artifact_text(output: AgentOutput) -> str:
    """从结构化产物提取文件和代码，排除作者的效果声明。"""
    changes = output.raw.get("changes")
    if isinstance(changes, list):
        parts = [
            f"## {change.get('file', '')}\n```diff\n{change.get('diff', '')}\n```"
            for change in changes
            if isinstance(change, dict) and change.get("diff")
        ]
        return "\n\n".join(parts) or "（没有代码改动）"
    test_files = output.raw.get("test_files")
    if isinstance(test_files, list):
        return "\n\n".join(
            f"## {item.get('path', '')}\n```python\n{item.get('content', '')}\n```"
            for item in test_files
            if isinstance(item, dict)
        )
    return output.content


def _reasoning_depth_for(agent_type: AgentType) -> float:
    """不同 Agent 的推理深度先验（用于路由）。"""
    return {
        AgentType.REQUIREMENT: 0.6,
        AgentType.ARCHITECT: 0.9,
        AgentType.CODER: 0.7,
        AgentType.TESTER: 0.5,
        AgentType.VERIFIER: 0.8,
        AgentType.REVIEWER: 0.7,
    }.get(agent_type, 0.5)


def _single_node(handoff: AgentHandoff) -> Any:
    """架构产出缺失时的降级单节点方案。"""
    from devagent.models.domain import TaskNode

    return TaskNode(
        id="N1",
        goal=handoff.goal,
        agent_type=AgentType.CODER,
        deps=(),
        acceptance_criteria=tuple(handoff.acceptance_criteria),
    )


__all__ = [
    "OrchestrationError",
    "Orchestrator",
    "OrchestratorConfig",
    "TaskRunResult",
]
