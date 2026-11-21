"""账号事件：注册、审核、登录、改密等账号层面的只追加记录。

target_user_id 与 actor_user_id 都只存整数、不加外键：pending/rejected 账号可以被删除，
而它的注册、审核记录要保留下来（注册事件的操作者就是注册人自己）。
"""

from sqlalchemy import CheckConstraint, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base

EVENT_TYPES = (
    "user_registered", "user_approved", "user_rejected", "user_disabled", "user_enabled", "user_deleted",
    "password_changed", "password_reset", "email_changed", "role_changed", "chat_limit_changed",
    "login_succeeded", "login_failed", "login_locked", "session_revoked",
)


class AccountEventORM(Base):
    __tablename__ = "account_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_user_id: Mapped[int] = mapped_column(nullable=False)
    # 本人操作时等于 target_user_id；命令行操作为空。
    actor_user_id: Mapped[int | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(nullable=True)
    # 变更前后的值、来源（web/cli）、IP 等。
    details: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    # UTC Unix 时间戳（秒），与 operation_events 一致。
    created_at: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event_type IN (" + ", ".join(f"'{name}'" for name in EVENT_TYPES) + ")",
            name="ck_account_event_type",
        ),
        Index("ix_account_events_target_user_id_id", "target_user_id", "id"),
    )
