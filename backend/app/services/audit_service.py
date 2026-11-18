"""Add an audit row to the caller's transaction; never commit independently."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.models.operation_event import OperationEventORM
from backend.app.schemas.operation_event import OperationEventRead


def record_event(
    session: Session,
    event_type: str,
    task_id: int,
    *,
    action_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
    details: dict | None = None,
    request_id: str | None = None,
) -> None:
    session.add(OperationEventORM(
        event_type=event_type,
        task_id=task_id,
        action_id=action_id,
        request_id=request_id or session.info.get("audit_request_id"),
        conversation_id=session.info.get("audit_conversation_id"),
        tool_call_id=session.info.get("audit_tool_call_id"),
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        details=details,
        created_at=int(now_utc().timestamp()),
    ))


def list_events(
    session: Session,
    *,
    task_id: int | None = None,
    action_id: int | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[OperationEventRead]:
    stmt = select(OperationEventORM)
    if task_id is not None:
        stmt = stmt.where(OperationEventORM.task_id == task_id)
    if action_id is not None:
        stmt = stmt.where(OperationEventORM.action_id == action_id)
    if before_id is not None:
        stmt = stmt.where(OperationEventORM.id < before_id)
    stmt = stmt.order_by(OperationEventORM.id.desc()).limit(limit)
    return [
        OperationEventRead(
            id=row.id,
            event_type=row.event_type,
            task_id=row.task_id,
            action_id=row.action_id,
            request_id=row.request_id,
            conversation_id=row.conversation_id,
            tool_call_id=row.tool_call_id,
            from_status=row.from_status,
            to_status=row.to_status,
            reason=row.reason,
            details=row.details,
            created_at=datetime.fromtimestamp(row.created_at, timezone.utc),
        )
        for row in session.scalars(stmt)
    ]
