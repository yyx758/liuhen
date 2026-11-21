"""业务数据归属用户：各业务表新增 user_id，待办时间冲突改为按用户判断。

存量数据不做迁移（上线时重建数据库）。库里已有业务数据时拒绝执行，避免 NOT NULL 列无值可填。

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNED_TABLES = (
    "tasks", "pending_actions", "conversations", "chat_turns",
    "task_create_requests", "deleted_chat_requests", "operation_events",
)
INDEXES = (
    ("ix_tasks_user_id_id", "tasks", ["user_id", "id"]),
    ("ix_pending_actions_user_id_status", "pending_actions", ["user_id", "status"]),
    ("ix_conversations_user_id", "conversations", ["user_id"]),
    ("ix_chat_turns_user_id_created_at", "chat_turns", ["user_id", "created_at"]),
    ("ix_operation_events_user_id_id", "operation_events", ["user_id", "id"]),
)
OVERLAP_WHERE = "status = 'todo' AND start_at IS NOT NULL AND end_at IS NOT NULL"


def upgrade() -> None:
    connection = op.get_bind()
    non_empty = [
        table for table in (*OWNED_TABLES, "conversation_messages")
        if connection.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar()
    ]
    if non_empty:
        raise RuntimeError(
            f"以下表已有数据：{', '.join(non_empty)}。多用户改造不迁移存量数据，请备份后重建数据库再执行迁移。"
        )

    # GiST 排他约束里对整数用 "=" 需要 btree_gist。
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    for table in OWNED_TABLES:
        op.add_column(table, sa.Column("user_id", sa.Integer(), nullable=False))
        op.create_foreign_key(f"{table}_user_id_fkey", table, "users", ["user_id"], ["id"])
    op.add_column("chat_turns", sa.Column("created_at", sa.Integer(), nullable=False))
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)

    op.drop_constraint("ex_tasks_no_overlap", "tasks")
    op.execute(
        "ALTER TABLE tasks ADD CONSTRAINT ex_tasks_no_overlap EXCLUDE USING gist "
        "(user_id WITH =, tsrange(start_at, end_at, '[)') WITH &&) "
        f"WHERE ({OVERLAP_WHERE})"
    )


def downgrade() -> None:
    op.drop_constraint("ex_tasks_no_overlap", "tasks")
    op.execute(
        "ALTER TABLE tasks ADD CONSTRAINT ex_tasks_no_overlap EXCLUDE USING gist "
        "(tsrange(start_at, end_at, '[)') WITH &&) "
        f"WHERE ({OVERLAP_WHERE})"
    )
    for name, table, _ in INDEXES:
        op.drop_index(name, table_name=table)
    op.drop_column("chat_turns", "created_at")
    for table in OWNED_TABLES:
        op.drop_constraint(f"{table}_user_id_fkey", table, type_="foreignkey")
        op.drop_column(table, "user_id")
