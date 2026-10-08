"""命令行入口。

一个开源项目如果没有「clone 下来就能跑」的 CLI，贡献者会在第一步流失。

提供四类命令：

- ``run``     —— 跑一个研发任务（真实 LLM）
- ``eval``    —— 跑 golden set 评测并产出报告
- ``index``   —— 构建代码索引（便于人工检查 AST 解析效果）
- ``serve``   —— 启动 API 服务

设计：用 ``argparse`` 而非 click/typer —— 少一个依赖就少一个「为什么不用 X」
的争论，且标准库足以表达这里的复杂度。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from devagent.config import get_settings
from devagent.console import force_utf8_stdio
from devagent.logging_config import configure_logging, get_logger
from devagent.observability import configure_observability, get_observability

logger = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent",
        description="多 Agent 协作的软件研发助手（上下文工程驱动）",
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别（默认 INFO）")
    parser.add_argument("--log-json", action="store_true", help="输出 JSON 日志")
    parser.add_argument("--trace", action="store_true", help="启用追踪与指标采集")
    parser.add_argument("--otlp-endpoint", default="", help="OTLP 导出端点（可选）")

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- run ----
    p_run = sub.add_parser("run", help="执行一个研发任务")
    p_run.add_argument("goal", help="自然语言需求")
    p_run.add_argument("--task-id", default="", help="任务 id（默认自动生成）")
    p_run.add_argument("--json", action="store_true", help="以 JSON 输出结果")

    # ---- eval ----
    p_eval = sub.add_parser("eval", help="运行 golden set 评测")
    p_eval.add_argument(
        "--dataset", default="", help="数据集路径（默认取配置 evaluation.golden_set_path）"
    )
    p_eval.add_argument("--category", action="append", default=[], help="只跑指定类别（可多次）")
    p_eval.add_argument("--max-samples", type=int, default=None, help="最多跑多少样本")
    p_eval.add_argument("--no-judge", action="store_true", help="跳过 LLM 裁判（只跑执行层指标）")
    p_eval.add_argument("--output", default="", help="报告输出路径")
    p_eval.add_argument("--json", action="store_true", help="stdout 输出 JSON 摘要")

    # ---- index ----
    p_idx = sub.add_parser("index", help="构建代码索引并打印统计")
    p_idx.add_argument("root", nargs="?", default=".", help="要索引的目录（默认当前目录）")
    p_idx.add_argument("--query", default="", help="索引后执行一次搜索")
    p_idx.add_argument("--limit", type=int, default=10, help="搜索结果上限")

    # ---- serve ----
    p_serve = sub.add_parser("serve", help="启动 API 服务")
    p_serve.add_argument("--host", default=None, help="覆盖后端配置中的监听地址")
    p_serve.add_argument("--port", type=int, default=None, help="覆盖后端配置中的监听端口")
    p_serve.add_argument("--reload", action="store_true")

    return parser


def _setup(args: argparse.Namespace) -> None:
    configure_logging(args.log_level, json_output=args.log_json)
    configure_observability(
        enabled=args.trace,
        otlp_endpoint=args.otlp_endpoint,
    )


# ---------------------------------------------------------------------- #
# run
# ---------------------------------------------------------------------- #


async def _cmd_run(args: argparse.Namespace) -> int:
    from devagent.orchestration import Orchestrator
    from devagent.tools.runtime import build_test_runtime

    settings = get_settings()
    # 接上测试运行时，否则 Tester 只是"生成测试"而从不执行，
    # Verifier 拿到的客观证据会是一条占位符。
    runtime = build_test_runtime(settings)
    orchestrator = Orchestrator(settings, test_runner=runtime.runner)
    try:
        result = await orchestrator.run(args.goal, task_id=args.task_id or None)
    finally:
        await orchestrator.aclose()
        await runtime.aclose()

    if args.json:
        print(
            json.dumps(
                {
                    "task_id": result.task_id,
                    "status": result.status.value,
                    "succeeded": result.succeeded,
                    "steps": len(result.steps),
                    "total_tokens": result.total_tokens,
                    "total_cost_usd": round(result.total_cost_usd, 6),
                    "duration_ms": result.duration_ms,
                    "error": result.error,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"任务 {result.task_id}：{result.status.value}")
        print(f"  成功：{result.succeeded}")
        print(f"  步骤：{len(result.steps)}")
        print(f"  token：{result.total_tokens}")
        print(f"  耗时：{result.duration_ms} ms")
        if result.error:
            print(f"  错误：{result.error}")

    if args.trace:
        obs = get_observability()
        print("\n--- 指标摘要 ---")
        print(json.dumps(obs.metrics.snapshot(), ensure_ascii=False, indent=2)[:2000])

    return 0 if result.succeeded else 1


# ---------------------------------------------------------------------- #
# eval
# ---------------------------------------------------------------------- #


async def _cmd_eval(args: argparse.Namespace) -> int:
    from devagent.agents.base import AgentContextError
    from devagent.evaluation import EvalRunner, GoldenSet, build_judge
    from devagent.models.gateway import ModelGateway
    from devagent.orchestration import Orchestrator

    settings = get_settings()
    dataset_path = args.dataset or settings.evaluation.golden_set_path
    dataset = GoldenSet.load(dataset_path)
    logger.info("eval_dataset_ready", path=dataset_path, count=len(dataset))

    from devagent.tools.runtime import build_test_runtime

    gateway = ModelGateway(settings)
    runtime = build_test_runtime(settings)
    orchestrator = Orchestrator(settings, gateway=gateway, test_runner=runtime.runner)

    judge = None
    if not args.no_judge:
        # 与 API 走同一个工厂，保证命令行与网页跑出的结论可比较
        judge = build_judge(
            gateway,
            settings,
            candidate_model=settings.routing.medium_model,
        )

    runner = EvalRunner(task_runner=orchestrator, judge=judge)
    try:
        report = await runner.run(
            dataset,
            categories=args.category or None,
            max_samples=args.max_samples,
            metadata={"dataset_path": dataset_path},
        )
    except AgentContextError as exc:
        logger.error("eval_aborted", error=str(exc))
        return 2
    finally:
        await orchestrator.aclose()
        await runtime.aclose()

    output = (
        Path(args.output) if args.output else Path(settings.evaluation.report_dir) / "latest.json"
    )
    report.save(output)

    summary = report.summary()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"数据集：{summary['dataset']}（{summary['total']} 样本）")
        print(f"  执行成功率    ：{summary['success_rate']:.1%}")
        print(f"  一次通过率    ：{summary['first_pass_rate']:.1%}")
        print(f"  裁判通过率    ：{summary['judge_pass_rate']:.1%}")
        print(f"  平均裁判分    ：{summary['mean_judge_score']:.2f}")
        print(f"  裁判矛盾率    ：{summary['inconsistent_judge_rate']:.1%}")
        print(f"  区分度(σ)     ：{summary['discrimination']:.3f}")
        print(f"  平均上下文节省：{summary['mean_context_savings']:.1%}")
        print(f"  总 token      ：{summary['total_tokens']}")
        print(f"  总成本        ：${summary['total_cost_usd']:.4f}")
        _print_calibration(summary)
        print(f"  报告已写入    ：{output}")

    return 0


def _print_calibration(summary: dict[str, Any]) -> None:
    """打印裁判偏差标定结果。

    单独抽出来，是因为这段输出的**存在本身**就是结论的一部分：
    若裁判与被测模型同源且未标定，读者必须知道 ``平均裁判分`` 可能系统性偏高，
    否则这个数字会被误读。

    ``calibration`` 存在两种"没有值"的情况，必须区分：
    - 空字典 → 未做标定（裁判已异构，或关闭了标定）
    - 有值但 ``reliable=False`` → 做了标定但样本不足，**分数未被校正**
    """
    calib: dict[str, Any] = summary.get("calibration") or {}
    if not calib:
        if summary.get("judge_same_source"):
            print("  裁判偏差      ：未标定（裁判与被测模型同族，分数可能系统性偏高）")
        return

    bias = calib.get("bias", 0.0)
    if not calib.get("reliable", False):
        print(
            f"  裁判偏差      ：样本不足（{calib.get('sample_count', 0)} < 20），"
            "标定不可靠，**分数未校正**"
        )
        return

    print(
        f"  裁判偏差      ：{bias:+.2f}（已校正，"
        f"参考裁判 {calib.get('reference_model', '')}，"
        f"{calib.get('sample_count', 0)} 样本）"
    )


# ---------------------------------------------------------------------- #
# index
# ---------------------------------------------------------------------- #


def _cmd_index(args: argparse.Namespace) -> int:
    from devagent.tools import CodeIndex

    index = CodeIndex(args.root)
    index.build()
    stats = index.stats()
    print(f"根目录    ：{Path(args.root).resolve()}")
    print(f"解析模式  ：{stats.get('parse_mode', 'unknown')}")
    print(f"文件数    ：{stats.get('files', 0)}")
    print(f"符号数    ：{stats.get('symbols', 0)}")
    print(f"  类      ：{stats.get('classes', 0)}")
    print(f"  函数    ：{stats.get('functions', 0)}")

    if args.query:
        # search 接收关键词列表（来自需求文本的分词），这里按空白切分便于手工调试
        keywords = args.query.split()
        print(f"\n搜索：{keywords}")
        hits = index.search(keywords, top_k=args.limit)
        if not hits:
            print("  （无结果）")
        for hit in hits:
            symbols = ", ".join(hit.matched_symbols[:5])
            print(f"  {hit.score:6.3f}  {hit.file}")
            if symbols:
                print(f"           命中符号：{symbols}")
            if hit.reason:
                print(f"           依据：{hit.reason}")
    return 0


# ---------------------------------------------------------------------- #
# serve
# ---------------------------------------------------------------------- #


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        # uvicorn 是基础依赖之一（pyproject 的 dependencies），不存在
        # ``devagent[api]`` 这个 extra —— 早先的提示会引导用户去装一个
        # 根本不存在的分组，属于文档级缺陷。
        print("缺少 uvicorn。请安装：pip install -e .", file=sys.stderr)
        return 2

    settings = get_settings()
    uvicorn.run(
        "devagent.api.app:create_app",
        factory=True,
        host=args.host if args.host is not None else settings.api_host,
        port=args.port if args.port is not None else settings.api_port,
        reload=args.reload,
    )
    return 0


# ---------------------------------------------------------------------- #
# entrypoint
# ---------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # CLI 的输出全是中文：在不支持中文的控制台上（英文 Windows 的 cp1252）
    # 直接打印会抛 UnicodeEncodeError 并以非零码退出，看起来像"程序崩了"。
    force_utf8_stdio()
    _setup(args)

    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "eval":
        return asyncio.run(_cmd_eval(args))
    if args.command == "index":
        return _cmd_index(args)
    if args.command == "serve":
        return _cmd_serve(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
