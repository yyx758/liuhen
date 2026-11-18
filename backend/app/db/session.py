from collections.abc import Iterator
from os import getenv
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, URL
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config.env import ENV_FILE


# 数据库文件路径
BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "liuhen_dev.db"

SQLITE_URL = URL.create(
    "sqlite+pysqlite",
    database=str(DB_PATH)
)
DATABASE_URL = (
    getenv("LIUHEN_DATABASE_URL")
    or dotenv_values(ENV_FILE, interpolate=False).get("database_url")
    or SQLITE_URL
)

# Engine：数据库连接基础设施
engine = create_engine(
    DATABASE_URL,
    echo=True,
    **({"connect_args": {"check_same_thread": False}} if make_url(DATABASE_URL).get_backend_name() == "sqlite" else {})
)

# Session 工厂
SessionLocal = sessionmaker(
    bind=engine,
    expire_on_commit=False
)


# FastAPI 数据库依赖
def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
