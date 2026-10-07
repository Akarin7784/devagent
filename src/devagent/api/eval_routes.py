"""评测路由的辅助实现。

从 ``routes.py`` 拆出来的原因：评测涉及「构造 Orchestrator + Judge + 跑数据集」
一串较重的装配逻辑，塞进路由函数会让路由臃肿且难以单测。
这里独立成一个可被直接调用的函数，路由只做参数转换。
"""

from __future__ import annotations

from typing import Any

from devagent.api.schemas import RunEvalRequest
from devagent.config import get_settings
from devagent.logging_config import get_logger

logger = get_logger(__name__)


async def execute_evaluation(app: Any, payload: RunEvalRequest) -> dict[str, Any]:
    """执行一次评测并返回摘要。

    复用 ``app.state`` 上已有的 gateway（若存在），避免重复建连接池。
    """
    from devagent.evaluation import EvalRunner, GoldenSet, LLMJudge
    from devagent.evaluation.judge import GatewayJudgeBackend
    from devagent.models.gateway import ModelGateway
    from devagent.orchestration import Orchestrator

    settings = get_settings()
    gateway = getattr(app.state, "gateway", None) or ModelGateway(settings)

    dataset_path = payload.dataset_path or settings.evaluation.golden_set_path
    dataset = GoldenSet.load(dataset_path)

    orchestrator = Orchestrator(settings, gateway=gateway)

    judge = None
    if payload.use_judge:
        judge = LLMJudge(
            GatewayJudgeBackend(gateway),
            model=settings.evaluation.judge_model,
            bidirectional=settings.evaluation.enable_bidirectional_judge,
        )

    runner = EvalRunner(task_runner=orchestrator, judge=judge)
    try:
        report = await runner.run(
            dataset,
            categories=payload.categories or None,
            max_samples=payload.max_samples,
            metadata={"source": "api"},
        )
    finally:
        # 只关闭本次新建的 orchestrator 资源；gateway 由 app 生命周期管理
        if getattr(app.state, "gateway", None) is None:
            await orchestrator.aclose()

    summary = report.summary()
    logger.info(
        "api_eval_finished",
        dataset=summary["dataset"],
        total=summary["total"],
        success_rate=summary["success_rate"],
    )
    return summary


__all__ = ["execute_evaluation"]
