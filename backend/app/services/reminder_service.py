"""任务提醒：随任务变化维护待触发的提醒（sync_task_reminder），后台轮询触发（fire_due_reminders）
并发送提醒邮件（send_pending_emails）。规则见 docs/prd/prd-reminder.md。

数据库是提醒状态的唯一来源，进程内存里不保存任何提醒：服务重启后，下一轮轮询自然会扫到漏掉的提醒。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.config.settings import USER_TIMEZONE
from backend.app.models.notification import NotificationORM
from backend.app.models.reminder import TaskReminderORM
from backend.app.models.task import TaskORM
from backend.app.models.user import UserORM
from backend.app.schemas.task import TaskRead
from backend.app.services import mailer

# 到点后超过这个时长才处理，只生成"已错过"通知，不发邮件（R8）；邮件超过这个时长仍未发出则放弃（R10）。
GRACE = timedelta(hours=1)
FIRE_BATCH = 100
EMAIL_BATCH = 20
MAX_EMAIL_ATTEMPTS = 3
# 第 1、2 次失败后分别等待多久重试。
EMAIL_BACKOFF = (timedelta(minutes=1), timedelta(minutes=5))
# 标记"发送中"后的租约：到期仍未记录结果（进程在发送途中崩溃），视为结果未知并重发。
EMAIL_LEASE = timedelta(minutes=5)


def _base_time(task: TaskRead) -> datetime | None:
    """提醒的基准时间：有开始时间用开始时间，否则用截止时间（R3）。"""
    return task.start_at or task.due_at


def _schedule_key(task: TaskRead) -> tuple:
    return _base_time(task), task.remind_before_minutes, task.status


def sync_task_reminder(session: Session, task: TaskRead, *, previous: TaskRead | None, user_id: int) -> None:
    """任务创建或修改后调用，在调用方的事务里作废旧的待触发提醒并按新状态重建（R6、R7）。

    previous 是修改前的快照（创建时为 None）。基准时间、提前分钟数和状态都没变时什么也不做：
    已经触发过的提醒不会因为改了标题而再提醒一次。任务删除时提醒由外键级联删除，不需要调用。
    """
    if previous is not None and _schedule_key(previous) == _schedule_key(task):
        return
    now = now_utc()
    session.execute(
        update(TaskReminderORM)
        .where(TaskReminderORM.task_id == task.id, TaskReminderORM.status == "pending")
        .values(status="cancelled", finished_at=now)
    )
    base = _base_time(task)
    if task.status != "todo" or task.remind_before_minutes is None or base is None or base <= now:
        return
    # 提醒时间已过但任务还没到：记为现在，下一轮立即提醒，而不是被当成"已错过"。
    remind_at = max(base - timedelta(minutes=task.remind_before_minutes), now)
    session.add(TaskReminderORM(user_id=user_id, task_id=task.id, remind_at=remind_at, created_at=now))
    session.flush()


def _email_allowed(user: UserORM) -> bool:
    """R9：邮箱已验证且开启了邮件提醒。"""
    return user.email is not None and user.email_verified_at is not None and user.email_reminders


def _db_time(value: datetime | None) -> datetime | None:
    # tasks 表的时间按 UTC 无时区值存储。
    return value.replace(tzinfo=timezone.utc) if value is not None else None


def fire_due_reminders(session: Session) -> int:
    """触发到点的提醒，返回生成的通知数。生成通知与标记提醒在同一事务里，不会重复生成。"""
    now = now_utc()
    with session.begin():
        rows = session.execute(
            select(TaskReminderORM, TaskORM, UserORM)
            .join(TaskORM, TaskORM.id == TaskReminderORM.task_id)
            .join(UserORM, UserORM.id == TaskReminderORM.user_id)
            .where(TaskReminderORM.status == "pending", TaskReminderORM.remind_at <= now)
            .order_by(TaskReminderORM.remind_at)
            .limit(FIRE_BATCH)
            # 只锁提醒行；SKIP LOCKED 跳过另一轮轮询正在处理的行，而不是等它。
            .with_for_update(of=TaskReminderORM, skip_locked=True)
        ).all()
        for reminder, task, user in rows:
            missed = now - reminder.remind_at > GRACE
            email = not missed and _email_allowed(user)
            session.add(NotificationORM(
                user_id=reminder.user_id, kind="missed" if missed else "reminder",
                task_id=task.id, reminder_id=reminder.id,
                task_title=task.title, task_time=_db_time(task.start_at or task.due_at), created_at=now,
                email_status="pending" if email else "not_needed", email_next_attempt_at=now if email else None,
            ))
            reminder.status = "missed" if missed else "fired"
            reminder.finished_at = now
    return len(rows)


def _email_content(notification: NotificationORM) -> tuple[str, str]:
    when = notification.task_time.astimezone(USER_TIMEZONE).strftime("%Y-%m-%d %H:%M")
    subject = f"【小记】提醒：{notification.task_title}"
    body = "\n".join([
        f"任务：{notification.task_title}",
        f"时间：{when}（UTC+08:00）",
        "",
        "可以在小记里查看任务，或在个人设置中关闭邮件提醒。",
    ])
    return subject, body


def send_pending_emails(session: Session) -> int:
    """发送到期的提醒邮件，返回尝试发送的封数。

    分三步：小事务里把要发的行标为"发送中"并提交 → 事务外发信 → 小事务记录结果。
    SMTP 可能耗时 10 秒，不能拿着行锁等待。代价是"至少一次"：发出后来不及记录就崩溃，租约到期后会重发（R10）。
    """
    now = now_utc()
    jobs: list[tuple[int, int, str, str, str]] = []
    with session.begin():
        rows = session.scalars(
            select(NotificationORM)
            .where(NotificationORM.email_status.in_(("pending", "sending")),
                   NotificationORM.email_next_attempt_at <= now)
            .order_by(NotificationORM.id)
            .limit(EMAIL_BATCH)
            .with_for_update(skip_locked=True)
        ).all()
        for notification in rows:
            if now - notification.created_at > GRACE or notification.email_attempts >= MAX_EMAIL_ATTEMPTS:
                notification.email_status = "failed"
                notification.email_error = notification.email_error or "超过重试期限仍未发出"
                continue
            # 触发和发送之间用户可能关了开关、换了邮箱，发送前再检查一次（R9）。
            user = session.get(UserORM, notification.user_id)
            if not _email_allowed(user):
                notification.email_status = "skipped"
                continue
            notification.email_status = "sending"
            notification.email_attempts += 1
            notification.email_next_attempt_at = now + EMAIL_LEASE
            jobs.append((notification.id, notification.email_attempts, user.email, *_email_content(notification)))

    for notification_id, attempt, to, subject, body in jobs:
        error = None
        try:
            mailer.send_mail(to=to, subject=subject, body=body)
        except mailer.MailNotConfigured:
            error = "邮件服务未配置"
        except mailer.MailError as exc:
            error = str(exc) or "邮件发送失败"
        _record_email_result(session, notification_id, attempt, error)
    return len(jobs)


def _record_email_result(session: Session, notification_id: int, attempt: int, error: str | None) -> None:
    now = now_utc()
    with session.begin():
        notification = session.get(NotificationORM, notification_id, with_for_update=True)
        # 租约到期后可能已被下一轮接手重发：结果以接手的那一轮为准。
        if notification is None or notification.email_status != "sending" or notification.email_attempts != attempt:
            return
        if error is None:
            notification.email_status = "sent"
            notification.email_sent_at = now
            notification.email_next_attempt_at = None
        elif attempt >= MAX_EMAIL_ATTEMPTS:
            notification.email_status = "failed"
            notification.email_error = error[:500]
            notification.email_next_attempt_at = None
        else:
            notification.email_status = "pending"
            notification.email_error = error[:500]
            notification.email_next_attempt_at = now + EMAIL_BACKOFF[attempt - 1]
