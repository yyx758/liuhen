import unittest

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

import backend.app.models  # noqa: F401
from backend.app.db.base import Base
from backend.app.db.isolated import isolated_engine
from backend.app.db.migrations import ensure_schema_current


class MigrationTests(unittest.TestCase):
    def test_migrated_schema_matches_models(self) -> None:
        # 迁移脚本建出的结构必须和 ORM 模型一致：改了模型却忘写迁移，这里会列出差异。
        with isolated_engine() as engine, engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_server_default": True})
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_named_constraints_exist(self) -> None:
        # compare_metadata 不比较 EXCLUDE 等约束，按名字逐个核对模型里声明的约束。
        expected = {
            (table.name, constraint.name)
            for table in Base.metadata.tables.values()
            for constraint in table.constraints
            if isinstance(constraint.name, str)
        }
        with isolated_engine() as engine, engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT rel.relname, con.conname FROM pg_constraint con "
                "JOIN pg_class rel ON rel.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = rel.relnamespace WHERE ns.nspname = 'public'"
            ))
            actual = {(table, name) for table, name in rows}
        self.assertTrue(expected)
        self.assertEqual(expected - actual, set())

    def test_startup_check_requires_latest_revision(self) -> None:
        with isolated_engine() as engine:
            ensure_schema_current(engine)
            with engine.begin() as connection:
                connection.execute(text("UPDATE alembic_version SET version_num = '0001'"))
            with self.assertRaisesRegex(RuntimeError, "upgrade head"):
                ensure_schema_current(engine)


if __name__ == "__main__":
    unittest.main()
