"""Export backend-owned frontend fixtures without reading live configuration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def export_contracts() -> None:
    from devagent.config import Settings
    from devagent.enums import AgentType, StepStatus, TaskStatus
    from devagent.provider_presets import PROVIDER_PRESETS

    outputs = {
        "test_contract_words.json": {
            "_generated_by": "scripts/export_web_contracts.py",
            "step_status": [s.value for s in StepStatus],
            "agent_type": [a.value for a in AgentType],
            "task_status": [s.value for s in TaskStatus],
        },
        "test_settings_schema.json": {
            "schema": Settings.model_json_schema(),
            "presets": PROVIDER_PRESETS,
        },
    }
    for name, value in outputs.items():
        (ROOT / "web" / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    export_contracts()
