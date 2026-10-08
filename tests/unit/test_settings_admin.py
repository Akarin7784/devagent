"""Configuration persistence, credential boundaries, access control and provider wire contracts."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from devagent.api.app import create_app
from devagent.config import ProviderConfig, Settings, get_settings
from devagent.models.configured import AnthropicProvider, ConfiguredProvider
from devagent.models.gateway import ModelGateway
from devagent.models.provider import ChatMessage, ModelError, ToolSpec
from devagent.settings_store import SettingsStore


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "settings.json"
    monkeypatch.setenv("DEVAGENT_SETTINGS_FILE", str(path))
    return path


def local_client(settings: Settings) -> TestClient:
    # No lifespan: admin routes require no model, DB or Docker resources.
    return TestClient(create_app(settings), base_url="http://localhost", client=("127.0.0.1", 1234))


def test_all_configuration_fields_are_exposed_without_secrets(settings_file: Path) -> None:
    settings = Settings(_env_file=None)
    settings.models.deepseek.api_key = SecretStr("provider-secret")
    settings.security.api_key = "admin-secret"
    settings.database.url = "postgresql://user:db-secret@host/db"
    store = SettingsStore(settings)
    view = store.view()
    assert set(view["values"]) == set(Settings.model_fields)
    assert "provider-secret" not in json.dumps(view)
    assert "admin-secret" not in json.dumps(view)
    assert "db-secret" not in json.dumps(view)
    assert view["secrets"]["models.deepseek.api_key"]
    assert view["values"]["database"]["url"] is None
    assert len(view["presets"]) >= 24


def test_save_is_partial_persistent_and_requires_restart(settings_file: Path) -> None:
    settings = Settings(_env_file=None)
    client = local_client(settings)
    before = client.get("/api/v1/settings")
    assert before.headers["cache-control"] == "no-store"
    response = client.put(
        "/api/v1/settings",
        json={
            "revision": before.json()["revision"],
            "changes": {
                "context": {"default_budget": 32000},
                "models": {
                    "providers": {
                        "openai": {
                            "base_url": "https://api.openai.com/v1",
                            "api_key": "new-secret",
                        }
                    }
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["restart_required"]
    assert "new-secret" not in response.text
    assert settings.context.default_budget == 16000
    assert json.loads(settings_file.read_text())["context"] == {"default_budget": 32000}
    restored = get_settings()
    assert restored.context.default_budget == 32000
    assert restored.models.providers["openai"].api_key.get_secret_value() == "new-secret"


def test_secret_omission_preserves_and_explicit_null_clears(settings_file: Path) -> None:
    settings = Settings(_env_file=None)
    settings.models.qwen = ProviderConfig(api_key="keep-secret", base_url="https://example.com/v1")
    store = SettingsStore(settings)
    view = store.save({"models": {"qwen": {"timeout_seconds": 90}}}, store.view()["revision"])
    assert view["secrets"]["models.qwen.api_key"]
    assert "keep-secret" not in settings_file.read_text()
    view = store.save({"models": {"qwen": {"api_key": None}}}, view["revision"])
    assert not view["secrets"]["models.qwen.api_key"]


def test_stale_revision_does_not_overwrite_other_page(settings_file: Path) -> None:
    store = SettingsStore(Settings(_env_file=None))
    revision = store.view()["revision"]
    store.save({"debug": True}, revision)
    with pytest.raises(FileExistsError):
        store.save({"debug": False}, revision)
    assert json.loads(settings_file.read_text())["debug"] is True


@pytest.mark.parametrize(
    "patch",
    [
        {"not_a_setting": True},
        {"context": {"unknown": 1}},
        {"context": {"default_budget": 0}},
        {"debug": "false"},
        {"models": {"providers": {"deepseek": {"base_url": "https://example.com"}}}},
        {"models": {"providers": {"custom": {"base_url": "file:///secret"}}}},
        {"models": {"providers": {"custom": {"base_url": "https://user:password@example.com"}}}},
        {"models": {"providers": {"custom": {"unknown": "hidden-secret"}}}},
    ],
)
def test_invalid_patch_never_writes_or_echoes_input(settings_file: Path, patch: dict) -> None:
    client = local_client(Settings(_env_file=None))
    revision = client.get("/api/v1/settings").json()["revision"]
    response = client.put("/api/v1/settings", json={"revision": revision, "changes": patch})
    assert response.status_code == 422
    assert "hidden-secret" not in response.text
    assert "password@example" not in response.text
    assert not settings_file.exists()


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Host": "evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_unauthenticated_admin_rejects_cross_site(settings_file: Path, headers: dict) -> None:
    client = local_client(Settings(_env_file=None))
    assert client.get("/api/v1/settings", headers=headers).status_code == 403


def test_remote_admin_requires_existing_authentication(settings_file: Path) -> None:
    settings = Settings(_env_file=None)
    remote = TestClient(
        create_app(settings), base_url="https://remote.example", client=("192.0.2.1", 1234)
    )
    assert remote.get("/api/v1/settings").status_code == 403
    settings.security.api_key = "admin-secret"
    assert remote.get("/api/v1/settings").status_code == 401
    response = remote.get("/api/v1/settings", headers={"X-API-Key": "admin-secret"})
    assert response.status_code == 200
    assert "admin-secret" not in response.text


def test_corrupt_file_fails_without_leaking_contents(settings_file: Path) -> None:
    settings_file.write_text("private-corrupt-secret", encoding="utf-8")
    response = local_client(Settings(_env_file=None)).get("/api/v1/settings")
    assert response.status_code == 503
    assert "private-corrupt-secret" not in response.text


def test_gateway_registers_new_protocols_and_local_no_key() -> None:
    settings = Settings(_env_file=None)
    settings.models.providers = {
        "anthropic": ProviderConfig(
            api_key="x", base_url="https://api.anthropic.com/v1", protocol="anthropic"
        ),
        "ollama": ProviderConfig(base_url="http://localhost:11434/v1", auth_mode="none"),
        "disabled": ProviderConfig(base_url="https://example.com/v1"),
    }
    gateway = ModelGateway(settings)
    assert "anthropic" in gateway.provider_names
    assert isinstance(gateway._providers["anthropic"], AnthropicProvider)
    assert "ollama" in gateway.provider_names
    assert "disabled" not in gateway.provider_names


@pytest.mark.asyncio
async def test_openai_auth_parameters_and_zero_retry_make_one_request() -> None:
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredProvider(
            "azure",
            ProviderConfig(
                api_key="x",
                base_url="https://resource.openai.azure.com/openai/v1",
                max_retries=0,
                auth_mode="api-key",
                send_temperature=False,
                max_tokens_field="max_completion_tokens",
            ),
            client=client,
        )
        result = await provider.chat(
            [ChatMessage("user", "hello")], model="deployment", max_tokens=64
        )
    assert result.content == "done"
    assert len(observed) == 1
    assert observed[0].headers["api-key"] == "x"
    payload = json.loads(observed[0].content)
    assert "temperature" not in payload
    assert "max_tokens" not in payload
    assert payload["max_completion_tokens"] == 64


@pytest.mark.asyncio
async def test_native_anthropic_system_tools_and_usage() -> None:
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "model": "claude-test",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "run test"},
                    {"type": "tool_use", "id": "tool1", "name": "test", "input": {"path": "a.py"}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 20},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(
            "anthropic",
            ProviderConfig(
                api_key="x",
                base_url="https://api.anthropic.com/v1",
                protocol="anthropic",
                max_retries=0,
            ),
            client=client,
        )
        result = await provider.chat(
            [
                ChatMessage("system", "system instruction"),
                ChatMessage("user", "hello"),
                ChatMessage(
                    "assistant",
                    "",
                    tool_calls=[
                        {"id": "previous", "function": {"name": "test", "arguments": "{}"}}
                    ],
                ),
                ChatMessage("tool", "passed", tool_call_id="previous"),
            ],
            model="claude-test",
            tools=[ToolSpec("test", "Run tests", {"type": "object"})],
        )
        with pytest.raises(ModelError, match="嵌入"):
            await provider.embed(["hello"], model="embedding")
    payload = json.loads(observed[0].content)
    assert str(observed[0].url).endswith("/v1/messages")
    assert observed[0].headers["anthropic-version"] == "2023-06-01"
    assert payload["system"] == "system instruction"
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"
    assert payload["tools"][0]["input_schema"] == {"type": "object"}
    assert payload["max_tokens"] == 4096
    assert result.usage.total_tokens == 35
    assert result.finish_reason == "tool_calls"
    assert json.loads(result.tool_calls[0]["function"]["arguments"])["path"] == "a.py"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code, expected_calls", [(400, 1), (401, 1), (429, 2), (503, 2)])
async def test_provider_retries_only_retryable_errors(
    status_code: int,
    expected_calls: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("devagent.models.provider.asyncio.sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status_code, json={"error": "failed"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ConfiguredProvider(
            "test",
            ProviderConfig(
                api_key="x",
                base_url="https://example.com/v1",
                max_retries=1,
            ),
            client=client,
        )
        if expected_calls == 1:
            with pytest.raises(ModelError):
                await provider.chat([ChatMessage("user", "hello")], model="test")
        else:
            assert (
                await provider.chat([ChatMessage("user", "hello")], model="test")
            ).content == "done"
    assert len(calls) == expected_calls


@pytest.mark.parametrize("overrides", [[], ["--host", "0.0.0.0", "--port", "9011"]])
def test_serve_honors_saved_settings_and_explicit_flags(
    monkeypatch: pytest.MonkeyPatch, overrides: list[str]
) -> None:
    import uvicorn

    from devagent import cli

    settings = Settings(_env_file=None, api_host="127.0.0.2", api_port=9010)
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **kwargs: captured.update(kwargs))
    args = cli._build_parser().parse_args(["serve", *overrides])
    assert cli._cmd_serve(args) == 0
    assert captured["host"] == ("0.0.0.0" if overrides else "127.0.0.2")
    assert captured["port"] == (9011 if overrides else 9010)
