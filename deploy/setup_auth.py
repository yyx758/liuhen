"""Create one local-only login credential for the Caddy gateway."""

import os
import secrets
import subprocess
from pathlib import Path


def write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(content)


def main() -> None:
    secret_dir = Path(__file__).resolve().parent / "secrets"
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    auth_file = secret_dir / "auth.env"
    login_file = secret_dir / "login-once.txt"
    if auth_file.exists() or login_file.exists():
        raise SystemExit("已有登录配置；未覆盖现有凭据")

    username = "liuhen"
    password = secrets.token_urlsafe(24)
    result = subprocess.run(
        ["docker", "run", "--rm", "-i", "caddy:2-alpine", "caddy", "hash-password"],
        input=password + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    password_hash = result.stdout.strip()
    if not password_hash.startswith("$2"):
        raise SystemExit("Caddy 未返回有效的 bcrypt 哈希")

    write_private(auth_file, f"LIUHEN_AUTH_USER='{username}'\nLIUHEN_AUTH_HASH='{password_hash}'\n")
    write_private(login_file, f"用户名：{username}\n密码：{password}\n")
    print(f"登录配置已创建。请通过 SSH 查看 {login_file}，保存密码后删除此一次性文件。")


if __name__ == "__main__":
    main()
