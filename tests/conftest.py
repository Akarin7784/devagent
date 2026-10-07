"""pytest 全局配置。

注意：``TesterAgent`` / ``TestRunOutcome`` / ``TestRunner`` 是产品类，
并非 pytest 测试类，但因其名称以 "Test" 开头会被 pytest 误收集。
此处通过 ``collect_ignore`` 与命名约定修正。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    """每个测试前重置配置缓存，避免环境变量串扰。"""
    from devagent.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()
