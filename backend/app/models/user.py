"""账号与登录会话。"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class UserORM(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 用户名与邮箱保存前统一转小写，唯一约束因此等同于"不区分大小写唯一"。
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    # 只保存 Argon2id 哈希（含参数和盐），不存明文。
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user", server_default="user")
    # pending：待审核；active：可登录；rejected：注册被拒；disabled：被停用。只有 active 能登录。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    # 管理员重置密码后为 true：登录后只能修改密码。
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 最近一次审核（通过或拒绝）的时间和管理员。只有 pending/rejected 账号能被删除，审核人一定是 active 管理员，
    # 所以这个自引用外键不会挡住删除。
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", name="fk_users_reviewed_by"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="ck_users_role"),
        CheckConstraint("status IN ('pending', 'active', 'rejected', 'disabled')", name="ck_users_status"),
        CheckConstraint("username = lower(username)", name="ck_users_username_lower"),
        CheckConstraint("email = lower(email)", name="ck_users_email_lower"),
    )


class UserSessionORM(Base):
    """登录会话。浏览器 Cookie 里是随机令牌原文，这里只存它的 SHA-256。"""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 以下时间均为 UTC Unix 时间戳（秒）。
    created_at: Mapped[int] = mapped_column(nullable=False)
    last_seen_at: Mapped[int] = mapped_column(nullable=False)
    # 绝对过期时间；不勾选"保持登录"时还受 idle_timeout_seconds 约束。
    expires_at: Mapped[int] = mapped_column(nullable=False)
    idle_timeout_seconds: Mapped[int | None] = mapped_column(nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
