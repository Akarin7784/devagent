"""评测体系。

三层结构：

1. ``dataset`` —— golden set 的加载/校验/切分；
2. ``judge`` —— LLM-as-Judge，含双向评估以对冲位置偏差；
3. ``runner`` —— 跑数据集、聚合轨迹级指标、产出可比较的报告。

设计哲学：**评测不是「跑个测试」，而是「让系统可被证伪」**。
如果只有 demo 没有评测，任何「效果很好」的说法都站不住脚。
"""

from devagent.evaluation.dataset import DatasetError, GoldenSample, GoldenSet
from devagent.evaluation.judge import (
    DIMENSIONS,
    DimensionScore,
    GatewayJudgeBackend,
    JudgeBackend,
    JudgeResult,
    LLMJudge,
)
from devagent.evaluation.runner import EvalReport, EvalRunner, SampleOutcome, TaskRunner

__all__ = [
    "DIMENSIONS",
    "DatasetError",
    "DimensionScore",
    "EvalReport",
    "EvalRunner",
    "GatewayJudgeBackend",
    "GoldenSample",
    "GoldenSet",
    "JudgeBackend",
    "JudgeResult",
    "LLMJudge",
    "SampleOutcome",
    "TaskRunner",
]
