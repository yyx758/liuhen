"""管理后台：账号审核、停用 / 启用、重置密码、删除申请与账号事件。

管理员只能管理账号本身，看不到其他用户的任务、提案、对话和操作留痕（PRD D2）。
"""

from fastapi import APIRouter, Query, Response, status

from backend.app.api.deps import AdminUser, DbSession
from backend.app.schemas.account import (
    AccountEventRead, AdminUserPage, AdminUserRead, ChatLimitRequest, PendingCountRead, ReasonRequest,
    TemporaryPasswordRead, UserStatus,
)
from backend.app.services import account_event_service, account_service
from backend.app.services.account_event_service import EventGroup

router = APIRouter(prefix="/admin", tags=["Admin"])


@router.get("/users", response_model=AdminUserPage)
def list_users(
    admin: AdminUser,
    session: DbSession,
    status_filter: UserStatus | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> AdminUserPage:
    return account_service.list_users(session, status=status_filter, q=q, page=page, page_size=page_size)


@router.get("/users/pending-count", response_model=PendingCountRead)
def pending_count(admin: AdminUser, session: DbSession) -> PendingCountRead:
    return PendingCountRead(count=account_service.pending_count(session))


@router.get("/users/{user_id}", response_model=AdminUserRead)
def get_user(user_id: int, admin: AdminUser, session: DbSession) -> AdminUserRead:
    return account_service.get_user(session, user_id=user_id)


@router.patch("/users/{user_id}", response_model=AdminUserRead)
def set_chat_limit(user_id: int, data: ChatLimitRequest, admin: AdminUser, session: DbSession) -> AdminUserRead:
    return account_service.set_chat_limit(
        session, actor_id=admin.id, target_id=user_id, daily_chat_limit=data.daily_chat_limit,
    )


@router.post("/users/{user_id}/approve", response_model=AdminUserRead)
def approve(user_id: int, admin: AdminUser, session: DbSession) -> AdminUserRead:
    return account_service.approve(session, actor_id=admin.id, target_id=user_id)


@router.post("/users/{user_id}/reject", response_model=AdminUserRead)
def reject(user_id: int, admin: AdminUser, session: DbSession, data: ReasonRequest | None = None) -> AdminUserRead:
    return account_service.reject(session, actor_id=admin.id, target_id=user_id, reason=data.reason if data else None)


@router.post("/users/{user_id}/disable", response_model=AdminUserRead)
def disable(user_id: int, admin: AdminUser, session: DbSession, data: ReasonRequest | None = None) -> AdminUserRead:
    return account_service.disable(session, actor_id=admin.id, target_id=user_id, reason=data.reason if data else None)


@router.post("/users/{user_id}/enable", response_model=AdminUserRead)
def enable(user_id: int, admin: AdminUser, session: DbSession) -> AdminUserRead:
    return account_service.enable(session, actor_id=admin.id, target_id=user_id)


@router.post("/users/{user_id}/reset-password", response_model=TemporaryPasswordRead)
def reset_password(user_id: int, admin: AdminUser, session: DbSession) -> TemporaryPasswordRead:
    # 临时密码只在这次响应中出现，不写日志、不入账号事件。
    return TemporaryPasswordRead(
        temporary_password=account_service.reset_password(session, target_id=user_id, actor_id=admin.id),
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, admin: AdminUser, session: DbSession) -> Response:
    account_service.delete_user(session, actor_id=admin.id, target_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/account-events", response_model=list[AccountEventRead])
def list_account_events(
    admin: AdminUser,
    session: DbSession,
    user_id: int | None = Query(default=None, gt=0),
    group: EventGroup = "all",
    before_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[AccountEventRead]:
    return account_event_service.list_events(
        session, target_user_id=user_id, group=group, before_id=before_id, limit=limit,
    )
