"""异构裁判与偏差标定测试。

## 测试策略

裁判的"意见"是外部输入，因此这里注入一个**可编程的假裁判后端**：
可以让它按模型名返回不同分数，从而精确构造出：

- 同源裁判偏高（模拟 self-preference）
- 同源裁判偏低（反向偏差，验证不假设方向）
- 分数完全一致（验证偏差为 0 时不乱扣）

这样测的是**校正逻辑**，而不依赖任何真实模型的偏好行为 ——
后者是模型特性，无法也不应由单测断言。
"""

from __future__ import annotations

from typing import Any

import pytest

from devagent.evaluation.heterogeneous import (
    CalibrationResult,
    JudgePanel,
    is_heterogeneous,
    model_family,
)
from devagent.evaluation.judge import LLMJudge
from devagent.models.provider import ChatMessage

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------- #
# 测试替身
# ---------------------------------------------------------------------- #


class FakeJudgeBackend:
    """按 model 名返回可编程分数。

    ``scores`` 映射 ``model → overall``；未命中时用 ``default``。
    生成合法的裁判 JSON（含四个维度），让 ``LLMJudge._parse`` 正常走通。
    """

    def __init__(self, scores: dict[str, float], *, default: float = 3.0) -> None:
        self.scores = scores
        self.default = default
        self.calls: list[str] = []

    async def complete(self, messages: list[ChatMessage], *, model: str) -> str:
        self.calls.append(model)
        score = self.scores.get(model, self.default)
        dims = ", ".join(
            f'{{"dimension": "{d}", "score": {score}, "reason": "fake"}}'
            for d in ("correctness", "completeness", "conciseness", "actionability")
        )
        return f'{{"scores": [{dims}], "overall": {score}}}'


def _judge(backend: FakeJudgeBackend, model: str, *, threshold: float = 3.5) -> LLMJudge:
    return LLMJudge(backend, model=model, pass_threshold=threshold, bidirectional=False)


SAMPLE = (
    "给用户列表加分页",
    ["响应体包含 total 字段", "支持 page 与 size 参数"],
    "已实现分页，返回 total。",
)


# ---------------------------------------------------------------------- #
# 模型族判定
# ---------------------------------------------------------------------- #


class TestModelFamily:
    def test_same_family_different_names(self) -> None:
        """deepseek-chat 与 deepseek-reasoner 名字不同但同族。

        这是本模块最容易写错、也最致命的一点：把同族误判为异构，
        会让"异构裁判"这个说法变成自欺欺人。
        """
        assert model_family("deepseek:deepseek-chat") == "deepseek"
        assert model_family("deepseek:deepseek-reasoner") == "deepseek"

    def test_cross_family(self) -> None:
        assert model_family("qwen:qwen-plus") == "qwen"
        assert model_family("zhipu:glm-4-air") == "zhipu"

    def test_unknown_model_falls_back_to_provider(self) -> None:
        """未知模型名退回 provider —— 跨 provider 几乎必然跨族，保守且安全。"""
        assert model_family("someprovider:some-model") == "someprovider"

    def test_bare_model_without_provider(self) -> None:
        assert model_family("deepseek-chat") == "deepseek"

    def test_heterogeneous_judgement(self) -> None:
        assert is_heterogeneous("qwen:qwen-plus", "deepseek:deepseek-chat") is True
        assert is_heterogeneous("deepseek:deepseek-chat", "deepseek:deepseek-chat") is False
        # 同族不同模型 → 不算异构（关键）
        assert is_heterogeneous("deepseek:deepseek-reasoner", "deepseek:deepseek-chat") is False

    def test_missing_models_are_not_heterogeneous(self) -> None:
        """信息缺失时保守判定为"同源" —— 宁可多提示风险，不可漏掉。"""
        assert is_heterogeneous("", "deepseek:deepseek-chat") is False
        assert is_heterogeneous("qwen:qwen-plus", "") is False


# ---------------------------------------------------------------------- #
# 标定
# ---------------------------------------------------------------------- #


