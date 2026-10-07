"""评测体系测试。

重点验证三类容易出错的地方：
1. 数据集的**容错边界**（坏行、重复 id、缺字段）；
2. 裁判的**去偏机制真的生效**（矛盾检测、位置交换）；
3. 报告指标的**定义正确性**（first_pass 的分母、加权平均、区分度）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from devagent.evaluation import (
    DIMENSIONS,
    DatasetError,
    EvalReport,
    EvalRunner,
    GoldenSample,
    GoldenSet,
    LLMJudge,
)
from devagent.evaluation.judge import JudgeResult
from devagent.evaluation.runner import SampleOutcome
from devagent.models.provider import ChatMessage

# ---------------------------------------------------------------------- #
# 数据集
# ---------------------------------------------------------------------- #


class TestGoldenSample:
    def test_from_dict_requires_id_category_goal(self) -> None:
        with pytest.raises(DatasetError, match="缺少必填字段"):
            GoldenSample.from_dict({"id": "a", "category": "c"})

    def test_roundtrip(self) -> None:
        raw = {
            "id": "a",
            "category": "req",
            "goal": "g",
            "expected_criteria": ["c1"],
            "forbidden_criteria": ["f1"],
            "tags": ["t"],
        }
        sample = GoldenSample.from_dict(raw)
        assert sample.to_dict() == raw

    def test_optional_fields_omitted_when_empty(self) -> None:
        sample = GoldenSample.from_dict({"id": "a", "category": "c", "goal": "g"})
        assert sample.to_dict() == {"id": "a", "category": "c", "goal": "g"}


class TestGoldenSet:
    def _raw(self, i: int, category: str = "req") -> dict[str, Any]:
        return {"id": f"s{i}", "category": category, "goal": f"goal {i}"}

    def test_by_category_and_tag(self) -> None:
        gs = GoldenSet.from_dicts(
            [
                {"id": "a", "category": "x", "goal": "g", "tags": ["t1"]},
                {"id": "b", "category": "y", "goal": "g", "tags": ["t2"]},
            ]
        )
        assert [s.id for s in gs.by_category("x")] == ["a"]
        assert [s.id for s in gs.by_tag("t2")] == ["b"]

    def test_categories_counts(self) -> None:
        gs = GoldenSet.from_dicts([self._raw(1, "a"), self._raw(2, "a"), self._raw(3, "b")])
        assert gs.categories == {"a": 2, "b": 1}

    def test_subset(self) -> None:
        gs = GoldenSet.from_dicts([self._raw(1), self._raw(2), self._raw(3)])
        sub = gs.subset({"s1", "s3"})
        assert [s.id for s in sub] == ["s1", "s3"]

    def test_duplicate_ids_rejected(self) -> None:
        with pytest.raises(DatasetError, match="id 重复"):
            GoldenSet.from_dicts([self._raw(1), self._raw(1)])

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        gs = GoldenSet.from_dicts([self._raw(1), self._raw(2, "other")], name="ds")
        path = tmp_path / "sub" / "out.jsonl"
        gs.save(path)
        loaded = GoldenSet.load(path)
        assert len(loaded) == 2
        assert loaded.categories == {"req": 1, "other": 1}

    def test_load_skips_comments_and_blanks(self, tmp_path: Path) -> None:
        path = tmp_path / "g.jsonl"
        path.write_text(
            '# 这是注释\n\n{"id": "a", "category": "c", "goal": "g"}\n\n',
            encoding="utf-8",
        )
        assert len(GoldenSet.load(path)) == 1

    def test_load_reports_lineno_on_bad_json(self, tmp_path: Path) -> None:
        path = tmp_path / "g.jsonl"
        path.write_text(
            '{"id": "a", "category": "c", "goal": "g"}\n{not json}\n',
            encoding="utf-8",
        )
        with pytest.raises(DatasetError, match="第 2 行"):
            GoldenSet.load(path)

    def test_load_reports_lineno_on_missing_field(self, tmp_path: Path) -> None:
        path = tmp_path / "g.jsonl"
        path.write_text('{"id": "a", "category": "c"}\n', encoding="utf-8")
        with pytest.raises(DatasetError, match="第 1 行"):
            GoldenSet.load(path)

    def test_load_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetError, match="不存在"):
            GoldenSet.load(tmp_path / "nope.jsonl")

    def test_load_real_golden_set(self) -> None:
        """项目自带的 golden set 必须可加载且格式合法。"""
        path = Path("datasets/golden_set.jsonl")
        if not path.exists():
            pytest.skip("golden set 不存在")
        gs = GoldenSet.load(path)
        assert len(gs) >= 10
        assert gs.categories
        # 必须包含检验过滤能力的样本
        assert any(s.forbidden_criteria for s in gs)
        # 必须覆盖歧义需求场景
        assert gs.by_category("edge_case")


# ---------------------------------------------------------------------- #
# LLM-as-Judge
# ---------------------------------------------------------------------- #


def _judge_payload(overall: float, scores: dict[str, float] | None = None) -> str:
    scores = scores or dict.fromkeys(DIMENSIONS, overall)
    return json.dumps(
        {
            "scores": [{"dimension": d, "score": s, "reason": "r"} for d, s in scores.items()],
            "overall": overall,
        }
    )


class _ScriptedJudgeBackend:
    """返回预设回复的裁判后端。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage], *, model: str) -> str:
        self.calls.append(messages)
        if not self.replies:
            return _judge_payload(0.0)
        return self.replies.pop(0)


