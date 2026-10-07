"""代码审查缺陷的回归测试。

每一个用例对应一次**已复现的真实缺陷**，命名里点明"以前会怎样"，
这样将来有人重构时能从失败信息直接看出被破坏的是哪条保证，
而不是只看到一句 assert 不成立。

分组与审查报告一致：
- P0：跨任务污染、验证 fail-open、测试证据、检查点语义
- P1：装配算法、上下文空间、账目
- P2：并发、内存上界、指标归属
- P3：告警去重、裁判一致性、渲染健壮性
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from devagent.api.service import TaskService, _context_metrics
from devagent.api.store import EventBus, InMemoryTaskStore, TaskEvent
from devagent.config import ContextConfig, Settings
from devagent.context import ContextEngine, make_chunk
from devagent.context.assembly import ContextAssembler, ScoringWeights, chunk_similarity
from devagent.context.budget import BudgetAllocation
from devagent.context.isolation import ContextIsolator
from devagent.context.tokenizer import lexical_similarity
from devagent.context.trust import InjectionGuard
from devagent.enums import AgentType, ContextKind, StepStatus
from devagent.models.domain import AgentHandoff
from devagent.models.gateway import ModelGateway
from devagent.models.provider import ChatMessage, ChatResult, ModelError, TokenUsage
from devagent.observability import (
    MetricNames,
    configure_observability,
    reset_observability,
)
from devagent.orchestration import Orchestrator, OrchestratorConfig
from devagent.orchestration.dag import DAG, DAGError
from devagent.reliability import CircuitBreaker

# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


def _json_block(payload: dict[str, Any]) -> str:
    return f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


class ScriptedProvider:
    """按角色脚本化应答的假模型。

    记录每一次 (角色, user 消息) 与调用次数，用于断言提示词内容与账目。
    """

    name = "scripted"
    TOKENS_PER_CALL = 100

    def __init__(
        self,
        *,
        verifier_sequence: list[bool] | None = None,
        verifier_raises: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.verifier_sequence = verifier_sequence or [True]
        self.verifier_raises = verifier_raises
        self.delay = delay
        self.verifier_calls = 0
        self.calls = 0
        self.seen_prompts: list[tuple[AgentType, str]] = []
        self.seen_use_cache: list[bool] = []

    def _role(self, messages: list[ChatMessage]) -> AgentType:
        system = next((m.content for m in messages if m.role == "system"), "")
        if "需求分析师" in system:
            return AgentType.REQUIREMENT
        if "软件架构师" in system:
            return AgentType.ARCHITECT
        if "软件工程师" in system:
            return AgentType.CODER
        if "测试工程师" in system:
            return AgentType.TESTER
        if "独立验证工程师" in system:
            return AgentType.VERIFIER
        if "代码审查员" in system:
            return AgentType.REVIEWER
        return AgentType.CODER

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        use_cache: bool = True,
        **kwargs: Any,
    ) -> ChatResult:
        role = self._role(messages)
        user = next((m.content for m in messages if m.role == "user"), "")
        self.seen_prompts.append((role, user))
        self.seen_use_cache.append(use_cache)
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)

        if role is AgentType.VERIFIER and self.verifier_raises:
            raise ModelError("上游 503：验证服务不可用")

        if role is AgentType.REQUIREMENT:
            reply = _json_block(
                {
                    "goal": "实现需求",
                    "acceptance_criteria": ["满足需求"],
                    "constraints": [],
                    "relevant_files": [],
                }
            )
        elif role is AgentType.ARCHITECT:
            reply = _json_block(
                {
                    "approach": "直接实现",
                    "nodes": [
                        {
                            "id": "N1",
                            "goal": "实现该需求",
                            "agent_type": "coder",
                            "deps": [],
                            "acceptance_criteria": ["满足需求"],
                        }
                    ],
                }
            )
        elif role is AgentType.TESTER:
            reply = _json_block(
                {
                    "test_files": [
                        {
                            "path": "test_x.py",
                            "content": "def test_x(): assert True",
                            "covers_criteria": ["满足需求"],
                        }
                    ],
                    "cases": [],
                }
            )
        elif role is AgentType.VERIFIER:
            idx = min(self.verifier_calls, len(self.verifier_sequence) - 1)
            passed = self.verifier_sequence[idx]
            self.verifier_calls += 1
            payload: dict[str, Any] = {
                "verdict": "pass" if passed else "reject",
                "criterion_checks": [
                    {
                        "criterion": "满足需求",
                        "passed": passed,
                        "reason": "测试" if passed else "未达标",
                    }
                ],
            }
            if not passed:
                payload["suggestions"] = ["补充实现"]
                payload["lesson"] = "必须真正实现需求"
                payload["root_cause"] = "实现不完整"
            reply = _json_block(payload)
        else:
            reply = _json_block(
                {
                    "summary": "已实现",
                    "changes": [
                        {
                            "file": "a.py",
                            "reason": "r",
                            "addresses_criteria": ["满足需求"],
                            "diff": "+x",
                        }
                    ],
                    "unresolved": [],
                }
            )

        usage = TokenUsage(
            prompt_tokens=self.TOKENS_PER_CALL,
            completion_tokens=self.TOKENS_PER_CALL,
            total_tokens=self.TOKENS_PER_CALL * 2,
        )
        return ChatResult(content=reply, model=model, provider=self.name, usage=usage)

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def _orchestrator(
    provider: ScriptedProvider,
    *,
    config: OrchestratorConfig | None = None,
    engine: ContextEngine | None = None,
) -> Orchestrator:
    settings = Settings()
    gateway = ModelGateway(
        settings, providers={"deepseek": provider, "qwen": provider, "zhipu": provider}
    )
    return Orchestrator(
        settings,
        gateway=gateway,
        context_engine=engine or ContextEngine(ContextConfig()),
        config=config or OrchestratorConfig(enable_tester=False, enable_reviewer=False),
    )


def _verifier_prompts(provider: ScriptedProvider) -> list[str]:
    return [p for role, p in provider.seen_prompts if role is AgentType.VERIFIER]


# --------------------------------------------------------------------------- #
# P0-1 跨任务隔离
# --------------------------------------------------------------------------- #


class TestPerRunIsolation:
    async def test_concurrent_runs_do_not_share_context(self) -> None:
        """并发任务的需求提示词里**不能**出现另一个任务的需求。

        缺陷形态：Orchestrator 被 API 层按进程单例持有，而它的
        ``ContextIsolator`` / 熔断器 / 反思记忆都挂在 ``self`` 上 ——
        实测任务 B 的提示词里读到了任务 A 的需求原文。
        """
        provider = ScriptedProvider(delay=0.02)
        orch = _orchestrator(provider)

        await asyncio.gather(
            orch.run("【任务A】购物车优惠券", task_id="ta"),
            orch.run("【任务B】订单退款", task_id="tb"),
        )

        requirement_prompts = [
            p for role, p in provider.seen_prompts if role is AgentType.REQUIREMENT
        ]
        assert len(requirement_prompts) == 2
        for prompt in requirement_prompts:
            has_a = "任务A" in prompt
            has_b = "任务B" in prompt
            assert has_a != has_b, f"同一次需求构建里混入了两个任务：A={has_a} B={has_b}"

    async def test_breaker_and_reflexion_are_per_run(self) -> None:
        """账本必须按运行隔离：第二次运行不能继承第一次的用量。"""
        provider = ScriptedProvider()
        orch = _orchestrator(provider)

        first = await orch.run("第一次", task_id="t1")
        after_first = orch._breaker.tokens_used
        assert after_first == first.total_tokens > 0

        second = await orch.run("第二次", task_id="t2")
        # 若 breaker 共享，这里会是 first+second（并可能直接触发熔断）
        assert orch._breaker.tokens_used == second.total_tokens

    async def test_task_service_attributes_events_to_the_right_task(self) -> None:
        """两个并发任务各自收到自己的节点事件。

        缺陷形态：``TaskService._current_task_id`` 是共享字符串，
        任务 B 一进来就把它覆盖掉，于是 A 的 node_started 被投递到 B 的流
        （实测 A 收到 0 个、B 收到 2 个）。
        """
        provider = ScriptedProvider(delay=0.02)
        orch = _orchestrator(provider)
        service = TaskService(orch, store=InMemoryTaskStore(), bus=EventBus())

        tid_a = await service.submit("任务A")
        tid_b = await service.submit("任务B")
        await asyncio.gather(service.wait(tid_a, timeout=10), service.wait(tid_b, timeout=10))

        started_a = [e for e in service.bus.history(tid_a) if e.kind == "node_started"]
        started_b = [e for e in service.bus.history(tid_b) if e.kind == "node_started"]
        assert len(started_a) == 1, "任务A 应恰好收到自己的 1 个 node_started"
        assert len(started_b) == 1, "任务B 应恰好收到自己的 1 个 node_started"
        for tid in (tid_a, tid_b):
            kinds = [e.kind for e in service.bus.history(tid)]
            assert kinds[0] == "task_started"
            assert kinds[-1] == "task_finished"


# --------------------------------------------------------------------------- #
# P0-2 验证失败必须 fail-closed
# --------------------------------------------------------------------------- #


class TestVerificationFailClosed:
    async def test_verifier_outage_never_yields_success(self) -> None:
        """验证器不可用时，任务**绝不能**报成功。

        缺陷形态：``_verify_node`` 捕获异常后 ``return Verdict.PASS``，
        一次瞬时 503 就能让未验证的产物被标记为"已验证成功"，
        任务状态还是 succeeded、error 为空。
        """
        reset_observability()
        obs = configure_observability(enabled=True)
        obs.metrics.reset()

        provider = ScriptedProvider(verifier_raises=True)
        orch = _orchestrator(provider)
        result = await orch.run("给接口加分页", task_id="t")

        assert result.status.value == "failed"
        assert result.succeeded is False
        assert result.dag is not None
        assert result.dag.states["N1"].status is StepStatus.FAILED
        assert obs.metrics.total(MetricNames.VERIFICATION_UNAVAILABLE) >= 1, (
            "验证不可用必须被计入指标，否则运维看不到"
        )

    async def test_verifier_rejection_still_retries_then_fails(self) -> None:
        """验证器**正常驳回**时，重试耗尽后节点应失败（对照组）。"""
        provider = ScriptedProvider(verifier_sequence=[False])
        orch = _orchestrator(provider)
        result = await orch.run("需求", task_id="t")

        assert result.status.value == "failed"
        assert result.dag is not None
        assert result.dag.states["N1"].status is StepStatus.FAILED


# --------------------------------------------------------------------------- #
# P0-3 / P0-4 测试证据
# --------------------------------------------------------------------------- #


class TestTestEvidence:
    async def test_tester_call_is_counted_in_tokens(self) -> None:
        """Tester 的模型调用必须计入总账与熔断器。

        缺陷形态：每个成功节点有 3 次真实模型调用（coder + tester + verifier），
        而账本只看到 1 次，实际可用预算约为配置值的 3 倍。
        """
        provider = ScriptedProvider()
        orch = _orchestrator(
            provider, config=OrchestratorConfig(enable_tester=True, enable_reviewer=False)
        )
        result = await orch.run("需求", task_id="t")

        expected = provider.calls * ScriptedProvider.TOKENS_PER_CALL * 2
        assert result.total_tokens == expected, (
            f"账目与会话数不一致：total_tokens={result.total_tokens} 实际调用={provider.calls}"
        )
        assert orch._breaker.tokens_used == result.total_tokens

    async def test_step_tokens_match_task_total(self) -> None:
        """steps[] 里的 token 之和必须与 total_tokens 对得上（此前需求/架构恒为 0）。"""
        provider = ScriptedProvider()
        orch = _orchestrator(provider)
        result = await orch.run("需求", task_id="t")

        step_tokens = sum(s.tokens_used for s in result.steps)
        assert step_tokens > 0
        # 每个模型调用都对应一条 StepResult（不含 Tester：它作为证据并入验证步骤）
        assert step_tokens == result.total_tokens

    async def test_unexecuted_tests_are_labelled_not_faked(self) -> None:
        """没有测试运行时，证据必须显式标注"未执行"，而不是编造通过。"""
        provider = ScriptedProvider()
        orch = _orchestrator(
            provider, config=OrchestratorConfig(enable_tester=True, enable_reviewer=False)
        )
        await orch.run("需求", task_id="t")

        prompts = _verifier_prompts(provider)
        assert prompts
        assert "测试未执行" in prompts[-1]
        assert "不构成" in prompts[-1], "必须说明这不算功能缺陷证据"


# --------------------------------------------------------------------------- #
# P1-1 装配算法在无向量时也必须工作
# --------------------------------------------------------------------------- #


class TestAssemblyWithoutEmbeddings:
    def test_duplicate_chunks_are_deduped_without_embeddings(self) -> None:
        """没有 embedding 时，内容相同的软片段仍必须被去重。

        缺陷形态：打分只算余弦相似度，而它遇到 ``None`` 恒返回 0 →
        相关性恒 0、冗余惩罚恒 0、**硬去重永不触发**，
        整套算法退化成"按插入顺序取片段"（实测两份完全相同的片段都进上下文）。
        """
        assembler = ContextAssembler()
        content = "这是一段完全相同的重复内容，用来触发硬去重。" * 5
        chunks = [
            make_chunk(content, ContextKind.CODE, source="a"),
            make_chunk(content, ContextKind.CODE, source="b"),
        ]
        result = assembler.assemble(
            chunks,
            task_embedding=None,
            current_step="s",
            budget=BudgetAllocation(total=16000),
            agent=AgentType.CODER,
        )
        assert len(result.chunks) == 1, "近似重复的候选最多保留 1 个"

    def test_relevance_uses_task_text_when_vectors_missing(self) -> None:
        """无向量时相关性应由任务文本的词法重叠给出，而不是恒为 0。"""
        assembler = ContextAssembler()
        relevant = make_chunk("为用户列表接口增加分页参数 page page_size", ContextKind.CODE)
        unrelated = make_chunk("数据库连接池的最大溢出连接数配置项", ContextKind.CODE)
        task = "为用户列表接口增加分页参数"

        rel_score = assembler.score_chunk(relevant, None, "s", [], task)
        unrel_score = assembler.score_chunk(unrelated, None, "s", [], task)

        assert rel_score.relevance > 0
        assert rel_score.relevance > unrel_score.relevance

    def test_embedding_path_still_takes_precedence(self) -> None:
        """有向量时必须仍然用余弦，不能被词法兜底覆盖。"""
        a = make_chunk("完全不同的文字甲", ContextKind.CODE, embedding=[1.0, 0.0])
        b = make_chunk("完全不同的文字乙", ContextKind.CODE, embedding=[1.0, 0.0])
        assert chunk_similarity(a, b) == pytest.approx(1.0)

    def test_lexical_similarity_is_language_agnostic(self) -> None:
        assert lexical_similarity("分页参数校验", "分页参数校验逻辑") > 0.5
        assert lexical_similarity("分页参数校验", "数据库索引优化") == 0.0
        assert lexical_similarity("", "") == 0.0


# --------------------------------------------------------------------------- #
# P1-2 上下文空间不得无限累积
# --------------------------------------------------------------------------- #


class TestContextSpaceHygiene:
    def test_handoff_replaces_previous_chunks(self) -> None:
        """同一个 Agent 再次 handoff 时，旧的目标/标准/约束必须被替换。"""
        isolator = ContextIsolator()
        handoff = AgentHandoff(
            task_id="t1",
            goal="目标",
            acceptance_criteria=["标准1"],
            constraints=["约束1"],
        )
        isolator.handoff_to(AgentType.CODER, handoff)
        first = len(isolator.space_for(AgentType.CODER).chunks)
        isolator.handoff_to(AgentType.CODER, handoff)
        assert len(isolator.space_for(AgentType.CODER).chunks) == first, (
            "重复投喂同一份 handoff 不应让硬约束翻倍"
        )

    async def test_verifier_space_is_cleared_between_attempts(self) -> None:
        """重试时 Verifier 只能看到**本轮**的产物。

        缺陷形态：verifier 空间只 add 不清理，于是 attempt 2 的上下文里
        同时存在 attempt 1 被驳回的产物与本轮产物，且 source 完全相同
        （``artifact://N1``），Verifier 无从分辨该审哪一个。
        """
        provider = ScriptedProvider(verifier_sequence=[False, True])
        orch = _orchestrator(provider)
        await orch.run("需求", task_id="t")

        prompts = _verifier_prompts(provider)
        assert len(prompts) >= 2, "应有驳回后的第二次验证"
        for index, prompt in enumerate(prompts, start=1):
            assert prompt.count("待验证的验收标准") == 1, f"第{index}次验证：验收标准重复出现"
            assert prompt.count("实际产出") == 1, (
                f"第{index}次验证：出现多份实际产出（上一轮被驳回的产物未被清理）"
            )

    def test_hard_overflow_is_reported_not_silent(self) -> None:
        """硬约束击穿预算时必须留痕（此前是静默无限投递）。

        刻意让每条约束**内容不同**（否则会先被硬去重合并掉，测不到溢出）。
        """
        assembler = ContextAssembler()
        chunks = [
            make_chunk(
                f"第{i}条不可违反的约束。" + "该约束必须在所有改动中保持成立。" * 100,
                ContextKind.TASK_SPEC,
                is_hard=True,
                source=f"handoff://{i}",
            )
            for i in range(4)
        ]
        result = assembler.assemble(
            chunks,
            task_embedding=None,
            current_step="s",
            budget=BudgetAllocation(total=2000),
            agent=AgentType.CODER,
        )
        assert result.decision.hard_overflow_tokens > 0
        assert "超预算" in result.decision.reason

    def test_duplicate_hard_chunks_are_collapsed(self) -> None:
        """内容完全相同的硬约束只保留一份。"""
        assembler = ContextAssembler()
        same = "验收标准：必须支持分页"
        chunks = [
            make_chunk(same, ContextKind.TASK_SPEC, is_hard=True, source="handoff://t"),
            make_chunk(same, ContextKind.TASK_SPEC, is_hard=True, source="handoff://t"),
        ]
        result = assembler.assemble(
            chunks,
            task_embedding=None,
            current_step="s",
            budget=BudgetAllocation(total=16000),
            agent=AgentType.CODER,
        )
        assert len(result.chunks) == 1

    def test_placement_keeps_newest_last(self) -> None:
        """位置编排：最新的片段必须在最尾（早先切片方向是反的）。"""
        assembler = ContextAssembler()
        hard = make_chunk("HARD", ContextKind.SYSTEM_PROMPT, is_hard=True)
        old = make_chunk("OLD", ContextKind.CODE, age=10)
        middle = make_chunk("MID", ContextKind.CODE, age=5)
        newest = make_chunk("NEW", ContextKind.CODE, age=0)
        ordered = assembler.placement_order([middle, newest, old, hard])
        assert ordered[0] is hard
        assert ordered[-1] is newest, "最新的片段必须在尾部"
        assert ordered[1] is old, "越旧越靠前"


# --------------------------------------------------------------------------- #
# P1-4 检查点语义
# --------------------------------------------------------------------------- #


class TestCheckpointSemantics:
    async def test_no_completed_checkpoint_for_failed_node(self) -> None:
        """被驳回且重试耗尽的节点，不能留下"已完成"检查点。

        缺陷形态：检查点在**验证之前**写入，于是失败的节点也留下
        ``completed=True``；同一个 task_id 再跑一次就会被短路，
        一步不执行却报成功。
        """
        provider = ScriptedProvider(verifier_sequence=[False])
        orch = _orchestrator(provider)
        result = await orch.run("需求", task_id="t")

        saved = orch._checkpoints.list_for_task("t")
        assert all(not cp.completed for cp in saved), (
            f"失败节点不应留下已完成检查点：{[(c.step_id, c.completed) for c in saved]}"
        )
        assert result.status.value == "failed"

    async def test_completed_checkpoint_only_after_pass(self) -> None:
        provider = ScriptedProvider(verifier_sequence=[False, True])
        orch = _orchestrator(provider)
        await orch.run("需求", task_id="t")

        completed = [c for c in orch._checkpoints.list_for_task("t") if c.completed]
        assert completed, "通过验证的节点应留下检查点"
        assert all(c.step_id.endswith(":N1") for c in completed)

    async def test_forget_task_forces_full_rerun(self) -> None:
        """``forget_task`` 之后必须真正重跑（评测重复执行同一 id 的安全性）。"""
        provider = ScriptedProvider()
        orch = _orchestrator(provider)
        await orch.run("需求", task_id="t")
        calls_after_first = provider.calls

        await orch.run("需求", task_id="t")
        assert provider.calls == calls_after_first, "未清除检查点时应从断点恢复"

        orch.forget_task("t")
        await orch.run("需求", task_id="t")
        assert provider.calls > calls_after_first, "清除检查点后必须重新执行"


# --------------------------------------------------------------------------- #
# P2 并发、内存上界、指标归属
# --------------------------------------------------------------------------- #


class TestConcurrencyAndBounds:
    async def test_cancel_while_queued_marks_cancelled(self) -> None:
        """排队期间被取消的任务必须离开 pending 且清理干净。

        缺陷形态：``try/finally`` 位于信号量**内部**，取消发生在等待期间时
        所有状态跃迁与清理都被跳过 —— 任务永远 pending、SSE 永不结束。
        """
        provider = ScriptedProvider(delay=0.3)
        orch = _orchestrator(provider)
        service = TaskService(orch, store=InMemoryTaskStore(), bus=EventBus(), max_concurrent=1)
        tid_a = await service.submit("A")
        tid_b = await service.submit("B")  # 卡在信号量上
        await asyncio.sleep(0.05)
        assert service.cancel(tid_b) is True

        data = await service.wait(tid_b, timeout=5)
        assert data is not None
        assert data["status"] == "cancelled", f"排队取消后状态应为 cancelled，实为 {data['status']}"
        assert service.bus.is_closed(tid_b) is True
        await service.wait(tid_a, timeout=5)

    async def test_duplicate_task_id_is_rejected(self) -> None:
        """同一 task_id 并发提交只能成功一次（此前是"先查再写"的 TOCTOU）。"""
        provider = ScriptedProvider(delay=0.05)
        orch = _orchestrator(provider)
        service = TaskService(orch, store=InMemoryTaskStore(), bus=EventBus())

        results = await asyncio.gather(
            service.submit("A", task_id="dup"),
            service.submit("B", task_id="dup"),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, ValueError)]
        accepted = [r for r in results if isinstance(r, str)]
        assert len(accepted) == 1
        assert len(errors) == 1
        await service.wait("dup", timeout=5)

    def test_event_bus_replays_newest_events(self) -> None:
        """回放必须取**最近**的事件，否则晚到订阅者永远看不到 task_finished。"""
        bus = EventBus(history_size=500, queue_size=200)
        for i in range(300):
            bus.publish(TaskEvent(kind=f"e{i}", task_id="t"))
        queue = bus.subscribe("t")
        assert queue.qsize() == 200
        first = queue.get_nowait()
        last = None
        while not queue.empty():
            last = queue.get_nowait()
        assert first.kind == "e100", f"应回放最近的 200 条（从 e100 开始），实为 {first.kind}"
        assert last is not None and last.kind == "e299"

    def test_event_bus_frees_history_of_old_tasks(self) -> None:
        """历史不能被无限保留（每条事件都含模型输出）。"""
        bus = EventBus(history_size=10, queue_size=10, max_tasks=5)
        for i in range(20):
            bus.publish(TaskEvent(kind="x", task_id=f"t{i}"))
        assert len(bus._history) <= 5

    def test_metrics_series_are_bounded(self) -> None:
        """指标序列必须有上限：节点/任务 id 当标签时取值无界。"""
        from devagent.observability.metrics import MetricsCollector

        collector = MetricsCollector(max_series_per_metric=10)
        for i in range(100):
            collector.inc_counter("c", 1, node_id=f"n{i}")
        assert len(collector._counters["c"]) <= 10


class TestEventPersistence:
    async def test_events_are_persisted_when_store_supports_it(self) -> None:
        """SQL 存储必须真的收到事件。

        缺陷形态：``SqlTaskStore.append_event`` / ``replay_events`` 只被
        测试调用过，SSE 路由只读内存 EventBus —— 于是 ``task_events`` 表
        恒为空，进程重启后事件流什么都回放不出来，而 db 模块的文档
        明确承诺了这一点。
        """
        persisted: list[TaskEvent] = []

        class PersistingStore(InMemoryTaskStore):
            async def append_event(self, event: TaskEvent) -> None:
                persisted.append(event)

        provider = ScriptedProvider()
        orch = _orchestrator(provider)
        service = TaskService(orch, store=PersistingStore(), bus=EventBus())
        tid = await service.submit("A")
        await service.wait(tid, timeout=10)

        kinds = [e.kind for e in persisted]
        assert "task_started" in kinds, f"事件未落库：{kinds}"
        assert "node_started" in kinds, f"节点事件未落库：{kinds}"
        assert "task_finished" in kinds, f"收尾事件未落库：{kinds}"
        assert all(e.task_id == tid for e in persisted)


class TestApiKeyGate:
    def test_api_key_guard_when_configured(self) -> None:
        """设置 ``security.api_key`` 后，`/api/v1/**` 必须要求 ``X-API-Key``。

        缺陷形态：全站没有任何认证，而服务默认绑定 0.0.0.0 ——
        任何能访问端口的人都能创建任务（即消耗模型额度）、删除任务、跑评测。
        """
        from fastapi.testclient import TestClient

        from devagent.api.app import create_app

        settings = Settings()
        settings.security.api_key = "secret-key"  # type: ignore[misc]
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/api/v1/tasks").status_code == 401
            assert client.get("/api/v1/tasks", headers={"X-API-Key": "wrong"}).status_code == 401
            ok = client.get("/api/v1/tasks", headers={"X-API-Key": "secret-key"})
            assert ok.status_code == 200
            # 非 /api/v1 路径不受影响：前端静态资源仍需可加载
            assert client.get("/").status_code == 200

    def test_api_key_disabled_by_default(self) -> None:
        """默认不启用鉴权，保持零配置可用。"""
        from fastapi.testclient import TestClient

        from devagent.api.app import create_app

        with TestClient(create_app(Settings())) as client:
            assert client.get("/api/v1/tasks").status_code == 200


class TestCompressionHonesty:
    async def test_extractive_summary_keeps_constraint_lines(self) -> None:
        """无 LLM 摘要器时的降级必须**结构化抽取**，而不是 300 字符盲截断。

        缺陷形态：温区最多 10 个片段被合并后只保留前 300 字符，
        而 `src/` 里并不存在 LLM 摘要器实现 —— 生产路径一直走这条降级分支，
        等于把"滚动摘要"的承诺变成了丢内容。
        """
        from devagent.context.compression import ContextCompressor

        # 关键约束放在很靠后的位置，盲截断必然丢掉它
        filler = "普通描述行。\n" * 60
        text = f"{filler}# 关键约束：必须保留分页参数校验\n" + "另一段说明。" * 200
        compressor = ContextCompressor(hot_window=0, warm_window=1)
        result = await compressor.compress([make_chunk(text, ContextKind.HISTORY, age=5)])

        summary = result.chunks[0].content
        assert "关键约束" in summary, "带约束关键词的行必须被保留"
        assert len(summary) > 320, "不应退化成 300 字符盲截断"

    async def test_hard_constraint_untouchable_flag_is_wired(self) -> None:
        """``hard_constraint_untouchable`` 必须真的影响压缩行为。

        缺陷形态：这个配置项从未被读取，硬约束恒为"免压缩" ——
        写着开关却没有接线，比没有开关更容易误导。
        """
        from devagent.context.compression import ContextCompressor

        hard = make_chunk("不可违反的约束。" * 20, ContextKind.TASK_SPEC, is_hard=True, age=0)
        soft = make_chunk("普通历史记录。" * 20, ContextKind.HISTORY, age=1)

        protected = ContextCompressor(hot_window=0, warm_window=1, protect_hard=True)
        kept = await protected.compress([hard, soft])
        assert any(c.id == hard.id for c in kept.chunks), "开启保护时硬约束应原样透传"

        unprotected = ContextCompressor(hot_window=0, warm_window=1, protect_hard=False)
        compressed = await unprotected.compress([hard, soft])
        assert not any(c.id == hard.id for c in compressed.chunks), "关闭保护后硬约束应进入可压缩区"


class TestPerTaskMetrics:
    def test_context_metrics_are_scoped_to_task(self) -> None:
        """每个任务看到的是**自己**的收益，而不是进程累计值。"""
        reset_observability()
        obs = configure_observability(enabled=True)
        obs.metrics.reset()

        obs.record_context_build(
            agent="coder", tokens_before=1000, tokens_after=100, budget=1000, task_id="T1"
        )
        obs.record_context_build(
            agent="coder", tokens_before=1000, tokens_after=500, budget=1000, task_id="T2"
        )

        m1 = _context_metrics("T1")
        m2 = _context_metrics("T2")
        assert m1["tokens_saved"] == 900
        assert m2["tokens_saved"] == 500
        assert m1["tokens_saved"] != m2["tokens_saved"]
        assert m1["task_id"] == "T1"


# --------------------------------------------------------------------------- #
# P3 零散正确性
# --------------------------------------------------------------------------- #


class TestSmallCorrectness:
    def test_breaker_warns_once_per_dimension(self) -> None:
        """同一维度的预警只能出现一次。

        缺陷形态：判据是 ``"tokens" not in str(self.warnings)``，
        而文案是 "token 用量已达 …"（不含 "tokens"），判据恒真 ——
        比例过线后每次 charge 都追加一条，实测 6 次得到 6 条重复告警。
        """
        breaker = CircuitBreaker(max_tokens=100_000, max_steps=1000, warn_ratio=0.8)
        breaker.charge(tokens=85_000, steps=1)
        for _ in range(5):
            breaker.charge(tokens=0, steps=1)
        assert len(breaker.warnings) == 1, breaker.warnings

    def test_judge_merge_is_self_consistent(self) -> None:
        """双向合并后 overall 与 passed 不能互相矛盾。"""
        from devagent.evaluation.judge import JudgeResult, LLMJudge

        # _merge 只用到阈值，backend 传一个占位对象即可
        judge = LLMJudge(backend=object(), pass_threshold=3.5)  # type: ignore[arg-type]

        # 边界：3.6（通过）+ 3.4（不通过）→ 平均 3.5，恰好压线
        merged = judge._merge(
            JudgeResult(scores=(), overall=3.6, passed=True),
            JudgeResult(scores=(), overall=3.4, passed=False),
        )
        assert merged.overall == pytest.approx(3.5)
        assert merged.passed is (merged.overall >= 3.5), (
            f"自相矛盾：overall={merged.overall} passed={merged.passed}"
        )
        # 两次的分歧不能丢：通过结论原样保留，供需要更严口径的下游使用
        assert merged.raw["passes"] == [True, False]

        # 分歧很大时：平均值不达标 → 不通过，且标记为矛盾
        merged_bad = judge._merge(
            JudgeResult(scores=(), overall=5.0, passed=True),
            JudgeResult(scores=(), overall=1.0, passed=False),
        )
        assert merged_bad.overall == pytest.approx(3.0)
        assert merged_bad.passed is False
        assert merged_bad.inconsistent is True

    def test_injection_guard_survives_brace_in_source(self) -> None:
        """来源含花括号时渲染不能抛异常（否则验证会被静默跳过）。"""
        guard = InjectionGuard()
        chunk = make_chunk("内容", ContextKind.CODE, source="plain/path{a}.py")
        text = guard.render([chunk])
        assert "内容" in text

    def test_dag_mark_rejects_unknown_field(self) -> None:
        """拼错的字段名必须报错，而不是静默丢数据。"""
        from devagent.models.domain import TaskNode

        dag = DAG.build([TaskNode(id="N1", goal="g", agent_type=AgentType.CODER, deps=())])
        with pytest.raises(DAGError):
            dag.mark("N1", StepStatus.RUNNING, token_used=10)  # 少了一个 s

    async def test_tester_honours_cache_disabled(self) -> None:
        """Tester 声明关闭缓存，就必须真的把 use_cache=False 传给网关。"""
        from devagent.agents.base import AgentInvocation
        from devagent.agents.tester import TesterAgent

        provider = ScriptedProvider()
        settings = Settings()
        gateway = ModelGateway(
            settings, providers={"deepseek": provider, "qwen": provider, "zhipu": provider}
        )
        captured: dict[str, Any] = {}
        original = gateway.chat

        async def spy(messages: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return await original(messages, **kwargs)

        gateway.chat = spy  # type: ignore[method-assign]

        engine = ContextEngine(ContextConfig())
        bundle = await engine.build(agent=AgentType.TESTER, task_embedding=None, current_step="s")
        agent = TesterAgent(gateway)
        await agent.generate_and_run(
            AgentInvocation(
                task_id="t",
                step_id="s",
                bundle=bundle,
                handoff=AgentHandoff(task_id="t", goal="g", acceptance_criteria=["c"]),
            )
        )
        assert captured.get("use_cache") is False, (
            "Tester 的调用必须显式 use_cache=False（cache_enabled 属性本身不生效）"
        )

    def test_loop_detector_wiring_is_configurable(self) -> None:
        """LoopDetector 的阈值必须来自配置，而不是构造默认值。"""
        cfg = OrchestratorConfig(max_backtrack_depth=7, same_failure_threshold=2)
        settings = Settings()
        orch = Orchestrator(
            settings,
            gateway=ModelGateway(
                settings,
                providers={
                    "deepseek": ScriptedProvider(),
                    "qwen": ScriptedProvider(),
                    "zhipu": ScriptedProvider(),
                },
            ),
            config=cfg,
        )
        assert orch._loop_detector.max_attempts == 7
        assert orch._loop_detector.same_failure_threshold == 2

    def test_scoring_weights_from_config_are_used(self) -> None:
        weights = ScoringWeights.from_config(ContextConfig(weight_relevance=2.5))
        assert weights.relevance == 2.5