class TestCalibration:
    async def test_detects_positive_bias(self) -> None:
        """同源裁判偏高 → bias 为正。这是 self-preference 的典型方向。"""
        primary = _judge(
            FakeJudgeBackend({"deepseek:deepseek-chat": 4.5}), "deepseek:deepseek-chat"
        )
        reference = _judge(FakeJudgeBackend({"qwen:qwen-plus": 3.5}), "qwen:qwen-plus")
        panel = JudgePanel(
            primary=primary,
            reference=reference,
            candidate_model="deepseek:deepseek-chat",
        )

        calib = await panel.calibrate([SAMPLE] * 25)
        assert calib is not None
        assert calib.bias == pytest.approx(1.0)
        assert calib.trustworth is True

    async def test_detects_negative_bias(self) -> None:
        """不假设偏差方向：同源裁判也可能偏低。"""
        primary = _judge(FakeJudgeBackend({"a:judge": 3.0}), "a:judge")
        reference = _judge(FakeJudgeBackend({"b:ref": 4.0}), "b:ref")
        panel = JudgePanel(primary=primary, reference=reference, candidate_model="a:cand")

        calib = await panel.calibrate([SAMPLE] * 25)
        assert calib is not None
        assert calib.bias == pytest.approx(-1.0)

    async def test_zero_bias_when_agreeing(self) -> None:
        primary = _judge(FakeJudgeBackend({"a:judge": 4.0}), "a:judge")
        reference = _judge(FakeJudgeBackend({"b:ref": 4.0}), "b:ref")
        panel = JudgePanel(primary=primary, reference=reference, candidate_model="a:cand")

        calib = await panel.calibrate([SAMPLE] * 25)
        assert calib is not None
        assert calib.bias == pytest.approx(0.0)

    async def test_insufficient_samples_not_trustworthy(self) -> None:
        """样本太少时偏差估计本身就是噪声，不能用于扣减。

        取 n=3：此时标准误约为 sigma/sqrt(3) ≈ 0.46，
        这比我们要测的偏差本身还大 —— 拿它去扣分等于随机调分。
        """
        primary = _judge(FakeJudgeBackend({"a:judge": 4.5}), "a:judge")
        reference = _judge(FakeJudgeBackend({"b:ref": 3.0}), "b:ref")
        panel = JudgePanel(primary=primary, reference=reference, candidate_model="a:cand")

        calib = await panel.calibrate([SAMPLE] * 3)
        assert calib is not None
        assert calib.sample_count == 3
        assert calib.trustworth is False

    async def test_calibration_without_reference_returns_none(self) -> None:
        primary = _judge(FakeJudgeBackend({"a:judge": 4.0}), "a:judge")
        panel = JudgePanel(primary=primary, candidate_model="a:cand")
        assert await panel.calibrate([SAMPLE] * 25) is None

    async def test_calibration_with_empty_samples(self) -> None:
        primary = _judge(FakeJudgeBackend({"a:judge": 4.0}), "a:judge")
        reference = _judge(FakeJudgeBackend({"b:ref": 3.0}), "b:ref")
        panel = JudgePanel(primary=primary, reference=reference, candidate_model="a:cand")
        assert await panel.calibrate([]) is None

    async def test_calibration_uses_same_samples_for_both(self) -> None:
        """两次评估必须跑在同一批样本上，否则测的是样本难度差而非裁判偏差。"""
        primary_backend = FakeJudgeBackend({"a:judge": 4.0})
        reference_backend = FakeJudgeBackend({"b:ref": 3.0})
        panel = JudgePanel(
            primary=_judge(primary_backend, "a:judge"),
            reference=_judge(reference_backend, "b:ref"),
            candidate_model="a:cand",
        )

        await panel.calibrate([SAMPLE] * 25)
        # 双向关闭 → 每个样本各调一次
        assert len(primary_backend.calls) == 25
        assert len(reference_backend.calls) == 25
        assert set(primary_backend.calls) == {"a:judge"}
        assert set(reference_backend.calls) == {"b:ref"}


# ---------------------------------------------------------------------- #
# 校正与标记
# ---------------------------------------------------------------------- #


