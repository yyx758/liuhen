#pending action相关数据库操作

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.models.action import PendingActionORM

# 1. 保存操作提案,这里是只保存到数据库,不是确定操作
def insert_action(
        session: Session,
        action: PendingActionORM
        ) -> PendingActionORM :
    session.add(action)
    session.flush()

    return action



# 2. 根据 ID 查询操作提案
def get_action_by_id(
            session:Session,
            id:int
        ) -> PendingActionORM:
    return session.get(PendingActionORM,id)


# 3. 条件更新操作状态
def transition_status(
    session: Session,
    action_id: int,
    from_status: str,
    to_status: str,
    not_expired_after: int | None = None
) -> bool:

    stmt = (
        update(PendingActionORM)
        .where(
            PendingActionORM.id == action_id,
            PendingActionORM.status == from_status
        )
        .values(status=to_status)
        .execution_options(synchronize_session=False)
    )

    # 可选：要求提案在指定时间之后才过期
    if not_expired_after is not None:
        stmt = stmt.where(
            PendingActionORM.expires_at > not_expired_after
        )

    result = session.execute(stmt)

    return result.rowcount == 1


def expire_pending(session: Session, now: int, task_id: int | None = None) -> int:
    stmt = update(PendingActionORM).where(
        PendingActionORM.status == "pending",
        PendingActionORM.expires_at <= now,
    ).values(status="expired")
    if task_id is not None:
        stmt = stmt.where(PendingActionORM.task_id == task_id)
    return session.execute(stmt.execution_options(synchronize_session=False)).rowcount


def list_due_pending(session: Session, now: int, task_id: int | None = None) -> list[PendingActionORM]:
    stmt = select(PendingActionORM).where(
        PendingActionORM.status == "pending",
        PendingActionORM.expires_at <= now,
    ).order_by(PendingActionORM.id)
    if task_id is not None:
        stmt = stmt.where(PendingActionORM.task_id == task_id)
    return list(session.scalars(stmt).all())


def list_pending_for_task(session: Session, task_id: int) -> list[PendingActionORM]:
    stmt = select(PendingActionORM).where(
        PendingActionORM.task_id == task_id,
        PendingActionORM.status == "pending",
    ).order_by(PendingActionORM.id)
    return list(session.scalars(stmt).all())


def replace_pending_for_task(session: Session, task_id: int) -> int:
    stmt = (
        update(PendingActionORM)
        .where(
            PendingActionORM.task_id == task_id,
            PendingActionORM.status == "pending",
        )
        .values(status="replaced")
        .execution_options(synchronize_session=False)
    )
    return session.execute(stmt).rowcount
