"""基线：初版部署（f581907）时由 create_all 建出的全部表。

线上库已有这些表，不执行本迁移，而是 `alembic -c backend/alembic.ini stamp 0001` 标记版本。

Revision ID: 0001
Revises:
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('conversations',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.Integer(), nullable=False),
    sa.Column('message_count', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('deleted_chat_requests',
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('deleted_at', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('request_id')
    )
    op.create_table('operation_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('action_id', sa.Integer(), nullable=True),
    sa.Column('request_id', sa.String(length=36), nullable=True),
    sa.Column('conversation_id', sa.String(length=36), nullable=True),
    sa.Column('tool_call_id', sa.String(length=128), nullable=True),
    sa.Column('from_status', sa.String(length=16), nullable=True),
    sa.Column('to_status', sa.String(length=16), nullable=True),
    sa.Column('reason', sa.String(), nullable=True),
    sa.Column('details', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.CheckConstraint("event_type IN ('task_created', 'task_updated', 'task_deleted', 'proposal_created', 'proposal_replaced', 'proposal_expired', 'proposal_executed', 'proposal_cancelled', 'proposal_conflicted', 'proposal_failed', 'proposal_deleted')", name='ck_operation_event_type'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_operation_events_action_id_id', 'operation_events', ['action_id', 'id'], unique=False)
    op.create_index('ix_operation_events_request_id', 'operation_events', ['request_id'], unique=False)
    op.create_index('ix_operation_events_task_id_id', 'operation_events', ['task_id', 'id'], unique=False)
    op.create_table('pending_actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('action_type', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('expected_version', sa.Integer(), nullable=False),
    sa.Column('batch_id', sa.String(length=36), nullable=True),
    sa.Column('before_snapshot', sa.JSON(), nullable=False),
    sa.Column('after_snapshot', sa.JSON(none_as_null=True), nullable=True),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.Integer(), nullable=False),
    sa.CheckConstraint("action_type IN ('update', 'delete')", name='ck_action_type'),
    sa.CheckConstraint("status IN ('pending', 'executed', 'cancelled', 'expired', 'conflicted', 'replaced', 'failed')", name='ck_action_status'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_pending_actions_task_id'), 'pending_actions', ['task_id'], unique=False)
    op.create_table('task_create_requests',
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('payload_hash', sa.String(length=64), nullable=False),
    sa.Column('result_snapshot', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('request_id')
    )
    op.create_table('tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=100), nullable=False),
    sa.Column('priority', sa.String(length=20), nullable=False),
    sa.Column('due_at', sa.DateTime(), nullable=True),
    sa.Column('start_at', sa.DateTime(), nullable=True),
    sa.Column('end_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    postgresql.ExcludeConstraint((sa.text("tsrange(start_at, end_at, '[)')"), '&&'), where=sa.text("status = 'todo' AND start_at IS NOT NULL AND end_at IS NOT NULL"), using='gist', name='ex_tasks_no_overlap'),
    sa.CheckConstraint('end_at IS NULL OR (start_at IS NOT NULL AND end_at > start_at)', name='ck_task_time_range'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('chat_turns',
    sa.Column('request_id', sa.String(length=36), nullable=False),
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('user_text', sa.String(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('active_conversation_id', sa.String(length=36), nullable=True),
    sa.Column('owner_token', sa.String(length=36), nullable=True),
    sa.Column('lease_until', sa.Integer(), nullable=True),
    sa.Column('base_message_count', sa.Integer(), nullable=False),
    sa.Column('messages', sa.JSON(), nullable=False),
    sa.Column('answer', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.PrimaryKeyConstraint('request_id'),
    sa.UniqueConstraint('active_conversation_id')
    )
    op.create_table('conversation_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.String(length=36), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.Integer(), nullable=False),
    sa.CheckConstraint("role IN ('user', 'assistant', 'tool')", name='ck_conversation_message_role'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conversation_id', 'position', name='uq_conversation_position')
    )


def downgrade() -> None:
    op.drop_table('conversation_messages')
    op.drop_table('chat_turns')
    op.drop_table('tasks')
    op.drop_table('task_create_requests')
    op.drop_index(op.f('ix_pending_actions_task_id'), table_name='pending_actions')
    op.drop_table('pending_actions')
    op.drop_index('ix_operation_events_task_id_id', table_name='operation_events')
    op.drop_index('ix_operation_events_request_id', table_name='operation_events')
    op.drop_index('ix_operation_events_action_id_id', table_name='operation_events')
    op.drop_table('operation_events')
    op.drop_table('deleted_chat_requests')
    op.drop_table('conversations')
