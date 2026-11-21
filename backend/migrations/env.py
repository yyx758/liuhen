"""Alembic 运行入口：命令行和应用代码（测试建库、启动检查）共用。"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

import backend.app.models  # noqa: F401  注册全部 ORM 模型到 Base.metadata
from backend.app.db.base import Base

config = context.config
if config.config_file_name and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_online() -> None:
    # 应用代码可通过 attributes 传入现成连接（如隔离测试库）；命令行则连接 .env 中配置的数据库。
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, compare_server_default=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    from backend.app.db.session import DATABASE_URL

    engine = create_engine(DATABASE_URL)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_server_default=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("不支持离线模式生成 SQL，请连接数据库执行迁移")
run_migrations_online()
