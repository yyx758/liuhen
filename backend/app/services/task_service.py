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
from backend.app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from backend.app.repositories import action_repository, task_repository
from backend.app.config.settings import USER_TIMEZONE
from backend.app.services import audit_service, reminder_service


# 将数据库实体转换为响应 DTO
def to_db_utc(value: datetime) -> datetime:
    # 统一使用 UTC 无时区值作为数据库表示
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
        version=task.version,
        remind_before_minutes=task.remind_before_minutes,
    )


# 新增待办
def create_task(
    data: TaskCreate,
    session: Session,
    request_id: str | None = None,
    *,
    user_id: int,
) -> TaskRead:
    try:
        return _create_task(data, session, request_id, user_id)
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23P01":
            raise HTTPException(409, "时间与已有任务冲突") from exc
        raise


def _create_task(
    data: TaskCreate,
    session: Session,
    request_id: str | None,
    user_id: int,
) -> TaskRead:

    # Service 管理事务
    owns_transaction = not session.info.get("chat_tool_transaction", False)
    with session.begin() if owns_transaction else nullcontext():

        payload_hash = None
        if request_id is not None:
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
                # 幂等键全局唯一：别人用过的键不能回放出别人的任务快照。
                if previous.user_id != user_id:
                    raise HTTPException(409, "幂等键已被使用，请换一个新的幂等键")
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

            conflict = task_repository.find_overlapping_task(session, start_at, end_at, user_id=user_id)
            if conflict is not None:
                raise HTTPException(409, f"时间与任务 {conflict.id} 冲突")

        # DTO 转 ORM
        task = TaskORM(
            user_id=user_id,
            title=data.title,
            priority=data.priority,
            due_at=to_db_utc(data.due_at) if data.due_at is not None else None,
            start_at=start_at,
            end_at=end_at,
            remind_before_minutes=data.remind_before_minutes,
        )

        # 调用 Repository 保存
        task = task_repository.insert_task(session, task)

        # ORM 转响应 DTO
        result = to_task_read(task)
        reminder_service.sync_task_reminder(session, result, previous=None, user_id=user_id)
        audit_service.record_event(
            session, "task_created", task.id, user_id=user_id,
            details={"task": result.model_dump(mode="json")},
            request_id=request_id,
        )
        if request_id is not None:
            session.add(TaskCreateRequestORM(
                request_id=request_id,
                user_id=user_id,
                payload_hash=payload_hash,
                result_snapshot=result.model_dump(mode="json"),
                created_at=int(datetime.now(timezone.utc).timestamp()),
            ))

    # 退出 begin 时已成功提交事务
    return result


# 查询所有待办
def list_tasks(session: Session, *, user_id: int) -> list[TaskRead]:

    tasks = task_repository.list_tasks(session, user_id=user_id)

    return [to_task_read(task) for task in tasks]


# 根据 ID 查询待办
def get_task(
    task_id: int,
    session: Session,
    *,
    user_id: int,
) -> TaskRead:

    task = task_repository.get_task_by_id(
        session,
        task_id,
        user_id=user_id,
    )

    if task is None:
        raise HTTPException(
            status_code=404,
            detail="待办不存在"
        )

    return to_task_read(task)


def _replace_pending_after_manual_change(
    session: Session, task_id: int, operation: str, *, user_id: int
) -> None:
    # 手动写入已改变任务版本；旧的 Agent 提案不再适用。
    from backend.app.services import action_service

    action_service.expire_pending_with_audit(session, action_service.now_timestamp(), task_id)
    pending = action_repository.list_pending_for_task(session, task_id)
    if action_repository.replace_pending_for_task(session, task_id) != len(pending):
        raise RuntimeError("手动操作后提案状态与审计记录不一致")
    for action in pending:
        audit_service.record_event(
            session, "proposal_replaced", task_id, user_id=user_id, action_id=action.id,
            from_status="pending", to_status="replaced",
            reason="任务已被用户手动修改",
            details={"manual_operation": operation},
        )


def update_task(
    session: Session, task_id: int, data: TaskUpdate, expected_version: int, *, user_id: int
) -> TaskRead:
    from backend.app.services.action_service import build_updated_snapshot, find_schedule_conflict

    try:
        with session.begin():
            task = task_repository.get_task_by_id(
                session, task_id, user_id=user_id, for_update=True
            )
            if task is None:
                raise HTTPException(404, "待办不存在")
            before = to_task_read(task)
            if before.version != expected_version:
                raise HTTPException(409, "任务已变化，请刷新后重试")
            after = build_updated_snapshot(before, data)
            if before.model_dump(exclude={"version"}) == after.model_dump(exclude={"version"}):
                raise HTTPException(409, "修改前后没有变化")
            conflict = find_schedule_conflict(session, task_id, after, user_id=user_id)
            if conflict is not None:
                raise HTTPException(409, f"时间与任务 {conflict.id} 冲突")
            if task_repository.update_if_version(session, task_id, expected_version, after) != 1:
                raise HTTPException(409, "任务已变化，请刷新后重试")
            reminder_service.sync_task_reminder(session, after, previous=before, user_id=user_id)
            _replace_pending_after_manual_change(session, task_id, "update", user_id=user_id)
            audit_service.record_event(
                session, "task_updated", task_id, user_id=user_id,
                details={
                    "source": "manual",
                    "before": before.model_dump(mode="json"),
                    "after": after.model_dump(mode="json"),
                },
            )
        return after
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23P01":
            raise HTTPException(409, "时间与已有任务冲突") from exc
        raise


def delete_task(session: Session, task_id: int, expected_version: int, *, user_id: int) -> None:
    with session.begin():
        task = task_repository.get_task_by_id(
            session, task_id, user_id=user_id, for_update=True
        )
        if task is None:
            raise HTTPException(404, "待办不存在")
        before = to_task_read(task)
        if before.version != expected_version:
            raise HTTPException(409, "任务已变化，请刷新后重试")
        if task_repository.delete_if_version(session, task_id, expected_version) != 1:
            raise HTTPException(409, "任务已变化，请刷新后重试")
        _replace_pending_after_manual_change(session, task_id, "delete", user_id=user_id)
        audit_service.record_event(
            session, "task_deleted", task_id, user_id=user_id,
            details={"source": "manual", "before": before.model_dump(mode="json")},
        )
