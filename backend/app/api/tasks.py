
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from uuid import UUID
from sqlalchemy.orm import Session

from backend.app.db.session import get_db
from backend.app.schemas.task import TaskCreate, TaskListItem, TaskRead, TaskUpdate
from backend.app.schemas.action import PendingAction
from backend.app.services import task_service, action_service, audit_service


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
    response_model=list[TaskListItem]
)
def list_tasks(
    session: DbSession
) -> list[TaskListItem]:

    tasks = task_service.list_tasks(session)
    agent_ids = audit_service.agent_created_task_ids(session, [task.id for task in tasks])
    return [TaskListItem.model_validate({
        **task.model_dump(), "created_by_agent": task.id in agent_ids,
    }) for task in tasks]


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


@router.patch("/{task_id}", response_model=TaskRead)
def update_task(
    task_id: int,
    data: TaskUpdate,
    session: DbSession,
    expected_version: int = Header(alias="If-Match", gt=0),
) -> TaskRead:
    return task_service.update_task(session, task_id, data, expected_version)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: int,
    session: DbSession,
    expected_version: int = Header(alias="If-Match", gt=0),
) -> Response:
    task_service.delete_task(session, task_id, expected_version)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
