"""登录、登出、当前用户与修改密码。"""

from fastapi import APIRouter, Request, Response, status

from backend.app.api.deps import DbSession, SessionUser
from backend.app.config.env import get_auth_config
from backend.app.schemas.auth import LoginRequest, PasswordChangeRequest, UserRead
from backend.app.services import auth_service
from backend.app.services.auth_service import REMEMBER_SECONDS, SESSION_COOKIE, CurrentUser

router = APIRouter(prefix="/auth", tags=["Auth"])
COOKIE_SECURE = get_auth_config()["cookie_secure"]


def to_user_read(user: CurrentUser) -> UserRead:
    return UserRead(
        id=user.id, username=user.username, email=user.email,
        role=user.role, must_change_password=user.must_change_password,
    )


@router.post("/login", response_model=UserRead)
def login(data: LoginRequest, request: Request, response: Response, session: DbSession) -> UserRead:
    user, token = auth_service.login(
        session, account=data.account, password=data.password, remember=data.remember,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    # 不勾选"保持登录"时不设 Max-Age：浏览器关闭即丢弃 Cookie，服务端另有 12 小时空闲过期。
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=REMEMBER_SECONDS if data.remember else None,
        httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/",
    )
    return to_user_read(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, session: DbSession) -> Response:
    # 会话已失效时也要清掉浏览器里的 Cookie，所以这里不要求有效会话。
    auth_service.logout(session, request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return response


@router.get("/me", response_model=UserRead)
def me(user: SessionUser) -> UserRead:
    return to_user_read(user)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(data: PasswordChangeRequest, user: SessionUser, session: DbSession) -> Response:
    auth_service.change_password(
        session, user_id=user.id, session_id=user.session_id,
        old_password=data.old_password, new_password=data.new_password,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
