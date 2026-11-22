"""P5：每日对话上限与用量统计（PRD 第 9 节、13.3）。"""

import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api import auth as auth_api
from backend.app.db.isolated import isolated_engine
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.models.account_event import AccountEventORM
from backend.app.models.conversation import ConversationORM
from backend.app.models.request import ChatTurnORM
from backend.app.models.user import UserORM
from backend.app.services import chat_turn_service, conversation_service, usage_service
from backend.app.services.auth_service import hash_password
from backend.app.services.rate_limit import login_failure_lock, login_ip_limiter

ORIGIN = "http://127.0.0.1:5173"
PASSWORD = "correct-horse-1"
LIMIT = 3


class DayBoundaryTests(unittest.TestCase):
    def test_days_split_at_midnight_utc_plus_8(self) -> None:
        def ts(text: str) -> int:
            return int(datetime.fromisoformat(text).timestamp())

        late = ts("2026-11-20T23:59:59+08:00")
        midnight = ts("2026-11-21T00:00:00+08:00")
        self.assertEqual(usage_service.day_index(late) + 1, usage_service.day_index(midnight))
        self.assertEqual(usage_service.day_start(late + 3600), midnight)
        self.assertEqual(datetime.fromtimestamp(midnight, timezone.utc).hour, 16)


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.factory = sessionmaker(bind=self.engine)
        login_ip_limiter.reset()
        login_failure_lock.reset()
        limit_patch = patch.object(usage_service, "DEFAULT_DAILY_CHAT_LIMIT", LIMIT)
        limit_patch.start()
        self.addCleanup(limit_patch.stop)

        def test_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_db] = test_db
        cookie_patch = patch.object(auth_api, "COOKIE_SECURE", False)
        cookie_patch.start()
        self.addCleanup(cookie_patch.stop)
        self.member = self.add_user("member")
        self.boss = self.add_user("boss", role="admin")

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.database.__exit__(None, None, None)

    def add_user(self, username: str, *, role: str = "user") -> int:
        with Session(self.engine) as session, session.begin():
            user = UserORM(username=username, password_hash=hash_password(PASSWORD), status="active", role=role)
            session.add(user)
            session.flush()
            return user.id

    def client_for(self, username: str) -> TestClient:
        client = TestClient(app, headers={"Origin": ORIGIN})
        self.addCleanup(client.close)
        self.assertEqual(client.post("/auth/login", json={"account": username, "password": PASSWORD}).status_code, 200)
        return client

    def start_turn(self, user_id: int, request_id: str | None = None, conversation_id: str | None = None):
        return chat_turn_service.acquire_turn(
            request_id or str(uuid4()), "你好", conversation_id, self.factory, user_id=user_id,
        )

    def finish_all(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(update(ChatTurnORM).values(
                status="completed", active_conversation_id=None, owner_token=None, lease_until=None, answer="好",
            ))

    def use_up(self, user_id: int, count: int = LIMIT) -> list[str]:
        request_ids = []
        for _ in range(count):
            request_ids.append(self.start_turn(user_id).request_id)
        self.finish_all()
        return request_ids

    def assert_limited(self, user_id: int) -> None:
        with self.assertRaises(HTTPException) as raised:
            self.start_turn(user_id)
        self.assertEqual((raised.exception.status_code, raised.exception.detail), (429, "今日对话次数已用完，明天再来"))

    def test_limit_reached_but_retry_is_free(self) -> None:
        first, *_ = self.use_up(self.member)
        self.assert_limited(self.member)
        # 用同一个 request_id 重试不算新的一轮。
        self.assertEqual(self.start_turn(self.member, request_id=first).answer, "好")
        with Session(self.engine) as session:
            self.assertEqual(usage_service.today_count(session, user_id=self.member, now=usage_service.current_timestamp()), LIMIT)

    def test_admin_is_unlimited_but_counted(self) -> None:
        self.use_up(self.boss, LIMIT + 2)
        me = self.client_for("boss").get("/auth/me").json()
        self.assertEqual(me["chat_usage"], {"today": LIMIT + 2, "limit": None})

    def test_yesterday_does_not_count(self) -> None:
        yesterday = usage_service.day_start(usage_service.current_timestamp()) - 60
        with Session(self.engine) as session, session.begin():
            session.add(ConversationORM(id="old", user_id=self.member, created_at=yesterday, updated_at=yesterday, message_count=0))
            for index in range(LIMIT):
                session.add(ChatTurnORM(
                    request_id=str(uuid4()), user_id=self.member, conversation_id="old", user_text="旧",
                    status="completed", base_message_count=0, messages=[], answer="好", created_at=yesterday - index,
                ))
        self.use_up(self.member)
        self.assert_limited(self.member)
        with Session(self.engine) as session:
            days = usage_service.week_counts(session, user_ids=[self.member], now=usage_service.current_timestamp())
        self.assertEqual(days[self.member][-2:], [LIMIT, LIMIT])

    def test_deleting_conversations_does_not_reset_usage(self) -> None:
        self.use_up(self.member)
        with Session(self.engine) as session:
            conversations = list(session.scalars(select(ConversationORM.id).where(ConversationORM.user_id == self.member)))
        for conversation_id in conversations:
            with Session(self.engine) as session:
                conversation_service.delete_conversation(session, conversation_id, user_id=self.member)
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(ChatTurnORM.request_id).where(ChatTurnORM.user_id == self.member)), None)
        self.assert_limited(self.member)

    def test_admin_changes_limit_immediately(self) -> None:
        self.use_up(self.member)
        self.assert_limited(self.member)
        admin = self.client_for("boss")
        raised = admin.patch(f"/admin/users/{self.member}", json={"daily_chat_limit": LIMIT + 1})
        self.assertEqual(raised.status_code, 200, raised.text)
        body = raised.json()
        self.assertEqual((body["daily_chat_limit"], body["chat_limit"], body["chat_today"]), (LIMIT + 1, LIMIT + 1, LIMIT))
        self.assertEqual(len(body["chat_days"]), 7)
        self.start_turn(self.member)
        self.finish_all()
        self.assert_limited(self.member)

        restored = admin.patch(f"/admin/users/{self.member}", json={"daily_chat_limit": None}).json()
        self.assertEqual((restored["daily_chat_limit"], restored["chat_limit"]), (None, LIMIT))
        with Session(self.engine) as session:
            details = list(session.scalars(
                select(AccountEventORM.details).where(AccountEventORM.event_type == "chat_limit_changed").order_by(AccountEventORM.id)
            ))
        self.assertEqual([(d["from"], d["to"]) for d in details], [(None, LIMIT + 1), (LIMIT + 1, None)])

    def test_limit_validation(self) -> None:
        admin = self.client_for("boss")
        for body in ({"daily_chat_limit": 0}, {"daily_chat_limit": 1001}, {}):
            with self.subTest(body=body):
                self.assertEqual(admin.patch(f"/admin/users/{self.member}", json=body).status_code, 422)
        other_admin = self.add_user("deputy", role="admin")
        response = admin.patch(f"/admin/users/{other_admin}", json={"daily_chat_limit": 10})
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "管理员对话不限量，不能设置上限"))
        self.assertEqual(self.client_for("member").patch(f"/admin/users/{self.member}", json={"daily_chat_limit": 999}).status_code, 403)

    def test_concurrent_turns_cannot_exceed_limit(self) -> None:
        self.use_up(self.member, LIMIT - 1)
        results: list[str] = []
        barrier = threading.Barrier(4)

        def attempt() -> None:
            barrier.wait()
            try:
                self.start_turn(self.member)
                results.append("ok")
            except HTTPException as exc:
                results.append(str(exc.status_code))

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["429", "429", "429", "ok"])

    def test_usage_in_me_and_admin_list(self) -> None:
        self.use_up(self.member, 2)
        self.use_up(self.boss, 1)
        member = self.client_for("member")
        self.assertEqual(member.get("/auth/me").json()["chat_usage"], {"today": 2, "limit": LIMIT})
        page = self.client_for("boss").get("/admin/users", params={"status": "active"}).json()
        self.assertEqual((page["chat_today"], page["chat_week"], page["default_chat_limit"]), (3, 3, LIMIT))
        rows = {u["username"]: (u["chat_today"], u["chat_week"], u["chat_limit"]) for u in page["items"]}
        self.assertEqual(rows, {"member": (2, 2, LIMIT), "boss": (1, 1, None)})


if __name__ == "__main__":
    unittest.main()
