from typing import Literal

from fastapi import APIRouter, Query

from backend.app.api.deps import CurrentUserId, DbSession
from backend.app.schemas.operation_event import AuditEventPageRead, AuditSummaryRead, OperationEventRead
from backend.app.services import audit_service


router = APIRouter(prefix="/audit", tags=["Audit"])


@router.get("/events/page", response_model=AuditEventPageRead)
def page_events(
    session: DbSession,
    user_id: CurrentUserId,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    anchor_id: int | None = Query(default=None, gt=0),
    task_id: int | None = Query(default=None, gt=0),
    action_id: int | None = Query(default=None, gt=0),
    request_id: str | None = Query(default=None, max_length=36),
    since: int | None = Query(default=None, ge=0),
    category: Literal["all", "failed", "cancelled"] = "all",
) -> AuditEventPageRead:
    return audit_service.page_events(
        session, user_id=user_id, page=page, page_size=page_size,
        task_id=task_id, action_id=action_id, request_id=request_id,
        since=since, category=category, anchor_id=anchor_id,
    )


@router.get("/events", response_model=list[OperationEventRead])
def list_events(
    session: DbSession,
    user_id: CurrentUserId,
    task_id: int | None = Query(default=None, gt=0),
    action_id: int | None = Query(default=None, gt=0),
    before_id: int | None = Query(default=None, gt=0),
    request_id: str | None = Query(default=None, max_length=36),
    since: int | None = Query(default=None, ge=0),
    category: Literal["all", "failed", "cancelled"] = "all",
    limit: int = Query(default=50, ge=1, le=100),
) -> list[OperationEventRead]:
    return audit_service.list_events(
        session, user_id=user_id, task_id=task_id, action_id=action_id,
        before_id=before_id, request_id=request_id,
        since=since, category=category, limit=limit,
    )


@router.get("/summary", response_model=AuditSummaryRead)
def get_summary(
    session: DbSession,
    user_id: CurrentUserId,
    since: int | None = Query(default=None, ge=0),
) -> AuditSummaryRead:
    return audit_service.summary(session, user_id=user_id, since=since)
