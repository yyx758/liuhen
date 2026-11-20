
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.models.action import PendingActionORM
from backend.app.schemas.action import ActionListItem, PendingAction
from backend.app.schemas.task import TaskRead, TaskUpdate
from backend.app.repositories import (
    action_repository,
    task_repository
)
from backend.app.services.task_service import to_db_utc, to_task_read
from backend.app.services import audit_service


class ActionExecutionError(Exception):
    """A deterministic execution failure that must close the proposal."""


class ActionConflictError(Exception):
    """A sibling proposal changed a field this proposal depends on."""


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


def rebase_batch_update(before: TaskRead, proposed: TaskRead, current: TaskRead) -> TaskRead:
    """Apply one independent change after another proposal from the same request."""
    fields = ("title", "priority", "start_at", "duration_minutes", "due_at", "status")
    changes = {
        field: getattr(proposed, field)
        for field in fields if getattr(before, field) != getattr(proposed, field)
    }
    if not changes:
        raise ActionConflictError("提案没有可独立应用的修改")
    if any(getattr(current, field) != getattr(before, field) for field in changes):
        raise ActionConflictError("同一字段已变化，请重新创建提案")
    if {"start_at", "duration_minutes"} & changes.keys() and any(
        getattr(current, field) != getattr(before, field)
        for field in ("start_at", "end_at", "duration_minutes")
    ):
        raise ActionConflictError("任务排期已变化，请重新创建提案")
    try:
        return build_updated_snapshot(current, TaskUpdate.model_validate(changes))
    except (HTTPException, ValidationError) as exc:
        raise ActionConflictError("任务已变化，无法安全合并提案") from exc


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
    old_pending = [
        old for old in action_repository.list_pending_for_task(session, action.task_id)
        if action.batch_id is None or old.batch_id != action.batch_id
    ]
    for old in old_pending:
        if not action_repository.transition_status(session, old.id, "pending", "replaced"):
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
            batch_id=session.info.get("proposal_batch_id") or session.info.get("audit_request_id"),
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
            batch_id=session.info.get("proposal_batch_id") or session.info.get("audit_request_id"),
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


def purge_expired_actions(session: Session) -> int:
    """Delete elapsed proposals; preserve expiry and deletion audit rows."""
    with session.begin():
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        now = now_timestamp()
        due = action_repository.list_expired_actions(
            session, now, for_update=dialect == "postgresql"
        )
        for action in due:
            if not action_repository.delete_expired_action(session, action.id, now):
                raise RuntimeError("过期提案清理时状态发生变化")
            if action.status == "pending":
                audit_service.record_event(
                    session, "proposal_expired", action.task_id, action_id=action.id,
                    from_status="pending", to_status="expired",
                    reason="提案超过确认期限",
                )
            audit_service.record_event(
                session, "proposal_deleted", action.task_id, action_id=action.id,
                from_status="expired", reason="系统清理过期提案",
            )
        return len(due)


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


def list_active_pending(session: Session) -> list[ActionListItem]:
    return [ActionListItem.model_validate(action.model_dump()) for action in (
        to_action_read(row) for row in action_repository.list_active_pending(session, now_timestamp())
    )]


def list_for_conversation(session: Session, conversation_id: str) -> list[ActionListItem]:
    return [ActionListItem.model_validate({
        **to_action_read(row).model_dump(), "request_id": request_id,
    }) for row, request_id in action_repository.list_for_conversation(session, conversation_id)]


def delete_expired_action(session: Session, action_id: int) -> None:
    """Remove an elapsed proposal while retaining its append-only audit trail."""
    with session.begin():
        if session.get_bind().dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")

        action = action_repository.get_action_by_id(
            session, action_id,
            for_update=session.get_bind().dialect.name == "postgresql",
        )
        if action is None:
            raise HTTPException(404, "操作提案不存在")

        now = now_timestamp()
        if action.expires_at > now or action.status not in ("pending", "expired"):
            raise HTTPException(409, "只能删除已过期的待确认提案")

        if not action_repository.delete_expired_action(session, action_id, now):
            raise HTTPException(409, "提案状态已变化，请刷新后重试")

        if action.status == "pending":
            audit_service.record_event(
                session, "proposal_expired", action.task_id, action_id=action_id,
                from_status="pending", to_status="expired",
                reason="提案超过确认期限",
            )
        audit_service.record_event(
            session, "proposal_deleted", action.task_id, action_id=action_id,
            from_status="expired", reason="用户删除过期提案",
        )


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

                        applied_before = None
                        expected_version = action.expected_version
                        if action.batch_id is not None:
                            current_task = task_repository.get_task_by_id(
                                session, action.task_id,
                                for_update=session.get_bind().dialect.name == "postgresql",
                            )
                            if current_task is not None:
                                current = to_task_read(current_task)
                                if current.version != expected_version:
                                    original = TaskRead.model_validate(action.before_snapshot)
                                    after = rebase_batch_update(original, after, current)
                                    applied_before = current
                                    expected_version = current.version

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
                                session, action.task_id, expected_version, after
                            )
                            if affected == 1 and applied_before is not None:
                                action.before_snapshot = applied_before.model_dump(mode="json")
                                action.after_snapshot = after.model_dump(mode="json")
                                action.expected_version = expected_version
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
            except ActionConflictError as exc:
                if not action_repository.transition_status(
                    session, action_id, "executed", "conflicted"
                ):
                    raise RuntimeError("无法记录提案冲突状态") from exc
                error = str(exc)
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
