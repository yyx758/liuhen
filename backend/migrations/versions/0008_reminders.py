"""任务提醒：tasks.remind_before_minutes、users.email_reminders，新增 task_reminders、notifications。

已有任务的 remind_before_minutes 为空（不提醒），已有用户默认开启邮件提醒。

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("remind_before_minutes", sa.Integer(), nullable=True))
    op.create_check_constraint("ck_tasks_remind_before_minutes", "tasks", "remind_before_minutes BETWEEN 0 AND 10080")
    op.add_column("users", sa.Column("email_reminders", sa.Boolean(), server_default="true", nullable=False))

    op.create_table(
        "task_reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'fired', 'missed', 'cancelled')", name="ck_task_reminders_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_task_reminders_user_id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_task_reminders_task_id", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_task_reminders_pending_task", "task_reminders", ["task_id"], unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_task_reminders_status_remind_at", "task_reminders", ["status", "remind_at"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=True),
        sa.Column("task_title", sa.String(length=100), nullable=False),
        sa.Column("task_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_status", sa.String(length=16), nullable=False),
        sa.Column("email_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("email_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_error", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('reminder', 'missed')", name="ck_notifications_kind"),
        sa.CheckConstraint(
            "email_status IN ('not_needed', 'pending', 'sending', 'sent', 'skipped', 'failed')",
            name="ck_notifications_email_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_notifications_user_id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_user_id_id", "notifications", ["user_id", "id"])
    op.create_index(
        "ix_notifications_unread", "notifications", ["user_id"], postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_index("ix_notifications_email_due", "notifications", ["email_status", "email_next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_email_due", table_name="notifications")
    op.drop_index("ix_notifications_unread", table_name="notifications")
    op.drop_index("ix_notifications_user_id_id", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_task_reminders_status_remind_at", table_name="task_reminders")
    op.drop_index("uq_task_reminders_pending_task", table_name="task_reminders")
    op.drop_table("task_reminders")
    op.drop_column("users", "email_reminders")
    op.drop_constraint("ck_tasks_remind_before_minutes", "tasks", type_="check")
    op.drop_column("tasks", "remind_before_minutes")
