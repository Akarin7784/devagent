"""SQLAlchemy ORM 模型。

## 为什么把 ORM 模型和领域模型分开

``devagent.models.domain`` 里的是**领域模型**（frozen Pydantic，服务于 Agent
之间的强类型握手）；这里是**持久化模型**（可变、带主键与外键、服务于存储）。

刻意不共用，理由有三：

1. 领域模型要求 ``frozen=True`` 与 ``extra="forbid"``，与 ORM 的可变映射冲突；
2. 存储结构（任务表/节点表/步骤表）是为**查询**设计的，与 Agent 视角的数据
   形状不同 —— 强行统一会让两边都别扭；
3. 数据库 schema 演进的节奏与领域模型不同，解耦后可以独立迁移。

两者之间的边界是本模块的 ``to_dict()`` / ``from_dict()``：API 层只认
普通的 ``dict[str, Any]``（``TaskStore`` 协议就是这么定义的），
因此 ORM 细节不会泄漏到上层。

## 类型选择的注意事项

- 用 SQLAlchemy 2.0 的 ``Mapped[...]`` / ``mapped_column`` 新式声明，
  以获得 mypy 静态检查能力（旧式 ``Column`` 在 strict 模式下到处是 Any）。
- ``JSON`` 而非 ``JSONB``：SQLite 不支持 JSONB，而我们需要 SQLite 作为
  测试与轻量部署的后端。Postgres 下 ``JSON`` 会退化为 ``json`` 类型，
  功能等价（只是没有索引与包含查询能力，本项目的用法不需要）。
- 时间戳统一用 ``float``（Unix 秒）而不是 ``DateTime``：与时序对接更直接，
  且避免时区处理在 SQLite/Postgres 上的行为差异。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


class TaskRow(Base):
    """任务主表。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 步骤与 DAG 节点以 JSON 整体存储：它们只被整体读写，从不按字段查询。
    # 拆表会带来 join 成本与映射复杂度，却换不到查询能力。
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    nodes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    context_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    __table_args__ = (
        # 列表页最常见的查询：按状态过滤 + 按时间倒序
        Index("ix_tasks_status_created", "status", "created_at"),
        Index("ix_tasks_created", "created_at"),
    )

    events: Mapped[list[TaskEventRow]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin"
    )


class TaskEventRow(Base):
    """任务事件表（SSE 事件流的历史持久化）。

    为什么需要：内存 ``EventBus`` 的历史缓冲随进程消失。任务跑很久、
    进程重启、或用户半小时后才打开页面时，前端会看到空时间线。
    落库后 SSE 可以回放到任意历史时刻。
    """

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    __table_args__ = (
        # (task_id, seq) 唯一：序列号在任务内单调递增，天然去重且可断点续拉
        UniqueConstraint("task_id", "seq", name="uq_task_events_task_seq"),
        Index("ix_task_events_task_seq", "task_id", "seq"),
    )

    task: Mapped[TaskRow] = relationship(back_populates="events")


__all__ = ["Base", "TaskEventRow", "TaskRow"]
