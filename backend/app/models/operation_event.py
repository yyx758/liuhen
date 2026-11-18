"""Append-only business operation history. Task rows may be deleted later."""

from sqlalchemy import CheckConstraint, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class OperationEventORM(Base):
    __tablename__ = "operation_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[int] = mapped_column(nullable=False)
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
            "'task_created', 'proposal_created', 'proposal_replaced', "
            "'proposal_expired', 'proposal_executed', 'proposal_cancelled', "
            "'proposal_conflicted', 'proposal_failed')",
            name="ck_operation_event_type",
        ),
        Index("ix_operation_events_task_id_id", "task_id", "id"),
        Index("ix_operation_events_action_id_id", "action_id", "id"),
        Index("ix_operation_events_request_id", "request_id"),
    )
