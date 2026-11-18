from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.db.session import get_db
from backend.app.schemas.action import PendingAction
from backend.app.services import action_service


router = APIRouter(
    prefix="/actions",
    tags=["Actions"]
)

DbSession = Annotated[Session, Depends(get_db)]


# 根据 ID 查询提案
@router.get(
    "/{action_id}",
    response_model=PendingAction
)
def get_action(
    action_id: int,
    session: DbSession
) -> PendingAction:

    return action_service.get_action(session, action_id)


# 用户确认执行
@router.post(
    "/{action_id}/confirm",
    response_model=PendingAction
)
def confirm_action(
    action_id: int,
    session: DbSession
) -> PendingAction:

    return action_service.confirm_action(
        session, action_id
    )


# 用户取消操作
@router.post(
    "/{action_id}/cancel",
    response_model=PendingAction
)
def cancel_action(
    action_id: int,
    session: DbSession
) -> PendingAction:

    return action_service.cancel_action(
        session, action_id
    )
