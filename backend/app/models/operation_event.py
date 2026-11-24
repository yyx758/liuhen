"""Append-only business operation history. Task rows may be deleted later."""

from sqlalchemy import CheckConstraint, ForeignKey, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class OperationEventORM(Base):
    __tablename__ = "operation_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 每条事件恰好属于任务或文档之一；都不加外键，被删除后审计照样保留。
    task_id: Mapped[int | None] = mapped_column(nullable=True)
    document_id: Mapped[int | None] = mapped_column(nullable=True)
    action_id: Mapped[int | None] = mapped_column(nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    created_at: Mapped[int] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'task_created', 'task_updated', 'task_deleted', "
            "'proposal_created', 'proposal_replaced', "
            "'proposal_expired', 'proposal_executed', 'proposal_cancelled', "
            "'proposal_conflicted', 'proposal_failed', 'proposal_deleted', "
            "'document_created', 'document_updated', 'document_deleted')",
            name="ck_operation_event_type",
        ),
        CheckConstraint("(task_id IS NULL) <> (document_id IS NULL)", name="ck_operation_events_target"),
        Index("ix_operation_events_task_id_id", "task_id", "id"),
        Index("ix_operation_events_document_id_id", "document_id", "id"),
        Index("ix_operation_events_action_id_id", "action_id", "id"),
        Index("ix_operation_events_request_id", "request_id"),
        Index("ix_operation_events_user_id_id", "user_id", "id"),
    )
