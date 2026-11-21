"""Create a private Compose environment file for the PostgreSQL service."""

import os
import secrets
from pathlib import Path


def main() -> None:
    secret_dir = Path(__file__).resolve().parent / "secrets"
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    path = secret_dir / "postgres.env"
    password = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(
            "POSTGRES_USER=liuhen\n"
            "POSTGRES_DB=liuhen\n"
            f"POSTGRES_PASSWORD={password}\n"
        )
    print(f"PostgreSQL 配置已创建：{path}")


if __name__ == "__main__":
    main()
