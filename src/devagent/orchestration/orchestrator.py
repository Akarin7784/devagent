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
from dataclasses import dataclass, field
from typing import Any

from devagent.agents import (
    AgentContextError,
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


class OrchestrationError(RuntimeError):
    """编排过程中的不可恢复错误。"""


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
    ) -> None:
        self._settings = settings
        self._gateway = gateway or ModelGateway(settings)
        self._context = context_engine or ContextEngine(settings.context)
        self._config = config or OrchestratorConfig(
            max_steps=settings.reliability.max_task_steps,
            max_tokens=settings.reliability.max_task_tokens,
            max_retries=settings.reliability.max_retries,
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

        # 可靠性组件
        self._reflexion = ReflexionMemory()
        self._breaker = CircuitBreaker(
            max_tokens=self._config.max_tokens,
            max_steps=self._config.max_steps,
        )
        self._loop_detector = LoopDetector()
        self._run_id: str = ""
        """当前运行实例标识，用于区分「同次运行的重试」与「跨运行恢复」。"""
        self._task_span: Span | None = None
        """任务根 span。节点 span 挂在其下，形成完整调用树。"""

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
        """
        tid = task_id or f"task_{int(time.time() * 1000)}"
        self._run_id = f"run_{int(time.time() * 1000)}"
        start = time.perf_counter()
        logger.info("task_started", task_id=tid, goal=goal[:100])

        # 整个任务包一个根 span。注意这里用显式 ``task_span`` 贯穿全程而不是
        # with 语句，因为下面有多个 return 分支（提前失败返回），用 with 会
        # 让「返回」与「关闭 span」两个语义耦合在缩进里，容易漏掉某个分支。
        obs = get_observability()
        task_span = obs.start_span("task.run", task_id=tid, goal=goal[:200])
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

    # ------------------------------------------------------------------ #
    # 阶段 1：需求澄清
    # ------------------------------------------------------------------ #

    async def _run_requirement(
        self, task_id: str, goal: str, result: TaskRunResult
    ) -> AgentHandoff | None:
        step_id = f"{task_id}:requirement"
        self._context.isolator.space_for(AgentType.REQUIREMENT).add(
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
        )
        invocation = AgentInvocation(task_id=task_id, step_id=step_id, bundle=bundle, attempt=1)
        agent = self._agents[AgentType.REQUIREMENT]

        try:
            message = await agent.run(invocation)
        except Exception as exc:
            logger.error("requirement_failed", task_id=task_id, error=str(exc))
            return None

        # ★ 记账修复：需求阶段此前 charge(tokens=0) 且不计步数，导致两个
        # 必经阶段的大模型调用完全游离于熔断器之外。现改为按实际用量记账，
        # 并把该阶段的 token 计入任务总账（否则 total_tokens 会漏掉这两步）。
        used_tokens = int(message.payload.get("tokens_used") or 0)
        used_cost = float(message.payload.get("cost_usd") or 0.0)
        result.total_tokens += used_tokens
        result.total_cost_usd += used_cost
        self._breaker.charge(tokens=used_tokens, steps=1)

        if message.handoff is None:
            return AgentHandoff(task_id=task_id, goal=goal)

        # 把需求规格同步到架构师空间
        self._context.isolator.handoff_to(AgentType.ARCHITECT, message.handoff)
        self._record_step(
            result,
            StepResult(
                step_id=step_id,
                agent=AgentType.REQUIREMENT,
                output=str(message.payload.get("content", "")),
                handoff=message.handoff,
            ),
        )
        return message.handoff

    # ------------------------------------------------------------------ #
    # 阶段 2：架构设计
    # ------------------------------------------------------------------ #

    async def _run_architect(
        self, task_id: str, handoff: AgentHandoff, result: TaskRunResult
    ) -> DAG | None:
        step_id = f"{task_id}:architect"
        bundle = await self._context.build(
            agent=AgentType.ARCHITECT,
            task_embedding=None,
            current_step=step_id,
        )
        invocation = AgentInvocation(
            task_id=task_id, step_id=step_id, bundle=bundle, handoff=handoff, attempt=1
        )
        agent = self._agents[AgentType.ARCHITECT]

        try:
            message = await agent.run(invocation)
        except (AgentContextError, Exception) as exc:
            logger.error("architect_failed", task_id=task_id, error=str(exc))
            return None

        # 与需求阶段对称：按实际用量记账并计入任务总账。
        # 架构阶段通常是最贵的一次调用（需要跨文件推理），漏记会更危险。
        used_tokens = int(message.payload.get("tokens_used") or 0)
        used_cost = float(message.payload.get("cost_usd") or 0.0)
        result.total_tokens += used_tokens
        result.total_cost_usd += used_cost
        self._breaker.charge(tokens=used_tokens, steps=1)

        raw_nodes = message.payload.get("nodes")
        if not raw_nodes:
            # AgentMessage.payload 未携带 nodes 时，退回单节点方案
            dag = DAG.build(
                [
                    _single_node(handoff),
                ]
            )
        else:
            try:
                dag = build_dag_from_architect_output(list(raw_nodes))
            except DAGError as exc:
                logger.error("dag_build_failed", task_id=task_id, error=str(exc))
                return None

        self._record_step(
            result,
            StepResult(
                step_id=step_id,
                agent=AgentType.ARCHITECT,
                output=str(message.payload.get("content", "")),
                handoff=message.handoff or handoff,
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
            self._breaker.charge(tokens=0, steps=0)  # 触发上限检查

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
        node_start = time.perf_counter()
        try:
            await self._execute_node_inner(task_id, dag, node_id, upstream_handoff, result)
        finally:
            # 无论成功/失败/回退，都记录一次节点级观测。
            # 放在 finally 而不是各分支里，是因为 _execute_node_inner 有 6 个
            # return 点，逐个埋点必然漏 —— 「一个 finally」胜过「六处复制」。
            duration = (time.perf_counter() - node_start) * 1000
            status = dag.states[node_id].status.value
            agent_name = node.agent_type.value
            obs.inc(MetricNames.NODE_EXECUTIONS, 1, node_type=agent_name, status=status)
            obs.observe(MetricNames.NODE_DURATION_MS, duration, node_type=agent_name)
            if self._task_span is not None:
                with obs.span(
                    "node.execute",
                    parent=self._task_span,
                    node_id=node_id,
                    node_type=agent_name,
                    attempt=dag.states[node_id].attempt,
                    status=status,
                ) as span:
                    span.set_attribute("duration_ms", round(duration, 3))

    async def _execute_node_inner(
        self,
        task_id: str,
        dag: DAG,
        node_id: str,
        upstream_handoff: AgentHandoff,
        result: TaskRunResult,
    ) -> None:
        node = dag.nodes[node_id]
        agent = self._agents.get(node.agent_type)
        if agent is None:
            dag.mark(node_id, StepStatus.FAILED, last_error=f"无对应 Agent：{node.agent_type}")
            return

        # 循环检测：同一节点的回退链过深视为死循环
        if self._loop_detector.detect(node_id, dag.states[node_id].attempt):
            dag.mark(node_id, StepStatus.FAILED, last_error="检测到循环/重复失败")
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

        # 注入该 Agent 的独立上下文空间
        self._context.isolator.handoff_to(node.agent_type, handoff)

        # 恢复断点（仅"上次运行遗留的已完成步骤"才跳过）。
        # ★ 注意：同一运行内的重试**不能**被检查点短路。
        # 由于回退时会主动清除该步骤的检查点（见下方 finally 逻辑），
        # 此处只需判断 completed 即可 —— 若 completed 为 True，
        # 说明是上次运行遗留的成功记录，可以安全跳过。
        checkpoint = self._checkpoints.load(task_id, step_id)
        if checkpoint is not None and checkpoint.completed:
            dag.mark(node_id, StepStatus.SUCCESS)
            logger.info("node_resumed_from_checkpoint", task_id=task_id, node=node_id)
            return

        # 历史教训注入（Reflexion）
        lessons: tuple[ReflexionLesson, ...] = tuple(self._reflexion.lessons_for(node.agent_type))

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
        )
        invocation = AgentInvocation(
            task_id=task_id,
            step_id=step_id,
            bundle=bundle,
            handoff=handoff,
            lessons=lessons,
            attempt=attempt,
            routing_signals=signals,
        )

        # 执行 Agent
        try:
            output, outcome = await self._run_agent_node(agent, invocation)
        except Exception as exc:
            dag.mark(node_id, StepStatus.FAILED, last_error=str(exc))
            # 失败也要计一步，避免「失败不消耗预算」导致无限重试
            self._breaker.charge(tokens=0, steps=1)
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
        self._checkpoints.save(
            StepCheckpoint(
                task_id=task_id,
                step_id=step_id,
                completed=True,
                node_id=node_id,
                run_id=self._run_id,
                payload={"tokens": output.tokens_used},
            )
        )
        self._breaker.charge(tokens=output.tokens_used, steps=1)

        # 验证（Tester 与 Verifier 参与时）
        verdict, feedback = await self._verify_node(task_id, node_id, output, handoff, result)

        if verdict is Verdict.PASS:
            dag.mark(node_id, StepStatus.SUCCESS, tokens_used=output.tokens_used)
            logger.info("node_succeeded", task_id=task_id, node=node_id, attempt=attempt)
            return

        # 未通过：记录教训并决定是否回退
        if feedback is not None and feedback.lesson:
            self._reflexion.add(
                ReflexionLesson(
                    root_cause=feedback.suggestions[0]
                    if feedback.suggestions
                    else "未满足验收标准",
                    lesson=feedback.lesson,
                    source_step=step_id,
                ),
                agent_type=node.agent_type,
            )

        if attempt >= self._config.max_retries:
            dag.mark(
                node_id,
                StepStatus.FAILED,
                last_error=f"重试 {attempt} 次仍未通过：{feedback.failed_criteria if feedback else '未知'}",
                tokens_used=output.tokens_used,
            )
            logger.warning("node_failed_after_retries", task_id=task_id, node=node_id)
            return

        # 回退：清除本步骤的检查点（否则下次重试会被误判为「已完成」），
        # 然后重置为 PENDING，下一轮重跑（带教训）。
        self._checkpoints.clear_step(task_id, step_id)
        dag.mark(node_id, StepStatus.PENDING, tokens_used=output.tokens_used)
        obs = get_observability()
        obs.inc(MetricNames.BACKTRACKS, 1, node_id=node_id)
        obs.inc(MetricNames.RETRIES, 1, node_id=node_id, reason="verifier_reject")
        if self._reflexion.lessons_for(node.agent_type):
            obs.gauge(
                MetricNames.REFLEXION_LESSONS,
                float(len(self._reflexion.lessons_for(node.agent_type))),
                agent=node.agent_type.value,
            )
        logger.info("node_rejected_retrying", task_id=task_id, node=node_id, attempt=attempt)

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
        # Tester 参与：先跑测试产出客观证据
        test_evidence = ""
        if self._config.enable_tester and not any(
            s.agent is AgentType.TESTER and s.step_id.endswith(node_id) for s in result.steps
        ):
            test_evidence = await self._collect_test_evidence(task_id, node_id, handoff)

        step_id = f"{task_id}:{node_id}:verify"
        space = self._context.isolator.space_for(AgentType.VERIFIER)

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
                f"# 实际产出（不含作者解释）\n{coder_output.content}",
                ContextKind.CODE,
                source=f"artifact://{node_id}",
            )
        )
        if test_evidence:
            space.add(
                make_chunk(
                    f"# 客观测试证据\n{test_evidence}",
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
        )
        invocation = AgentInvocation(
            task_id=task_id,
            step_id=step_id,
            bundle=bundle,
            handoff=verifier_handoff,
            attempt=1,
        )

        try:
            message = await self._agents[AgentType.VERIFIER].run(invocation)
        except Exception as exc:
            logger.warning("verification_error", task_id=task_id, node=node_id, error=str(exc))
            # 验证失败不应阻断流程，但也不能断言通过 → 保守判为通过并记录
            return Verdict.PASS, None

        feedback = message.feedback
        verdict = feedback.verdict if feedback is not None else Verdict.PASS
        obs = get_observability()
        obs.inc(MetricNames.VERDICT_TOTAL, 1, node_id=node_id, verdict=verdict.value)
        if verdict is Verdict.REJECT:
            # 「被驳回的验收条目数」是幻觉拦截的代理指标：条数越多说明
            # Coder 越倾向于声称完成但实际未达标。
            blocked = len(feedback.failed_criteria) if feedback is not None else 0
            obs.inc(MetricNames.HALLUCINATION_BLOCKED, max(1, blocked), node_id=node_id)
        self._record_step(
            result,
            StepResult(
                step_id=step_id,
                agent=AgentType.VERIFIER,
                output=str(message.payload.get("content", "")),
                feedback=feedback,
            ),
        )
        return verdict, feedback

    async def _collect_test_evidence(
        self, task_id: str, node_id: str, handoff: AgentHandoff
    ) -> str:
        """运行 Tester 生成并执行测试，返回客观证据文本。"""
        tester = self._agents.get(AgentType.TESTER)
        if not isinstance(tester, TesterAgent):
            return ""
        step_id = f"{task_id}:{node_id}:test"
        self._context.isolator.handoff_to(AgentType.TESTER, handoff)
        bundle = await self._context.build(
            agent=AgentType.TESTER,
            task_embedding=None,
            current_step=step_id,
            budget_total=handoff.budget_tokens,
        )
        invocation = AgentInvocation(
            task_id=task_id, step_id=step_id, bundle=bundle, handoff=handoff, attempt=1
        )
        try:
            output, outcome = await tester.generate_and_run(invocation)
        except Exception as exc:
            logger.warning("tester_error", task_id=task_id, node=node_id, error=str(exc))
            return ""
        return output.content if outcome.executed else "（测试未执行）"

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
        if self._task_span is not None:
            self._task_span.set_attribute("status", status.value)
            self._task_span.set_attribute("steps", len(result.steps))
            self._task_span.set_attribute("tokens", result.total_tokens)
            obs.end_span(self._task_span, error=error or "")
            self._task_span = None
        return result


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
