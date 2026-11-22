"""任务提醒（docs/prd/prd-reminder.md）：提醒时间计算、随任务变化作废与重建、轮询触发、邮件发送与重试、通知接口。

全程用固定时钟：业务"现在"是 T0 = 2030-01-01 09:00（UTC+08:00）。轮询函数在测试线程里直接调用，
所以 fixed_now 对它们生效；不需要真的等待。
"""

import threading
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from backend.app.api import auth as auth_api
from backend.app.config.clock import fixed_now
from backend.app.config.settings import USER_TIMEZONE
from backend.app.db.isolated import isolated_engine
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.models.notification import NotificationORM
from backend.app.models.reminder import TaskReminderORM
from backend.app.models.user import UserORM
from backend.app.schemas.task import TaskCreate, TaskUpdate
from backend.app.services import action_service, mailer, reminder_service, task_service
from backend.app.services.auth_service import hash_password
from backend.app.services.rate_limit import login_failure_lock, login_ip_limiter
from backend.tests.mail_helpers import install_outbox

ORIGIN = "http://127.0.0.1:5173"
PASSWORD = "correct-horse-1"


def at(hour: int, minute: int = 0, second: int = 0, day: int = 1) -> datetime:
    """2030-01-<day> 的本地时间（UTC+08:00）。"""
    return datetime(2030, 1, day, hour, minute, second, tzinfo=USER_TIMEZONE)


T0 = at(9)


class ReminderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.outbox = install_outbox(self)
        self.user_id = self.add_user("alice", email="alice@example.com", verified=True)

    def tearDown(self) -> None:
        self.database.__exit__(None, None, None)

    # ---------- helpers ----------
    def add_user(self, username: str, *, email: str | None = None, verified: bool = False,
                 email_reminders: bool = True) -> int:
        with Session(self.engine) as session, session.begin():
            user = UserORM(
                username=username, email=email, password_hash=hash_password(PASSWORD), status="active",
                email_verified_at=T0 if verified else None, email_reminders=email_reminders,
            )
            session.add(user)
            session.flush()
            return user.id

    def set_user(self, user_id: int, **values) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(update(UserORM).where(UserORM.id == user_id).values(**values))

    def create(self, title: str = "写周报", *, start: datetime | None = None, due: datetime | None = None,
               remind: int | None = None, user_id: int | None = None, now: datetime = T0):
        with Session(self.engine) as session, fixed_now(now):
            return task_service.create_task(
                TaskCreate(title=title, start_at=start, due_at=due, remind_before_minutes=remind),
                session, user_id=user_id or self.user_id,
            )

    def update(self, task, now: datetime = T0, **changes):
        with Session(self.engine) as session, fixed_now(now):
            return task_service.update_task(
                session, task.id, TaskUpdate(**changes), task.version, user_id=self.user_id,
            )

    def reminders(self, task_id: int) -> list[TaskReminderORM]:
        with Session(self.engine) as session:
            return list(session.scalars(
                select(TaskReminderORM).where(TaskReminderORM.task_id == task_id).order_by(TaskReminderORM.id)
            ))

    def pending(self, task_id: int) -> list[datetime]:
        return [r.remind_at for r in self.reminders(task_id) if r.status == "pending"]

    def tick(self, now: datetime) -> int:
        with Session(self.engine) as session, fixed_now(now):
            return reminder_service.fire_due_reminders(session)

    def send(self, now: datetime) -> None:
        with Session(self.engine) as session, fixed_now(now):
            reminder_service.send_pending_emails(session)

    def notifications(self, user_id: int | None = None) -> list[NotificationORM]:
        with Session(self.engine) as session:
            return list(session.scalars(
                select(NotificationORM).where(NotificationORM.user_id == (user_id or self.user_id))
                .order_by(NotificationORM.id)
            ))


