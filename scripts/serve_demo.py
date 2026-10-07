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


def build_app(port: int = 8812):
    """构造注入了 DemoProvider 的 FastAPI 应用。

    关键点：**orchestrator 必须用新网关重建**。
    只替换 `app.state.gateway` 是无效的 —— `create_app` 在 lifespan 里
    已经用旧网关构造了 `Orchestrator`，而 Orchestrator 是按值持有网关引用的。
    这是一个很容易踩的坑：替换后「看起来什么都没变」，任务依旧走真实供应商。
    """
    from demo_smoke import DemoProvider

    from devagent.api.app import create_app
    from devagent.api.service import TaskService
    from devagent.config import get_settings
    from devagent.models.gateway import ModelGateway
    from devagent.orchestration import Orchestrator

    settings = get_settings()
    app = create_app(settings)
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
                f"[serve_demo] 已注入脚本化假模型，"
                f"打开 web/index.html?api=http://127.0.0.1:{port} 查看 DAG",
                flush=True,
            )
            yield

    app.router.lifespan_context = lifespan
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="DevAgent 演示服务器（假模型 + 真实 API）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8812)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(build_app(args.port), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
