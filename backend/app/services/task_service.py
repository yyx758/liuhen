from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.models.task import TaskORM
from backend.app.models.request import TaskCreateRequestORM
from backend.app.schemas.task import TaskCreate, TaskRead
from backend.app.repositories import task_repository
from backend.app.config.settings import USER_TIMEZONE
from backend.app.services import audit_service


# 将数据库实体转换为响应 DTO
def to_db_utc(value: datetime) -> datetime:
    # SQLite 的 DATETIME 不保留时区；统一使用 UTC 无时区值作为数据库表示
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def from_db_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(USER_TIMEZONE)


def to_task_read(task: TaskORM) -> TaskRead:
    duration_minutes = None
    if task.start_at is not None and task.end_at is not None:
        duration_minutes = int((task.end_at - task.start_at).total_seconds() // 60)

    return TaskRead(
        id=task.id,
        title=task.title,
        priority=task.priority,
        start_at=from_db_utc(task.start_at),
        end_at=from_db_utc(task.end_at),
        duration_minutes=duration_minutes,
        due_at=from_db_utc(task.due_at),
        status=task.status,
        version=task.version
    )


# 新增待办
def create_task(
    data: TaskCreate,
    session: Session,
    request_id: str | None = None,
) -> TaskRead:
    try:
        return _create_task(data, session, request_id)
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23P01":
            raise HTTPException(409, "时间与已有任务冲突") from exc
        raise


def _create_task(
    data: TaskCreate,
    session: Session,
    request_id: str | None,
) -> TaskRead:

    # Service 管理事务
    owns_transaction = not session.info.get("chat_tool_transaction", False)
    with session.begin() if owns_transaction else nullcontext():

        # SQLite 先取得写锁，避免两个创建请求同时查询到“没有冲突”。
        # 查询、插入和提交在同一个事务里完成。
        if owns_transaction and session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

        payload_hash = None
        if request_id is not None:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(1, hashtext(:key))"),
                    {"key": request_id},
                )
            payload = data.model_dump(mode="json")
            payload_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            previous = session.get(TaskCreateRequestORM, request_id)
            if previous is not None:
                if previous.payload_hash != payload_hash:
                    raise HTTPException(409, "幂等键已用于不同的创建请求")
                return TaskRead.model_validate(previous.result_snapshot)

        start_at = to_db_utc(data.start_at) if data.start_at is not None else None
        end_at = None
        if start_at is not None and data.duration_minutes is not None:
            try:
                end_at = start_at + timedelta(minutes=data.duration_minutes)
            except OverflowError as exc:
                raise HTTPException(422, "任务时长超出可表示的范围") from exc

            conflict = task_repository.find_overlapping_task(session, start_at, end_at)
            if conflict is not None:
                raise HTTPException(409, f"时间与任务 {conflict.id} 冲突")

        # DTO 转 ORM
        task = TaskORM(
            title=data.title,
            priority=data.priority,
            due_at=to_db_utc(data.due_at) if data.due_at is not None else None,
            start_at=start_at,
            end_at=end_at
        )

        # 调用 Repository 保存
        task = task_repository.insert_task(session, task)

        # ORM 转响应 DTO
        result = to_task_read(task)
        audit_service.record_event(
            session, "task_created", task.id,
            details={"task": result.model_dump(mode="json")},
            request_id=request_id,
        )
        if request_id is not None:
            session.add(TaskCreateRequestORM(
                request_id=request_id,
                payload_hash=payload_hash,
                result_snapshot=result.model_dump(mode="json"),
                created_at=int(datetime.now(timezone.utc).timestamp()),
            ))

    # 退出 begin 时已成功提交事务
    return result


# 查询所有待办
def list_tasks(session: Session) -> list[TaskRead]:

    tasks = task_repository.list_tasks(session)

    return [to_task_read(task) for task in tasks]


# 根据 ID 查询待办
def get_task(
    task_id: int,
    session: Session
) -> TaskRead:

    task = task_repository.get_task_by_id(
        session,
        task_id
    )

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="待办不存在"
        )

    return to_task_read(task)