class TestCorrection:
    async def test_heterogeneous_judge_needs_no_correction(self) -> None:
        """裁判本身已异构 → 直接采信，不扣分。"""
        backend = FakeJudgeBackend({"qwen:qwen-plus": 4.0})
        panel = JudgePanel(
            primary=_judge(backend, "qwen:qwen-plus"),
            candidate_model="deepseek:deepseek-chat",
        )

        result = await panel.evaluate(goal=SAMPLE[0], criteria=SAMPLE[1], candidate=SAMPLE[2])
        assert result.overall == pytest.approx(4.0)
        assert result.raw["judge_panel"]["judge_relation"] == "heterogeneous"
        assert "calibrated" not in result.raw["judge_panel"]

    async def test_calibrated_self_judge_score_is_discounted(self) -> None:
        """同源裁判 + 已标定 → 按偏差扣减。

        裁判给 4.5，标定偏差 1.0 → 校正后 3.5。
        """
        panel = JudgePanel(
            primary=_judge(
                FakeJudgeBackend({"deepseek:deepseek-chat": 4.5}), "deepseek:deepseek-chat"
            ),
            reference=_judge(FakeJudgeBackend({"qwen:qwen-plus": 3.5}), "qwen:qwen-plus"),
            candidate_model="deepseek:deepseek-chat",
        )
        await panel.calibrate([SAMPLE] * 25)

        result = await panel.evaluate(goal=SAMPLE[0], criteria=SAMPLE[1], candidate=SAMPLE[2])
        assert result.overall == pytest.approx(3.5)
        assert result.raw["judge_panel"]["calibrated"] is True
        assert result.raw["judge_panel"]["bias_applied"] == pytest.approx(1.0)

    async def test_correction_updates_every_dimension(self) -> None:
        """分项也必须同幅度扣减，不能只改总分 —— 否则分项与总分矛盾。"""
        panel = JudgePanel(
            primary=_judge(FakeJudgeBackend({"a:judge": 5.0}), "a:judge"),
            reference=_judge(FakeJudgeBackend({"b:ref": 3.0}), "b:ref"),
            candidate_model="a:cand",
        )
        await panel.calibrate([SAMPLE] * 25)

        result = await panel.evaluate(goal=SAMPLE[0], criteria=SAMPLE[1], candidate=SAMPLE[2])
        assert result.overall == pytest.approx(3.0)
        for s in result.scores:
            assert s.score == pytest.approx(3.0), f"{s.dimension} 未同步扣减"

    async def test_correction_recomputes_passed(self) -> None:
        """扣减后必须重算 passed，否则出现"分数低于阈值但仍算通过"的矛盾。"""
        # 裁判给 4.0，阈值 3.5 → 原本通过；标定偏差 1.0 → 校正后 3.0 → 不通过
        panel = JudgePanel(
            primary=_judge(FakeJudgeBackend({"a:judge": 4.0}), "a:judge", threshold=3.5),
            reference=_judge(FakeJudgeBackend({"b:ref": 3.0}), "b:ref"),
            candidate_model="a:cand",
        )
        await panel.calibrate([SAMPLE] * 25)

        result = await panel.evaluate(goal=SAMPLE[0], criteria=SAMPLE[1], candidate=SAMPLE[2])
        assert result.overall == pytest.approx(3.0)
        assert result.passed is False
        assert result.overall < 3.5

    async def test_correction_clamps_at_zero(self) -> None:
        """扣减不能把分数打到负数。

        构造方式：用高分样本标定出较大偏差，再用**低分**样本评估。
        此时 ``score - bias`` 会为负，必须被夹到 0。

        （若标定与评估用同一批样本，``score - bias`` 恒等于参考裁判的均值，
        永远不会为负 —— 因此必须让两者分布不同才能真正覆盖截断分支。）
        """
        backend = FakeJudgeBackend({"a:judge": 5.0}, default=5.0)
        panel = JudgePanel(
            primary=_judge(backend, "a:judge"),
            reference=_judge(FakeJudgeBackend({"b:ref": 3.0}), "b:ref"),
            candidate_model="a:cand",
        )
        await panel.calibrate([SAMPLE] * 25)  # 偏差 = 5.0 - 3.0 = 2.0

        # 改成低分输出后再评估 → 1.0 - 2.0 = -1.0 → 应夹到 0
        backend.scores["a:judge"] = 1.0
        backend.default = 1.0

        result = await panel.evaluate(goal=SAMPLE[0], criteria=SAMPLE[1], candidate=SAMPLE[2])
        assert result.overall == 0.0
        assert all(s.score >= 0.0 for s in result.scores)

    async def test_zero_bias_leaves_score_untouched(self) -> None:
        """偏差为 0 时不该引入任何数值扰动（浮点也要精确）。"""
        panel = JudgePanel(
            primary=_judge(FakeJudgeBackend({"a:judge": 4.0}), "a:judge"),
            reference=_judge(FakeJudgeBackend({"b:ref": 4.0}), "b:ref"),
            candidate_model="a:cand",
        )
        await panel.calibrate([SAMPLE] * 25)

        result = await panel.evaluate(goal=SAMPLE[0], criteria=SAMPLE[1], candidate=SAMPLE[2])
        assert result.overall == pytest.approx(4.0)

    async def test_negative_bias_raises_score(self) -> None:
        """偏差为负 → 同源裁判偏低 → 向上校正。不假设偏差方向。"""
        panel = JudgePanel(
            primary=_judge(FakeJudgeBackend({"a:judge": 3.0}), "a:judge"),
            reference=_judge(FakeJudgeBackend({"b:ref": 4.0}), "b:ref"),
            candidate_model="a:cand",
        )
        await panel.calibrate([SAMPLE] * 25)

        result = await panel.evaluate(goal=SAMPLE[0], criteria=SAMPLE[1], candidate=SAMPLE[2])
        assert result.overall == pytest.approx(4.0)