class TestLLMJudge:
    async def test_single_pass_parses_scores(self) -> None:
        backend = _ScriptedJudgeBackend([_judge_payload(4.0)])
        judge = LLMJudge(backend, bidirectional=False)
        result = await judge.evaluate(goal="g", criteria=["c"], candidate="out")
        assert result.overall == 4.0
        assert result.passed is True
        assert {s.dimension for s in result.scores} == set(DIMENSIONS)

    async def test_bidirectional_averages_two_passes(self) -> None:
        backend = _ScriptedJudgeBackend([_judge_payload(4.0), _judge_payload(4.0)])
        judge = LLMJudge(backend, bidirectional=True)
        result = await judge.evaluate(goal="g", criteria=["c"], candidate="out")
        assert len(backend.calls) == 2
        assert result.overall == 4.0
        assert result.inconsistent is False

    async def test_bidirectional_flags_inconsistency(self) -> None:
        """两次评分差异大时必须标记矛盾——这是裁判不可信的信号。"""
        backend = _ScriptedJudgeBackend([_judge_payload(5.0), _judge_payload(1.0)])
        judge = LLMJudge(backend, bidirectional=True)
        result = await judge.evaluate(goal="g", criteria=["c"], candidate="out")
        assert result.inconsistent is True
        assert result.overall == 3.0
        assert result.confidence < 1.0

    async def test_bidirectional_prompts_differ(self) -> None:
        """正向/反向两次提问的内容必须不同，否则「双向」是假的。"""
        backend = _ScriptedJudgeBackend([_judge_payload(4.0), _judge_payload(4.0)])
        judge = LLMJudge(backend, bidirectional=True)
        await judge.evaluate(goal="g", criteria=["c"], candidate="out")
        first = backend.calls[0][1].content
        second = backend.calls[1][1].content
        assert first != second
        assert "优点" in first
        assert "缺陷" in second

    async def test_reference_swaps_position(self) -> None:
        """提供参考输出时，反向 pass 必须交换两者位置以抵消位置偏差。"""
        backend = _ScriptedJudgeBackend([_judge_payload(4.0), _judge_payload(4.0)])
        judge = LLMJudge(backend, bidirectional=True)
        await judge.evaluate(
            goal="g", criteria=["c"], candidate="CANDIDATE_TEXT", reference="REF_TEXT"
        )
        first, second = backend.calls[0][1].content, backend.calls[1][1].content
        assert first.index("REF_TEXT") < first.index("CANDIDATE_TEXT")
        assert second.index("CANDIDATE_TEXT") < second.index("REF_TEXT")

    async def test_pass_threshold(self) -> None:
        backend = _ScriptedJudgeBackend([_judge_payload(3.0)])
        judge = LLMJudge(backend, bidirectional=False, pass_threshold=3.5)
        result = await judge.evaluate(goal="g", criteria=["c"], candidate="out")
        assert result.passed is False

    async def test_parse_failure_is_low_score_not_crash(self) -> None:
        """解析失败必须如实记为 0 分不可信，绝不能抛异常中断整轮评测。"""
        backend = _ScriptedJudgeBackend(["这不是 JSON"])
        judge = LLMJudge(backend, bidirectional=False)
        result = await judge.evaluate(goal="g", criteria=["c"], candidate="out")
        assert result.overall == 0.0
        assert result.passed is False
        assert result.inconsistent is True

    async def test_parses_json_in_markdown_fence(self) -> None:
        backend = _ScriptedJudgeBackend([f"```json\n{_judge_payload(4.5)}\n```"])
        judge = LLMJudge(backend, bidirectional=False)
        result = await judge.evaluate(goal="g", criteria=["c"], candidate="out")
        assert result.overall == 4.5

    async def test_clamps_out_of_range_scores(self) -> None:
        payload = json.dumps({"scores": [{"dimension": "correctness", "score": 99}], "overall": 42})
        backend = _ScriptedJudgeBackend([payload])
        judge = LLMJudge(backend, bidirectional=False)
        result = await judge.evaluate(goal="g", criteria=["c"], candidate="out")
        assert result.overall == 5.0
        assert result.score_of("correctness") == 5.0

    async def test_overall_derived_when_missing(self) -> None:
        """未给总分时应由分项均值推导，避免总分与分项脱节。"""
        payload = json.dumps(
            {
                "scores": [
                    {"dimension": "correctness", "score": 4},
                    {"dimension": "completeness", "score": 2},
                ]
            }
        )
        backend = _ScriptedJudgeBackend([payload])
        judge = LLMJudge(backend, bidirectional=False)
        result = await judge.evaluate(goal="g", criteria=["c"], candidate="out")
        assert result.overall == 3.0

    def test_same_source_detection(self) -> None:
        judge_same = LLMJudge(
            _ScriptedJudgeBackend([]),
            model="deepseek:deepseek-chat",
            candidate_model="qwen:deepseek-chat",
        )
        assert judge_same.same_source_as_candidate is True
        judge_diff = LLMJudge(
            _ScriptedJudgeBackend([]),
            model="deepseek:deepseek-chat",
            candidate_model="deepseek:deepseek-reasoner",
        )
        assert judge_diff.same_source_as_candidate is False
        judge_unknown = LLMJudge(_ScriptedJudgeBackend([]), model="m")
        assert judge_unknown.same_source_as_candidate is False


