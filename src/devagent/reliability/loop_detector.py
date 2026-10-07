"""循环与死锁检测。

对齐 ``docs/03``：Agent 之间可能互相推诿（A 推给 B，B 又推回 A），
或同一节点反复失败形成实质死循环。本模块负责识别并中断。

检测维度：
1. **同节点重试超限**：同一节点尝试次数超过阈值；
2. **重复失败模式**：相同失败原因连续出现；
3. **回退环**：节点 A 的回退反复触发同一组节点。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class LoopDetector:
    """循环检测器。

    用法::

        detector = LoopDetector(max_attempts=3)
        if detector.detect(node_id, attempt):
            ...  # 视为卡死，升级策略
    """

    max_attempts: int = 3
    """同一节点的最大尝试次数；超过即判定卡死。"""

    same_failure_threshold: int = 3
    """相同失败原因连续出现该次数后判定卡死。"""

    max_path_length: int = 20
    """记录的回退路径最大长度（防止内存无限增长）。"""

    _attempts: dict[str, int] = field(default_factory=dict)
    _failure_history: dict[str, deque[str]] = field(default_factory=dict)
    _path: deque[str] = field(default_factory=deque)

    def __post_init__(self) -> None:
        # deque 的 maxlen 必须基于实例字段 max_path_length，
        # 而 default_factory 在构造期无法访问其他字段，
        # 因此在 __post_init__ 中重建（早期实现硬编码 20，导致配置失效）。
        self._path = deque(self._path, maxlen=self.max_path_length)

    # ------------------------------------------------------------------ #
    # 检测
    # ------------------------------------------------------------------ #

    def detect(self, node_id: str, attempt: int) -> bool:
        """判断指定节点是否已陷入循环。

        Args:
            node_id: 节点 id。
            attempt: 当前尝试次数。

        Returns:
            True 表示应中断该节点（视为卡死）。
        """
        self._attempts[node_id] = attempt
        return attempt > self.max_attempts

    def record_failure(self, node_id: str, signature: str) -> bool:
        """记录一次失败；返回是否已达到「重复失败」判定。

        Args:
            node_id: 节点 id。
            signature: 失败特征（如失败标准列表的哈希）。

        Returns:
            True 表示相同失败连续出现已达阈值。
        """
        history = self._failure_history.setdefault(
            node_id, deque(maxlen=self.same_failure_threshold)
        )
        history.append(signature)
        if len(history) < self.same_failure_threshold:
            return False
        return len(set(history)) == 1

    def record_path(self, node_id: str) -> bool:
        """记录回退路径并检测环。

        检测方式：同一节点在最近 N 次回退中出现次数超过阈值时，
        视为循环（比精确环检测更实用——因为回退链可能包含中间节点）。

        Returns:
            True 表示检测到循环。
        """
        self._path.append(node_id)
        threshold = max(2, self.max_attempts - 1)
        return list(self._path).count(node_id) > threshold

    # ------------------------------------------------------------------ #
    # 查询与重置
    # ------------------------------------------------------------------ #

    def attempts_for(self, node_id: str) -> int:
        return self._attempts.get(node_id, 0)

    def path(self) -> list[str]:
        return list(self._path)

    def reset(self, node_id: str | None = None) -> None:
        if node_id is None:
            self._attempts.clear()
            self._failure_history.clear()
            self._path.clear()
            return
        self._attempts.pop(node_id, None)
        self._failure_history.pop(node_id, None)

    def snapshot(self) -> dict[str, object]:
        return {
            "attempts": dict(self._attempts),
            "path": list(self._path),
            "failure_signatures": {k: list(v) for k, v in self._failure_history.items()},
        }


__all__ = ["LoopDetector"]
