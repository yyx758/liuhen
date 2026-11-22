from uuid import UUID

from fastapi import APIRouter, Response, status

from backend.app.api.deps import CurrentUserId, DbSession
from backend.app.schemas.action import ActionListItem, PendingAction
from backend.app.services import action_service


router = APIRouter(
    prefix="/actions",
    tags=["Actions"]
)


@router.get("/pending", response_model=list[ActionListItem])
def list_pending_actions(session: DbSession, user_id: CurrentUserId) -> list[ActionListItem]:
    return action_service.list_active_pending(session, user_id=user_id)


@router.get("/conversations/{conversation_id}", response_model=list[ActionListItem])
def list_conversation_actions(
    conversation_id: UUID, session: DbSession, user_id: CurrentUserId
) -> list[ActionListItem]:
    return action_service.list_for_conversation(session, str(conversation_id), user_id=user_id)


# 根据 ID 查询提案
@router.get(
    "/{action_id}",
    response_model=PendingAction
)
def get_action(
    action_id: int,
    session: DbSession,
    user_id: CurrentUserId,
) -> PendingAction:

    return action_service.get_action(session, action_id, user_id=user_id)


# 用户确认执行
@router.post(
    "/{action_id}/confirm",
    response_model=PendingAction
)
def confirm_action(
    action_id: int,
    session: DbSession,
    user_id: CurrentUserId,
) -> PendingAction:

    return action_service.confirm_action(
        session, action_id, user_id=user_id
    )


# 用户取消操作
@router.post(
    "/{action_id}/cancel",
    response_model=PendingAction
)
def cancel_action(
    action_id: int,
    session: DbSession,
    user_id: CurrentUserId,
) -> PendingAction:

    return action_service.cancel_action(
        session, action_id, user_id=user_id
    )


@router.delete("/{action_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_expired_action(action_id: int, session: DbSession, user_id: CurrentUserId) -> Response:
    action_service.delete_expired_action(session, action_id, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
