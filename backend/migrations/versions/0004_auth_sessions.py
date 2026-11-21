"""登录：users 增加角色、状态与强制改密，新增 user_sessions。

is_active 由 status 取代。本阶段数据库按 D17 重建，users 表为空，不做数据转换。

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKS = (
    ("ck_users_role", "role IN ('user', 'admin')"),
    ("ck_users_status", "status IN ('pending', 'active', 'rejected', 'disabled')"),
    ("ck_users_username_lower", "username = lower(username)"),
    ("ck_users_email_lower", "email = lower(email)"),
)


def upgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM users)")).scalar():
        raise RuntimeError("users 表已有数据；本迁移不转换旧账号，请按 D17 重建数据库后再执行。")

    op.drop_column("users", "is_active")
    op.add_column("users", sa.Column("role", sa.String(length=16), server_default="user", nullable=False))
    op.add_column("users", sa.Column("status", sa.String(length=16), server_default="pending", nullable=False))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    for name, condition in CHECKS:
        op.create_check_constraint(name, "users", condition)

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("idle_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")
    for name, _ in CHECKS:
        op.drop_constraint(name, "users", type_="check")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "status")
    op.drop_column("users", "role")
    op.add_column("users", sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False))
