"""任务提醒的触发状态。每行的状态只单向变化：pending → fired / missed / cancelled。

任务改期时不修改旧行，而是把旧的 pending 作废并插入新行，便于排查"为什么提醒了 / 为什么没提醒"。
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base

STATUSES = ("pending", "fired", "missed", "cancelled")


class TaskReminderORM(Base):
    __tablename__ = "task_reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", name="fk_task_reminders_user_id"), nullable=False)
    # 任务是物理删除的；提醒随任务一起删除，已生成的通知里有快照。
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", name="fk_task_reminders_task_id", ondelete="CASCADE"), nullable=False,
    )
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 触发、错过或作废的时间。
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{name}'" for name in STATUSES) + ")", name="ck_task_reminders_status",
        ),
        # 同一任务最多一条待触发的提醒。
        Index("uq_task_reminders_pending_task", "task_id", unique=True, postgresql_where=text("status = 'pending'")),
        # 轮询：WHERE status = 'pending' AND remind_at <= now。
        Index("ix_task_reminders_status_remind_at", "status", "remind_at"),
    )
