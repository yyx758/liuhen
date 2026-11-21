"""路由共用的依赖：数据库会话与当前用户。

依赖链：current_session_user（有效会话）→ current_user（且不处于强制改密）→ current_user_id / require_admin。
FastAPI 在同一请求内只执行一次 get_db，三者拿到的是同一个数据库 Session。
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.app.db.session import get_db
from backend.app.services import auth_service
from backend.app.services.auth_service import SESSION_COOKIE, CurrentUser

DbSession = Annotated[Session, Depends(get_db)]


def current_session_user(request: Request, session: DbSession) -> CurrentUser:
    user = auth_service.resolve_session(session, request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(401, "请先登录")
    return user


# 强制改密状态下仍可用：只给 /auth/me、/auth/password 这类接口使用。
SessionUser = Annotated[CurrentUser, Depends(current_session_user)]


def current_user(user: SessionUser) -> CurrentUser:
    if user.must_change_password:
        raise HTTPException(403, "管理员已重置你的密码，请先设置新密码")
    return user


# 个人设置等需要完整用户信息（如当前会话 ID）的接口使用。
ActiveUser = Annotated[CurrentUser, Depends(current_user)]


def current_user_id(user: ActiveUser) -> int:
    return user.id


def require_admin(user: ActiveUser) -> CurrentUser:
    # 角色每次请求都从数据库重新读取（resolve_session），撤销管理员后立即生效。
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


# /admin/* 路由只依赖这个类型。
AdminUser = Annotated[CurrentUser, Depends(require_admin)]


# 业务路由只依赖这个类型；user_id 只能从这里来，不接受请求参数里的 user_id。
CurrentUserId = Annotated[int, Depends(current_user_id)]
