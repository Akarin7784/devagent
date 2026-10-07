"""pytest 全局配置。

注意：``TesterAgent`` / ``TestRunOutcome`` / ``TestRunner`` 是产品类，
并非 pytest 测试类，但因其名称以 "Test" 开头会被 pytest 误收集。
此处通过 ``collect_ignore`` 与命名约定修正。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_WORDS = _REPO_ROOT / "web" / "test_contract_words.json"


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    """每个测试前重置配置缓存，避免环境变量串扰。"""
    from devagent.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


def _build_contract_words() -> dict[str, Any]:
    """把后端枚举导出为前端可读的词表。

    `web/graph.js` 里有多张「字面量 → 视觉」的映射表，它们消费的是
    Python 产出的字符串，中间没有类型系统兜底。漏登记一个枚举成员的
    后果是**静默降级**（状态显示成"待执行"、角色退成灰色），不会报错，
    也不会被任何单个模块的测试发现。

    因此把 enums.py 当作唯一真相源导出为 fixture，由
    `tests/unit/test_node_contract.py` 断言其新鲜度（导出内容必须与当前
    枚举完全一致），`web/graph.test.js` 则读取它逐个断言覆盖。
    这样 Python 侧新增枚举成员时，前端契约测试会立即变红。
    """
    from devagent.enums import AgentType, StepStatus, TaskStatus

    return {
        "_generated_by": "tests/conftest.py::_build_contract_words",
        "step_status": [s.value for s in StepStatus],
        "agent_type": [a.value for a in AgentType],
        # 任务级状态同样被前端消费（任务列表/摘要卡）。此前只导出了
        # step_status，于是「后端加枚举 → 前端变红」这条链路对 TaskStatus
        # 是断的：概览页曾拿 'success' 去比较后端实际发出的 'succeeded'，
        # 结果全成功时显示"—"、9 成功 1 失败时显示 900%，而没有任何测试发现。
        "task_status": [s.value for s in TaskStatus],
    }


@pytest.fixture(scope="session")
def contract_words_path() -> Path:
    """导出跨语言词表并返回路径（整个测试会话只写一次）。"""
    _CONTRACT_WORDS.parent.mkdir(parents=True, exist_ok=True)
    _CONTRACT_WORDS.write_text(
        json.dumps(_build_contract_words(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return _CONTRACT_WORDS
