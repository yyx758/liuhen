from datetime import datetime

from sqlalchemy import String, DateTime, CheckConstraint, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class TaskORM(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    title: Mapped[str] = mapped_column(
        String(100),
        nullable=False
    )

    priority: Mapped[str] = mapped_column(
        String(20),
        default="medium"
    )

    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False),
        nullable=True
    )

    # 按 UTC 无时区值存储，读写时在 Service 边界转换
    start_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20),
        default="todo"
    )

    version: Mapped[int] = mapped_column(
        default=1
    )

    __table_args__ = (
        CheckConstraint(
            "end_at IS NULL OR (start_at IS NOT NULL AND end_at > start_at)",
            name="ck_task_time_range"
        ),
        ExcludeConstraint(
            (text("tsrange(start_at, end_at, '[)')"), "&&"),
            where=text("status = 'todo' AND start_at IS NOT NULL AND end_at IS NOT NULL"),
            name="ex_tasks_no_overlap",
        ),
    )
