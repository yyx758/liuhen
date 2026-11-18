from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.db.session import get_db
from backend.app.schemas.operation_event import OperationEventRead
from backend.app.services import audit_service


router = APIRouter(prefix="/audit", tags=["Audit"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/events", response_model=list[OperationEventRead])
def list_events(
    session: DbSession,
    task_id: int | None = Query(default=None, gt=0),
    action_id: int | None = Query(default=None, gt=0),
    before_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[OperationEventRead]:
    return audit_service.list_events(
        session, task_id=task_id, action_id=action_id,
        before_id=before_id, limit=limit,
    )
