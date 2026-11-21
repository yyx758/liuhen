"""账号认证：密码哈希、登录会话的创建 / 校验 / 吊销、修改密码与邮箱。"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.models.user import UserORM, UserSessionORM
from backend.app.schemas.account import SessionRead
from backend.app.services import account_event_service
from backend.app.services.rate_limit import login_failure_lock, login_ip_limiter

SESSION_COOKIE = "liuhen_session"
REMEMBER_SECONDS = 7 * 24 * 3600
IDLE_SECONDS = 12 * 3600
MAX_SESSIONS_PER_USER = 10
TOUCH_INTERVAL_SECONDS = 60

USERNAME_PATTERN = re.compile(r"[a-z0-9_]{3,32}")
PASSWORD_MIN, PASSWORD_MAX = 8, 128

# argon2-cffi 默认即 Argon2id，参数随库版本更新；旧哈希在登录成功时自动升级。
_hasher = PasswordHasher()
# 账号不存在时也做一次同样耗时的校验，避免通过响应时间判断账号是否存在。
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


@dataclass(frozen=True)
class CurrentUser:
    """认证依赖交给路由的当前用户。用普通数据而非 ORM 对象，避免提交后再访问属性时隐式开启事务。"""

    id: int
    username: str
    email: str | None
    role: str
    must_change_password: bool
    created_at: datetime
    session_id: int


def now_timestamp() -> int:
    return int(now_utc().timestamp())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def normalize_account(value: str) -> str:
    return value.strip().lower()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        # 不合法的哈希（如占位值 "!"）一律视为校验失败。
        return False


def validate_username(username: str) -> str:
    username = normalize_account(username)
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(422, "用户名须为 3–32 位小写字母、数字或下划线")
    return username


def validate_password(password: str, username: str) -> None:
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise HTTPException(422, f"密码长度须为 {PASSWORD_MIN}–{PASSWORD_MAX} 位")
    if password.lower() == username.lower():
        raise HTTPException(422, "密码不能与用户名相同")


def normalize_email(email: str | None) -> str | None:
    """邮箱可选：空白视为不填；保存前去空格并转小写。"""
    if email is None or not email.strip():
        return None
    email = normalize_account(email)
    local, _, domain = email.partition("@")
    if not local or "." not in domain or len(email) > 255:
        raise HTTPException(422, "邮箱格式不正确")
    return email


def find_user_by_account(session: Session, account: str) -> UserORM | None:
    account = normalize_account(account)
    column = UserORM.email if "@" in account else UserORM.username
    return session.scalar(select(UserORM).where(column == account))


def _to_current(user: UserORM, session_id: int) -> CurrentUser:
    return CurrentUser(
        id=user.id, username=user.username, email=user.email, role=user.role,
        must_change_password=user.must_change_password, created_at=user.created_at, session_id=session_id,
    )


STATUS_ERRORS = {
    "pending": "账号正在审核中，审核通过后即可登录",
    "rejected": "注册申请未通过",
    "disabled": "账号已被停用，请联系管理员",
}


def login(
    session: Session, *, account: str, password: str, remember: bool,
    ip: str | None, user_agent: str | None,
) -> tuple[CurrentUser, str]:
    """校验账号密码并创建会话，返回 (当前用户, 令牌原文)。令牌原文只交给 Cookie，不入库。

    登录失败也要写账号事件，而在事务里抛异常会让事件一起回滚，所以失败时先记下错误，
    等事务提交后再抛出。
    """
    key = normalize_account(account)
    if not login_ip_limiter.allow(ip or "unknown") or login_failure_lock.is_locked(key):
        raise HTTPException(429, "尝试次数过多，请稍后再试")

    error: HTTPException | None = None
    with session.begin():
        user = find_user_by_account(session, key)
        password_ok = verify_password(user.password_hash if user else _DUMMY_HASH, password)
        if user is None or not password_ok:
            locked = login_failure_lock.record_failure(key)
            # 只记录能对应到已有账号的失败；账号不存在的失败只计入限流。
            if user is not None:
                for event_type in ("login_failed", "login_locked") if locked else ("login_failed",):
                    account_event_service.record(
                        session, event_type=event_type, target_user_id=user.id, actor_user_id=user.id,
                        details={"ip": ip},
                    )
            # 账号不存在与密码错误使用同一提示。
            error = HTTPException(401, "账号或密码错误")
        elif user.status in STATUS_ERRORS:
            login_failure_lock.clear(key)
            # 只有密码正确时才告诉对方账号状态。
            error = HTTPException(403, STATUS_ERRORS[user.status])
        else:
            login_failure_lock.clear(key)
            result = _open_session(session, user, password=password, remember=remember, ip=ip, user_agent=user_agent)
    if error is not None:
        raise error
    return result


def _open_session(
    session: Session, user: UserORM, *, password: str, remember: bool, ip: str | None, user_agent: str | None,
) -> tuple[CurrentUser, str]:
    """密码与状态都已校验通过：创建会话、更新登录时间并记录登录成功。调用方负责事务。"""
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    token = secrets.token_urlsafe(32)
    now = now_timestamp()
    row = UserSessionORM(
        user_id=user.id, token_hash=hash_token(token),
        created_at=now, last_seen_at=now, expires_at=now + REMEMBER_SECONDS,
        idle_timeout_seconds=None if remember else IDLE_SECONDS,
        user_agent=(user_agent or "")[:255] or None, ip=ip,
    )
    session.add(row)
    user.last_login_at = now_utc()
    session.flush()
    _prune_sessions(session, user.id)
    account_event_service.record(
        session, event_type="login_succeeded", target_user_id=user.id, actor_user_id=user.id,
        details={"ip": ip},
    )
    return _to_current(user, row.id), token


def _prune_sessions(session: Session, user_id: int) -> None:
    keep = select(UserSessionORM.id).where(UserSessionORM.user_id == user_id).order_by(
        UserSessionORM.id.desc()
    ).limit(MAX_SESSIONS_PER_USER)
    session.execute(delete(UserSessionORM).where(
        UserSessionORM.user_id == user_id, UserSessionORM.id.not_in(keep)
    ))


def resolve_session(session: Session, token: str | None) -> CurrentUser | None:
    """按 Cookie 令牌找到有效会话；过期会话顺手删除。"""
    if not token:
        return None
    with session.begin():
        row = session.execute(
            select(UserSessionORM, UserORM)
            .join(UserORM, UserORM.id == UserSessionORM.user_id)
            .where(UserSessionORM.token_hash == hash_token(token))
        ).first()
        if row is None:
            return None
        user_session, user = row
        now = now_timestamp()
        idle = user_session.idle_timeout_seconds
        if user_session.expires_at <= now or (idle is not None and user_session.last_seen_at + idle <= now):
            session.delete(user_session)
            return None
        if user.status != "active":
            return None
        # 每分钟最多写一次，避免每个请求都写库。
        if now - user_session.last_seen_at >= TOUCH_INTERVAL_SECONDS:
            user_session.last_seen_at = now
        return _to_current(user, user_session.id)


def logout(session: Session, token: str | None) -> None:
    if not token:
        return
    with session.begin():
        session.execute(delete(UserSessionORM).where(UserSessionORM.token_hash == hash_token(token)))


def revoke_sessions(session: Session, user_id: int, *, except_session_id: int | None = None) -> int:
    """吊销用户的会话；调用方负责事务。"""
    stmt = delete(UserSessionORM).where(UserSessionORM.user_id == user_id)
    if except_session_id is not None:
        stmt = stmt.where(UserSessionORM.id != except_session_id)
    return session.execute(stmt).rowcount


def change_password(
    session: Session, *, user_id: int, session_id: int, old_password: str, new_password: str,
) -> None:
    with session.begin():
        user = session.get(UserORM, user_id, with_for_update=True)
        if user is None or not verify_password(user.password_hash, old_password):
            raise HTTPException(400, "当前密码不正确")
        if old_password == new_password:
            raise HTTPException(422, "新密码不能与当前密码相同")
        validate_password(new_password, user.username)
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        # 其他设备上的会话全部失效，当前会话保留。
        revoked = revoke_sessions(session, user_id, except_session_id=session_id)
        account_event_service.record(
            session, event_type="password_changed", target_user_id=user_id, actor_user_id=user_id,
            details={"revoked_sessions": revoked},
        )


def change_email(session: Session, *, user_id: int, email: str | None, current_password: str) -> str | None:
    """修改或清空自己的邮箱，需要当前密码；返回保存后的邮箱。"""
    email = normalize_email(email)
    try:
        with session.begin():
            user = session.get(UserORM, user_id, with_for_update=True)
            if user is None or not verify_password(user.password_hash, current_password):
                raise HTTPException(400, "当前密码不正确")
            if user.email == email:
                return email
            if email is not None and session.scalar(
                select(UserORM.id).where(UserORM.email == email, UserORM.id != user_id)
            ) is not None:
                raise HTTPException(409, "邮箱已被使用")
            account_event_service.record(
                session, event_type="email_changed", target_user_id=user_id, actor_user_id=user_id,
                details={"from": user.email, "to": email},
            )
            user.email = email
            session.flush()
    except IntegrityError as exc:
        # 查重与写入之间被别人抢先用了同一个邮箱，由唯一约束兜底。
        raise HTTPException(409, "邮箱已被使用") from exc
    return email


def _is_live(row: UserSessionORM, now: int) -> bool:
    idle = row.idle_timeout_seconds
    return row.expires_at > now and (idle is None or row.last_seen_at + idle > now)


def list_sessions(session: Session, *, user_id: int, current_session_id: int) -> list[SessionRead]:
    """当前用户未过期的会话，最近活动的在前。"""
    now = now_timestamp()
    rows = session.scalars(
        select(UserSessionORM).where(UserSessionORM.user_id == user_id)
        .order_by(UserSessionORM.last_seen_at.desc(), UserSessionORM.id.desc())
    )
    return [
        SessionRead(
            id=row.id, ip=row.ip, user_agent=row.user_agent, current=row.id == current_session_id,
            created_at=datetime.fromtimestamp(row.created_at, timezone.utc),
            last_seen_at=datetime.fromtimestamp(row.last_seen_at, timezone.utc),
        )
        for row in rows if _is_live(row, now)
    ]


def revoke_own_session(session: Session, *, user_id: int, current_session_id: int, target_session_id: int) -> None:
    """下线自己的另一个设备。别人的会话、当前会话都按不存在处理（下线当前设备请用退出登录）。"""
    if target_session_id == current_session_id:
        raise HTTPException(404, "会话不存在")
    with session.begin():
        deleted = session.execute(delete(UserSessionORM).where(
            UserSessionORM.id == target_session_id, UserSessionORM.user_id == user_id,
        )).rowcount
        if not deleted:
            raise HTTPException(404, "会话不存在")
        account_event_service.record(
            session, event_type="session_revoked", target_user_id=user_id, actor_user_id=user_id,
            details={"session_id": target_session_id},
        )


def revoke_other_sessions(session: Session, *, user_id: int, current_session_id: int) -> int:
    with session.begin():
        revoked = revoke_sessions(session, user_id, except_session_id=current_session_id)
        if revoked:
            account_event_service.record(
                session, event_type="session_revoked", target_user_id=user_id, actor_user_id=user_id,
                details={"revoked_sessions": revoked, "scope": "others"},
            )
    return revoked


def purge_expired_sessions(session: Session) -> int:
    """后台定期删除已过期（绝对过期或空闲超时）的会话。"""
    now = now_timestamp()
    with session.begin():
        return session.execute(delete(UserSessionORM).where(
            (UserSessionORM.expires_at <= now)
            | (
                UserSessionORM.idle_timeout_seconds.is_not(None)
                & (UserSessionORM.last_seen_at + UserSessionORM.idle_timeout_seconds <= now)
            )
        )).rowcount