class ReminderScheduleTests(ReminderTestBase):
    """提醒时间的计算（R3、R6）与随任务变化的作废、重建（R7）。"""

    def test_remind_time_uses_start_then_due(self) -> None:
        by_start = self.create("开会", start=at(10), remind=15)
        by_due = self.create("交报告", due=at(12), remind=60)
        both = self.create("写代码", start=at(10), due=at(18), remind=0)
        silent = self.create("不提醒", start=at(10))

        self.assertEqual(by_start.remind_before_minutes, 15)
        self.assertEqual(self.pending(by_start.id), [at(9, 45)])
        self.assertEqual(self.pending(by_due.id), [at(11)])
        self.assertEqual(self.pending(both.id), [at(10)])
        self.assertIsNone(silent.remind_before_minutes)
        self.assertEqual(self.reminders(silent.id), [])

    def test_passed_remind_time_fires_now_but_passed_task_time_creates_nothing(self) -> None:
        # 10 点开始、提前 1 天：提醒时间已过但任务还没开始，记为"现在"，下一轮立即提醒，而不是算作错过。
        soon = self.create("马上开始", start=at(10), remind=1440)
        self.assertEqual(self.pending(soon.id), [T0])
        # 任务时间本身已过：不生成提醒。
        passed = self.create("已经开始", start=at(8), remind=0)
        self.assertEqual(self.reminders(passed.id), [])

        self.assertEqual(self.tick(T0), 1)
        [notification] = self.notifications()
        self.assertEqual(notification.kind, "reminder")

    def test_remind_range_is_validated(self) -> None:
        for value in (-1, 10081):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TaskCreate(title="x", start_at=at(10), remind_before_minutes=value)
        TaskCreate(title="x", start_at=at(10), remind_before_minutes=10080)

    def test_reschedule_cancels_and_recreates(self) -> None:
        task = self.create(start=at(10), remind=15)

        task = self.update(task, start_at=at(14))
        self.assertEqual(self.pending(task.id), [at(13, 45)])
        self.assertEqual(self.reminders(task.id)[0].status, "cancelled")

        task = self.update(task, remind_before_minutes=30)
        self.assertEqual(self.pending(task.id), [at(13, 30)])

        # 只改标题：待触发的提醒原样保留，不作废重建。
        before_ids = [r.id for r in self.reminders(task.id)]
        task = self.update(task, title="写月报")
        self.assertEqual([r.id for r in self.reminders(task.id)], before_ids)

        task = self.update(task, remind_before_minutes=None)
        self.assertIsNone(task.remind_before_minutes)
        self.assertEqual(self.pending(task.id), [])
        self.assertEqual({r.status for r in self.reminders(task.id)}, {"cancelled"})

    def test_done_cancels_and_reopen_recreates(self) -> None:
        task = self.create(start=at(10), remind=15)
        task = self.update(task, status="done")
        self.assertEqual(self.pending(task.id), [])
        task = self.update(task, status="todo")
        self.assertEqual(self.pending(task.id), [at(9, 45)])

    def test_delete_removes_reminders(self) -> None:
        task = self.create(start=at(10), remind=15)
        with Session(self.engine) as session, fixed_now(T0):
            task_service.delete_task(session, task.id, task.version, user_id=self.user_id)
        self.assertEqual(self.reminders(task.id), [])
        self.assertEqual(self.tick(at(9, 45)), 0)

    def test_confirmed_proposal_reschedules(self) -> None:
        task = self.create(start=at(10), remind=15)
        with Session(self.engine) as session, fixed_now(T0):
            action = action_service.propose_update(
                session, task.id, TaskUpdate(start_at=at(15)), user_id=self.user_id,
            )
        # 提案只是建议，确认前提醒不变。
        self.assertEqual(self.pending(task.id), [at(9, 45)])
        with Session(self.engine) as session, fixed_now(T0):
            action_service.confirm_action(session, action.id, user_id=self.user_id)
        self.assertEqual(self.pending(task.id), [at(14, 45)])

        with Session(self.engine) as session, fixed_now(T0):
            action = action_service.propose_update(
                session, task.id, TaskUpdate(remind_before_minutes=60), user_id=self.user_id,
            )
            action_service.confirm_action(session, action.id, user_id=self.user_id)
        self.assertEqual(self.pending(task.id), [at(14)])

    def test_unrelated_edit_after_firing_does_not_remind_again(self) -> None:
        task = self.create(start=at(10), remind=15)
        self.tick(at(9, 45))
        task = self.update(task, now=at(9, 50), title="改个名字")
        self.assertEqual(self.pending(task.id), [])
        # 改到更晚的时间，才会重新安排提醒。
        task = self.update(task, now=at(9, 50), start_at=at(16))
        self.assertEqual(self.pending(task.id), [at(15, 45)])


