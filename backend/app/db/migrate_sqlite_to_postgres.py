"""Copy the local SQLite database into a new, empty PostgreSQL database.

Run from the project root after setting database_url in .env:
    python -m backend.app.db.migrate_sqlite_to_postgres
"""

from pathlib import Path

from sqlalchemy import Integer, MetaData, Table, URL, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url

from backend.app.db.base import Base
from backend.app.db.session import DATABASE_URL, DB_PATH

# Register all ORM tables before asking metadata for their dependency order.
from backend.app.models import action, conversation, operation_event, request, task  # noqa: F401


def migrate(source_path: Path = DB_PATH) -> dict[str, int]:
    if not source_path.is_file():
        raise RuntimeError(f"SQLite 源文件不存在：{source_path}")
    if make_url(DATABASE_URL).get_backend_name() != "postgresql":
        raise RuntimeError("请先在项目根目录 .env 配置 PostgreSQL database_url")

    source_engine = create_engine(URL.create("sqlite+pysqlite", database=str(source_path)))
    target_engine = create_engine(DATABASE_URL)
    copied: dict[str, int] = {}
    try:
        with source_engine.connect() as source, target_engine.begin() as target:
            source.exec_driver_sql("PRAGMA query_only = ON")
            existing = inspect(target).get_table_names(schema="public")
            if existing:
                raise RuntimeError(f"目标数据库不是空库，已有表：{', '.join(existing)}")

            source_tables = set(inspect(source).get_table_names())
            Base.metadata.create_all(target)
            for table in Base.metadata.sorted_tables:
                if table.name not in source_tables:
                    copied[table.name] = 0
                    continue

                old_table = Table(table.name, MetaData(), autoload_with=source)
                missing = set(table.columns.keys()) - set(old_table.columns.keys())
                if missing:
                    raise RuntimeError(f"源表 {table.name} 缺少列：{', '.join(sorted(missing))}")

                rows = [dict(row._mapping) for row in source.execute(
                    select(old_table).order_by(*old_table.primary_key.columns)
                )]
                if rows:
                    target.execute(table.insert(), rows)
                copied[table.name] = len(rows)

                new_rows = [dict(row._mapping) for row in target.execute(
                    select(table).order_by(*table.primary_key.columns)
                )]
                if rows != new_rows:
                    raise RuntimeError(f"表 {table.name} 迁移后数据校验失败")

                if "id" in table.primary_key.columns and isinstance(table.c.id.type, Integer) and rows:
                    max_id = target.scalar(select(func.max(table.c.id)))
                    target.execute(text(
                        "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), :max_id, true)"
                    ), {"table_name": table.name, "max_id": max_id})

        return copied
    finally:
        source_engine.dispose()
        target_engine.dispose()


if __name__ == "__main__":
    for table_name, count in migrate().items():
        print(f"{table_name}: {count}")
