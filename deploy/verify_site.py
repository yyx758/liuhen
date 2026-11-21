"""Read-only checks for the public gateway after deployment."""

import base64
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


HOST = "liuhen.yyyyx.top"
BASE = f"https://{HOST}"


def fetch(path: str, authorization: str | None = None) -> tuple[int, bytes]:
    headers = {"Authorization": authorization} if authorization else {}
    try:
        with urlopen(Request(BASE + path, headers=headers), timeout=15) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def main() -> None:
    login_file = Path(__file__).resolve().parent / "secrets/login-once.txt"
    credentials = None
    if login_file.exists():
        entries = dict(line.strip().split("：", 1) for line in login_file.read_text(encoding="utf-8").splitlines())
        credentials = (entries["用户名"], entries["密码"])
    elif os.getenv("LIUHEN_VERIFY_AUTH_USER") and os.getenv("LIUHEN_VERIFY_AUTH_PASSWORD"):
        credentials = (os.environ["LIUHEN_VERIFY_AUTH_USER"], os.environ["LIUHEN_VERIFY_AUTH_PASSWORD"])

    status, landing = fetch("/")
    assert status == 200 and "赣ICP备2026013160号-1".encode() in landing
    assert b"https://beian.miit.gov.cn/" in landing

    assert fetch("/app/")[0] == 401
    assert fetch("/tasks")[0] == 401
    assert fetch("/docs")[0] == 404

    if credentials:
        token = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode()
        authorization = f"Basic {token}"
        status, app = fetch("/app/", authorization)
        assert status == 200 and b"/app/assets/" in app
        assert fetch("/app/chat", authorization)[0] == 200
        # 通过网关后，业务接口还需要应用内登录（会话 Cookie）。
        for path in ("/tasks", "/audit/summary", "/auth/me"):
            status, body = fetch(path, authorization)
            assert status == 401 and json.loads(body)["detail"]["code"] == "unauthenticated", path
        print("HTTPS、ICP备案页、网关保护、应用页面与应用内登录保护：全部通过")
    else:
        print("HTTPS、ICP备案页和未登录保护：全部通过；未提供凭据，跳过登录后检查")


if __name__ == "__main__":
    main()
