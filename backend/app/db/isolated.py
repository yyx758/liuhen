"""Create a disposable PostgreSQL database for tests and Agent evaluations."""

from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import create_engine

from backend.app.db.base import Base
from backend.app.db.session import DATABASE_URL
from backend.app.models import action, conversation, operation_event, request, task  # noqa: F401


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
            Base.metadata.create_all(engine)
            yield engine
        finally:
            engine.dispose()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
