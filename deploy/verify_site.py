"""部署后的站点检查：HTTPS、备案入口页、应用页面与接口的登录保护。

只读检查，不需要凭据：
    python3 deploy/verify_site.py

同时检查登录后的接口（会产生一次登录记录，结束时退出登录）：
    LIUHEN_VERIFY_USER=<用户名> LIUHEN_VERIFY_PASSWORD=<密码> python3 deploy/verify_site.py
"""

import json
import os
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

HOST = "liuhen.yyyyx.top"
BASE = f"https://{HOST}"
ORIGIN = BASE


def make_opener():
    return build_opener(HTTPCookieProcessor(CookieJar()))


def fetch(opener, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Origin": ORIGIN} if method != "GET" else {}
    try:
        with opener.open(Request(BASE + path, data=data, method=method, headers=headers), timeout=15) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def main() -> None:
    anonymous = make_opener()
    status, landing = fetch(anonymous, "/")
    assert status == 200 and "赣ICP备2026013160号-1".encode() in landing
    assert b"https://beian.miit.gov.cn/" in landing

    # 网关不再做 Basic Auth：应用页面公开，由前端跳转到登录页。
    status, app = fetch(anonymous, "/app/")
    assert status == 200 and b"/app/assets/" in app
    assert fetch(anonymous, "/app/admin")[0] == 200
    assert fetch(anonymous, "/docs")[0] == 404

    # 业务接口必须先登录。
    for path in ("/tasks", "/audit/summary", "/auth/me", "/admin/users", "/notifications/unread-count", "/documents"):
        status, body = fetch(anonymous, path)
        assert status == 401 and json.loads(body)["detail"] == "请先登录", path
    # 缺少 Origin 的登录请求被拒绝（CSRF 防护）。
    try:
        anonymous.open(Request(BASE + "/auth/login", data=b"{}", method="POST", headers={"Content-Type": "application/json"}), timeout=15)
        raise AssertionError("缺少 Origin 的登录请求应被拒绝")
    except HTTPError as error:
        assert error.code == 403, error.code

    username, password = os.getenv("LIUHEN_VERIFY_USER"), os.getenv("LIUHEN_VERIFY_PASSWORD")
    if not (username and password):
        print("HTTPS、备案入口页、应用页面与接口登录保护：全部通过；未提供 LIUHEN_VERIFY_USER/PASSWORD，跳过登录后检查")
        return

    user = make_opener()
    status, body = fetch(user, "/auth/login", "POST", {"account": username, "password": password, "remember": False})
    assert status == 200, body.decode(errors="replace")
    try:
        me = json.loads(fetch(user, "/auth/me")[1])
        assert me["username"] == username.strip().lower() and "chat_usage" in me
        assert not me["must_change_password"], "该账号需要先修改密码"
        for path in ("/tasks", "/audit/summary", "/auth/sessions", "/notifications/unread-count", "/documents"):
            assert fetch(user, path)[0] == 200, path
        admin_status = fetch(user, "/admin/users/pending-count")[0]
        assert admin_status == (200 if me["role"] == "admin" else 403), admin_status
    finally:
        assert fetch(user, "/auth/logout", "POST")[0] == 204
    assert fetch(user, "/auth/me")[0] == 401
    print("HTTPS、备案入口页、登录保护、登录后接口与退出登录：全部通过")


if __name__ == "__main__":
    main()
