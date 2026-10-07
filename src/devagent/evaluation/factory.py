"""裁判装配工厂。

CLI 与 API 都要构造裁判，且必须**构造出完全相同的裁判** ——
否则用命令行跑出的结论与从网页跑的结论不可比较，评测就失去意义。

因此把装配逻辑集中在这里，两处调用同一个函数。
这是本项目反复使用的一条原则：**同一个决策只能有一处实现**。

## 默认配置下的诚实提示

默认 ``judge_model`` 与 ``candidate_model`` 很可能同族
（例如都是 deepseek），此时裁判在评自己的输出。工厂会在构造时
**主动打日志警告**，而不是让用户以为分数是可信的。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from devagent.evaluation.heterogeneous import JudgePanel, is_heterogeneous
from devagent.evaluation.judge import GatewayJudgeBackend, LLMJudge
from devagent.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from devagent.config import Settings

logger = get_logger(__name__)


def build_judge(
    gateway: Any,
    settings: Settings,
    *,
    candidate_model: str = "",
) -> JudgePanel | None:
    """按配置构造裁判面板。

    Args:
        gateway: ``ModelGateway``（用作裁判后端）。
        settings: 全局配置。
        candidate_model: 被测系统实际使用的模型（用于同源判定）。
            留空则只依据 ``judge_model`` 做诊断，不做同源标记。

    Returns:
        ``JudgePanel``；若配置要求禁用裁判则返回 ``None``。
    """
    cfg = settings.evaluation
    backend = GatewayJudgeBackend(gateway)

    primary = LLMJudge(
        backend,
        model=cfg.judge_model,
        candidate_model=candidate_model,
        bidirectional=cfg.enable_bidirectional_judge,
    )

    reference: LLMJudge | None = None
    if cfg.reference_judge_model:
        if is_heterogeneous(cfg.reference_judge_model, cfg.judge_model):
            reference = LLMJudge(
                backend,
                model=cfg.reference_judge_model,
                candidate_model=candidate_model,
                bidirectional=cfg.enable_bidirectional_judge,
            )
        else:
            # 配了一个与主裁判同族的"参考裁判"是无效配置：
            # 它无法提供独立的比较基准，标定出来的偏差恒为 0（自己减自己）。
            logger.warning(
                "reference_judge_same_family",
                reference_model=cfg.reference_judge_model,
                judge_model=cfg.judge_model,
                hint="参考裁判必须与主裁判不同模型族，否则标定结果无意义",
            )

    panel = JudgePanel(primary=primary, reference=reference, candidate_model=candidate_model)

    if candidate_model and not is_heterogeneous(cfg.judge_model, candidate_model):
        logger.warning(
            "judge_self_preference_risk",
            judge_model=cfg.judge_model,
            candidate_model=candidate_model,
            reference_available=reference is not None,
            hint=(
                "主裁判与被测模型同族（自我偏好风险）。"
                "已配置异构参考裁判，可用 calibrate() 标定偏差。"
                if reference is not None
                else "主裁判与被测模型同族且无参考裁判；分数可能系统性偏高。"
            ),
        )

    return panel


__all__ = ["build_judge"]
