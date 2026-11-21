"""账号管理操作：目前供命令行使用，P4 的管理员接口复用。"""

import secrets

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.models.user import UserORM
from backend.app.schemas.auth import UserRead
from backend.app.services.auth_service import (
    hash_password, normalize_account, revoke_sessions, validate_password, validate_username,
)


def normalize_email(email: str | None) -> str | None:
    if email is None or not email.strip():
        return None
    email = normalize_account(email)
    local, _, domain = email.partition("@")
    if not local or "." not in domain or len(email) > 255:
        raise HTTPException(422, "邮箱格式不正确")
    return email


def create_admin(session: Session, *, username: str, password: str, email: str | None = None) -> UserRead:
    username = validate_username(username)
    validate_password(password, username)
    email = normalize_email(email)
    with session.begin():
        conditions = [UserORM.username == username]
        if email is not None:
            conditions.append(UserORM.email == email)
        if session.scalar(select(UserORM.id).where(or_(*conditions))) is not None:
            raise HTTPException(409, "用户名或邮箱已被使用")
        user = UserORM(
            username=username, email=email, password_hash=hash_password(password),
            role="admin", status="active",
        )
        session.add(user)
        session.flush()
        # 返回普通数据：提交后 ORM 对象可能过期，调用方关闭 Session 后再读属性会出错。
        return UserRead(
            id=user.id, username=user.username, email=user.email,
            role=user.role, must_change_password=user.must_change_password,
        )


def _get_by_username(session: Session, username: str) -> UserORM:
    user = session.scalar(
        select(UserORM).where(UserORM.username == normalize_account(username)).with_for_update()
    )
    if user is None:
        raise HTTPException(404, "用户不存在")
    return user


def promote_to_admin(session: Session, *, username: str) -> None:
    with session.begin():
        user = _get_by_username(session, username)
        if user.status != "active":
            raise HTTPException(409, "只能提升状态为正常的账号")
        user.role = "admin"


def reset_password(session: Session, *, username: str) -> str:
    """生成临时密码并让该用户所有设备下线；返回的临时密码只应展示一次。"""
    temporary = secrets.token_urlsafe(12)
    with session.begin():
        user = _get_by_username(session, username)
        user.password_hash = hash_password(temporary)
        user.must_change_password = True
        revoke_sessions(session, user.id)
    return temporary
