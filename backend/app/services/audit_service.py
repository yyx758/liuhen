"""Add an audit row to the caller's transaction; never commit independently."""

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.models.operation_event import OperationEventORM
from backend.app.schemas.operation_event import AuditEventPageRead, AuditSummaryRead, OperationEventRead

EventCategory = Literal["all", "failed", "cancelled", "documents"]
DOCUMENT_EVENT_TYPES = ("document_created", "document_updated", "document_deleted")


def record_event(
    session: Session,
    event_type: str,
    task_id: int,
    *,
    user_id: int,
    action_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
    details: dict | None = None,
    request_id: str | None = None,
) -> None:
    _add_event(
        session, event_type, user_id=user_id, task_id=task_id, action_id=action_id,
        from_status=from_status, to_status=to_status, reason=reason, details=details, request_id=request_id,
    )


def record_document_event(
    session: Session,
    event_type: str,
    document_id: int,
    *,
    user_id: int,
    details: dict,
    request_id: str | None = None,
) -> None:
    """文档的创建、修改、删除（PRD D14）；与业务变更在同一事务里写入。"""
    _add_event(session, event_type, user_id=user_id, document_id=document_id, details=details, request_id=request_id)


def _add_event(
    session: Session,
    event_type: str,
    *,
    user_id: int,
    task_id: int | None = None,
    document_id: int | None = None,
    action_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
    details: dict | None = None,
    request_id: str | None = None,
) -> None:
    session.add(OperationEventORM(
        user_id=user_id,
        event_type=event_type,
        task_id=task_id,
        document_id=document_id,
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


def _event_conditions(
    *,
    user_id: int,
    task_id: int | None = None,
    document_id: int | None = None,
    action_id: int | None = None,
    request_id: str | None = None,
    since: int | None = None,
    category: EventCategory = "all",
):
    conditions = [OperationEventORM.user_id == user_id]
    if task_id is not None:
        conditions.append(OperationEventORM.task_id == task_id)
    if document_id is not None:
        conditions.append(OperationEventORM.document_id == document_id)
    if action_id is not None:
        conditions.append(OperationEventORM.action_id == action_id)
    if request_id is not None:
        conditions.append(OperationEventORM.request_id == request_id)
    if since is not None:
        conditions.append(OperationEventORM.created_at >= since)
    if category == "failed":
        conditions.append(OperationEventORM.event_type.in_(("proposal_failed", "proposal_conflicted")))
    elif category == "cancelled":
        conditions.append(OperationEventORM.event_type == "proposal_cancelled")
    elif category == "documents":
        conditions.append(OperationEventORM.event_type.in_(DOCUMENT_EVENT_TYPES))
    return conditions


def _read_rows(session: Session, rows: list[OperationEventORM]) -> list[OperationEventRead]:

    def title_from(details: dict | None) -> str | None:
        for key in ("task", "document", "before", "after"):
            snapshot = (details or {}).get(key)
            if isinstance(snapshot, dict) and isinstance(snapshot.get("title"), str):
                return snapshot["title"]
        return None

    missing_action_ids = {
        row.action_id for row in rows
        if row.action_id is not None and title_from(row.details) is None
    }
    proposal_titles = {}
    if missing_action_ids:
        created = session.scalars(select(OperationEventORM).where(
            OperationEventORM.user_id.in_({row.user_id for row in rows}),
            OperationEventORM.action_id.in_(missing_action_ids),
            OperationEventORM.event_type == "proposal_created",
        )).all()
        proposal_titles = {row.action_id: title_from(row.details) for row in created}
    return [
        OperationEventRead(
            id=row.id,
            event_type=row.event_type,
            task_id=row.task_id,
            document_id=row.document_id,
            action_id=row.action_id,
            request_id=row.request_id,
            conversation_id=row.conversation_id,
            tool_call_id=row.tool_call_id,
            from_status=row.from_status,
            to_status=row.to_status,
            reason=row.reason,
            details=row.details,
            created_at=datetime.fromtimestamp(row.created_at, timezone.utc),
            subject_title=title_from(row.details) or proposal_titles.get(row.action_id),
        )
        for row in rows
    ]


def list_events(
    session: Session,
    *,
    user_id: int,
    task_id: int | None = None,
    document_id: int | None = None,
    action_id: int | None = None,
    before_id: int | None = None,
    request_id: str | None = None,
    since: int | None = None,
    category: EventCategory = "all",
    limit: int = 50,
) -> list[OperationEventRead]:
    conditions = _event_conditions(
        user_id=user_id, task_id=task_id, document_id=document_id, action_id=action_id,
        request_id=request_id, since=since, category=category,
    )
    if before_id is not None:
        conditions.append(OperationEventORM.id < before_id)
    rows = session.scalars(
        select(OperationEventORM).where(*conditions)
        .order_by(OperationEventORM.id.desc()).limit(limit)
    ).all()
    return _read_rows(session, list(rows))


def page_events(
    session: Session,
    *,
    user_id: int,
    page: int,
    page_size: int,
    task_id: int | None = None,
    document_id: int | None = None,
    action_id: int | None = None,
    request_id: str | None = None,
    since: int | None = None,
    category: EventCategory = "all",
    anchor_id: int | None = None,
) -> AuditEventPageRead:
    conditions = _event_conditions(
        user_id=user_id, task_id=task_id, document_id=document_id, action_id=action_id,
        request_id=request_id, since=since, category=category,
    )
    if anchor_id is None:
        anchor_id = session.scalar(
            select(func.max(OperationEventORM.id)).where(*conditions)
        )
    if anchor_id is not None:
        conditions.append(OperationEventORM.id <= anchor_id)
    total = session.scalar(
        select(func.count(OperationEventORM.id)).where(*conditions)
    ) or 0
    rows = session.scalars(
        select(OperationEventORM).where(*conditions)
        .order_by(OperationEventORM.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return AuditEventPageRead(
        items=_read_rows(session, list(rows)),
        page=page,
        page_size=page_size,
        total=total,
        total_pages=(total + page_size - 1) // page_size,
        anchor_id=anchor_id,
    )


def summary(session: Session, *, user_id: int, since: int | None = None) -> AuditSummaryRead:
    conditions = [OperationEventORM.user_id == user_id]
    if since is not None:
        conditions.append(OperationEventORM.created_at >= since)
    counts = dict(session.execute(
        select(OperationEventORM.event_type, func.count())
        .where(*conditions)
        .group_by(OperationEventORM.event_type)
    ).all())
    requests = session.scalar(
        select(func.count(func.distinct(OperationEventORM.request_id))).where(*conditions)
    ) or 0
    return AuditSummaryRead(
        total=sum(counts.values()),
        proposals=counts.get("proposal_created", 0),
        executed=counts.get("proposal_executed", 0),
        cancelled=counts.get("proposal_cancelled", 0),
        expired=counts.get("proposal_expired", 0),
        failures=counts.get("proposal_failed", 0) + counts.get("proposal_conflicted", 0),
        requests=requests,
    )


def agent_created_task_ids(session: Session, task_ids: list[int]) -> set[int]:
    if not task_ids:
        return set()
    return set(session.scalars(
        select(OperationEventORM.task_id).where(
            OperationEventORM.task_id.in_(task_ids),
            OperationEventORM.event_type == "task_created",
            OperationEventORM.conversation_id.is_not(None),
        )
    ).all())
