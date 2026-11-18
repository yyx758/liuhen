
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from uuid import UUID
from sqlalchemy.orm import Session

from backend.app.db.session import get_db
from backend.app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from backend.app.schemas.action import PendingAction
from backend.app.services import task_service, action_service


router = APIRouter(
    prefix="/tasks",
    tags=["Tasks"]
)

DbSession = Annotated[Session, Depends(get_db)]


# 新增待办
@router.post(
    "",
    response_model=TaskRead,
    status_code=201
)
def create_task(
    data: TaskCreate,
    session: DbSession,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> TaskRead:

    return task_service.create_task(data, session, str(idempotency_key))


# 查询全部待办
@router.get(
    "",
    response_model=list[TaskRead]
)
def list_tasks(
    session: DbSession
) -> list[TaskRead]:

    return task_service.list_tasks(session)


# 按 ID 查询
@router.get(
    "/{task_id}",
    response_model=TaskRead
)
def get_task(
    task_id: int,
    session: DbSession
) -> TaskRead:

    return task_service.get_task(task_id, session)


# 创建修改提案
@router.post(
    "/{task_id}/proposals/update",
    response_model=PendingAction,
    status_code=201
)
def propose_update(
    task_id: int,
    data: TaskUpdate,
    session: DbSession
) -> PendingAction:

    return action_service.propose_update(
        session,
        task_id,
        data
    )


# 创建删除提案
@router.post(
    "/{task_id}/proposals/delete",
    response_model=PendingAction,
    status_code=201
)
def propose_delete(
    task_id: int,
    session: DbSession
) -> PendingAction:

    return action_service.propose_delete(
        session,
        task_id
    )
