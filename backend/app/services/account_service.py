"""账号管理：注册、状态机（审核 / 停用 / 启用 / 删除）、重置密码，以及管理后台的账号列表。

每个改变账号的操作都在同一个事务里写一条账号事件。actor_id 为空表示命令行操作。
状态转换先对目标行加锁（SELECT ... FOR UPDATE）再检查当前状态，两个管理员同时操作同一账号时，
后提交的一方会看到状态已变化并收到 409，不会重复执行。
"""

import secrets

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.models.account_event import AccountEventORM
from backend.app.models.user import UserORM
from backend.app.schemas.account import AdminUserPage, AdminUserRead, RegisterRead
from backend.app.schemas.auth import UserRead
from backend.app.services import account_event_service, usage_service
from backend.app.services.auth_service import (
    hash_password, normalize_account, normalize_email, revoke_sessions, validate_password, validate_username,
)
from backend.app.services.rate_limit import register_ip_limiter

STATUS_LABELS = {"pending": "待审核", "active": "正常", "rejected": "已拒绝", "disabled": "已停用"}
STATUSES = tuple(STATUS_LABELS)


def _source(actor_id: int | None) -> str:
    return "cli" if actor_id is None else "web"


def _ensure_unique(session: Session, *, username: str | None, email: str | None, exclude_id: int | None = None) -> None:
    def taken(condition) -> bool:
        stmt = select(UserORM.id).where(condition)
        if exclude_id is not None:
            stmt = stmt.where(UserORM.id != exclude_id)
        return session.scalar(stmt) is not None

    if username is not None and taken(UserORM.username == username):
        raise HTTPException(409, "用户名已被占用")
    if email is not None and taken(UserORM.email == email):
        raise HTTPException(409, "邮箱已被使用")


def _insert_user(session: Session, user: UserORM) -> None:
    # 先查重给出明确提示；两个请求同时注册同名时，由唯一约束兜底。
    _ensure_unique(session, username=user.username, email=user.email)
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "用户名或邮箱已被使用") from exc


def register(session: Session, *, username: str, password: str, email: str | None, ip: str | None) -> RegisterRead:
    """注册新账号，状态为 pending，不登录。"""
    if not register_ip_limiter.allow(ip or "unknown"):
        raise HTTPException(429, "注册过于频繁，请稍后再试")
    username = validate_username(username)
    validate_password(password, username)
    email = normalize_email(email)
    with session.begin():
        user = UserORM(username=username, email=email, password_hash=hash_password(password))
        _insert_user(session, user)
        account_event_service.record(
            session, event_type="user_registered", target_user_id=user.id, actor_user_id=user.id,
            details={"source": "web", "ip": ip},
        )
        return RegisterRead(username=user.username, status="pending")


def create_admin(session: Session, *, username: str, password: str, email: str | None = None) -> UserRead:
    """命令行创建状态为正常的管理员。"""
    username = validate_username(username)
    validate_password(password, username)
    email = normalize_email(email)
    with session.begin():
        user = UserORM(
            username=username, email=email, password_hash=hash_password(password),
            role="admin", status="active",
        )
        _insert_user(session, user)
        account_event_service.record(
            session, event_type="user_registered", target_user_id=user.id, actor_user_id=None,
            details={"source": "cli", "role": "admin"},
        )
        # 返回普通数据：提交后 ORM 对象可能过期，调用方关闭 Session 后再读属性会出错。
        return UserRead(
            id=user.id, username=user.username, email=user.email, role=user.role,
            must_change_password=user.must_change_password, created_at=user.created_at,
        )


def user_id_by_username(session: Session, username: str) -> int:
    # 单独开一个事务并立即结束：SQLAlchemy 执行查询会自动开启事务，不结束的话调用方随后的 session.begin() 会报错。
    with session.begin():
        user_id = session.scalar(select(UserORM.id).where(UserORM.username == normalize_account(username)))
    if user_id is None:
        raise HTTPException(404, "用户不存在")
    return user_id


def _lock(session: Session, user_id: int) -> UserORM:
    user = session.get(UserORM, user_id, with_for_update=True)
    if user is None:
        raise HTTPException(404, "用户不存在")
    return user


def _require_status(user: UserORM, *allowed: str) -> None:
    if user.status not in allowed:
        raise HTTPException(409, f"该账号当前状态为「{STATUS_LABELS[user.status]}」，不能执行这个操作")


