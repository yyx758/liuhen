"""页面内通知；提醒到点时生成，邮件发送状态也记在这里（outbox：同一行既是通知也是待发邮件）。

task_id、reminder_id 不加外键：任务删除后通知仍保留，内容来自生成时的快照。
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base

KINDS = ("reminder", "missed")
EMAIL_STATUSES = ("not_needed", "pending", "sending", "sent", "skipped", "failed")


class NotificationORM(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", name="fk_notifications_user_id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    task_id: Mapped[int] = mapped_column(nullable=False)
    reminder_id: Mapped[int | None] = mapped_column(nullable=True)
    # 快照：生成通知时的任务标题与基准时间（start_at，没有时为 due_at）。
    task_title: Mapped[str] = mapped_column(String(100), nullable=False)
    task_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 以下只用于邮件重试与防重，不对用户展示（PRD R14）。
    email_status: Mapped[str] = mapped_column(String(16), nullable=False)
    email_attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    # pending：下次可以尝试的时间；sending：租约到期时间，到期仍未完成视为发送结果未知，可以重发。
    email_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN (" + ", ".join(f"'{k}'" for k in KINDS) + ")", name="ck_notifications_kind"),
        CheckConstraint(
            "email_status IN (" + ", ".join(f"'{s}'" for s in EMAIL_STATUSES) + ")",
            name="ck_notifications_email_status",
        ),
        Index("ix_notifications_user_id_id", "user_id", "id"),
        Index("ix_notifications_unread", "user_id", postgresql_where=text("read_at IS NULL")),
        Index("ix_notifications_email_due", "email_status", "email_next_attempt_at"),
    )
