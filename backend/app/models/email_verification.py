"""邮箱验证码。只存验证码的 SHA-256；每条记录只能成功使用一次。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base

PURPOSES = ("register", "change_email", "reset_password")


class EmailVerificationCodeORM(Base):
    __tablename__ = "email_verification_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    # 规范化（去空格、小写）后的收件邮箱。
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    # change_email / reset_password 绑定发起或被找回的用户；register 时账号还不存在，为空。
    # 只有 pending / rejected 账号能被删除，它们不可能有这两种验证码，CASCADE 只是兜底。
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_email_verification_codes_user_id", ondelete="CASCADE"), nullable=True,
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    failed_attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 验证成功的时间。
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 被新验证码替换或输错次数过多而作废的时间。
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "purpose IN (" + ", ".join(f"'{name}'" for name in PURPOSES) + ")",
            name="ck_email_verification_codes_purpose",
        ),
        CheckConstraint(
            "(purpose = 'register') = (user_id IS NULL)", name="ck_email_verification_codes_user",
        ),
        Index("ix_email_verification_codes_email_created_at", "email", "created_at"),
    )
