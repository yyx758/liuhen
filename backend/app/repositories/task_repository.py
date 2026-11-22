
from datetime import datetime, timezone

from sqlalchemy import select, update, delete
from sqlalchemy.orm import Session

from backend.app.models.task import TaskORM
from backend.app.schemas.task import TaskRead


# 新增待办
def insert_task(session: Session, task: TaskORM) -> TaskORM:
    session.add(task)
    session.flush()
    return task


# 查询某个用户的所有待办
def list_tasks(session: Session, *, user_id: int) -> list[TaskORM]:
    stmt = select(TaskORM).where(TaskORM.user_id == user_id).order_by(TaskORM.id)
    return list(session.scalars(stmt).all())


# 按主键查询；不属于该用户时与不存在一样返回 None
def get_task_by_id(
    session: Session,
    task_id: int,
    *,
    user_id: int,
    for_update: bool = False,
) -> TaskORM | None:
    stmt = select(TaskORM).where(TaskORM.id == task_id, TaskORM.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def find_overlapping_task(
    session: Session,
    start_at: datetime,
    end_at: datetime,
    exclude_task_id: int | None = None,
    *,
    user_id: int,
) -> TaskORM | None:
    stmt = (
        select(TaskORM)
        .where(
            TaskORM.user_id == user_id,
            TaskORM.status == "todo",
            TaskORM.start_at < end_at,
            TaskORM.end_at > start_at
        )
        .limit(1)
    )
    if exclude_task_id is not None:
        stmt = stmt.where(TaskORM.id != exclude_task_id)
    return session.scalars(stmt).first()


# 带版本条件的更新
def update_if_version(
    session: Session,
    task_id: int,
    expected_version: int,
    after: TaskRead
) -> int:

    stmt = (
        update(TaskORM)
        .where(
            TaskORM.id == task_id,
            TaskORM.version == expected_version
        )
        .values(
            title=after.title,
            priority=after.priority,
            due_at=(
                after.due_at.astimezone(timezone.utc).replace(tzinfo=None)
                if after.due_at is not None else None
            ),
            start_at=(
                after.start_at.astimezone(timezone.utc).replace(tzinfo=None)
                if after.start_at is not None else None
            ),
            end_at=(
                after.end_at.astimezone(timezone.utc).replace(tzinfo=None)
                if after.end_at is not None else None
            ),
            status=after.status,
            remind_before_minutes=after.remind_before_minutes,
            version=expected_version + 1
        )
        .execution_options(synchronize_session=False)
    )

    result = session.execute(stmt)

    return result.rowcount


# 带版本条件的删除
def delete_if_version(
    session: Session,
    task_id: int,
    expected_version: int
) -> int:

    stmt = (
        delete(TaskORM)
        .where(
            TaskORM.id == task_id,
            TaskORM.version == expected_version
        )
        .execution_options(synchronize_session=False)
    )

    result = session.execute(stmt)

    return result.rowcount