def promote_to_admin(session: Session, *, user_id: int) -> None:
    """命令行把正常状态的账号提升为管理员。"""
    with session.begin():
        user = _lock(session, user_id)
        if user.status != "active":
            raise HTTPException(409, "只能提升状态为正常的账号")
        if user.role == "admin":
            return
        user.role = "admin"
        account_event_service.record(
            session, event_type="role_changed", target_user_id=user.id, actor_user_id=None,
            details={"source": "cli", "from": "user", "to": "admin"},
        )


def reset_password(session: Session, *, target_id: int, actor_id: int | None) -> str:
    """生成临时密码并让该用户所有设备下线；返回的临时密码只应展示一次。"""
    if actor_id is not None and actor_id == target_id:
        raise HTTPException(400, "不能重置自己的密码，请在个人设置中修改密码")
    temporary = secrets.token_urlsafe(12)
    with session.begin():
        user = _lock(session, target_id)
        _require_status(user, "active", "disabled")
        user.password_hash = hash_password(temporary)
        user.must_change_password = True
        revoked = revoke_sessions(session, user.id)
        account_event_service.record(
            session, event_type="password_reset", target_user_id=user.id, actor_user_id=actor_id,
            details={"source": _source(actor_id), "revoked_sessions": revoked},
        )
    return temporary


def _review(session: Session, *, actor_id: int, target_id: int, to_status: str, event_type: str, reason: str | None) -> AdminUserRead:
    with session.begin():
        user = _lock(session, target_id)
        _require_status(user, "pending")
        user.status = to_status
        user.reviewed_at = now_utc()
        user.reviewed_by = actor_id
        account_event_service.record(
            session, event_type=event_type, target_user_id=user.id, actor_user_id=actor_id,
            reason=reason, details={"source": "web"},
        )
        session.flush()
        return _admin_read_with_usage(session, user, reason if to_status == "rejected" else None)


def approve(session: Session, *, actor_id: int, target_id: int) -> AdminUserRead:
    return _review(session, actor_id=actor_id, target_id=target_id, to_status="active", event_type="user_approved", reason=None)


def reject(session: Session, *, actor_id: int, target_id: int, reason: str | None) -> AdminUserRead:
    return _review(session, actor_id=actor_id, target_id=target_id, to_status="rejected", event_type="user_rejected", reason=reason)


def disable(session: Session, *, actor_id: int, target_id: int, reason: str | None) -> AdminUserRead:
    """停用账号并吊销它所有会话。

    管理员不能停用自己；同时锁住操作者和目标两行（按 id 顺序加锁避免死锁），确认操作者此刻仍是正常的管理员。
    这样两个管理员同时停用对方时，后执行的一方会发现自己已被停用而失败，系统总会保留至少一个正常的管理员。
    """
    if actor_id == target_id:
        raise HTTPException(400, "不能停用自己的账号")
    with session.begin():
        rows = session.scalars(
            select(UserORM).where(UserORM.id.in_((actor_id, target_id))).order_by(UserORM.id).with_for_update()
        ).all()
        users = {user.id: user for user in rows}
        actor = users.get(actor_id)
        if actor is None or actor.status != "active" or actor.role != "admin":
            raise HTTPException(403, "需要管理员权限")
        user = users.get(target_id)
        if user is None:
            raise HTTPException(404, "用户不存在")
        _require_status(user, "active")
        user.status = "disabled"
        revoked = revoke_sessions(session, user.id)
        account_event_service.record(
            session, event_type="user_disabled", target_user_id=user.id, actor_user_id=actor_id,
            reason=reason, details={"source": "web", "revoked_sessions": revoked},
        )
        session.flush()
        return _admin_read_with_usage(session, user, reason)


def enable(session: Session, *, actor_id: int, target_id: int) -> AdminUserRead:
    with session.begin():
        user = _lock(session, target_id)
        _require_status(user, "disabled")
        user.status = "active"
        account_event_service.record(
            session, event_type="user_enabled", target_user_id=user.id, actor_user_id=actor_id,
            details={"source": "web"},
        )
        session.flush()
        return _admin_read_with_usage(session, user, None)


def delete_user(session: Session, *, actor_id: int, target_id: int) -> None:
    """删除待审核或已拒绝的账号以释放用户名。这两种状态从未登录过，没有任何业务数据。"""
    with session.begin():
        user = _lock(session, target_id)
        _require_status(user, "pending", "rejected")
        # 账号删除后事件里查不到用户名，所以把名字记在 details 里。
        account_event_service.record(
            session, event_type="user_deleted", target_user_id=user.id, actor_user_id=actor_id,
            details={"source": "web", "username": user.username, "status": user.status},
        )
        session.delete(user)