class ReminderFiringTests(ReminderTestBase):
    """轮询触发（5.3、R8）。"""

    def test_fires_once_with_snapshot(self) -> None:
        task = self.create("开周会", start=at(10), remind=15)
        self.assertEqual(self.tick(at(9, 44, 59)), 0)
        self.assertEqual(self.tick(at(9, 45)), 1)
        self.assertEqual(self.tick(at(9, 46)), 0)

        [notification] = self.notifications()
        self.assertEqual(
            (notification.kind, notification.task_id, notification.task_title, notification.task_time),
            ("reminder", task.id, "开周会", at(10)),
        )
        self.assertIsNone(notification.read_at)
        [reminder] = self.reminders(task.id)
        self.assertEqual(reminder.status, "fired")
        self.assertEqual(notification.reminder_id, reminder.id)

    def test_more_than_one_hour_late_is_missed_without_email(self) -> None:
        task = self.create(start=at(10), remind=15)
        self.tick(at(10, 45, 1))
        [notification] = self.notifications()
        self.assertEqual((notification.kind, notification.email_status), ("missed", "not_needed"))
        self.assertEqual(self.reminders(task.id)[0].status, "missed")

    def test_exactly_one_hour_late_still_reminds(self) -> None:
        self.create(start=at(10), remind=15)
        self.tick(at(10, 45))
        self.assertEqual(self.notifications()[0].kind, "reminder")

    def test_locked_reminder_is_skipped_by_concurrent_tick(self) -> None:
        task = self.create(start=at(10), remind=15)
        # 另一个连接锁住这条提醒，模拟另一轮轮询正在处理它。
        with self.engine.connect() as other:
            transaction = other.begin()
            other.execute(text("SELECT id FROM task_reminders WHERE task_id = :id FOR UPDATE"), {"id": task.id})

            result: list[int] = []
            worker = threading.Thread(target=lambda: result.append(self.tick(at(9, 45))))
            worker.start()
            worker.join(timeout=10)
            # SKIP LOCKED：不等待被锁的行，直接跳过。
            self.assertFalse(worker.is_alive(), "轮询被行锁阻塞，应使用 SKIP LOCKED")
            self.assertEqual(result, [0])
            transaction.rollback()

        self.assertEqual(self.tick(at(9, 45)), 1)
        self.assertEqual(len(self.notifications()), 1)

    def test_snapshot_survives_rename_and_delete(self) -> None:
        task = self.create("原标题", start=at(10), remind=15)
        self.tick(at(9, 45))
        task = self.update(task, now=at(9, 50), title="新标题")
        with Session(self.engine) as session, fixed_now(at(9, 50)):
            task_service.delete_task(session, task.id, task.version, user_id=self.user_id)
        [notification] = self.notifications()
        self.assertEqual((notification.task_id, notification.task_title), (task.id, "原标题"))