# ---------------------------------------------------------------------- #
# 未标定的同源裁判（默认配置下的实际路径）
# ---------------------------------------------------------------------- #


class TestUncalibratedSelfJudge:
    async def test_marked_not_silently_corrected(self) -> None:
        """无标定时**不扣分**，但必须显式标记风险。

        这是刻意的设计：用一个猜出来的常数去扣分，会让分数看起来
        已经被修正过，从而掩盖问题。宁可如实标注，也不要假装校正了。
        """
        panel = JudgePanel(
            primary=_judge(
                FakeJudgeBackend({"deepseek:deepseek-chat": 5.0}), "deepseek:deepseek-chat"
            ),
            candidate_model="deepseek:deepseek-chat",
        )

        result = await panel.evaluate(goal=SAMPLE[0], criteria=SAMPLE[1], candidate=SAMPLE[2])
        assert result.overall == pytest.approx(5.0), "未标定时不应改动分数"
        tag = result.raw["judge_panel"]
        assert tag["judge_relation"] == "self"
        assert tag["calibrated"] is False
        assert tag["uncalibrated_self_preference"] is True
        assert "REFERENCE_JUDGE_MODEL" in tag["hint"]

    async def test_insufficient_calibration_still_marked_uncalibrated(self) -> None:
        """标定样本不足 → 视同未标定，而不是勉强用上去。"""
        panel = JudgePanel(
            primary=_judge(FakeJudgeBackend({"a:judge": 5.0}), "a:judge"),
            reference=_judge(FakeJudgeBackend({"b:ref": 3.0}), "b:ref"),
            candidate_model="a:cand",
        )
        await panel.calibrate([SAMPLE] * 2)  # 样本不足

        result = await panel.evaluate(goal=SAMPLE[0], criteria=SAMPLE[1], candidate=SAMPLE[2])
        assert result.overall == pytest.approx(5.0), "样本不足时不得扣分"
        assert result.raw["judge_panel"]["uncalibrated_self_preference"] is True

    async def test_default_config_is_flagged(self) -> None:
        """默认配置（judge 与候选都是 deepseek-chat）必须被标记为未标定同源。

        这正是开箱即用的实际情形 —— 如果这个标记不出现，
        说明整套偏差防护在最常见的配置下是失效的。
        """
        panel = JudgePanel(
            primary=_judge(
                FakeJudgeBackend({"deepseek:deepseek-chat": 4.0}), "deepseek:deepseek-chat"
            ),
            candidate_model="deepseek:deepseek-chat",
        )
        assert panel.primary_is_heterogeneous is False

        result = await panel.evaluate(goal=SAMPLE[0], criteria=SAMPLE[1], candidate=SAMPLE[2])
        assert result.raw["judge_panel"]["uncalibrated_self_preference"] is True


# ---------------------------------------------------------------------- #
# 诊断信息不污染结果
# ---------------------------------------------------------------------- #