def set_chat_limit(session: Session, *, actor_id: int, target_id: int, daily_chat_limit: int | None) -> AdminUserRead:
    """调整每日对话上限；None 表示恢复默认值。立即生效（下一次新建轮次时检查）。"""
    with session.begin():
        user = _lock(session, target_id)
        if user.role == "admin":
            raise HTTPException(409, "管理员对话不限量，不能设置上限")
        before = user.daily_chat_limit
        if before != daily_chat_limit:
            user.daily_chat_limit = daily_chat_limit
            account_event_service.record(
                session, event_type="chat_limit_changed", target_user_id=user.id, actor_user_id=actor_id,
                details={
                    "source": "web", "from": before, "to": daily_chat_limit,
                    "default": usage_service.DEFAULT_DAILY_CHAT_LIMIT,
                },
            )
        session.flush()
        return _admin_read_with_usage(session, user, None, with_days=True)


def _admin_read(user: UserORM, status_reason: str | None, days: list[int], *, with_days: bool = False) -> AdminUserRead:
    return AdminUserRead(
        id=user.id, username=user.username, email=user.email, role=user.role, status=user.status,
        must_change_password=user.must_change_password, created_at=user.created_at,
        last_login_at=user.last_login_at, reviewed_at=user.reviewed_at, status_reason=status_reason,
        daily_chat_limit=user.daily_chat_limit,
        chat_limit=usage_service.effective_limit(user.role, user.daily_chat_limit),
        chat_today=days[-1], chat_week=sum(days), chat_days=days if with_days else None,
    )


def _admin_read_with_usage(
    session: Session, user: UserORM, status_reason: str | None, *, with_days: bool = False,
) -> AdminUserRead:
    days = usage_service.week_counts(session, user_ids=[user.id], now=usage_service.current_timestamp())[user.id]
    return _admin_read(user, status_reason, days, with_days=with_days)


def _status_reasons(session: Session, users: list[UserORM]) -> dict[int, str | None]:
    """已停用 / 已拒绝账号最近一次停用或拒绝的原因。"""
    ids = [user.id for user in users if user.status in ("disabled", "rejected")]
    if not ids:
        return {}
    rows = session.execute(
        select(AccountEventORM.target_user_id, AccountEventORM.reason)
        .where(
            AccountEventORM.target_user_id.in_(ids),
            AccountEventORM.event_type.in_(("user_disabled", "user_rejected")),
        )
        .order_by(AccountEventORM.id.desc())
    )
    reasons: dict[int, str | None] = {}
    for user_id, reason in rows:
        reasons.setdefault(user_id, reason)
    return reasons


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_users(
    session: Session, *, status: str | None, q: str | None, page: int, page_size: int,
) -> AdminUserPage:
    conditions = []
    if status is not None:
        conditions.append(UserORM.status == status)
    keyword = (q or "").strip().lower()
    if keyword:
        pattern = f"%{_escape_like(keyword)}%"
        conditions.append(or_(UserORM.username.like(pattern, escape="\\"), UserORM.email.like(pattern, escape="\\")))
    # 待审核按注册先后（等得最久的在前），其他按最近登录。
    order = (
        (UserORM.id.asc(),) if status == "pending"
        else (UserORM.last_login_at.desc().nulls_last(), UserORM.id.asc())
    )
    total = session.scalar(select(func.count(UserORM.id)).where(*conditions)) or 0
    users = list(session.scalars(
        select(UserORM).where(*conditions).order_by(*order).offset((page - 1) * page_size).limit(page_size)
    ))
    reasons = _status_reasons(session, users)
    now = usage_service.current_timestamp()
    weeks = usage_service.week_counts(session, user_ids=[user.id for user in users], now=now)
    chat_today, chat_week = usage_service.totals(session, now=now)
    counts = {name: 0 for name in STATUSES}
    for name, count in session.execute(select(UserORM.status, func.count(UserORM.id)).group_by(UserORM.status)):
        counts[name] = count
    counts["all"] = sum(counts[name] for name in STATUSES)
    return AdminUserPage(
        items=[_admin_read(user, reasons.get(user.id), weeks[user.id]) for user in users],
        total=total, page=page, page_size=page_size, counts=counts,
        chat_today=chat_today, chat_week=chat_week, default_chat_limit=usage_service.DEFAULT_DAILY_CHAT_LIMIT,
    )


def get_user(session: Session, *, user_id: int) -> AdminUserRead:
    user = session.get(UserORM, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    return _admin_read_with_usage(session, user, _status_reasons(session, [user]).get(user.id), with_days=True)


def pending_count(session: Session) -> int:
    return session.scalar(select(func.count(UserORM.id)).where(UserORM.status == "pending")) or 0

