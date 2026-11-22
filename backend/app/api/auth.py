"""注册、登录、登出、邮箱找回密码与个人设置（当前用户、密码、邮箱、在线设备、登录记录）。"""

from fastapi import APIRouter, Request, Response, status

from backend.app.api.deps import ActiveUser, DbSession, SessionUser
from backend.app.config.env import get_auth_config
from backend.app.schemas.account import (
    EmailChangeRequest, EmailCodeRequest, EmailCodeSent, OwnEmailCodeRequest, OwnEventRead, PasswordResetRequest,
    RegisterRead, RegisterRequest, RevokedRead, SessionRead,
)
from backend.app.schemas.auth import ChatUsage, LoginRequest, PasswordChangeRequest, UserRead
from backend.app.services import account_event_service, account_service, auth_service, usage_service
from backend.app.services.auth_service import REMEMBER_SECONDS, SESSION_COOKIE, CurrentUser

router = APIRouter(prefix="/auth", tags=["Auth"])
COOKIE_SECURE = get_auth_config()["cookie_secure"]


def to_user_read(user: CurrentUser, session: DbSession) -> UserRead:
    today, limit = usage_service.usage_of(
        session, user_id=user.id, role=user.role, daily_chat_limit=user.daily_chat_limit,
    )
    return UserRead(
        id=user.id, username=user.username, email=user.email, email_verified=user.email_verified,
        role=user.role, must_change_password=user.must_change_password, created_at=user.created_at,
        chat_usage=ChatUsage(today=today, limit=limit),
    )


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/register/email-code", response_model=EmailCodeSent)
def register_email_code(data: EmailCodeRequest, request: Request, session: DbSession) -> dict:
    return account_service.request_register_code(session, email=data.email, ip=client_ip(request))


@router.post("/register", response_model=RegisterRead, status_code=status.HTTP_201_CREATED)
def register(data: RegisterRequest, request: Request, session: DbSession) -> RegisterRead:
    # 注册后账号待审核，不创建会话、不设置 Cookie。
    return account_service.register(
        session, username=data.username, password=data.password, email=data.email, code=data.code,
        ip=client_ip(request),
    )


@router.post("/password-reset/email-code", response_model=EmailCodeSent)
def password_reset_email_code(data: EmailCodeRequest, request: Request, session: DbSession) -> dict:
    # 无论邮箱是否对应账号都返回同样的结果，不暴露账号是否存在。
    return auth_service.request_password_reset_code(session, email=data.email, ip=client_ip(request))


@router.post("/password-reset", status_code=status.HTTP_204_NO_CONTENT)
def password_reset(data: PasswordResetRequest, session: DbSession) -> Response:
    # 重置后所有设备下线，不自动登录。
    auth_service.reset_password_by_email(session, email=data.email, code=data.code, new_password=data.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/login", response_model=UserRead)
def login(data: LoginRequest, request: Request, response: Response, session: DbSession) -> UserRead:
    user, token = auth_service.login(
        session, account=data.account, password=data.password, remember=data.remember,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    # 不勾选"保持登录"时不设 Max-Age：浏览器关闭即丢弃 Cookie，服务端另有 12 小时空闲过期。
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=REMEMBER_SECONDS if data.remember else None,
        httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/",
    )
    return to_user_read(user, session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, session: DbSession) -> Response:
    # 会话已失效时也要清掉浏览器里的 Cookie，所以这里不要求有效会话。
    auth_service.logout(session, request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return response


@router.get("/me", response_model=UserRead)
def me(user: SessionUser, session: DbSession) -> UserRead:
    return to_user_read(user, session)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(data: PasswordChangeRequest, user: SessionUser, session: DbSession) -> Response:
    auth_service.change_password(
        session, user_id=user.id, session_id=user.session_id,
        old_password=data.old_password, new_password=data.new_password,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/me/email-code", response_model=EmailCodeSent)
def own_email_code(data: OwnEmailCodeRequest, request: Request, user: ActiveUser, session: DbSession) -> dict:
    return auth_service.request_change_email_code(
        session, user_id=user.id, email=data.email, current_password=data.current_password, ip=client_ip(request),
    )


@router.patch("/me", response_model=UserRead)
def change_email(data: EmailChangeRequest, user: ActiveUser, session: DbSession) -> UserRead:
    email, verified = auth_service.change_email(
        session, user_id=user.id, email=data.email, code=data.code, current_password=data.current_password,
    )
    return to_user_read(user, session).model_copy(update={"email": email, "email_verified": verified})


@router.get("/sessions", response_model=list[SessionRead])
def list_sessions(user: ActiveUser, session: DbSession) -> list[SessionRead]:
    return auth_service.list_sessions(session, user_id=user.id, current_session_id=user.session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session(session_id: int, user: ActiveUser, session: DbSession) -> Response:
    auth_service.revoke_own_session(
        session, user_id=user.id, current_session_id=user.session_id, target_session_id=session_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/revoke-others", response_model=RevokedRead)
def revoke_other_sessions(user: ActiveUser, session: DbSession) -> RevokedRead:
    revoked = auth_service.revoke_other_sessions(session, user_id=user.id, current_session_id=user.session_id)
    return RevokedRead(revoked=revoked)


@router.get("/events", response_model=list[OwnEventRead])
def list_own_events(user: ActiveUser, session: DbSession) -> list[OwnEventRead]:
    return account_event_service.list_own_events(session, user_id=user.id)
