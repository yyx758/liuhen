"""Small SQLite migration for the proposal status check constraint."""

from sqlalchemy.engine import Engine


def migrate_postgres_schedule_exclusion(engine: Engine) -> None:
    """Preserve the no-overlap rule when PostgreSQL handles concurrent writers."""
    if engine.dialect.name != "postgresql":
        return

    with engine.begin() as connection:
        exists = connection.exec_driver_sql("""
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'tasks'::regclass AND conname = 'ex_tasks_no_overlap'
        """).scalar_one_or_none()
        if exists is None:
            connection.exec_driver_sql("""
                ALTER TABLE tasks ADD CONSTRAINT ex_tasks_no_overlap
                EXCLUDE USING gist (tsrange(start_at, end_at, '[)') WITH &&)
                WHERE (status = 'todo' AND start_at IS NOT NULL AND end_at IS NOT NULL)
            """)


def migrate_task_time_range(engine: Engine) -> None:
    """Allow a start time without a duration in existing SQLite task tables."""
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        definition = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).scalar_one_or_none()
        if definition is None or "end_at IS NULL OR" in definition:
            return
        if "ck_task_time_range" not in definition:
            raise RuntimeError("tasks 表结构未知，不能自动迁移时间约束")

        connection.exec_driver_sql("""
            CREATE TABLE tasks_new (
                id INTEGER NOT NULL PRIMARY KEY,
                title VARCHAR(100) NOT NULL,
                priority VARCHAR(20) NOT NULL,
                due_at DATETIME,
                start_at DATETIME,
                end_at DATETIME,
                status VARCHAR(20) NOT NULL,
                version INTEGER NOT NULL,
                CONSTRAINT ck_task_time_range CHECK (
                    end_at IS NULL OR (start_at IS NOT NULL AND end_at > start_at)
                )
            )
        """)
        connection.exec_driver_sql("""
            INSERT INTO tasks_new (id, title, priority, due_at, start_at, end_at, status, version)
            SELECT id, title, priority, due_at, start_at, end_at, status, version FROM tasks
        """)
        connection.exec_driver_sql("DROP TABLE tasks")
        connection.exec_driver_sql("ALTER TABLE tasks_new RENAME TO tasks")


def migrate_action_statuses(engine: Engine) -> None:
    """Rebuild the old table once; SQLite cannot ALTER an existing CHECK constraint."""
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        definition = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pending_actions'"
        ).scalar_one_or_none()
        if definition is None:
            return
        if "'replaced'" in definition and "'failed'" in definition:
            return
        if "ck_action_status" not in definition:
            raise RuntimeError("pending_actions 表结构未知，不能自动迁移状态约束")

        connection.exec_driver_sql("""
            CREATE TABLE pending_actions_new (
                id INTEGER NOT NULL PRIMARY KEY,
                task_id INTEGER NOT NULL,
                action_type VARCHAR(10) NOT NULL,
                status VARCHAR(16) NOT NULL,
                expected_version INTEGER NOT NULL,
                before_snapshot JSON NOT NULL,
                after_snapshot JSON,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                CONSTRAINT ck_action_type CHECK (action_type IN ('update', 'delete')),
                CONSTRAINT ck_action_status CHECK (status IN (
                    'pending', 'executed', 'cancelled', 'expired',
                    'conflicted', 'replaced', 'failed'
                ))
            )
        """)
        connection.exec_driver_sql("""
            INSERT INTO pending_actions_new (
                id, task_id, action_type, status, expected_version,
                before_snapshot, after_snapshot, created_at, expires_at
            )
            SELECT id, task_id, action_type, status, expected_version,
                   before_snapshot, after_snapshot, created_at, expires_at
            FROM pending_actions
        """)
        connection.exec_driver_sql("DROP TABLE pending_actions")
        connection.exec_driver_sql("ALTER TABLE pending_actions_new RENAME TO pending_actions")
        connection.exec_driver_sql(
            "CREATE INDEX ix_pending_actions_task_id ON pending_actions (task_id)"
        )
