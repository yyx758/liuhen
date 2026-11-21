#pending action相关数据库操作

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from backend.app.models.action import PendingActionORM
from backend.app.models.operation_event import OperationEventORM
from backend.app.models.request import ChatTurnORM

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
            id:int,
            for_update: bool = False,
        ) -> PendingActionORM:
    return session.get(PendingActionORM, id, with_for_update=for_update)


def list_active_pending(session: Session, now: int) -> list[PendingActionORM]:
    stmt = select(PendingActionORM).where(
        PendingActionORM.status == "pending",
        PendingActionORM.expires_at > now,
    ).order_by(PendingActionORM.id.desc())
    return list(session.scalars(stmt).all())


def list_for_conversation(session: Session, conversation_id: str) -> list[tuple[PendingActionORM, str]]:
    request_ids = select(ChatTurnORM.request_id).where(ChatTurnORM.conversation_id == conversation_id)
    links = session.execute(select(
        OperationEventORM.action_id, OperationEventORM.request_id,
    ).where(
        OperationEventORM.event_type == "proposal_created",
        OperationEventORM.request_id.in_(request_ids),
    )).all()
    request_by_action = {
        action_id: request_id for action_id, request_id in links
        if action_id is not None and request_id is not None
    }
    if not request_by_action:
        return []
    actions = session.scalars(select(PendingActionORM).where(
        PendingActionORM.id.in_(request_by_action)
    ).order_by(PendingActionORM.id.desc())).all()
    return [(action, request_by_action[action.id]) for action in actions]


def delete_expired_action(session: Session, action_id: int, now: int) -> bool:
    stmt = delete(PendingActionORM).where(
        PendingActionORM.id == action_id,
        PendingActionORM.expires_at <= now,
        PendingActionORM.status.in_(("pending", "expired")),
    ).execution_options(synchronize_session=False)
    return session.execute(stmt).rowcount == 1


def list_expired_actions(session: Session, now: int, *, for_update: bool = False) -> list[PendingActionORM]:
    stmt = select(PendingActionORM).where(
        PendingActionORM.expires_at <= now,
        PendingActionORM.status.in_(("pending", "expired")),
    ).order_by(PendingActionORM.id)
    if for_update:
        stmt = stmt.with_for_update()
    return list(session.scalars(stmt).all())


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


def expire_pending(session: Session, now: int, task_id: int) -> int:
    stmt = update(PendingActionORM).where(
        PendingActionORM.task_id == task_id,
        PendingActionORM.status == "pending",
        PendingActionORM.expires_at <= now,
    ).values(status="expired")
    return session.execute(stmt.execution_options(synchronize_session=False)).rowcount


def list_due_pending(session: Session, now: int, task_id: int) -> list[PendingActionORM]:
    stmt = select(PendingActionORM).where(
        PendingActionORM.task_id == task_id,
        PendingActionORM.status == "pending",
        PendingActionORM.expires_at <= now,
    ).order_by(PendingActionORM.id)
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