class ReminderEmailTests(ReminderTestBase):
    """邮件发送（5.4、R9、R10）。"""

    def email_state(self) -> tuple:
        [n] = self.notifications()
        return n.email_status, n.email_attempts

    def test_email_sent_to_verified_enabled_user(self) -> None:
        self.create("写周报", start=at(10), remind=15)
        self.tick(at(9, 45))
        self.assertEqual(self.email_state(), ("pending", 0))
        self.send(at(9, 45))

        [mail] = self.outbox.sent
        self.assertEqual(mail["to"], "alice@example.com")
        self.assertEqual(mail["subject"], "【小记】提醒：写周报")
        self.assertIn("2030-01-01 10:00", mail["body"])
        [n] = self.notifications()
        self.assertEqual((n.email_status, n.email_attempts, n.email_sent_at), ("sent", 1, at(9, 45)))

        self.send(at(9, 50))
        self.assertEqual(len(self.outbox.sent), 1)

    def test_no_email_when_unverified_or_disabled(self) -> None:
        unverified = self.add_user("bob", email="bob@example.com")
        disabled = self.add_user("carol", email="carol@example.com", verified=True, email_reminders=False)
        for user_id in (unverified, disabled):
            self.create(start=at(10), remind=15, user_id=user_id)
        self.tick(at(9, 45))
        for user_id in (unverified, disabled):
            [n] = self.notifications(user_id)
            self.assertEqual(n.email_status, "not_needed")
        self.send(at(9, 45))
        self.assertEqual([m["to"] for m in self.outbox.sent], [])

    def test_conditions_rechecked_before_sending(self) -> None:
        self.create(start=at(10), remind=15)
        self.tick(at(9, 45))
        self.set_user(self.user_id, email_reminders=False)
        self.send(at(9, 45))
        self.assertEqual(self.outbox.sent, [])
        self.assertEqual(self.email_state()[0], "skipped")

    def test_failures_retry_with_backoff_then_give_up(self) -> None:
        self.create(start=at(10), remind=15)
        self.tick(at(9, 45))
        with patch.object(mailer, "send_mail", side_effect=mailer.MailError("connection refused")) as failing:
            self.send(at(9, 45))
            self.assertEqual(self.email_state(), ("pending", 1))
            self.send(at(9, 45, 59))  # 还没到 1 分钟后的重试时间
            self.assertEqual(failing.call_count, 1)
            self.send(at(9, 46))
            self.assertEqual(self.email_state(), ("pending", 2))
            self.send(at(9, 50, 59))  # 第二次失败后等 5 分钟
            self.assertEqual(failing.call_count, 2)
            self.send(at(9, 51))
            self.assertEqual(failing.call_count, 3)
        [n] = self.notifications()
        self.assertEqual((n.email_status, n.email_attempts), ("failed", 3))
        self.assertIn("connection refused", n.email_error)

    def test_gives_up_one_hour_after_firing(self) -> None:
        self.create(start=at(10), remind=15)
        self.tick(at(9, 45))
        with patch.object(mailer, "send_mail", side_effect=mailer.MailError("timeout")) as failing:
            self.send(at(9, 45))
            self.send(at(10, 45, 1))
        self.assertEqual(failing.call_count, 1)
        self.assertEqual(self.email_state(), ("failed", 1))

    def test_stuck_sending_is_retried_after_lease(self) -> None:
        # 模拟进程在发送途中崩溃：状态停在 sending，租约到 9:50。
        self.create(start=at(10), remind=15)
        self.tick(at(9, 45))
        with Session(self.engine) as session, session.begin():
            session.execute(update(NotificationORM).values(
                email_status="sending", email_attempts=1, email_next_attempt_at=at(9, 50),
            ))
        self.send(at(9, 49))
        self.assertEqual(self.outbox.sent, [])
        self.send(at(9, 50))
        self.assertEqual(len(self.outbox.sent), 1)
        self.assertEqual(self.email_state(), ("sent", 2))


