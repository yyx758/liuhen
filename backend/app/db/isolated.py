"""Create a disposable PostgreSQL database for tests and Agent evaluations."""

from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import create_engine

from backend.app.db.migrations import upgrade_to_head
from backend.app.db.session import DATABASE_URL


@contextmanager
def isolated_engine():
    name = f"liuhen_test_{uuid4().hex[:12]}"
    with psycopg.connect(
        host=DATABASE_URL.host,
        port=DATABASE_URL.port,
        user=DATABASE_URL.username,
        password=DATABASE_URL.password,
        dbname="postgres",
        autocommit=True,
    ) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        engine = create_engine(DATABASE_URL.set(database=name))
        try:
            # 与线上同样走 Alembic 迁移建表，保证测试库结构和迁移结果一致。
            upgrade_to_head(engine)
            yield engine
        finally:
            engine.dispose()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def create_user(engine, username: str, *, daily_chat_limit: int | None = None) -> int:
    """在临时库中创建一个状态正常、无法用密码登录的用户，返回其 ID；测试与测评用来归属业务数据。"""
    from sqlalchemy.orm import Session

    from backend.app.models.user import UserORM

    with Session(engine) as session, session.begin():
        user = UserORM(username=username, password_hash="!", status="active", daily_chat_limit=daily_chat_limit)
        session.add(user)
        session.flush()
        return user.id
