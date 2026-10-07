"""端到端冒烟脚本：不依赖任何 API Key，跑通完整流水线。

用途：
1. 贡献者 clone 项目后立刻能验证「代码能跑」，无需先申请模型 API Key；
2. CI 中作为集成冒烟测试（比单元测试更接近真实调用链）；
3. 面试演示：一条命令展示 DAG 编排、验证回退、上下文工程指标全链路。

原理：用 `DemoProvider`（脚本化假模型）替换真实供应商，按系统提示词关键词
路由到不同的预设响应，从而驱动 Requirement→Architect→Coder→Tester→Verifier
完整流程。

用法::

    python scripts/demo_smoke.py
    python scripts/demo_smoke.py --goal "为订单接口增加幂等性"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from devagent.config import get_settings  # noqa: E402
from devagent.models.gateway import ModelGateway  # noqa: E402
from devagent.models.provider import (  # noqa: E402
    ChatMessage,
    ChatResult,
    ModelProvider,
    TokenUsage,
)

# --------------------------------------------------------------------------- #
# 预设响应：按系统提示词里的角色关键词路由
# --------------------------------------------------------------------------- #

_CRIT_1 = "GET /users?page=2&page_size=20 返回第 2 页且条数不超过 20"
_CRIT_2 = "响应体包含 total 字段且等于全表用户数"
_CRIT_3 = "page_size 超过 100 时被钳制为 100"

_REQUIREMENT = json.dumps(
    {
        "goal": "为用户列表接口增加分页能力",
        "acceptance_criteria": [_CRIT_1, _CRIT_2, _CRIT_3],
        "constraints": ["不改变既有响应字段的语义"],
        "open_questions": ["是否需要支持按 cursor 的游标分页？"],
        "relevant_files": ["src/users/repository.py"],
    },
    ensure_ascii=False,
)

_ARCHITECT = json.dumps(
    {
        "approach": (
            "在服务层引入不可变的 PaginationParams 值对象统一解析/钳制入参，"
            "仓储层新增 list_page 与 count 两个方法。选值对象而非散装参数，"
            "是为了让「钳制规则」只有一处定义、可被单测直接覆盖。"
        ),
        "nodes": [
            {
                "id": "N1",
                "goal": "实现 PaginationParams 并完成边界钳制",
                "agent_type": "coder",
                "deps": [],
                "acceptance_criteria": [_CRIT_1, _CRIT_3],
            },
            {
                "id": "N2",
                "goal": "仓储层实现分页查询与总数统计",
                "agent_type": "coder",
                "deps": ["N1"],
                "acceptance_criteria": [_CRIT_1, _CRIT_2],
            },
            {
                "id": "N3",
                "goal": "为分页边界与总数计算补充测试",
                "agent_type": "tester",
                "deps": ["N1", "N2"],
                "acceptance_criteria": [_CRIT_1, _CRIT_2, _CRIT_3],
            },
        ],
    },
    ensure_ascii=False,
)

_CODER = json.dumps(
    {
        "summary": "实现分页参数值对象与仓储分页查询",
        "changes": [
            {
                "file": "src/users/pagination.py",
                "reason": "把钳制规则集中到一处，避免各处重复实现导致不一致",
                "addresses_criteria": [_CRIT_1, _CRIT_3],
                "diff": (
                    "+from dataclasses import dataclass\n"
                    "+\n"
                    "+MAX_PAGE_SIZE = 100\n"
                    "+\n"
                    "+@dataclass(frozen=True)\n"
                    "+class PaginationParams:\n"
                    "+    page: int = 1\n"
                    "+    page_size: int = 20\n"
                    "+\n"
                    "+    @classmethod\n"
                    "+    def from_query(cls, page: int, page_size: int) -> 'PaginationParams':\n"
                    "+        return cls(page=max(1, page), page_size=min(max(1, page_size), MAX_PAGE_SIZE))\n"
                    "+\n"
                    "+    @property\n"
                    "+    def offset(self) -> int:\n"
                    "+        return (self.page - 1) * self.page_size\n"
                ),
            },
            {
                "file": "src/users/repository.py",
                "reason": "提供带 limit/offset 的查询与独立的 count 查询",
                "addresses_criteria": [_CRIT_1, _CRIT_2],
                "diff": (
                    "+    def list_page(self, params: PaginationParams) -> list[User]:\n"
                    "+        return self._fetch(limit=params.page_size, offset=params.offset)\n"
                    "+\n"
                    "+    def count(self) -> int:\n"
                    "+        return self._count_all()\n"
                ),
            },
        ],
        "unresolved": ["大 offset 下 count 与 list 之间可能存在数据漂移，需在文档中注明"],
    },
    ensure_ascii=False,
)

_TESTER = json.dumps(
    {
        "test_files": [
            {
                "path": "tests/users/test_pagination.py",
                "content": (
                    "import pytest\n"
                    "from users.pagination import PaginationParams, MAX_PAGE_SIZE\n\n\n"
                    "@pytest.mark.parametrize(\n"
                    "    ('page', 'page_size', 'exp_page', 'exp_size'),\n"
                    "    [(0, 0, 1, 1), (1, 20, 1, 20), (2, 20, 2, 20), (1, 1000, 1, MAX_PAGE_SIZE)],\n"
                    ")\n"
                    "def test_clamp(page, page_size, exp_page, exp_size):\n"
                    "    p = PaginationParams.from_query(page, page_size)\n"
                    "    assert (p.page, p.page_size) == (exp_page, exp_size)\n"
                ),
                "covers_criteria": [_CRIT_1, _CRIT_3],
            }
        ],
        "cases": [
            {
                "name": "test_clamp",
                "covers_criterion": _CRIT_3,
                "description": "验证 page/page_size 的边界钳制行为",
            },
            {
                "name": "test_offset",
                "covers_criterion": _CRIT_1,
                "description": "验证第 2 页的 offset 计算正确",
            },
        ],
    },
    ensure_ascii=False,
)

_VERIFIER_PASS = json.dumps(
    {
        "verdict": "pass",
        "criterion_checks": [
            {
                "criterion": _CRIT_1,
                "passed": True,
                "reason": "pagination.py 的 offset 属性与 list_page 的 limit/offset 传递可支撑第 2 页查询",
            },
            {"criterion": _CRIT_2, "passed": True, "reason": "repository.count() 已实现全表统计"},
            {
                "criterion": _CRIT_3,
                "passed": True,
                "reason": "from_query 中 min(max(1, size), MAX_PAGE_SIZE) 完成钳制",
            },
        ],
        "suggestions": [],
        "root_cause": "",
        "lesson": "",
    },
    ensure_ascii=False,
)

_VERIFIER_FAIL = json.dumps(
    {
        "verdict": "reject",
        "criterion_checks": [
            {
                "criterion": _CRIT_2,
                "passed": False,
                "reason": "仓储层未见 count 查询实现，total 字段无数据来源",
            },
        ],
        "suggestions": ["在 UserRepository 中补充 count() 并在响应体回填 total"],
        "root_cause": "编码阶段遗漏了总数统计这一条验收标准",
        "lesson": "每完成一个改动应逐条回戳验收标准，确认无遗漏再提交验证",
    },
    ensure_ascii=False,
)


class DemoProvider(ModelProvider):
    """脚本化假模型：按系统提示词中的角色关键词返回预设响应。

    只在冒烟脚本中使用。它会**真实经过**上下文装配、DAG 调度、
    Verifier 判定与回退逻辑，因此能覆盖绝大部分集成路径。
    """

    name = "demo"
    default_model = "demo-scripted"

    def __init__(
        self, *, verifier_always_fail: bool = False, fail_once_then_pass: bool = True
    ) -> None:
        self.calls: list[str] = []
        self._verifier_always_fail = verifier_always_fail
        self._fail_once_then_pass = fail_once_then_pass
        self._verify_count = 0

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        system = "\n".join(m.content for m in messages if m.role == "system")
        self.calls.append(system[:40])

        if "验证工程师" in system or "Verifier" in system:
            self._verify_count += 1
            if self._verifier_always_fail:
                content = _VERIFIER_FAIL
            elif self._fail_once_then_pass and self._verify_count == 1:
                # 第一次判失败 → 触发回退重试路径（这是本脚本最有价值的覆盖）
                content = _VERIFIER_FAIL
            else:
                content = _VERIFIER_PASS
        elif "需求分析" in system:
            content = _REQUIREMENT
        elif "架构" in system:
            content = _ARCHITECT
        elif "测试工程" in system:
            content = _TESTER
        elif "工程师" in system:
            content = _CODER
        else:
            content = _CODER

        prompt_tokens = sum(len(m.content) for m in messages) // 3
        return ChatResult(
            content=content,
            model=model,
            provider=self.name,
            usage=TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=len(content) // 3),
            latency_ms=17,
        )

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        # 确定性伪向量：保证上下文装配的「相关性」打分可复现
        out: list[list[float]] = []
        for text in texts:
            h = hash(text) & 0xFFFFFFFF
            out.append([((h >> (i * 3)) & 0xFF) / 255.0 for i in range(8)])
        return out

    async def aclose(self) -> None:
        return None

    def estimate_cost(self, usage: TokenUsage, *, model: str = "") -> float:
        return usage.prompt_tokens * 1e-6 + usage.completion_tokens * 2e-6


def main() -> int:
    parser = argparse.ArgumentParser(description="DevAgent 端到端冒烟（无需 API Key）")
    parser.add_argument("goal", nargs="?", default="为用户列表接口增加分页能力", help="需求描述")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    from devagent.orchestration import Orchestrator

    settings = get_settings()
    provider = DemoProvider()
    # 关键：把假模型注册成三个供应商名，路由到任意档位都会命中它
    gateway = ModelGateway(
        settings,
        providers={"deepseek": provider, "qwen": provider, "zhipu": provider},
    )
    orch = Orchestrator(settings, gateway=gateway)

    import asyncio

    result = asyncio.run(orch.run(args.goal))

    payload: dict[str, Any] = {
        "task_id": result.task_id,
        "status": result.status.value if hasattr(result.status, "value") else str(result.status),
        "steps": [
            f"{s.agent.value}:{'ok' if s.feedback is None else s.feedback.verdict}"
            for s in result.steps
        ],
        "nodes": {
            node_id: state.status.value
            for node_id, state in (result.dag.states.items() if result.dag else {})
        },
        "total_tokens": result.total_tokens,
        "total_cost_usd": round(result.total_cost_usd, 6),
        "duration_ms": result.duration_ms,
        "model_calls": len(provider.calls),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("=" * 66)
        print(f"任务 {payload['task_id']}  状态={payload['status']}")
        print("=" * 66)
        print(f"模型调用次数 : {payload['model_calls']}")
        print(f"DAG 节点     : {json.dumps(payload['nodes'], ensure_ascii=False)}")
        print(f"执行步骤     : {' -> '.join(payload['steps'])}")
        print(f"token 总量   : {payload['total_tokens']}   成本: ${payload['total_cost_usd']}")
        print(f"耗时         : {payload['duration_ms']} ms")

        metrics = _collect_metrics()
        if metrics:
            print("-" * 66)
            print("上下文工程指标（核心卖点，量化 token 节省）：")
            for key, value in metrics.items():
                print(f"  {key:24s}= {value}")
        print("=" * 66)

    asyncio.run(gateway.aclose())
    ok = payload["status"] == "succeeded" and payload["model_calls"] >= 5
    print("SMOKE OK" if ok else "SMOKE FAILED")
    return 0 if ok else 1


def _collect_metrics() -> dict[str, Any]:
    """从进程级可观测性单例中提取上下文工程指标。"""
    from devagent.observability import get_observability

    obs = get_observability()
    if not obs.enabled:
        # 冒烟默认开启指标（零依赖，纯内存），便于直接看到上下文节省率
        return {}
    snapshot = obs.metrics.snapshot()
    out: dict[str, Any] = {}
    for name, values in snapshot.get("counters", {}).items():
        if "context" in name or "token" in name:
            out[name] = sum(values.values()) if isinstance(values, dict) else values
    return out


if __name__ == "__main__":
    raise SystemExit(main())