class NotificationApiTests(ReminderTestBase):
    """通知接口与邮件提醒开关（5.5、5.6、第 6 节）。"""

    def setUp(self) -> None:
        super().setUp()
        login_ip_limiter.reset()
        login_failure_lock.reset()

        def test_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_db] = test_db
        self.addCleanup(app.dependency_overrides.clear)
        patcher = patch.object(auth_api, "COOKIE_SECURE", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client_for(self, username: str) -> TestClient:
        client = TestClient(app, headers={"Origin": ORIGIN})
        self.addCleanup(client.close)
        response = client.post("/auth/login", json={"account": username, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def fire_three(self) -> list[int]:
        for minute in (10, 20, 30):
            self.create(f"任务{minute}", start=at(10, minute), remind=0)
        self.tick(at(10, 30))
        return [n.id for n in self.notifications()]

    def test_list_unread_count_and_read(self) -> None:
        first, second, third = self.fire_three()
        alice = self.client_for("alice")

        items = alice.get("/notifications").json()
        self.assertEqual([n["id"] for n in items], [third, second, first])
        self.assertEqual(
            set(items[0]), {"id", "kind", "task_id", "task_title", "task_time", "created_at", "read_at"},
        )
        self.assertEqual(items[0]["task_title"], "任务30")

        page = alice.get("/notifications", params={"limit": 2}).json()
        self.assertEqual([n["id"] for n in page], [third, second])
        rest = alice.get("/notifications", params={"limit": 2, "before_id": second}).json()
        self.assertEqual([n["id"] for n in rest], [first])

        self.assertEqual(alice.get("/notifications/unread-count").json(), {"count": 3})
        self.assertEqual(alice.post(f"/notifications/{first}/read").status_code, 204)
        self.assertEqual(alice.post(f"/notifications/{first}/read").status_code, 204)
        self.assertEqual(alice.get("/notifications/unread-count").json(), {"count": 2})
        self.assertIsNotNone(alice.get("/notifications").json()[-1]["read_at"])

        self.assertEqual(alice.post("/notifications/read-all").status_code, 204)
        self.assertEqual(alice.get("/notifications/unread-count").json(), {"count": 0})

    def test_notifications_are_isolated(self) -> None:
        first, *_ = self.fire_three()
        self.add_user("bob")
        bob = self.client_for("bob")
        self.assertEqual(bob.get("/notifications").json(), [])
        self.assertEqual(bob.get("/notifications/unread-count").json(), {"count": 0})
        self.assertEqual(bob.post(f"/notifications/{first}/read").status_code, 404)
        self.assertEqual(bob.post("/notifications/read-all").status_code, 204)
        with Session(self.engine) as session:
            unread = session.scalar(select(func.count()).select_from(NotificationORM).where(
                NotificationORM.read_at.is_(None)
            ))
        self.assertEqual(unread, 3)

    def test_email_reminders_preference(self) -> None:
        alice = self.client_for("alice")
        self.assertTrue(alice.get("/auth/me").json()["email_reminders"])
        response = alice.patch("/auth/me/preferences", json={"email_reminders": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["email_reminders"])
        self.assertFalse(alice.get("/auth/me").json()["email_reminders"])
        self.assertEqual(alice.patch("/auth/me/preferences", json={"email_reminders": True, "x": 1}).status_code, 422)

    def test_task_api_exposes_remind_before_minutes(self) -> None:
        alice = self.client_for("alice")
        created = alice.post(
            "/tasks", json={"title": "开会", "start_at": "2030-01-01T10:00:00+08:00", "remind_before_minutes": 15},
            headers={"Idempotency-Key": "8a9e5c1e-6b55-4c1b-9d55-111111111111"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["remind_before_minutes"], 15)
        [listed] = alice.get("/tasks").json()
        self.assertEqual(listed["remind_before_minutes"], 15)

        cleared = alice.patch(
            f"/tasks/{listed['id']}", json={"remind_before_minutes": None}, headers={"If-Match": "1"},
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIsNone(cleared.json()["remind_before_minutes"])
        self.assertEqual(self.pending(listed["id"]), [])


if __name__ == "__main__":
    unittest.main()