# ---------------------------------------------------------------------- #
# EvalRunner
# ---------------------------------------------------------------------- #


class _FakeTaskResult:
    def __init__(
        self,
        *,
        succeeded: bool = True,
        status: str = "succeeded",
        tokens: int = 100,
        steps: int = 3,
        attempts: list[int] | None = None,
    ) -> None:
        from devagent.enums import TaskStatus

        self.succeeded = succeeded
        self.status = TaskStatus(status)
        self.total_tokens = tokens
        self.total_cost_usd = 0.001
        self.error = ""
        self.steps = [object()] * steps

        class _State:
            def __init__(self, attempt: int) -> None:
                self.attempt = attempt

        class _DAG:
            def __init__(self, atts: list[int]) -> None:
                self.states = {f"N{i}": _State(a) for i, a in enumerate(atts)}

        self.dag = _DAG(attempts if attempts is not None else [1])


class _FakeTaskRunner:
    def __init__(
        self,
        results: list[_FakeTaskResult] | None = None,
        fail_ids: set[str] | None = None,
    ) -> None:
        self.results = results or []
        self.fail_ids = fail_ids or set()
        self.call_count = 0

    async def run(self, goal: str, *, task_id: str | None = None) -> Any:
        self.call_count += 1
        if task_id and task_id in self.fail_ids:
            raise RuntimeError("simulated failure")
        if self.results:
            return self.results.pop(0)
        return _FakeTaskResult()


