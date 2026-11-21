"""CSRF 防护：带会话 Cookie 的写请求必须来自允许的页面来源。

SameSite=Lax 已阻止跨站 POST 携带 Cookie；这里再校验 Origin 头作为第二层。
不带 Cookie 的写请求本来就会 401，不需要校验（登录接口除外，防止被诱导登录到别人的账号）。
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.app.config.env import get_auth_config
from backend.app.services.auth_service import SESSION_COOKIE

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ALLOWED_ORIGINS = get_auth_config()["allowed_origins"]


async def check_origin(request: Request, call_next):
    needs_check = request.method not in SAFE_METHODS and (
        SESSION_COOKIE in request.cookies or request.url.path == "/auth/login"
    )
    if needs_check:
        origin = (request.headers.get("origin") or "").rstrip("/")
        if origin not in ALLOWED_ORIGINS:
            return JSONResponse(
                status_code=403,
                content={"detail": "请求来源不被允许"},
            )
    return await call_next(request)
