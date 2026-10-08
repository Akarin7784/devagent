"""Admin configuration API: authenticated, or strictly local and same-origin."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from devagent.settings_store import SettingsStore

router = APIRouter(tags=["settings"])


class SaveSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=128)
    changes: dict[str, Any]


def _store(request: Request, response: Response) -> SettingsStore:
    response.headers["Cache-Control"] = "no-store"
    settings = request.app.state.settings
    if not settings.security.api_key:
        host = request.url.hostname
        client = request.client.host if request.client else ""
        if host not in {"localhost", "127.0.0.1", "::1"} or client not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "后端配置只允许本机访问；远程管理须先启用 API Key")
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme, parsed.netloc) != (request.url.scheme, request.url.netloc):
                raise HTTPException(403, "后端配置拒绝跨站请求")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "后端配置拒绝跨站请求")
    store = getattr(request.app.state, "settings_store", None)
    if store is None:
        store = SettingsStore(settings)
        request.app.state.settings_store = store
    return store


@router.get("/settings")
async def read_settings(request: Request, response: Response) -> dict[str, Any]:
    try:
        view = _store(request, response).view()
        view["runtime_mode"] = "demo" if getattr(request.app.state, "demo_mode", False) else "live"
        return view
    except (OSError, ValueError):
        raise HTTPException(503, "无法读取后端配置文件，请检查文件格式与权限") from None


@router.put("/settings")
async def save_settings(
    body: SaveSettingsRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    store = _store(request, response)
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(415, "配置保存只接受 application/json")
    if len(await request.body()) > 131072:
        raise HTTPException(413, "配置内容过大")
    try:
        view = store.save(body.changes, body.revision)
        view["runtime_mode"] = "demo" if getattr(request.app.state, "demo_mode", False) else "live"
        return view
    except FileExistsError:
        raise HTTPException(409, "配置已被其他页面修改，请重新载入后再编辑") from None
    except ValidationError as exc:
        # No input / ctx in response: validation errors can contain plaintext secrets.
        errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        raise HTTPException(422, "；".join(errors)) from None
    except ValueError:
        raise HTTPException(422, "配置字段或结构不正确，请检查后重新保存") from None
    except OSError:
        raise HTTPException(503, "无法保存后端配置，请检查目录权限") from None