class TestDiagnosticsAreNonInvasive:
    async def test_judge_panel_key_not_in_plain_judge(self) -> None:
        """普通 LLMJudge 的结果不应凭空多出 judge_panel 键。"""
        judge = _judge(FakeJudgeBackend({"m": 4.0}), "m")
        result = await judge.evaluate(goal="g", criteria=["c"], candidate="x")
        assert "judge_panel" not in result.raw

    async def test_tagging_preserves_scores_and_verdict(self) -> None:
        panel = JudgePanel(
            primary=_judge(FakeJudgeBackend({"a:judge": 4.0}), "a:judge"),
            candidate_model="b:cand",
        )
        result = await panel.evaluate(goal="g", criteria=["c"], candidate="x")
        assert result.overall == pytest.approx(4.0)
        assert result.passed is True
        assert len(result.scores) == 4

    async def test_tagging_does_not_mutate_original_raw(self) -> None:
        """附加信息必须是新 dict，不能就地修改原结果（避免共享状态污染）。"""
        judge = _judge(FakeJudgeBackend({"a:judge": 4.0}), "a:judge")
        original = await judge.evaluate(goal="g", criteria=["c"], candidate="x")
        original_keys = set(original.raw)

        panel = JudgePanel(primary=judge, candidate_model="b:cand")
        # 直接调用内部标记逻辑所走的外层路径
        await panel.evaluate(goal="g", criteria=["c"], candidate="x")

        # 原始结果的 raw 键集合不应被改变
        fresh = await judge.evaluate(goal="g", criteria=["c"], candidate="x")
        assert set(fresh.raw) == original_keys


# ---------------------------------------------------------------------- #
# CalibrationResult 的自洽性
# ---------------------------------------------------------------------- #


class TestCalibrationResult:
    def test_relative_bias(self) -> None:
        calib = CalibrationResult(
            judge_model="j",
            reference_model="r",
            sample_count=30,
            mean_judge=4.5,
            mean_reference=3.0,
        )
        assert calib.bias == pytest.approx(1.5)
        assert calib.relative_bias == pytest.approx(0.5)

    def test_relative_bias_no_division_by_zero(self) -> None:
        calib = CalibrationResult(
            judge_model="j",
            reference_model="r",
            sample_count=30,
            mean_judge=0.5,
            mean_reference=0.0,
        )
        assert calib.relative_bias == 0.0

    def test_to_dict_is_serializable(self) -> None:
        calib = CalibrationResult(
            judge_model="j",
            reference_model="r",
            sample_count=25,
            mean_judge=4.0,
            mean_reference=3.0,
        )
        data = calib.to_dict()
        assert set(data) == {
            "judge_model",
            "reference_model",
            "sample_count",
            "mean_judge",
            "mean_reference",
            "bias",
            "relative_bias",
            "reliable",
        }
        assert data["bias"] == pytest.approx(1.0)
        assert data["reliable"] is True

    def test_unreliable_when_few_samples(self) -> None:
        calib = CalibrationResult(
            judge_model="j", reference_model="r", sample_count=5, mean_judge=4.0, mean_reference=3.0
        )
        assert calib.trustworth is False


# ---------------------------------------------------------------------- #
# 工厂（CLI 与 API 共用，必须构造出相同结果）
# ---------------------------------------------------------------------- #


