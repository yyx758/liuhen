"""在应用代码中调用 Alembic：测试/测评建库与启动时的版本检查。"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
UPGRADE_HINT = "alembic -c backend/alembic.ini upgrade head"


def _config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logger"] = False
    return config


def upgrade_to_head(engine: Engine) -> None:
    config = _config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def ensure_schema_current(engine: Engine) -> None:
    head = ScriptDirectory.from_config(_config()).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != head:
        raise RuntimeError(f"数据库结构版本为 {current}，需要 {head}；请先执行：{UPGRADE_HINT}")
