"""可靠性组件的单元测试。"""

from __future__ import annotations

import pytest

from devagent.enums import AgentType
from devagent.models.domain import ReflexionLesson
from devagent.reliability import (
    BudgetExceededError,
    CircuitBreaker,
    LoopDetector,
    ReflexionMemory,
    StepCheckpoint,
    TaskCheckpointStore,
)


class TestCircuitBreaker:
    def test_charges_within_limit(self) -> None:
        breaker = CircuitBreaker(max_tokens=1000, max_steps=10)
        breaker.charge(tokens=500, steps=5)
        assert breaker.tokens_used == 500
        assert breaker.steps_used == 5
        assert not breaker.is_exhausted()

    def test_raises_on_token_exceeded(self) -> None:
        breaker = CircuitBreaker(max_tokens=100, max_steps=100)
        with pytest.raises(BudgetExceededError) as exc:
            breaker.charge(tokens=200)
        assert exc.value.kind == "tokens"
        assert exc.value.used == 200
        assert exc.value.limit == 100

    def test_raises_on_step_exceeded(self) -> None:
        breaker = CircuitBreaker(max_tokens=1_000_000, max_steps=2)
        with pytest.raises(BudgetExceededError) as exc:
            breaker.charge(steps=3)
        assert exc.value.kind == "steps"

    def test_warns_near_limit(self) -> None:
        breaker = CircuitBreaker(max_tokens=1000, max_steps=100, warn_ratio=0.8)
        breaker.charge(tokens=850)
        assert any("token" in w for w in breaker.warnings)

    def test_remaining_and_snapshot(self) -> None:
        breaker = CircuitBreaker(max_tokens=1000, max_steps=10)
        breaker.charge(tokens=300, steps=4)
        assert breaker.remaining_tokens() == 700
        assert breaker.remaining_steps() == 6
        snap = breaker.snapshot()
        assert snap["tokens_used"] == 300
        assert snap["remaining_tokens"] == 700

    def test_reset(self) -> None:
        breaker = CircuitBreaker(max_tokens=1000)
        breaker.charge(tokens=900)
        breaker.reset()
        assert breaker.tokens_used == 0
        assert breaker.warnings == []


class TestReflexionMemory:
    def _lesson(self, root: str = "r", lesson: str = "l") -> ReflexionLesson:
        return ReflexionLesson(root_cause=root, lesson=lesson)

    def test_adds_and_retrieves(self) -> None:
        mem = ReflexionMemory()
        mem.add(self._lesson("未处理边界", "需校验下界"), agent_type=AgentType.CODER)
        lessons = mem.lessons_for(AgentType.CODER)
        assert len(lessons) == 1
        assert lessons[0].lesson == "需校验下界"

    def test_deduplicates(self) -> None:
        mem = ReflexionMemory()
        for _ in range(3):
            mem.add(self._lesson("same", "same"), agent_type=AgentType.CODER)
        assert len(mem.lessons_for(AgentType.CODER)) == 1

    def test_per_agent_isolation(self) -> None:
        """教训按角色隔离：Coder 的教训不污染 Verifier。"""
        mem = ReflexionMemory()
        mem.add(self._lesson("coder issue", "fix code"), agent_type=AgentType.CODER)
        assert len(mem.lessons_for(AgentType.CODER)) == 1
        assert mem.lessons_for(AgentType.VERIFIER) == []

    def test_fifo_cap(self) -> None:
        mem = ReflexionMemory(max_per_agent=3)
        for i in range(6):
            mem.add(self._lesson(f"root{i}", f"lesson{i}"), agent_type=AgentType.CODER)
        lessons = mem.lessons_for(AgentType.CODER)
        assert len(lessons) == 3
        assert lessons[0].root_cause == "root3"  # 最早的被淘汰

    def test_context_block_rendering(self) -> None:
        mem = ReflexionMemory()
        mem.add(
            ReflexionLesson(root_cause="未校验边界", lesson="需加下界校验", avoid="假设输入合法"),
            agent_type=AgentType.CODER,
        )
        block = mem.as_context_block(AgentType.CODER)
        assert "历史失败教训" in block
        assert "未校验边界" in block
        assert "需加下界校验" in block

    def test_empty_context_block(self) -> None:
        assert ReflexionMemory().as_context_block(AgentType.CODER) == ""


class TestCheckpointStore:
    def test_save_and_load(self) -> None:
        store = TaskCheckpointStore()
        cp = StepCheckpoint(task_id="T1", step_id="S1", completed=True, node_id="N1")
        store.save(cp)
        loaded = store.load("T1", "S1")
        assert loaded is not None
        assert loaded.completed is True

    def test_load_missing_returns_none(self) -> None:
        assert TaskCheckpointStore().load("T1", "NOPE") is None

    def test_list_for_task(self) -> None:
        store = TaskCheckpointStore()
        store.save(StepCheckpoint(task_id="T1", step_id="S1", completed=True))
        store.save(StepCheckpoint(task_id="T1", step_id="S2", completed=False))
        store.save(StepCheckpoint(task_id="T2", step_id="S1", completed=True))
        assert len(store.list_for_task("T1")) == 2
        assert len(store.list_for_task("T2")) == 1

    def test_completed_step_ids(self) -> None:
        store = TaskCheckpointStore()
        store.save(StepCheckpoint(task_id="T1", step_id="S1", completed=True))
        store.save(StepCheckpoint(task_id="T1", step_id="S2", completed=False))
        assert store.completed_step_ids("T1") == {"S1"}

    def test_clear(self) -> None:
        store = TaskCheckpointStore()
        store.save(StepCheckpoint(task_id="T1", step_id="S1"))
        store.clear("T1")
        assert store.list_for_task("T1") == []


class TestLoopDetector:
    def test_detects_excessive_attempts(self) -> None:
        detector = LoopDetector(max_attempts=3)
        assert detector.detect("N1", 1) is False
        assert detector.detect("N1", 3) is False
        assert detector.detect("N1", 4) is True

    def test_detects_repeated_failure(self) -> None:
        detector = LoopDetector(same_failure_threshold=3)
        assert detector.record_failure("N1", "sig") is False
        assert detector.record_failure("N1", "sig") is False
        assert detector.record_failure("N1", "sig") is True

    def test_different_failures_not_flagged(self) -> None:
        detector = LoopDetector(same_failure_threshold=3)
        detector.record_failure("N1", "a")
        detector.record_failure("N1", "b")
        assert detector.record_failure("N1", "c") is False

    def test_detects_cyclic_path(self) -> None:
        """节点在回退路径中反复出现 → 判定循环。"""
        detector = LoopDetector(max_attempts=3)
        assert detector.record_path("N1") is False
        assert detector.record_path("N2") is False
        assert detector.record_path("N1") is False
        # 第三次出现 N1 → 超阈值
        assert detector.record_path("N1") is True

    def test_path_bounded(self) -> None:
        """路径长度必须有上限，防止内存无限增长。"""
        detector = LoopDetector(max_path_length=5)
        for i in range(50):
            detector.record_path(f"N{i}")
        assert len(detector.path()) <= 5

    def test_reset(self) -> None:
        detector = LoopDetector()
        detector.detect("N1", 5)
        detector.record_path("N1")
        detector.reset("N1")
        assert detector.attempts_for("N1") == 0

    def test_snapshot(self) -> None:
        detector = LoopDetector()
        detector.detect("N1", 2)
        detector.record_failure("N1", "sig")
        snap = detector.snapshot()
        assert snap["attempts"] == {"N1": 2}
        assert "N1" in snap["failure_signatures"]
