"""账号事件：写入（与账号变更同一事务）与查询。只追加，不提供修改和删除。"""

from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from backend.app.config.clock import now_utc
from backend.app.models.account_event import AccountEventORM
from backend.app.models.user import UserORM
from backend.app.schemas.account import AccountEventRead, OwnEventRead

EventGroup = Literal["all", "review", "account", "login"]
GROUP_TYPES: dict[str, tuple[str, ...]] = {
    "review": ("user_registered", "user_approved", "user_rejected"),
    "account": (
        "user_disabled", "user_enabled", "user_deleted", "password_changed", "password_reset",
        "email_changed", "role_changed", "chat_limit_changed", "session_revoked",
    ),
    "login": ("login_succeeded", "login_failed", "login_locked"),
}
# 用户在个人设置里能看到的自己的事件（PRD 5.8）。
OWN_EVENT_TYPES = ("login_succeeded", "login_failed", "login_locked", "password_changed", "password_reset")
OWN_EVENT_DAYS = 30


def record(
    session: Session, *, event_type: str, target_user_id: int, actor_user_id: int | None,
    reason: str | None = None, details: dict | None = None,
) -> None:
    """追加一条账号事件；调用方负责事务，保证事件与账号变更一起提交或一起回滚。"""
    session.add(AccountEventORM(
        event_type=event_type, target_user_id=target_user_id, actor_user_id=actor_user_id,
        reason=reason or None, details=details or None, created_at=int(now_utc().timestamp()),
    ))


def _time(value: int) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def list_events(
    session: Session, *, target_user_id: int | None = None, group: EventGroup = "all",
    before_id: int | None = None, limit: int = 50,
) -> list[AccountEventRead]:
    """管理员查看账号事件，按 id 倒序，用 before_id 翻页。"""
    target = aliased(UserORM)
    actor = aliased(UserORM)
    stmt = (
        select(AccountEventORM, target.username, actor.username)
        .outerjoin(target, target.id == AccountEventORM.target_user_id)
        .outerjoin(actor, actor.id == AccountEventORM.actor_user_id)
        .order_by(AccountEventORM.id.desc()).limit(limit)
    )
    if target_user_id is not None:
        stmt = stmt.where(AccountEventORM.target_user_id == target_user_id)
    if group != "all":
        stmt = stmt.where(AccountEventORM.event_type.in_(GROUP_TYPES[group]))
    if before_id is not None:
        stmt = stmt.where(AccountEventORM.id < before_id)
    rows = session.execute(stmt).all()
    deleted_names = _deleted_usernames(session, {event.target_user_id for event, name, _ in rows if name is None})
    return [
        AccountEventRead(
            id=event.id, event_type=event.event_type,
            target_user_id=event.target_user_id, actor_user_id=event.actor_user_id,
            target_username=target_name or deleted_names.get(event.target_user_id),
            actor_username=actor_name,
            reason=event.reason, details=event.details, created_at=_time(event.created_at),
        )
        for event, target_name, actor_name in rows
    ]


def _deleted_usernames(session: Session, user_ids: set[int]) -> dict[int, str]:
    """账号删除后查不到用户名，改用删除事件里记下的名字。"""
    if not user_ids:
        return {}
    rows = session.execute(
        select(AccountEventORM.target_user_id, AccountEventORM.details).where(
            AccountEventORM.event_type == "user_deleted", AccountEventORM.target_user_id.in_(user_ids),
        )
    )
    return {user_id: (details or {}).get("username") for user_id, details in rows}


def list_own_events(session: Session, *, user_id: int) -> list[OwnEventRead]:
    since = int((now_utc() - timedelta(days=OWN_EVENT_DAYS)).timestamp())
    rows = session.scalars(
        select(AccountEventORM).where(
            AccountEventORM.target_user_id == user_id,
            AccountEventORM.event_type.in_(OWN_EVENT_TYPES),
            AccountEventORM.created_at >= since,
        ).order_by(AccountEventORM.id.desc()).limit(200)
    )
    return [
        OwnEventRead(
            id=row.id, event_type=row.event_type,
            ip=(row.details or {}).get("ip"), created_at=_time(row.created_at),
        )
        for row in rows
    ]
