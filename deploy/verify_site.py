"""Read-only checks for the public gateway after deployment."""

import base64
import http.client
import json
import ssl
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


def check_redirect(host: str) -> None:
    connection = http.client.HTTPSConnection(host, 443, timeout=10, context=ssl.create_default_context())
    try:
        connection.request("GET", "/", headers={"Host": host})
        response = connection.getresponse()
        assert response.status == 301, (host, response.status)
        assert response.getheader("Location") == BASE + "/", host
        response.read()
    finally:
        connection.close()


def main() -> None:
    login_file = Path(__file__).resolve().parent / "secrets/login-once.txt"
    entries = dict(line.strip().split("：", 1) for line in login_file.read_text(encoding="utf-8").splitlines())
    token = base64.b64encode(f"{entries['用户名']}:{entries['密码']}".encode()).decode()
    authorization = f"Basic {token}"

    status, landing = fetch("/")
    assert status == 200 and "赣ICP备2026013160号-1".encode() in landing
    assert b"https://beian.miit.gov.cn/" in landing

    assert fetch("/app/")[0] == 401
    assert fetch("/tasks")[0] == 401
    status, app = fetch("/app/", authorization)
    assert status == 200 and b"/app/assets/" in app
    assert fetch("/app/chat", authorization)[0] == 200
    status, tasks = fetch("/tasks", authorization)
    assert status == 200 and isinstance(json.loads(tasks), list)
    status, summary = fetch("/audit/summary", authorization)
    assert status == 200 and isinstance(json.loads(summary), dict)
    assert fetch("/docs")[0] == 404

    check_redirect("yyyyx.top")
    check_redirect("www.yyyyx.top")
    print("HTTPS、ICP备案页、登录保护、应用、API 和旧域名跳转：全部通过")


if __name__ == "__main__":
    main()
