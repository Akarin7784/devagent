"""Validated server-side overrides; never return stored credentials to clients."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

if TYPE_CHECKING:
    from devagent.config import Settings


def settings_path() -> Path:
    configured = os.environ.get("DEVAGENT_SETTINGS_FILE")
    return (
        Path(configured)
        if configured
        else Path(__file__).resolve().parents[2] / ".devagent/settings.json"
    )


def plain(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return value


def merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def read_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("配置文件不能是符号链接")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    return data


def load_saved_settings(base: Settings) -> Settings:
    from devagent.config import Settings

    overrides = read_overrides(settings_path())
    validate_patch(overrides, plain(base.model_dump()))
    return Settings.model_validate(merge(plain(base.model_dump()), overrides))


def validate_patch(patch: dict[str, Any], template: dict[str, Any], path: str = "") -> None:
    from devagent.config import ProviderConfig

    for key, value in patch.items():
        field = f"{path}.{key}".strip(".")
        if path == "models.providers":
            if not isinstance(value, dict):
                raise ValueError(f"{field} 必须是供应商配置对象")
            validate_patch(value, plain(ProviderConfig().model_dump()), field)
        elif key not in template:
            raise ValueError(f"未知配置项：{field}")
        elif isinstance(template[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"{field} 必须是配置对象")
            validate_patch(value, template[key], field)
        elif isinstance(template[key], bool) and not isinstance(value, bool):
            raise ValueError(f"{field} 必须是布尔值")
        elif type(template[key]) is int and type(value) is not int:
            raise ValueError(f"{field} 必须是整数")
        elif isinstance(template[key], float) and (
            type(value) not in {int, float} or not math.isfinite(value)
        ):
            raise ValueError(f"{field} 必须是有限数值")
        elif isinstance(template[key], list) and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            raise ValueError(f"{field} 必须是字符串列表")


def is_secret(path: str) -> bool:
    return path.endswith(".api_key") or path in {"database.url", "redis.url"}


def redact(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bool]]:
    secrets: dict[str, bool] = {}

    def visit(value: Any, path: str = "") -> Any:
        if is_secret(path):
            secrets[path] = bool(value)
            return None
        if isinstance(value, dict):
            return {key: visit(item, f"{path}.{key}".strip(".")) for key, item in value.items()}
        return copy.deepcopy(value)

    return visit(data), secrets


class SettingsStore:
    def __init__(self, running: Settings, path: Path | None = None) -> None:
        self.running = running
        self.path = path or settings_path()
        self.lock = RLock()

    def _state(self) -> tuple[dict[str, Any], dict[str, Any], str]:
        running = plain(self.running.model_dump())
        overrides = read_overrides(self.path)
        validate_patch(overrides, running)
        effective = merge(running, overrides)
        stamp = json.dumps([running, overrides], sort_keys=True, ensure_ascii=False)
        return effective, overrides, hashlib.sha256(stamp.encode()).hexdigest()

    def view(self) -> dict[str, Any]:
        from devagent.config import Settings
        from devagent.provider_presets import PROVIDER_PRESETS

        with self.lock:
            effective, _, revision = self._state()
            values, secrets = redact(effective)
            running, _ = redact(plain(self.running.model_dump()))
            schema = Settings.model_json_schema()
            return {
                "values": values,
                "running_values": running,
                "secrets": secrets,
                "schema": schema,
                "revision": revision,
                "restart_required": effective != plain(self.running.model_dump()),
                "presets": PROVIDER_PRESETS,
                "storage_path": str(self.path),
            }

    def save(self, patch: dict[str, Any], revision: str) -> dict[str, Any]:
        from devagent.config import Settings

        with self.lock:
            effective, overrides, current = self._state()
            if current != revision:
                raise FileExistsError("配置已被其他页面修改，请重新载入后再编辑")
            validate_patch(patch, effective)
            Settings.model_validate(merge(effective, patch), strict=True)
            updated = merge(overrides, patch)
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    delete=False,
                    prefix=".settings-",
                    suffix=".tmp",
                ) as handle:
                    temporary = handle.name
                    os.chmod(temporary, 0o600)
                    json.dump(updated, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                temporary = None
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
            return self.view()
