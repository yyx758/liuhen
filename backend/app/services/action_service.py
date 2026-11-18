
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.models.action import PendingActionORM
from backend.app.schemas.action import PendingAction
from backend.app.schemas.task import TaskRead, TaskUpdate
from backend.app.repositories import (
    action_repository,
    task_repository
)
from backend.app.services.task_service import to_db_utc, to_task_read
from backend.app.services import audit_service


class ActionExecutionError(Exception):
    """A deterministic execution failure that must close the proposal."""


# ========= 通用方法 =========

def now_timestamp() -> int:
    return int(now_utc().timestamp())


def unavailable_status_message(status: str, operation: str) -> str:
    reasons = {
        "executed": "提案已执行",
        "cancelled": "提案已取消",
        "expired": "提案已过期",
        "conflicted": "提案与当前任务冲突",
        "replaced": "提案已被新提案替换",
        "failed": "提案执行失败",
    }
    return f"{reasons.get(status, f'提案状态为 {status}')}，不能{operation}"


# ORM → Pydantic 响应 DTO
def to_action_read(action: PendingActionORM) -> PendingAction:

    before = TaskRead.model_validate(
        action.before_snapshot
    )

    after = None
    if action.after_snapshot is not None:
        try:
            after = TaskRead.model_validate(action.after_snapshot)
        except ValidationError:
            if action.status != "failed":
                raise

    return PendingAction(
        id=action.id,
        task_id=action.task_id,
        action_type=action.action_type,
        before=before,
        after=after,
        status=action.status,
        created_at=datetime.fromtimestamp(action.created_at, timezone.utc),
        expires_at=datetime.fromtimestamp(action.expires_at, timezone.utc),
    )


