from sqlalchemy import (
    String,
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class PendingActionORM(Base):
    __tablename__ = "pending_actions"

    # 操作提案主键
    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    # 提案归属，与所操作任务的 user_id 一致
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    # 被操作的任务 ID
    task_id: Mapped[int] = mapped_column(
        index=True,
        nullable=False
    )

    # 操作类型：update / delete
    action_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False
    )

    # 状态：默认等待确认
    status: Mapped[str] = mapped_column(
        String(16),
        default="pending",
        nullable=False
    )

    # 创建提案时任务的版本
    expected_version: Mapped[int] = mapped_column(
        nullable=False
    )

    # 同一轮 Agent 请求中的独立提案可以同时待确认。
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # 操作前快照
    before_snapshot: Mapped[dict] = mapped_column(
        JSON,
        nullable=False
    )

    # 操作后快照
    after_snapshot: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True),
        nullable=True
    )

    # UTC Unix 时间戳（秒）
    created_at: Mapped[int] = mapped_column(
        nullable=False
    )

    expires_at: Mapped[int] = mapped_column(
        nullable=False
    )

    # 数据库层约束
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('update', 'delete')",
            name="ck_action_type"
        ),
        CheckConstraint(
            "status IN ("
            "'pending', 'executed', 'cancelled', "
            "'expired', 'conflicted', 'replaced', 'failed')",
            name="ck_action_status"
        ),
        Index("ix_pending_actions_user_id_status", "user_id", "status"),
    )
