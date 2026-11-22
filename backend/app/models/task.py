from datetime import datetime

from sqlalchemy import String, DateTime, CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base


class TaskORM(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    # 任务归属；所有按 ID 或列表的查询都必须带上 user_id 条件。
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

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

    # 提前多少分钟提醒；为空表示不提醒，0 表示准时。基准时间是 start_at，没有时用 due_at。
    remind_before_minutes: Mapped[int | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "end_at IS NULL OR (start_at IS NOT NULL AND end_at > start_at)",
            name="ck_task_time_range"
        ),
        # 同一用户的待办时间段不能重叠；不同用户互不影响（user_id 的 "=" 需要 btree_gist 扩展）。
        ExcludeConstraint(
            ("user_id", "="),
            (text("tsrange(start_at, end_at, '[)')"), "&&"),
            where=text("status = 'todo' AND start_at IS NOT NULL AND end_at IS NOT NULL"),
            name="ex_tasks_no_overlap",
        ),
        Index("ix_tasks_user_id_id", "user_id", "id"),
        CheckConstraint("remind_before_minutes BETWEEN 0 AND 10080", name="ck_tasks_remind_before_minutes"),
    )
