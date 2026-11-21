"""账号管理命令行（不对外暴露 HTTP 接口）。在项目根目录执行：

    python -m backend.app.cli create-admin --username <名字> [--email <邮箱>]
    python -m backend.app.cli promote --username <名字>
    python -m backend.app.cli reset-password --username <名字>
"""

import argparse
import getpass
import sys

from fastapi import HTTPException

from backend.app.db.migrations import ensure_schema_current
from backend.app.db.session import SessionLocal, engine
from backend.app.services import account_service


def read_new_password() -> str:
    first = getpass.getpass("设置密码：")
    if first != getpass.getpass("再次输入："):
        raise SystemExit("两次输入的密码不一致")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.app.cli", description="留痕账号管理")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="创建状态为正常的管理员账号")
    create.add_argument("--username", required=True)
    create.add_argument("--email")
    commands.add_parser("promote", help="把正常状态的账号提升为管理员").add_argument("--username", required=True)
    commands.add_parser("reset-password", help="生成临时密码并让该账号所有设备下线").add_argument("--username", required=True)
    args = parser.parse_args(argv)

    ensure_schema_current(engine)
    try:
        with SessionLocal() as session:
            if args.command == "create-admin":
                user = account_service.create_admin(
                    session, username=args.username, password=read_new_password(), email=args.email,
                )
                print(f"已创建管理员 {user.username}（ID {user.id}）")
            elif args.command == "promote":
                account_service.promote_to_admin(session, username=args.username)
                print(f"已将 {args.username} 提升为管理员")
            else:
                temporary = account_service.reset_password(session, username=args.username)
                print(f"临时密码（只显示这一次，对方登录后须立即修改）：{temporary}")
    except HTTPException as exc:
        print(exc.detail, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
