"""每日对话上限：users 增加 daily_chat_limit；deleted_chat_requests 记录被删轮次的创建时间。

删除会话会删掉 chat_turns，用量统计改为 chat_turns 与 deleted_chat_requests 两处相加。
已有的删除记录无法知道原轮次时间，用删除时间回填（只可能把旧轮次算到更晚的一天）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("daily_chat_limit", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_users_daily_chat_limit", "users", "daily_chat_limit BETWEEN 1 AND 1000")

    op.add_column("deleted_chat_requests", sa.Column("turn_created_at", sa.Integer(), nullable=True))
    op.execute("UPDATE deleted_chat_requests SET turn_created_at = deleted_at")
    op.alter_column("deleted_chat_requests", "turn_created_at", nullable=False)
    op.create_index(
        "ix_deleted_chat_requests_user_id_turn_created_at", "deleted_chat_requests", ["user_id", "turn_created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_deleted_chat_requests_user_id_turn_created_at", table_name="deleted_chat_requests")
    op.drop_column("deleted_chat_requests", "turn_created_at")
    op.drop_constraint("ck_users_daily_chat_limit", "users", type_="check")
    op.drop_column("users", "daily_chat_limit")
