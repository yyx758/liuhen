"""邮箱验证：users 增加 email_verified_at；新增 email_verification_codes。

已有邮箱一律视为未验证（email_verified_at 为空），用户可在个人设置里通过验证码验证。

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_users_email_verified", "users", "email IS NOT NULL OR email_verified_at IS NULL")

    op.create_table(
        "email_verification_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('register', 'change_email', 'reset_password')", name="ck_email_verification_codes_purpose",
        ),
        sa.CheckConstraint("(purpose = 'register') = (user_id IS NULL)", name="ck_email_verification_codes_user"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_email_verification_codes_user_id", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_verification_codes_email_created_at", "email_verification_codes", ["email", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_verification_codes_email_created_at", table_name="email_verification_codes")
    op.drop_table("email_verification_codes")
    op.drop_constraint("ck_users_email_verified", "users", type_="check")
    op.drop_column("users", "email_verified_at")