class TestBuildJudge:
    """工厂测试。

    这个工厂存在的唯一理由是「CLI 与 API 构造出完全相同的裁判」——
    否则两个入口的评测结论不可比较，评测体系就失去意义。
    因此这里验证的核心是**配置 → 行为**的映射是否如实。
    """

    def test_builds_panel_with_heterogeneous_reference(self) -> None:
        from devagent.config import Settings
        from devagent.evaluation.factory import build_judge

        settings = Settings()
        settings.evaluation.judge_model = "deepseek:deepseek-chat"
        settings.evaluation.reference_judge_model = "qwen:qwen-plus"

        panel = build_judge(FakeGateway(), settings, candidate_model="deepseek:deepseek-chat")
        assert panel is not None
        assert panel.reference is not None
        assert panel.primary_is_heterogeneous is False

    def test_same_family_reference_is_rejected(self) -> None:
        """参考裁判与主裁判同族 → 不构造。

        同族参考裁判只能算出一个恒为 0 的偏差（自己减自己），
        提供它比不提供更糟：会让报告看起来"已经做过标定"。
        """
        from devagent.config import Settings
        from devagent.evaluation.factory import build_judge

        settings = Settings()
        settings.evaluation.judge_model = "deepseek:deepseek-chat"
        settings.evaluation.reference_judge_model = "deepseek:deepseek-reasoner"

        panel = build_judge(FakeGateway(), settings, candidate_model="deepseek:deepseek-chat")
        assert panel is not None
        assert panel.reference is None

    def test_empty_reference_disables_calibration(self) -> None:
        from devagent.config import Settings
        from devagent.evaluation.factory import build_judge

        settings = Settings()
        settings.evaluation.reference_judge_model = ""

        panel = build_judge(FakeGateway(), settings, candidate_model="deepseek:deepseek-chat")
        assert panel is not None
        assert panel.reference is None

    def test_heterogeneous_primary_needs_no_reference(self) -> None:
        from devagent.config import Settings
        from devagent.evaluation.factory import build_judge

        settings = Settings()
        settings.evaluation.judge_model = "qwen:qwen-max"
        settings.evaluation.reference_judge_model = ""

        panel = build_judge(FakeGateway(), settings, candidate_model="deepseek:deepseek-chat")
        assert panel is not None
        assert panel.primary_is_heterogeneous is True

    def test_judge_model_recorded_in_panel(self) -> None:
        from devagent.config import Settings
        from devagent.evaluation.factory import build_judge

        settings = Settings()
        settings.evaluation.judge_model = "zhipu:glm-4-plus"

        panel = build_judge(FakeGateway(), settings, candidate_model="deepseek:deepseek-chat")
        assert panel is not None
        assert panel._primary_model == "zhipu:glm-4-plus"


class FakeGateway:
    """极小网关替身：工厂只把它透传给后端，不会真的调用。"""

    async def chat(self, messages: Any, **kwargs: Any) -> Any:
        raise AssertionError("工厂构造阶段不应发起任何模型调用")


class TestCliCalibrationOutput:
    """CLI 的偏差输出。

    这段文案本身就是结论的一部分：同源且未标定时必须**显式**提醒
    "分数可能系统性偏高"，否则用户会把一个偏高的数字当成可信结论。
    """

    def _capture(self, summary: dict[str, Any]) -> str:
        import io
        from contextlib import redirect_stdout

        from devagent.cli import _print_calibration

        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_calibration(summary)
        return buf.getvalue()

    def test_heterogeneous_judge_prints_nothing(self) -> None:
        """裁判已异构 → 无需标定，也无需刷屏。"""
        out = self._capture({"judge_same_source": False, "calibration": {}})
        assert out == ""

    def test_same_source_without_calibration_warns(self) -> None:
        out = self._capture({"judge_same_source": True, "calibration": {}})
        assert "未标定" in out
        assert "系统性偏高" in out

    def test_unreliable_calibration_says_scores_uncorrected(self) -> None:
        """样本不足时必须说清"分数未校正"，不能只报一个 bias 数字。

        否则用户看到 bias=2.0 会以为分数已经被减过了 —— 而实际没有。
        """
        out = self._capture(
            {
                "judge_same_source": True,
                "calibration": {"sample_count": 5, "bias": 2.0, "reliable": False},
            }
        )
        assert "样本不足" in out
        assert "未校正" in out

    def test_reliable_calibration_shows_signed_bias_and_reference(self) -> None:
        out = self._capture(
            {
                "judge_same_source": True,
                "calibration": {
                    "sample_count": 25,
                    "bias": 0.42,
                    "reliable": True,
                    "reference_model": "qwen:qwen-plus",
                },
            }
        )
        assert "+0.42" in out  # 带符号：方向本身就是信息
        assert "已校正" in out
        assert "qwen:qwen-plus" in out

    def test_negative_bias_is_signed(self) -> None:
        """偏差可能为负（裁判比参考更严格），符号不能丢。"""
        out = self._capture(
            {
                "judge_same_source": True,
                "calibration": {
                    "sample_count": 25,
                    "bias": -0.3,
                    "reliable": True,
                    "reference_model": "qwen:qwen-plus",
                },
            }
        )
        assert "-0.30" in out

    def test_missing_calibration_key_is_safe(self) -> None:
        """报告可能来自旧版本，缺少 calibration 字段时不得抛异常。"""
        assert self._capture({}) == ""
