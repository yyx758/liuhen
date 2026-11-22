"""注册审核：users 增加审核时间与审核人，新增只追加的 account_events。

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_TYPES = (
    "user_registered", "user_approved", "user_rejected", "user_disabled", "user_enabled", "user_deleted",
    "password_changed", "password_reset", "email_changed", "role_changed", "chat_limit_changed",
    "login_succeeded", "login_failed", "login_locked", "session_revoked",
)


def upgrade() -> None:
    op.add_column("users", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("reviewed_by", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_users_reviewed_by", "users", "users", ["reviewed_by"], ["id"])

    # 迁移脚本是历史快照，事件类型在这里抄一份，不从模型导入：模型以后再改，这个文件也不能变。
    op.create_table(
        "account_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("target_user_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("details", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "event_type IN (" + ", ".join(f"'{name}'" for name in EVENT_TYPES) + ")",
            name="ck_account_event_type",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_account_events_target_user_id_id", "account_events", ["target_user_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_account_events_target_user_id_id", table_name="account_events")
    op.drop_table("account_events")
    op.drop_constraint("fk_users_reviewed_by", "users", type_="foreignkey")
    op.drop_column("users", "reviewed_by")
    op.drop_column("users", "reviewed_at")