def build_updated_snapshot(before: TaskRead, data: TaskUpdate) -> TaskRead:
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "没有提供修改字段")

    for field in ("title", "priority", "start_at", "end_at", "duration_minutes", "status"):
        if field in changes and changes[field] is None:
            raise HTTPException(422, f"{field} 不能为 null")

    time_fields = {"start_at", "end_at", "duration_minutes"}
    if time_fields & changes.keys():
        start_at = changes.get("start_at", before.start_at)
        if start_at is None:
            raise HTTPException(422, "请同时提供开始时间")

        if "end_at" in changes:
            end_at = changes["end_at"]
            seconds = (end_at - start_at).total_seconds()
            if seconds <= 0 or seconds % 60 != 0:
                raise HTTPException(422, "结束时间必须晚于开始时间，且时长为整分钟")
            duration_minutes = int(seconds // 60)
            if (
                "duration_minutes" in changes
                and changes["duration_minutes"] != duration_minutes
            ):
                raise HTTPException(422, "结束时间与任务时长不一致")
        else:
            duration_minutes = changes.get("duration_minutes", before.duration_minutes)
            end_at = None
            if duration_minutes is not None:
                try:
                    end_at = start_at + timedelta(minutes=duration_minutes)
                except OverflowError as exc:
                    raise HTTPException(422, "任务时长超出可表示的范围") from exc

        changes.update(
            start_at=start_at,
            end_at=end_at,
            duration_minutes=duration_minutes,
        )

    if (
        (before.start_at is not None or before.due_at is not None)
        and changes.get("start_at", before.start_at) is None
        and changes.get("due_at", before.due_at) is None
    ):
        raise HTTPException(422, "开始时间和截止时间至少保留一个")

    return TaskRead.model_validate({
        **before.model_dump(),
        **changes,
        "version": before.version + 1,
    })


def find_schedule_conflict(
    session: Session, task_id: int, candidate: TaskRead
):
    if candidate.status != "todo" or candidate.start_at is None or candidate.end_at is None:
        return None
    return task_repository.find_overlapping_task(
        session,
        to_db_utc(candidate.start_at),
        to_db_utc(candidate.end_at),
        exclude_task_id=task_id,
    )


def expire_pending_with_audit(
    session: Session, now: int, task_id: int | None = None
) -> int:
    due = action_repository.list_due_pending(session, now, task_id)
    if not due:
        return 0
    changed = action_repository.expire_pending(session, now, task_id)
    if changed != len(due):
        raise RuntimeError("提案过期状态与审计记录不一致")
    for old in due:
        audit_service.record_event(
            session, "proposal_expired", old.task_id, action_id=old.id,
            from_status="pending", to_status="expired",
            reason="提案超过确认期限",
        )
    return changed


def save_proposal(session: Session, action: PendingActionORM, now: int) -> PendingAction:
    expire_pending_with_audit(session, now, action.task_id)
    old_pending = action_repository.list_pending_for_task(session, action.task_id)
    changed = action_repository.replace_pending_for_task(session, action.task_id)
    if changed != len(old_pending):
        raise RuntimeError("提案替换状态与审计记录不一致")

    action_repository.insert_action(session, action)
    for old in old_pending:
        audit_service.record_event(
            session, "proposal_replaced", old.task_id, action_id=old.id,
            from_status="pending", to_status="replaced",
            details={"replaced_by_action_id": action.id},
        )
    audit_service.record_event(
        session, "proposal_created", action.task_id, action_id=action.id,
        to_status="pending",
        details={
            "action_type": action.action_type,
            "before": action.before_snapshot,
            "after": action.after_snapshot,
        },
    )
    return to_action_read(action)


# ========= 创建修改提案 =========

def propose_update(
    session: Session,
    task_id: int,
    data: TaskUpdate
) -> PendingAction:

    with nullcontext() if session.info.get("chat_tool_transaction") else session.begin():

        if not session.info.get("chat_tool_transaction") and session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

        task = task_repository.get_task_by_id(
            session, task_id,
            for_update=session.get_bind().dialect.name == "postgresql",
        )

        if task is None:
            raise HTTPException(404, "待办不存在")

        before = to_task_read(task)
        # 只在内存中构造完整新快照；任务要等用户确认后才会写入。
        after = build_updated_snapshot(before, data)

        if (
            before.model_dump(exclude={"version"})
            == after.model_dump(exclude={"version"})
        ):
            raise HTTPException(409, "修改前后没有变化")

        conflict = find_schedule_conflict(session, task_id, after)
        if conflict is not None:
            raise HTTPException(409, f"时间与任务 {conflict.id} 冲突")

        now = now_timestamp()
        action = PendingActionORM(
            task_id=task_id,
            action_type="update",
            status="pending",
            expected_version=before.version,
            before_snapshot=before.model_dump(mode="json"),
            after_snapshot=after.model_dump(mode="json"),
            created_at=now,
            expires_at=now + 600
        )

        result = save_proposal(session, action, now)

    return result


# ========= 创建删除提案 =========

def propose_delete(
    session: Session,
    task_id: int
) -> PendingAction:

    with nullcontext() if session.info.get("chat_tool_transaction") else session.begin():

        if not session.info.get("chat_tool_transaction") and session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

        task = task_repository.get_task_by_id(
            session, task_id,
            for_update=session.get_bind().dialect.name == "postgresql",
        )

        if task is None:
            raise HTTPException(404, "待办不存在")

        before = to_task_read(task)
        now = now_timestamp()
        action = PendingActionORM(
            task_id=task_id,
            action_type="delete",
            status="pending",
            expected_version=before.version,
            before_snapshot=before.model_dump(mode="json"),
            after_snapshot=None,
            created_at=now,
            expires_at=now + 600
        )

        result = save_proposal(session, action, now)

    return result


def expire_due_actions(session: Session) -> int:
    """Mark unattended proposals expired; retain their snapshots as history."""
    with session.begin():
        if session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        return expire_pending_with_audit(session, now_timestamp())


# ========= 查询提案 =========

def get_action(
    session: Session,
    action_id: int
) -> PendingAction:

    action = action_repository.get_action_by_id(
        session, action_id
    )

    if action is None:
        raise HTTPException(404, "操作提案不存在")

    return to_action_read(action)


# ========= 确认执行 =========

def confirm_action(
    session: Session,
    action_id: int
) -> PendingAction:

    error = None
    error_status = 409

    with session.begin():
        # 与创建任务共用 SQLite 写锁，确保“检查冲突 → 更新”不可交错。
        if session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

        action = action_repository.get_action_by_id(
            session, action_id
        )

        if action is None:
            raise HTTPException(404, "操作提案不存在")

        if action.status != "pending":
            raise HTTPException(
                409,
                unavailable_status_message(action.status, "确认")
            )

        now = now_timestamp()

        # 1. 过期判断
        if action.expires_at <= now:
            if not action_repository.transition_status(
                session, action_id,
                "pending", "expired"
            ):
                raise RuntimeError("无法记录提案过期状态")
            audit_service.record_event(
                session, "proposal_expired", action.task_id, action_id=action_id,
                from_status="pending", to_status="expired",
                reason="提案超过确认期限",
            )
            error = "操作提案已过期"

        # 2. 尝试原子占用提案
        elif not action_repository.transition_status(
            session,
            action_id,
            "pending",
            "executed",
            not_expired_after=now
        ):
            session.refresh(action)
            error = unavailable_status_message(action.status, "确认")

        else:
            # 3. 提案状态占用成功，执行任务操作
            try:
                # 只把任务执行放入保存点；失败时外层事务仍持有提案写锁。
                with session.begin_nested():
                    if action.action_type == "update":
                        if action.after_snapshot is None:
                            raise ActionExecutionError("修改提案缺少新数据")
                        try:
                            after = TaskRead.model_validate(action.after_snapshot)
                        except ValidationError as exc:
                            raise ActionExecutionError("提案快照数据不合法") from exc
                        if (
                            after.id != action.task_id
                            or after.version != action.expected_version + 1
                        ):
                            raise ActionExecutionError("提案快照数据不合法")

                        if after.start_at is None:
                            valid_interval = after.end_at is None and after.duration_minutes is None
                        elif after.end_at is None:
                            valid_interval = after.duration_minutes is None
                        else:
                            try:
                                valid_interval = (
                                    after.end_at is not None
                                    and after.duration_minutes is not None
                                    and after.duration_minutes > 0
                                    and after.start_at + timedelta(minutes=after.duration_minutes) == after.end_at
                                )
                            except OverflowError as exc:
                                raise ActionExecutionError("提案时间快照不合法") from exc
                        if not valid_interval or (
                            after.start_at is None and after.due_at is None
                            and (
                                action.before_snapshot.get("start_at") is not None
                                or action.before_snapshot.get("due_at") is not None
                            )
                        ):
                            raise ActionExecutionError("提案时间快照不合法")

                        conflict = find_schedule_conflict(session, action.task_id, after)
                        if conflict is not None:
                            if not action_repository.transition_status(
                                session, action_id, "executed", "conflicted"
                            ):
                                raise RuntimeError("无法记录提案冲突状态")
                            error = f"时间与任务 {conflict.id} 冲突，请重新创建提案"
                            audit_service.record_event(
                                session, "proposal_conflicted", action.task_id,
                                action_id=action_id, from_status="pending",
                                to_status="conflicted", reason=error,
                            )
                            affected = None
                        else:
                            affected = task_repository.update_if_version(
                                session, action.task_id, action.expected_version, after
                            )
                    elif action.action_type == "delete":
                        affected = task_repository.delete_if_version(
                            session, action.task_id, action.expected_version
                        )
                    else:
                        raise ActionExecutionError("不支持的操作类型")

                    # 原任务已变化，属于业务冲突，不是执行失败。
                    if affected is not None and affected != 1:
                        if not action_repository.transition_status(
                            session, action_id, "executed", "conflicted"
                        ):
                            raise RuntimeError("无法记录提案冲突状态")
                        error = "任务版本已变化，请重新创建提案"
                        audit_service.record_event(
                            session, "proposal_conflicted", action.task_id,
                            action_id=action_id, from_status="pending",
                            to_status="conflicted", reason=error,
                        )
            except IntegrityError as exc:
                if getattr(exc.orig, "sqlstate", None) != "23P01":
                    raise
                if not action_repository.transition_status(
                    session, action_id, "executed", "conflicted"
                ):
                    raise RuntimeError("无法记录提案冲突状态") from exc
                error = "时间与其他任务冲突，请重新创建提案"
                audit_service.record_event(
                    session, "proposal_conflicted", action.task_id,
                    action_id=action_id, from_status="pending",
                    to_status="conflicted", reason=error,
                )
            except ActionExecutionError as exc:
                # 保存点已经回滚了任务写入，外层事务继续提交 failed。
                if not action_repository.transition_status(
                    session, action_id, "executed", "failed"
                ):
                    raise RuntimeError("无法记录提案执行失败状态") from exc
                session.refresh(action)
                error = str(exc)
                error_status = 500
                audit_service.record_event(
                    session, "proposal_failed", action.task_id, action_id=action_id,
                    from_status="pending", to_status="failed", reason=error,
                )
            if error is None:
                audit_service.record_event(
                    session, "proposal_executed", action.task_id, action_id=action_id,
                    from_status="pending", to_status="executed",
                    details={
                        "action_type": action.action_type,
                        "before": action.before_snapshot,
                        "after": action.after_snapshot,
                    },
                )

        # 读取数据库最新状态
        if error_status != 500:
            session.refresh(action)
            result = to_action_read(action)

    # 到这里事务已经提交
    # 在事务外抛出业务异常，避免回滚状态变化
    if error is not None:
        raise HTTPException(error_status, error)

    return result


# ========= 取消提案 =========

def cancel_action(
    session: Session,
    action_id: int
) -> PendingAction:

    error = None

    with session.begin():
        # 在读取状态前取得写锁，避免确认和取消同时读到 pending。
        if session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

        action = action_repository.get_action_by_id(
            session, action_id
        )

        if action is None:
            raise HTTPException(404, "操作提案不存在")

        if action.status != "pending":
            raise HTTPException(
                409,
                unavailable_status_message(action.status, "取消")
            )

        now = now_timestamp()

        if action.expires_at <= now:
            if not action_repository.transition_status(
                session, action_id,
                "pending", "expired"
            ):
                raise RuntimeError("无法记录提案过期状态")
            audit_service.record_event(
                session, "proposal_expired", action.task_id, action_id=action_id,
                from_status="pending", to_status="expired",
                reason="提案超过确认期限",
            )
            error = "操作提案已过期"

        elif not action_repository.transition_status(
            session,
            action_id,
            "pending",
            "cancelled",
            not_expired_after=now
        ):
            session.refresh(action)
            error = unavailable_status_message(action.status, "取消")
        else:
            audit_service.record_event(
                session, "proposal_cancelled", action.task_id, action_id=action_id,
                from_status="pending", to_status="cancelled",
            )

        session.refresh(action)
        result = to_action_read(action)

    if error is not None:
        raise HTTPException(409, error)

    return result