class TestEvalRunner:
    def _dataset(self, n: int = 3) -> GoldenSet:
        return GoldenSet.from_dicts(
            [{"id": f"s{i}", "category": "req", "goal": f"g{i}"} for i in range(n)],
            name="test",
        )

    async def test_runs_all_samples(self) -> None:
        runner = EvalRunner(task_runner=_FakeTaskRunner())
        report = await runner.run(self._dataset(3))
        assert report.total == 3
        assert all(o.succeeded for o in report.outcomes)

    async def test_max_samples(self) -> None:
        runner = EvalRunner(task_runner=_FakeTaskRunner())
        report = await runner.run(self._dataset(5), max_samples=2)
        assert report.total == 2

    async def test_category_filter(self) -> None:
        gs = GoldenSet.from_dicts(
            [
                {"id": "a", "category": "x", "goal": "g"},
                {"id": "b", "category": "y", "goal": "g"},
            ]
        )
        runner = EvalRunner(task_runner=_FakeTaskRunner())
        report = await runner.run(gs, categories=["x"])
        assert [o.sample_id for o in report.outcomes] == ["a"]

    async def test_exception_in_sample_does_not_abort_run(self) -> None:
        """单样本异常绝不能中断整轮评测——否则一次超时会让报告全废。"""
        runner = EvalRunner(task_runner=_FakeTaskRunner(fail_ids={"eval-s1"}))
        report = await runner.run(self._dataset(3))
        assert report.total == 3
        errored = [o for o in report.outcomes if o.error]
        assert len(errored) == 1
        assert errored[0].sample_id == "s1"
        assert "simulated failure" in errored[0].error

    async def test_first_pass_detection(self) -> None:
        """有节点 attempt>1 时必须判定为非一次通过。"""
        runner = EvalRunner(task_runner=_FakeTaskRunner([_FakeTaskResult(attempts=[1, 2])]))
        report = await runner.run(self._dataset(1))
        assert report.outcomes[0].first_pass is False

    async def test_success_rate_and_first_pass_rate(self) -> None:
        results = [
            _FakeTaskResult(succeeded=True, attempts=[1]),
            _FakeTaskResult(succeeded=False, status="failed", attempts=[1, 2]),
            _FakeTaskResult(succeeded=True, attempts=[1, 1]),
        ]
        runner = EvalRunner(task_runner=_FakeTaskRunner(results))
        report = await runner.run(self._dataset(3))
        assert report.success_rate == pytest.approx(2 / 3)
        # 分母是「成功样本数」= 2，其中 2 个都是一次通过
        assert report.first_pass_rate == pytest.approx(1.0)

    async def test_judge_integration(self) -> None:
        backend = _ScriptedJudgeBackend([_judge_payload(4.0)] * 6)
        judge = LLMJudge(backend, bidirectional=True)
        runner = EvalRunner(task_runner=_FakeTaskRunner(), judge=judge)
        report = await runner.run(self._dataset(2))
        assert report.judge_pass_rate == 1.0
        assert report.mean_judge_score == pytest.approx(4.0)

    async def test_report_save(self, tmp_path: Path) -> None:
        runner = EvalRunner(task_runner=_FakeTaskRunner())
        report = await runner.run(self._dataset(1))
        path = report.save(tmp_path / "r" / "out.json")
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["summary"]["total"] == 1
        assert len(data["outcomes"]) == 1


# ---------------------------------------------------------------------- #
# 报告指标
# ---------------------------------------------------------------------- #


