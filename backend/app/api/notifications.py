"""页面内通知：列表、未读数、标为已读。"""

from fastapi import APIRouter, Query, Response, status

from backend.app.api.deps import CurrentUserId, DbSession
from backend.app.schemas.notification import NotificationRead, UnreadCountRead
from backend.app.services import notification_service

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", response_model=list[NotificationRead])
def list_notifications(
    session: DbSession,
    user_id: CurrentUserId,
    limit: int = Query(default=20, ge=1, le=100),
    before_id: int | None = Query(default=None, gt=0),
) -> list[NotificationRead]:
    return notification_service.list_notifications(session, user_id=user_id, limit=limit, before_id=before_id)


@router.get("/unread-count", response_model=UnreadCountRead)
def unread_count(session: DbSession, user_id: CurrentUserId) -> UnreadCountRead:
    return UnreadCountRead(count=notification_service.unread_count(session, user_id=user_id))


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_read(session: DbSession, user_id: CurrentUserId) -> Response:
    notification_service.mark_all_read(session, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_read(notification_id: int, session: DbSession, user_id: CurrentUserId) -> Response:
    notification_service.mark_read(session, user_id=user_id, notification_id=notification_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
