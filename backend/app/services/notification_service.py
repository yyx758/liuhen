"""页面内通知的查询与已读标记。所有查询都带 user_id 条件；别人的通知与不存在一样返回 404。"""

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.models.notification import NotificationORM
from backend.app.schemas.notification import NotificationRead


def list_notifications(session: Session, *, user_id: int, limit: int, before_id: int | None) -> list[NotificationRead]:
    """新的在前；before_id 传上一页最后一条的 id 读取更早的。"""
    stmt = select(NotificationORM).where(NotificationORM.user_id == user_id)
    if before_id is not None:
        stmt = stmt.where(NotificationORM.id < before_id)
    rows = session.scalars(stmt.order_by(NotificationORM.id.desc()).limit(limit))
    return [NotificationRead.model_validate(row) for row in rows]


def unread_count(session: Session, *, user_id: int) -> int:
    return session.scalar(
        select(func.count()).select_from(NotificationORM)
        .where(NotificationORM.user_id == user_id, NotificationORM.read_at.is_(None))
    )


def mark_read(session: Session, *, user_id: int, notification_id: int) -> None:
    """标为已读；已读的再标一次不报错，也不改已读时间。"""
    with session.begin():
        found = session.scalar(select(NotificationORM.id).where(
            NotificationORM.id == notification_id, NotificationORM.user_id == user_id,
        ))
        if found is None:
            raise HTTPException(404, "通知不存在")
        session.execute(
            update(NotificationORM)
            .where(NotificationORM.id == notification_id, NotificationORM.read_at.is_(None))
            .values(read_at=now_utc())
        )


def mark_all_read(session: Session, *, user_id: int) -> None:
    with session.begin():
        session.execute(
            update(NotificationORM)
            .where(NotificationORM.user_id == user_id, NotificationORM.read_at.is_(None))
            .values(read_at=now_utc())
        )