class TestEvalReportMetrics:
    def _report(self, outcomes: list[SampleOutcome]) -> EvalReport:
        return EvalReport(
            dataset_name="d",
            started_at=0.0,
            finished_at=1.0,
            outcomes=outcomes,
        )

    def _outcome(self, **kwargs: Any) -> SampleOutcome:
        o = SampleOutcome(sample_id="s", category="c", goal="g")
        for k, v in kwargs.items():
            setattr(o, k, v)
        return o

    def test_empty_report_is_safe(self) -> None:
        r = self._report([])
        assert r.success_rate == 0.0
        assert r.first_pass_rate == 0.0
        assert r.mean_judge_score == 0.0
        assert r.discrimination() == 0.0

    def test_weighted_mean_judge_score_discounts_inconsistent(self) -> None:
        """矛盾样本必须被降权，否则不可信的评分会污染整体结论。"""
        good = JudgeResult(scores=(), overall=5.0, passed=True, inconsistent=False)
        bad = JudgeResult(scores=(), overall=1.0, passed=False, inconsistent=True)
        r = self._report(
            [
                self._outcome(judge=good),
                self._outcome(judge=bad),
            ]
        )
        # 简单平均 = 3.0；加权后应偏向高置信的 good，故 > 3.0
        assert r.mean_judge_score > 3.0

    def test_inconsistent_judge_rate(self) -> None:
        ok = JudgeResult(scores=(), overall=4.0, passed=True, inconsistent=False)
        flaky = JudgeResult(scores=(), overall=3.0, passed=True, inconsistent=True)
        r = self._report([self._outcome(judge=ok), self._outcome(judge=flaky)])
        assert r.inconsistent_judge_rate == pytest.approx(0.5)

    def test_discrimination_exposes_score_clustering(self) -> None:
        """所有样本同分时区分度应为 0，暴露裁判无效。"""
        same = [self._outcome(judge=JudgeResult((), 4.0, True)) for _ in range(5)]
        assert self._report(same).discrimination() == 0.0

        varied = [self._outcome(judge=JudgeResult((), s, True)) for s in (1.0, 3.0, 5.0)]
        assert self._report(varied).discrimination() > 0.0

    def test_by_category_breakdown(self) -> None:
        r = self._report(
            [
                self._outcome(category="a", succeeded=True, tokens=100),
                self._outcome(category="a", succeeded=False, tokens=200),
                self._outcome(category="b", succeeded=True, tokens=300),
            ]
        )
        cats = r.by_category()
        assert cats["a"]["count"] == 2
        assert cats["a"]["success_rate"] == pytest.approx(0.5)
        assert cats["a"]["mean_tokens"] == pytest.approx(150)
        assert cats["b"]["success_rate"] == 1.0

    def test_context_savings_ratio(self) -> None:
        o = self._outcome(context_tokens_before=1000, context_tokens_after=600)
        assert o.context_savings_ratio == pytest.approx(0.4)
        assert self._report([o]).mean_context_savings == pytest.approx(0.4)

    def test_context_savings_zero_when_no_before(self) -> None:
        o = self._outcome(context_tokens_before=0, context_tokens_after=0)
        assert o.context_savings_ratio == 0.0

    def test_summary_contains_all_keys(self) -> None:
        r = self._report([self._outcome(succeeded=True)])
        summary = r.summary()
        for key in (
            "success_rate",
            "first_pass_rate",
            "judge_pass_rate",
            "mean_judge_score",
            "discrimination",
            "mean_context_savings",
            "categories",
        ):
            assert key in summary, key

    def test_outcome_to_dict_omits_empty_error(self) -> None:
        o = self._outcome(succeeded=True)
        assert "error" not in o.to_dict()
        assert "judge" not in o.to_dict()

    def test_p50_latency(self) -> None:
        r = self._report([self._outcome(duration_ms=d) for d in (10, 20, 30, 40, 50)])
        assert r.p50_latency_ms == 30.0
