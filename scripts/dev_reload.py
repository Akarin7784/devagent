"""开发用前端热更新（零依赖）：改 `web/` 下的文件，浏览器自己刷新。

## 为什么需要

`web/` 是零构建的原生 ES 模块 —— 没有打包器，也就没有 HMR：改一个页面模块
必须手动刷新。而"手动刷新"在这里还经常不管用：Starlette 的 `StaticFiles` 只发
`Last-Modified` 与 `ETag`，**不发 `Cache-Control`**，浏览器于是按启发式规则
自行决定缓存多久（经验值是距上次修改时间的 10%）。刚改完的文件正好落在这个
窗口里，F5 也照样命中缓存。表现就是那句话：「我明明改了，刷新还是旧的」。

## 这个模块做两件事

1. `DevStaticFiles`：给静态资源补 `Cache-Control: no-store` —— 普通刷新必定
   拿到磁盘上的最新内容，"神秘缓存"从根上消失；
2. `LiveReload`：轮询 `web/` 的文件指纹，变化时通过 SSE 通知浏览器整页重载。
   客户端脚本由**服务端注入** `index.html`，因此 `web/` 目录里不残留任何开发
   代码，生产镜像与既有前端测试完全不受影响。

## 取舍：为什么轮询而不是 watchdog

不引入新依赖（前端零构建、后端零额外依赖是本项目的硬约束），开发场景下
0.5s 的延迟完全够用；这个规模（几十个文件）的 `rglob` 开销可忽略。

## 边界

只监听前端目录。改了 `src/devagent/**` 需要重启进程才生效 —— 浏览器重载
救不了 Python 代码，那属于 uvicorn `--reload` 的职责（`devagent serve --reload`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

if TYPE_CHECKING:  # 只在类型检查时导入：本模块要被 node 无关的测试直接加载
    from fastapi import FastAPI

__all__ = [
    "RELOAD_CLIENT",
    "DevStaticFiles",
    "LiveReload",
    "inject_reload_client",
    "install_dev_frontend",
]

# 客户端脚本的识别标记：用它做幂等判断，避免二次注入。
_MARKER = "__dev/events"

# 注入到 index.html 的客户端：保持极小，且任何失败都不能影响页面本身。
RELOAD_CLIENT = (
    "<script>\n"
    "/* 开发用热更新客户端（由 scripts/serve_demo.py 注入，生产构建里不存在）。\n"
    "   服务端在 web/ 变化时推一条 reload 事件，这里整页重载 ——\n"
    "   重载保留 hash 与查询参数，所以当前路由与 ?api= 都不会丢。\n"
    "   URL 上带 ?no-reload=1 时不连接：这条 SSE 是**永不结束**的请求，\n"
    "   会让 headless 截图/爬虫的 --virtual-time-budget 与 networkidle 永远等不到，\n"
    "   自动化需要稳定页面时用它显式退出。 */\n"
    "(() => {\n"
    "  if (new URLSearchParams(location.search).has('no-reload')) return;\n"
    "  const source = new EventSource('/__dev/events');\n"
    "  source.addEventListener('reload', (event) => {\n"
    "    let changed = [];\n"
    "    try { changed = JSON.parse(event.data).changed || []; } catch { /* 载荷不是 JSON，照样重载 */ }\n"
    "    console.info('[dev-reload] 检测到改动，重新加载：' + changed.join(', '));\n"
    "    window.location.reload();\n"
    "  });\n"
    "})();\n"
    "</script>\n"
)


def inject_reload_client(html: str) -> str:
    """把热更新客户端注入 `index.html`。

    插在 `</body>` **之前**（浏览器解析到 `</body>` 时就已经开始执行脚本，
    放后面在极端情况下会被丢弃）；没有 `</body>` 时追加到末尾。

    幂等：已经注入过就原样返回，避免"服务重启后又注入一次"。
    """
    if _MARKER in html:
        return html
    if "</body>" in html:
        return html.replace("</body>", RELOAD_CLIENT + "</body>", 1)
    return html + RELOAD_CLIENT


class LiveReload:
    """轮询目录指纹，把「文件变了」推给所有已连接的浏览器。"""

    def __init__(self, root: Path, *, interval: float = 0.5, debounce: float = 0.15) -> None:
        self._root = Path(root)
        self._interval = interval
        self._debounce = debounce
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None
        self._snapshot = self._scan()
        self._version = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._subscribers.clear()

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """登记一个订阅者。队列有上限：客户端卡住时宁可丢事件也不占内存。"""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def version(self) -> int:
        return self._version

    # ------------------------------------------------------------------ #
    # 指纹与推送
    # ------------------------------------------------------------------ #

    def _scan(self) -> dict[str, tuple[int, int]]:
        """目录指纹：相对路径 → (mtime_ns, size)。

        用 mtime+size 而不是内容哈希：开发场景只需要回答"变没变"，而哈希
        要求每次轮询把所有文件读一遍。文件名也参与：新增/删除都是变化。
        """
        out: dict[str, tuple[int, int]] = {}
        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                # 编辑器临时文件、正在被删除的文件：跳过，下一轮自然会收敛
                continue
            # 用 POSIX 分隔符：变化的文件名会推到浏览器控制台，输出不应随平台变形
            out[path.relative_to(self._root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
        return out

    def _diff(self, current: dict[str, tuple[int, int]]) -> list[str]:
        changed = [name for name, stamp in current.items() if self._snapshot.get(name) != stamp]
        changed.extend(name for name in self._snapshot if name not in current)
        return sorted(changed)

    def changed(self) -> list[str]:
        """自上次快照以来变化的文件（**不**更新快照）。

        与 `_loop` 共用同一套比较逻辑：新增、修改、删除都要算变化 ——
        漏掉"删除"的后果是删了文件页面还在跑旧模块，同样不会有任何报错。
        """
        return self._diff(self._scan())

    def _publish(self, changed: list[str]) -> None:
        self._version += 1
        payload = {"version": self._version, "changed": changed}
        for queue in list(self._subscribers):
            # 队列满就丢这一条：下一次变更还会再推，而卡住的客户端本来也处理不过来
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def _loop(self) -> None:
        pending: list[str] = []
        quiet_at: float | None = None
        while True:
            await asyncio.sleep(self._interval)
            current = self._scan()
            changed = self._diff(current)
            if changed:
                self._snapshot = current
                pending.extend(changed)
                quiet_at = time.monotonic()
                continue
            # 编辑器保存常常一次落多个文件：等安静下来再推一次，避免连刷两次
            if pending and quiet_at is not None and time.monotonic() - quiet_at >= self._debounce:
                self._publish(sorted(set(pending)))
                pending = []
                quiet_at = None

    # ------------------------------------------------------------------ #
    # SSE 端点
    # ------------------------------------------------------------------ #

    async def events(self, _request: Request) -> StreamingResponse:
        """`GET /__dev/events`：一条只推 `reload` 的 SSE 流。"""
        queue = self.subscribe()

        async def stream() -> AsyncIterator[str]:
            try:
                yield f"event: ready\ndata: {json.dumps({'version': self._version})}\n\n"
                while True:
                    payload = await queue.get()
                    yield f"event: reload\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                # 关闭标签页 / 换页时走到这里，避免订阅集合无限增长
                self.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            # no-store：代理/浏览器都不得缓存这条流
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )


class DevStaticFiles(StaticFiles):
    """开发版静态目录：禁掉缓存，并把热更新客户端注入 `index.html`。"""

    def __init__(self, *, live: LiveReload | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._live = live

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        # 开发时"改了看不到"几乎都出在这里：没有 Cache-Control，浏览器自行其是
        response.headers["Cache-Control"] = "no-store, must-revalidate"

        if self._live is None or path not in ("", ".", "index.html"):
            return response
        if response.status_code != 200:
            return response
        html = self._read_index()
        if html is None:
            return response
        return Response(
            inject_reload_client(html),
            media_type="text/html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    def _read_index(self) -> str | None:
        directory = self.directory
        if directory is None:
            return None
        try:
            return (Path(directory) / "index.html").read_text(encoding="utf-8")
        except OSError:
            return None


def install_dev_frontend(app: FastAPI) -> LiveReload | None:
    """把 `create_app` 挂上的前端目录换成开发版，并挂上热更新事件流。

    两个必须注意的点：

    1. Starlette 按**注册顺序**匹配路由，而 `create_app` 把 `StaticFiles` 挂在
       `/` 且注册在最后 —— 挂在 `/` 的 mount 会吞掉**它之后**注册的一切，
       所以 `/__dev/events` 必须插在挂载点**之前**（`app.add_api_route()`
       只会追加到末尾，等于没挂上，表现为请求 404 或被静态目录接管）；
    2. 目录直接取自已挂载的 `StaticFiles.directory`，不在这里重算「项目根/web」：
       那段解析（相对路径按项目根回退）已经由 `_maybe_mount_frontend` 做过一次，
       再写一份迟早会不一致。

    返回 `LiveReload` 实例（启停由调用方负责）；未挂载前端目录时返回 `None`。
    """
    from starlette.routing import Mount, Route

    routes = app.router.routes
    index = next(
        (
            i
            for i, route in enumerate(routes)
            # 注意：`Mount("/")` 的 `path` 会被 Starlette 归一成**空串**
            # （`Mount.__init__` 里 `path.rstrip("/")`），只比较 "/" 永远找不到
            # 挂载点 —— 表现就是"热更新静默不生效"，页面照常打开。
            if isinstance(route, Mount) and getattr(route, "path", None) in ("", "/")
        ),
        None,
    )
    if index is None:
        return None

    directory = getattr(routes[index].app, "directory", None)
    if directory is None:
        return None

    live = LiveReload(Path(directory))
    routes[index] = Mount(
        "/",
        app=DevStaticFiles(directory=directory, html=True, live=live),
        name="web",
    )
    routes.insert(index, Route("/__dev/events", live.events))
    return live
