"""开发用前端热更新（`scripts/dev_reload.py`）的回归测试。

这个模块只在开发服务器里跑，但它的失效方式全是**静默**的：

- 注入位置写错 → 浏览器不执行那段脚本，没有任何报错，只是"热更新不好使"；
- 指纹比较漏掉新增/删除，或 debounce 逻辑写反 → 表现同样是"改了不刷新"，
  而开发者第一反应是自己没保存 —— 这正是它值得被断言的原因。

因此这里直接断言"注入结果、指纹差异、推送时序、SSE 首帧"，不依赖真实浏览器：
浏览器那一侧（EventSource + location.reload）是 6 行标准代码，由端到端手测覆盖。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"

pytestmark = pytest.mark.unit


def _load_script_module(name: str) -> Any:
    """按路径加载 `scripts/` 下的模块。

    `scripts` 不是包，pytest 也不会把它加进 `sys.path`（生产代码里是
    `serve_demo.py` 自己插的）。这里用 importlib 显式加载，避免为了一个
    开发脚本去改全局 `sys.path`。
    """
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dev_reload = _load_script_module("dev_reload")


def _frontend_app(root: Path, live: Any = None) -> Starlette:
    """最小可用的前端托管：只挂一个目录，不含任何 devagent 依赖。"""
    return Starlette(
        routes=[
            Mount("/", app=dev_reload.DevStaticFiles(directory=str(root), html=True, live=live))
        ]
    )


# ---------------------------------------------------------------------- #
# 注入
# ---------------------------------------------------------------------- #


def test_客户端注入在_body_之前且不动原有内容() -> None:
    html = "<html><body><div>hi</div></body></html>"
    out = dev_reload.inject_reload_client(html)
    assert "__dev/events" in out
    assert out.index("__dev/events") < out.index("</body>"), (
        "脚本必须在 </body> 之前 —— 放到后面在极端情况下会随文档结束被丢弃"
    )
    assert "<div>hi</div>" in out, "注入不能改动原有内容"


def test_没有_body_标签时追加到末尾() -> None:
    out = dev_reload.inject_reload_client("<div>x</div>")
    assert out.startswith("<div>x</div>")
    assert "__dev/events" in out


def test_重复注入是幂等的() -> None:
    once = dev_reload.inject_reload_client("<body></body>")
    twice = dev_reload.inject_reload_client(once)
    assert twice == once, "二次注入会插入两段脚本 —— 两个 EventSource 各自触发一次 reload"
    assert once.count("__dev/events") == 1


def test_客户端支持_no_reload_退出开关() -> None:
    # 这条 SSE 是永不结束的请求：headless 截图/爬虫的 networkidle 与
    # --virtual-time-budget 会因此永远等不到（实测把无头截图卡死）。
    assert "no-reload" in dev_reload.RELOAD_CLIENT, (
        "缺少 ?no-reload=1 退出开关 —— 自动化工具会被这条长连接挂住"
    )


# ---------------------------------------------------------------------- #
# 静态资源不再被缓存
# ---------------------------------------------------------------------- #


def test_静态资源带_no_store(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html><body>hi</body></html>", encoding="utf-8")
    (tmp_path / "app.js").write_text("export const a = 1;", encoding="utf-8")

    with TestClient(_frontend_app(tmp_path)) as client:
        js = client.get("/app.js")
        assert js.status_code == 200
        assert js.headers["cache-control"] == "no-store, must-revalidate", (
            "没有 no-store 时浏览器会按启发式规则缓存 ES 模块 —— 改完刷新还是旧的"
        )
        assert client.get("/").headers["cache-control"] == "no-store, must-revalidate"


def test_只有_index_被注入静态资源不受影响(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html><body>hi</body></html>", encoding="utf-8")
    (tmp_path / "app.js").write_text("export const a = 1;", encoding="utf-8")
    live = dev_reload.LiveReload(tmp_path)

    with TestClient(_frontend_app(tmp_path, live=live)) as client:
        html = client.get("/").text
        assert "__dev/events" in html, "index.html 必须带上热更新客户端"
        assert "__dev/events" not in client.get("/app.js").text, "脚本文件不该被改动"


# ---------------------------------------------------------------------- #
# 指纹
# ---------------------------------------------------------------------- #


def test_指纹覆盖新增与修改(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("a", encoding="utf-8")
    live = dev_reload.LiveReload(tmp_path)
    assert live.changed() == [], "初始化快照之后，无改动不应报变化"

    (tmp_path / "a.js").write_text("aa", encoding="utf-8")  # 内容长度也变了
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.js").write_text("b", encoding="utf-8")
    assert set(live.changed()) == {"a.js", "sub/b.js"}


def test_删除文件也算变化(tmp_path: Path) -> None:
    (tmp_path / "gone.js").write_text("1", encoding="utf-8")
    live = dev_reload.LiveReload(tmp_path)
    (tmp_path / "gone.js").unlink()
    assert live.changed() == ["gone.js"], (
        "漏掉「删除」的后果是页面继续跑一个已经不存在的模块，同样不会报错"
    )


# ---------------------------------------------------------------------- #
# 推送时序
# ---------------------------------------------------------------------- #


async def test_文件变化后推送一次并带上文件名(tmp_path: Path) -> None:
    live = dev_reload.LiveReload(tmp_path, interval=0.01, debounce=0.01)
    queue = live.subscribe()
    await live.start()
    try:
        (tmp_path / "page.js").write_text("// v2", encoding="utf-8")
        payload = await asyncio.wait_for(queue.get(), timeout=5)
    finally:
        await live.stop()

    assert payload["changed"] == ["page.js"]
    assert payload["version"] == 1, "版本号必须递增：客户端靠它区分是第几次改动"


async def test_没有变化时不推送(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("a", encoding="utf-8")
    live = dev_reload.LiveReload(tmp_path, interval=0.01, debounce=0.01)
    queue = live.subscribe()
    await live.start()
    try:
        await asyncio.sleep(0.1)
        assert queue.empty(), "没有改动却推送 —— 浏览器会莫名其妙地自己刷新"
    finally:
        await live.stop()
    assert live.version == 0


async def test_连续多次保存只推送一次(tmp_path: Path) -> None:
    live = dev_reload.LiveReload(tmp_path, interval=0.01, debounce=0.2)
    queue = live.subscribe()
    await live.start()
    try:
        (tmp_path / "a.js").write_text("1", encoding="utf-8")
        await asyncio.sleep(0.05)  # 编辑器保存通常一次落多个文件
        (tmp_path / "b.js").write_text("1", encoding="utf-8")

        payload = await asyncio.wait_for(queue.get(), timeout=5)
        assert set(payload["changed"]) == {"a.js", "b.js"}, "同一批改动应合并成一次推送"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.4)
    finally:
        await live.stop()


async def test_sse_端点先发_ready_再推变更(tmp_path: Path) -> None:
    live = dev_reload.LiveReload(tmp_path, interval=0.01, debounce=0.01)
    # 该参数在实现里未被使用（Starlette 的 Route 一定会传 request 进来）
    response = await live.events(None)  # type: ignore[arg-type]
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-store"

    body = response.body_iterator
    first = await asyncio.wait_for(body.__anext__(), timeout=1)
    assert first.startswith("event: ready"), f"首帧必须是握手事件，实际：{first!r}"

    await live.start()
    try:
        (tmp_path / "x.js").write_text("1", encoding="utf-8")
        second = await asyncio.wait_for(body.__anext__(), timeout=5)
    finally:
        await live.stop()
    assert second.startswith("event: reload")
    assert "x.js" in second, "推送里应带上变化的文件名，方便在控制台定位"
    await body.aclose()


# ---------------------------------------------------------------------- #
# 接线：真实 create_app 上的路由顺序与注入
# ---------------------------------------------------------------------- #
# 这一节守的是一个已经踩过的坑：`Mount("/")` 的 `path` 被 Starlette
# `rstrip("/")` 成**空串**，只比较 "/" 就永远找不到挂载点 —— 于是热更新
# 静默失效，页面照常打开，只是"改了不刷新"，且控制台里没有任何线索。
# 另一半是路由顺序：挂在 "/" 的静态目录会吞掉注册在它之后的 SSE 路由。


def test_在真实应用上完成接线(tmp_path: Path) -> None:
    from starlette.routing import Mount as MountCls

    from devagent.api.app import create_app
    from devagent.config import Settings

    (tmp_path / "index.html").write_text("<html><body>hi</body></html>", encoding="utf-8")
    (tmp_path / "app.js").write_text("export const a = 1;", encoding="utf-8")

    settings = Settings()
    settings.web_dir = str(tmp_path)
    app = create_app(settings)

    live = dev_reload.install_dev_frontend(app)
    assert live is not None, '没有找到 Mount("/")：挂载点的 path 是被 rstrip 过的空串'

    routes = app.router.routes
    dev_index = next(i for i, r in enumerate(routes) if getattr(r, "path", None) == "/__dev/events")
    mount_index = next(
        i
        for i, r in enumerate(routes)
        if isinstance(r, MountCls) and getattr(r, "path", None) in ("", "/")
    )
    assert dev_index < mount_index, (
        "SSE 路由注册在静态挂载之后 —— 挂在 / 的 StaticFiles 会吞掉它，请求会 404"
    )

    with TestClient(app) as client:
        html = client.get("/").text
        assert "__dev/events" in html, "index.html 没有注入热更新客户端"
        assert client.get("/").headers["cache-control"] == "no-store, must-revalidate"
        js = client.get("/app.js")
        assert js.status_code == 200
        assert js.headers["cache-control"] == "no-store, must-revalidate"
        assert "__dev/events" not in js.text


def test_没有前端目录时不接线(tmp_path: Path) -> None:
    from devagent.api.app import create_app
    from devagent.config import Settings

    settings = Settings()
    settings.web_dir = None  # 仅 API 模式
    app = create_app(settings)
    assert dev_reload.install_dev_frontend(app) is None
