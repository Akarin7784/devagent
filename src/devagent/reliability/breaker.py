"""成本与步数熔断。

AI 系统最怕「跑飞」——反复重试导致 token 与费用失控。
熔断器提供硬性上限，超限立即中断并保留中间产物。

设计要点：
- **双重限制**：token 总量 + 步骤数，任一超限即触发；
- **可恢复**：熔断是「暂停」而非「失败」——保留已完成产出，
  用户可提高上限后继续；
- **提前预警**：接近阈值时先告警，留出人工介入窗口。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceededError(RuntimeError):
    """超出预算或步数上限。"""

    def __init__(self, message: str, *, kind: str, used: int, limit: int) -> None:
        super().__init__(message)
        self.kind = kind
        self.used = used
        self.limit = limit

    @property
    def ratio(self) -> float:
        return self.used / self.limit if self.limit else 0.0


@dataclass(slots=True)
class CircuitBreaker:
    """token / 步数熔断器。

    用法::

        breaker = CircuitBreaker(max_tokens=500_000, max_steps=50)
        breaker.charge(tokens=1200, steps=1)   # 超限抛 BudgetExceededError
        print(breaker.snapshot())
    """

    max_tokens: int = 500_000
    max_steps: int = 50
    warn_ratio: float = 0.8
    """预警阈值：用量达到该比例时发出告警（但不中断）。"""

    tokens_used: int = 0
    steps_used: int = 0
    warnings: list[str] = None  # type: ignore[assignment]
    _warned: set[str] = field(default_factory=set)
    """已发出过预警的维度（tokens / steps），保证每个维度只告警一次。

    早先的判断是 ``"tokens" not in str(self.warnings)`` —— 而实际文案是
    ``f"token 用量已达 {ratio:.0%}"``，里面并没有子串 "tokens"（是 "token " 加空格）。
    于是判据恒为真：比例一旦超过阈值，**每次 charge() 都会再追加一条**。
    编排器每轮调度都会 ``charge(tokens=0, steps=0)``，实测在 85% 恒定比例下
    连调 6 次就得到 6 条一模一样的告警，列表随任务长度无界增长。
    """

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []

    # ------------------------------------------------------------------ #
    # 记账与检查
    # ------------------------------------------------------------------ #

    def charge(self, *, tokens: int = 0, steps: int = 0) -> None:
        """记账并检查上限。

        Raises:
            BudgetExceededError: 超出 token 或步数上限。
        """
        self.tokens_used += max(0, tokens)
        self.steps_used += max(0, steps)

        if self.tokens_used > self.max_tokens:
            raise BudgetExceededError(
                f"token 预算耗尽：{self.tokens_used} > {self.max_tokens}",
                kind="tokens",
                used=self.tokens_used,
                limit=self.max_tokens,
            )
        if self.steps_used > self.max_steps:
            raise BudgetExceededError(
                f"步骤数超限：{self.steps_used} > {self.max_steps}",
                kind="steps",
                used=self.steps_used,
                limit=self.max_steps,
            )
        self._maybe_warn()

    def _maybe_warn(self) -> None:
        """跨过预警线时**只告警一次**（按维度去重，而不是按文本猜）。"""
        if self.max_tokens:
            token_ratio = self.tokens_used / self.max_tokens
            if token_ratio >= self.warn_ratio and "tokens" not in self._warned:
                self._warned.add("tokens")
                self.warnings.append(f"token 用量已达 {token_ratio:.0%}")
        if self.max_steps:
            step_ratio = self.steps_used / self.max_steps
            if step_ratio >= self.warn_ratio and "steps" not in self._warned:
                self._warned.add("steps")
                self.warnings.append(f"步骤用量已达 {step_ratio:.0%}")

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.tokens_used)

    def remaining_steps(self) -> int:
        return max(0, self.max_steps - self.steps_used)

    def is_exhausted(self) -> bool:
        return self.tokens_used >= self.max_tokens or self.steps_used >= self.max_steps

    def snapshot(self) -> dict[str, object]:
        return {
            "tokens_used": self.tokens_used,
            "tokens_limit": self.max_tokens,
            "steps_used": self.steps_used,
            "steps_limit": self.max_steps,
            "remaining_tokens": self.remaining_tokens(),
            "remaining_steps": self.remaining_steps(),
            "exhausted": self.is_exhausted(),
            "warnings": list(self.warnings),
        }

    def reset(self) -> None:
        self.tokens_used = 0
        self.steps_used = 0
        self.warnings.clear()
        self._warned.clear()


__all__ = ["BudgetExceededError", "CircuitBreaker"]
