from collections.abc import Iterator
from os import getenv

from dotenv import dotenv_values
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config.env import ENV_FILE


configured_url = getenv("LIUHEN_DATABASE_URL") or dotenv_values(ENV_FILE, interpolate=False).get("database_url")
if configured_url:
    DATABASE_URL = make_url(configured_url)
else:
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    if any(not getenv(name) for name in required):
        raise RuntimeError("请配置 database_url 或 POSTGRES_USER、POSTGRES_PASSWORD、POSTGRES_DB")
    DATABASE_URL = URL.create(
        "postgresql+psycopg",
        username=getenv("POSTGRES_USER"),
        password=getenv("POSTGRES_PASSWORD"),
        host=getenv("LIUHEN_DATABASE_HOST", "db"),
        database=getenv("POSTGRES_DB"),
    )
if DATABASE_URL.get_backend_name() != "postgresql":
    raise RuntimeError("留痕仅支持 PostgreSQL 数据库")

# Engine：数据库连接基础设施
engine = create_engine(
    DATABASE_URL,
    echo=getenv("LIUHEN_SQL_ECHO", "true").lower() in {"1", "true", "yes"},
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
