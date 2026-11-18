
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


# 查询所有待办
def list_tasks(session: Session) -> list[TaskORM]:
    stmt = select(TaskORM).order_by(TaskORM.id)
    return list(session.scalars(stmt).all())


# 按主键查询
def get_task_by_id(
    session: Session,
    task_id: int,
    *,
    for_update: bool = False,
) -> TaskORM | None:
    if for_update:
        return session.scalar(select(TaskORM).where(TaskORM.id == task_id).with_for_update())
    return session.get(TaskORM, task_id)


def find_overlapping_task(
    session: Session,
    start_at: datetime,
    end_at: datetime,
    exclude_task_id: int | None = None,
) -> TaskORM | None:
    stmt = (
        select(TaskORM)
        .where(
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
