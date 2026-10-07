"""评测路由的辅助实现。

从 ``routes.py`` 拆出来的原因：评测涉及「构造 Orchestrator + Judge + 跑数据集」
一串较重的装配逻辑，塞进路由函数会让路由臃肿且难以单测。
这里独立成一个可被直接调用的函数，路由只做参数转换。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from devagent.api.schemas import RunEvalRequest
from devagent.config import get_settings
from devagent.logging_config import get_logger

logger = get_logger(__name__)


def _resolve_dataset_path(raw: str, default: str, allowed_dir: str) -> Path:
    """把调用方给的数据集路径限制在允许的根目录内。

    评测接口对匿名调用方开放（默认无鉴权），而它接受一个任意路径参数。
    不限制范围的话，服务端会变成"任意文件读取器"：错误信息里还会带上
    文件内容的片段。这里要求解析后的路径必须落在 ``dataset_dir`` 里。
    """
    root = Path(allowed_dir).resolve()
    candidate = Path(raw or default)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"数据集路径必须位于 {root} 之内") from exc
    return resolved


async def execute_evaluation(app: Any, payload: RunEvalRequest) -> dict[str, Any]:
    """执行一次评测并返回摘要。

    复用 ``app.state`` 上已有的 gateway（若存在），避免重复建连接池。
    """
    from devagent.evaluation import EvalRunner, GoldenSet, build_judge
    from devagent.models.gateway import ModelGateway
    from devagent.orchestration import Orchestrator

    settings = get_settings()
    gateway = getattr(app.state, "gateway", None) or ModelGateway(settings)
    # 复用 app 生命周期管理的测试运行时：评测与线上跑同一条链路，
    # 否则"评测通过"与"线上可用"之间会存在一条没人验证的缝。
    test_runtime = getattr(app.state, "test_runtime", None)

    dataset_path = _resolve_dataset_path(
        payload.dataset_path,
        settings.evaluation.golden_set_path,
        settings.evaluation.dataset_dir,
    )
    # 用异步加载：同步 read_text 会阻塞事件循环（见 GoldenSet.aload 的说明）
    dataset = await GoldenSet.aload(dataset_path)

    orchestrator = Orchestrator(
        settings,
        gateway=gateway,
        test_runner=test_runtime.runner if test_runtime is not None else None,
    )

    judge = None
    if payload.use_judge:
        # 与 CLI 走同一个工厂，保证两个入口跑出的结论可比较
        judge = build_judge(
            gateway,
            settings,
            candidate_model=settings.routing.medium_model,
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
