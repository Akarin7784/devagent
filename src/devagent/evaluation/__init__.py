"""评测体系。

三层结构：

1. ``dataset`` —— golden set 的加载/校验/切分；
2. ``judge`` —— LLM-as-Judge，含双向评估以对冲位置偏差；
3. ``heterogeneous`` —— 异构裁判 + 偏差标定，对冲**自我偏好**偏差；
4. ``runner`` —— 跑数据集、聚合轨迹级指标、产出可比较的报告。

设计哲学：**评测不是「跑个测试」，而是「让系统可被证伪」**。
如果只有 demo 没有评测，任何「效果很好」的说法都站不住脚。

三类评委偏差各有对冲手段，互不替代：

| 偏差 | 对冲 |
| --- | --- |
| 位置偏差 | 双向评估（``judge.py``） |
| 自我偏好 | 异构裁判 + 标定（``heterogeneous.py``） |
| 长度偏差 | 评分维度分离（``DIMENSIONS`` 含 ``conciseness``） |
"""

from devagent.evaluation.dataset import DatasetError, GoldenSample, GoldenSet
from devagent.evaluation.factory import build_judge
from devagent.evaluation.heterogeneous import (
    CalibrationResult,
    JudgePanel,
    is_heterogeneous,
    model_family,
)
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
    "CalibrationResult",
    "DatasetError",
    "DimensionScore",
    "EvalReport",
    "EvalRunner",
    "GatewayJudgeBackend",
    "GoldenSample",
    "GoldenSet",
    "JudgeBackend",
    "JudgePanel",
    "JudgeResult",
    "LLMJudge",
    "SampleOutcome",
    "TaskRunner",
    "build_judge",
    "is_heterogeneous",
    "model_family",
]
