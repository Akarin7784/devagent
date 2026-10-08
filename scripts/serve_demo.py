"""本地演示服务器：把脚本化假模型注入 API，使完整 DAG 能流经 HTTP + SSE。

## 为什么需要这个（而不只是 demo_smoke.py）

`demo_smoke.py` 直接调 `Orchestrator`，绕过了 API 层 —— 它验证不了
「DAG 节点事件是否真的通过 SSE 抵达前端」这条链路。

而没有 API Key 时，真实供应商会在「需求澄清」阶段就失败，任务详情里的
`nodes` 恒为空数组，前端 DAG 面板永远只能显示空态。这对开发前端是致命的：
看不见数据就没法调样式，只能靠脑补。

因此这个脚本做一件很具体的事：**在 app 启动后把 `app.state.gateway`
替换为注入了 `DemoProvider` 的网关**。API 的路由、service、EventBus、
SSE 全是真实代码路径，只有最底层的模型调用被脚本化。

用法::

    PYTHONPATH=src python scripts/serve_demo.py --port 8812
    # 然后浏览器打开 web/index.html?api=http://127.0.0.1:8812

注意：这是**开发工具**，不进生产镜像，也不要暴露到公网 —— 它没有任何
鉴权，且会执行固定的脚本化响应而不是真实 LLM 推理。
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))


def build_app(port: int = 8812, web_dir: str | None = "web", live_reload: bool = True):
    """构造注入了 DemoProvider 的 FastAPI 应用。

    关键点一：**orchestrator 必须用新网关重建**。
    只替换 `app.state.gateway` 是无效的 —— `create_app` 在 lifespan 里
    已经用旧网关构造了 `Orchestrator`，而 Orchestrator 是按值持有网关引用的。
    这是一个很容易踩的坑：替换后「看起来什么都没变」，任务依旧走真实供应商。

    关键点二：**默认把 `web/` 挂到根路径**（同源托管）。
    分端口运行时前端必须靠 `?api=` 才知道后端在哪，这个参数一旦丢失
    （直接敲 5173、或从收藏夹打开）界面就报「无法连接到服务端」。
    同源后基址恒为空，问题从根上消失。传 `web_dir=None` 可关闭。

    关键点三：**前端热更新**（见 `dev_reload.py`）。零构建的原生 ES 模块没有
    HMR，而 `StaticFiles` 不发 `Cache-Control`，浏览器会按启发式规则缓存 ——
    改完刷新还是旧的是常态。这里换成 `DevStaticFiles`（no-store）并挂一条
    `/__dev/events` 的 SSE 流，改 `web/` 下任何文件浏览器自动整页重载。
    """
    from demo_smoke import DemoProvider
    from dev_reload import LiveReload, install_dev_frontend

    from devagent.api.app import create_app
    from devagent.api.service import TaskService
    from devagent.config import get_settings
    from devagent.models.gateway import ModelGateway
    from devagent.orchestration import Orchestrator

    settings = get_settings()
    # 演示服务器默认同源托管前端；通过环境变量可覆盖。
    if web_dir is not None:
        settings.web_dir = web_dir
    app = create_app(settings)

    live: LiveReload | None = install_dev_frontend(app) if live_reload else None
    if live_reload and live is None:
        print(
            "[serve_demo] 未挂载前端静态目录，热更新不可用（--web-dir 为空？）",
            flush=True,
        )

    # `app.router.lifespan_context` 已经是 `@asynccontextmanager` 包装后的
    # **可调用对象**（Starlette 在 `Router.__init__` 里替原始 lifespan 包好了）。
    # 两个必须同时满足的约束：
    #   1. 调用它 → `async with original(inner_app)`；
    #   2. 我们自己的 `lifespan` 是**裸异步生成器函数**，直接赋给
    #      `lifespan_context` 会被 Starlette 当作「已经是包装好的对象」而
    #      不再包装，运行时就会报
    #      `'async_generator' object does not support the asynchronous
    #      context manager protocol`。因此必须自己 `asynccontextmanager` 一次。
    original_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(inner_app):
        async with original_lifespan(inner_app):
            provider = DemoProvider()
            gateway = ModelGateway(
                settings,
                providers={"deepseek": provider, "qwen": provider, "zhipu": provider},
            )
            orchestrator = Orchestrator(settings, gateway=gateway)
            inner_app.state.gateway = gateway
            inner_app.state.orchestrator = orchestrator
            # service 同样按值持有 orchestrator，必须一并替换
            inner_app.state.task_service = TaskService(
                orchestrator,
                store=inner_app.state.store,
                bus=inner_app.state.bus,
            )
            print(
                f"[serve_demo] 已注入脚本化假模型\n"
                f"[serve_demo] 界面与接口同源，直接打开：http://127.0.0.1:{port}/",
                flush=True,
            )
            if live is not None:
                await live.start()
                print(
                    "[serve_demo] 前端热更新已开启：改 web/ 下任何文件，浏览器自动刷新"
                    "（--no-reload 可关闭）",
                    flush=True,
                )
            try:
                yield
            finally:
                if live is not None:
                    await live.stop()

    app.router.lifespan_context = lifespan
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="DevAgent 演示服务器（假模型 + 真实 API）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8812)
    parser.add_argument(
        "--web-dir",
        default="web",
        help="前端静态目录（相对项目根）；传空字符串则仅提供 API",
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="关闭前端热更新（默认开启：改 web/ 自动刷新浏览器）",
    )
    args = parser.parse_args()

    import uvicorn

    app = build_app(args.port, web_dir=args.web_dir or None, live_reload=not args.no_reload)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
