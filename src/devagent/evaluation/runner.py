"""评测运行器与报告。

把 golden set 跑一遍，产出可比较的报告。

## 关键设计：轨迹级指标

大多数评测只看最终输出，但 Agent 系统的价值恰恰在**过程**里：

- ``steps``：走了几步完成（效率）；
- ``tokens`` / ``cost``：花了多少（经济性）；
- ``context_savings``：上下文工程节省了多少（核心卖点的量化）；
- ``retries`` / ``backtracks``：可靠性事件的频率；
- ``first_pass_rate``：一次通过率（无回退即通过的比例）。

这些指标在「只换装配算法、其他不变」的 A/B 对比中才真正显示出价值，
也是面试里最有说服力的部分 —— 可以说「把 gamma 从 2 调到 4，冗余率下降
X%，而首轮通过率不变」。
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from devagent.evaluation.dataset import GoldenSample, GoldenSet
from devagent.evaluation.judge import JudgeResult, LLMJudge
from devagent.logging_config import get_logger

logger = get_logger(__name__)


class TaskRunner(Protocol):
    """执行一个 goal 并返回结果（便于注入假实现）。"""

    async def run(self, goal: str, *, task_id: str | None = None) -> Any: ...


@dataclass(slots=True)
class SampleOutcome:
    """单样本的评测结果。"""

    sample_id: str
    category: str
    goal: str

    # 执行层
    succeeded: bool = False
    status: str = ""
    duration_ms: int = 0
    steps: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""
    first_pass: bool = True
    """是否一次通过（全程无回退/重试）。"""

    # 上下文工程层（最能体现项目价值的指标）
    context_tokens_before: int = 0
    context_tokens_after: int = 0
    context_chunks_dropped: int = 0

    # 语义层
    judge: JudgeResult | None = None

    @property
    def context_savings_ratio(self) -> float:
        if self.context_tokens_before <= 0:
            return 0.0
        return 1.0 - (self.context_tokens_after / self.context_tokens_before)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sample_id": self.sample_id,
            "category": self.category,
            "goal": self.goal,
            "succeeded": self.succeeded,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "steps": self.steps,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6),
            "first_pass": self.first_pass,
            "context_savings_ratio": round(self.context_savings_ratio, 4),
            "context_tokens_before": self.context_tokens_before,
            "context_tokens_after": self.context_tokens_after,
            "context_chunks_dropped": self.context_chunks_dropped,
        }
        if self.error:
            out["error"] = self.error
        if self.judge is not None:
            out["judge"] = {
                "overall": round(self.judge.overall, 3),
                "passed": self.judge.passed,
                "inconsistent": self.judge.inconsistent,
                "scores": {s.dimension: round(s.score, 3) for s in self.judge.scores},
            }
        return out


@dataclass(slots=True)
class EvalReport:
    """整体评测报告。"""

    dataset_name: str
    started_at: float
    finished_at: float = 0.0
    outcomes: list[SampleOutcome] = field(default_factory=list)
    judge_model: str = ""
    candidate_model: str = ""
    judge_same_source: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    # ------------------------------------------------------------------ #
    # 聚合指标
    # ------------------------------------------------------------------ #

    @property
    def success_rate(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.succeeded) / len(self.outcomes)

    @property
    def first_pass_rate(self) -> float:
        """一次通过率：只统计执行成功的样本中无需回退的比例。

        分母排除执行失败的样本，否则「失败导致没机会回退」会虚假抬高该值。
        """
        succeeded = [o for o in self.outcomes if o.succeeded]
        if not succeeded:
            return 0.0
        return sum(1 for o in succeeded if o.first_pass) / len(succeeded)

    @property
    def judge_pass_rate(self) -> float:
        judged = [o for o in self.outcomes if o.judge is not None]
        if not judged:
            return 0.0
        assert all(o.judge is not None for o in judged)
        return sum(1 for o in judged if o.judge and o.judge.passed) / len(judged)

    @property
    def mean_judge_score(self) -> float:
        """按置信度加权的平均分（矛盾样本降权）。"""
        judged = [o.judge for o in self.outcomes if o.judge is not None]
        if not judged:
            return 0.0
        total_weight = sum(j.confidence for j in judged)
        if total_weight == 0:
            return 0.0
        weighted = sum(j.overall * j.confidence for j in judged)
        return weighted / total_weight

    @property
    def inconsistent_judge_rate(self) -> float:
        """裁判结论矛盾率 —— 衡量评测体系自身的可靠性。"""
        judged = [o for o in self.outcomes if o.judge is not None]
        if not judged:
            return 0.0
        return sum(1 for o in judged if o.judge and o.judge.inconsistent) / len(judged)

    @property
    def total_tokens(self) -> int:
        return sum(o.tokens for o in self.outcomes)

    @property
    def total_cost_usd(self) -> float:
        return sum(o.cost_usd for o in self.outcomes)

    @property
    def mean_context_savings(self) -> float:
        vals = [o.context_savings_ratio for o in self.outcomes if o.context_tokens_before > 0]
        return statistics.fmean(vals) if vals else 0.0

    @property
    def p50_latency_ms(self) -> float:
        vals = sorted(o.duration_ms for o in self.outcomes)
        if not vals:
            return 0.0
        return float(vals[len(vals) // 2])

    def by_category(self) -> dict[str, dict[str, float]]:
        groups: dict[str, list[SampleOutcome]] = defaultdict(list)
        for o in self.outcomes:
            groups[o.category].append(o)

        out: dict[str, dict[str, float]] = {}
        for cat, items in groups.items():
            judged = [i for i in items if i.judge is not None]
            out[cat] = {
                "count": float(len(items)),
                "success_rate": sum(1 for i in items if i.succeeded) / len(items),
                "first_pass_rate": (
                    sum(1 for i in items if i.succeeded and i.first_pass)
                    / max(1, sum(1 for i in items if i.succeeded))
                ),
                "mean_judge_score": (
                    statistics.fmean(i.judge.overall for i in judged if i.judge) if judged else 0.0
                ),
                "mean_tokens": statistics.fmean(i.tokens for i in items),
                "mean_context_savings": (
                    statistics.fmean(
                        i.context_savings_ratio for i in items if i.context_tokens_before > 0
                    )
                    if any(i.context_tokens_before > 0 for i in items)
                    else 0.0
                ),
            }
        return out

    def discrimination(self) -> float:
        """区分度：裁判分数的标准差。

        若接近 0，说明裁判对所有样本打同样的分（分数聚集），
        此时通过率再高也说明不了系统变好了。这是评测体系的自检指标。
        """
        vals = [o.judge.overall for o in self.outcomes if o.judge is not None]
        if len(vals) < 2:
            return 0.0
        return statistics.pstdev(vals)

    def summary(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset_name,
            "total": self.total,
            "success_rate": round(self.success_rate, 4),
            "first_pass_rate": round(self.first_pass_rate, 4),
            "judge_pass_rate": round(self.judge_pass_rate, 4),
            "mean_judge_score": round(self.mean_judge_score, 4),
            "inconsistent_judge_rate": round(self.inconsistent_judge_rate, 4),
            "discrimination": round(self.discrimination(), 4),
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "mean_context_savings": round(self.mean_context_savings, 4),
            "p50_latency_ms": self.p50_latency_ms,
            "duration_s": round(self.finished_at - self.started_at, 2),
            "judge_model": self.judge_model,
            "candidate_model": self.candidate_model,
            "judge_same_source": self.judge_same_source,
            "categories": self.by_category(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "outcomes": [o.to_dict() for o in self.outcomes],
            "metadata": self.metadata,
        }

    def save(self, path: str | Path) -> Path:
        import json

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("report_saved", path=str(p))
        return p


class EvalRunner:
    """评测运行器。

    用法::

        runner = EvalRunner(task_runner=orchestrator, judge=judge)
        report = await runner.run(golden_set)
        report.save("reports/eval/run.json")
    """

    def __init__(
        self,
        *,
        task_runner: TaskRunner,
        judge: LLMJudge | None = None,
        collect_context_metrics: bool = True,
    ) -> None:
        self._task_runner = task_runner
        self._judge = judge
        self._collect_context = collect_context_metrics

    async def run(
        self,
        dataset: GoldenSet,
        *,
        categories: list[str] | None = None,
        max_samples: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EvalReport:
        samples: list[GoldenSample] = list(dataset)
        if categories:
            wanted = set(categories)
            samples = [s for s in samples if s.category in wanted]
        if max_samples is not None:
            samples = samples[:max_samples]

        report = EvalReport(
            dataset_name=dataset.name,
            started_at=time.time(),
            judge_model=getattr(self._judge, "_model", "") if self._judge else "",
            candidate_model=getattr(self._judge, "_candidate_model", "") if self._judge else "",
            judge_same_source=bool(self._judge and self._judge.same_source_as_candidate),
            metadata=metadata or {},
        )

        for sample in samples:
            outcome = await self._evaluate_one(sample)
            report.outcomes.append(outcome)

        report.finished_at = time.time()
        logger.info(
            "eval_finished",
            dataset=dataset.name,
            samples=len(samples),
            success_rate=round(report.success_rate, 3),
            judge_score=round(report.mean_judge_score, 3),
        )
        return report

    async def _evaluate_one(self, sample: GoldenSample) -> SampleOutcome:
        outcome = SampleOutcome(
            sample_id=sample.id,
            category=sample.category,
            goal=sample.goal,
        )

        before_snapshot = self._metrics_snapshot()
        start = time.perf_counter()
        try:
            result = await self._task_runner.run(sample.goal, task_id=f"eval-{sample.id}")
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.status = "error"
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)
            logger.warning("eval_sample_error", sample=sample.id, error=outcome.error)
            return outcome

        outcome.duration_ms = int((time.perf_counter() - start) * 1000)
        outcome.succeeded = bool(getattr(result, "succeeded", False))
        outcome.status = str(getattr(getattr(result, "status", None), "value", ""))
        outcome.steps = len(getattr(result, "steps", []) or [])
        outcome.tokens = int(getattr(result, "total_tokens", 0) or 0)
        outcome.cost_usd = float(getattr(result, "total_cost_usd", 0.0) or 0.0)
        outcome.error = str(getattr(result, "error", "") or "")
        outcome.first_pass = self._is_first_pass(result)

        if self._collect_context:
            self._fill_context_metrics(outcome, before_snapshot)

        if self._judge is not None:
            candidate = self._collect_candidate_text(result)
            outcome.judge = await self._judge.evaluate(
                goal=sample.goal,
                criteria=sample.expected_criteria or ["完成用户目标"],
                candidate=candidate,
            )

        return outcome

    @staticmethod
    def _is_first_pass(result: Any) -> bool:
        """判断是否一次通过。

        依据：没有节点经历 attempt > 1。用 DAG 状态而非日志，避免日志级别
        变化影响指标（指标不应依赖日志）。
        """
        dag = getattr(result, "dag", None)
        if dag is None:
            return True
        states = getattr(dag, "states", {}) or {}
        return all(getattr(s, "attempt", 1) <= 1 for s in states.values())

    @staticmethod
    def _collect_candidate_text(result: Any) -> str:
        """把最终产出拼成裁判看的文本。"""
        steps = getattr(result, "steps", None) or []
        if not steps:
            return str(getattr(result, "error", "") or "（无产出）")
        chunks: list[str] = []
        for step in steps[-3:]:  # 只取最后几步：前面的是过程，最终方案才是被评对象
            agent = getattr(getattr(step, "agent", None), "value", "")
            output = str(getattr(step, "output", ""))
            chunks.append(f"### [{agent}] {output[:2000]}")
        return "\n\n".join(chunks)

    def _metrics_snapshot(self) -> dict[str, float]:
        from devagent.observability import MetricNames, get_observability

        obs = get_observability()
        return {
            "saved": obs.metrics.total(MetricNames.CONTEXT_TOKENS_SAVED),
            "dropped": obs.metrics.total(MetricNames.CONTEXT_CHUNKS_DROPPED),
            "input": obs.metrics.total(MetricNames.LLM_TOKENS, direction="input"),
        }

    def _fill_context_metrics(self, outcome: SampleOutcome, before: dict[str, float]) -> None:
        """把「本次样本」消耗的上下文指标差值填入结果。

        这里用**指标差值**而不是直接读 bundle，原因是一次任务会有多次
        ``build()``（每个 Agent 一次），只有聚合口径才能反映该样本的真实成本。

        绝对值不可得（指标只记节省量），因此把 ``saved`` 记为 before、
        ``0`` 记为 after 是不对的 —— 那会让 savings_ratio 恒为 1。
        正确做法：把节省量与同期的总输入用量一起还原出比例：

            before = saved + (同期实际送入的 token)
            after  = 同期实际送入的 token
        """
        from devagent.observability import MetricNames, get_observability

        obs = get_observability()
        saved = obs.metrics.total(MetricNames.CONTEXT_TOKENS_SAVED) - before["saved"]
        dropped = obs.metrics.total(MetricNames.CONTEXT_CHUNKS_DROPPED) - before["dropped"]
        delivered = obs.metrics.total(MetricNames.LLM_TOKENS, direction="input") - before["input"]

        outcome.context_chunks_dropped = int(dropped)
        if saved > 0:
            outcome.context_tokens_before = int(saved + delivered)
            outcome.context_tokens_after = int(delivered)
        else:
            outcome.context_tokens_before = int(delivered)
            outcome.context_tokens_after = int(delivered)


__all__ = ["EvalReport", "EvalRunner", "SampleOutcome", "TaskRunner"]
