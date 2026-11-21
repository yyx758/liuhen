import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.app.api import auth as auth_api
from backend.app.db.isolated import isolated_engine
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.models.user import UserORM, UserSessionORM
from backend.app.services import account_service, auth_service
from backend.app.services.auth_service import IDLE_SECONDS, SESSION_COOKIE, hash_password
from backend.app.services.rate_limit import login_failure_lock, login_ip_limiter

ORIGIN = "http://127.0.0.1:5173"
PASSWORD = "correct-horse-1"


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        login_ip_limiter.reset()
        login_failure_lock.reset()

        def test_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_db] = test_db
        # 测试客户端走 http，Secure Cookie 不会被回传。
        patcher = patch.object(auth_api, "COOKIE_SECURE", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.new_client()

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.database.__exit__(None, None, None)

    def new_client(self) -> TestClient:
        client = TestClient(app, headers={"Origin": ORIGIN})
        self.addCleanup(client.close)
        return client

    def add_user(self, username: str, *, status: str = "active", email: str | None = None) -> int:
        with Session(self.engine) as session, session.begin():
            user = UserORM(username=username, email=email, password_hash=hash_password(PASSWORD), status=status)
            session.add(user)
            session.flush()
            return user.id

    def login(self, client: TestClient, account: str, password: str = PASSWORD, remember: bool = False):
        return client.post("/auth/login", json={"account": account, "password": password, "remember": remember})

    def session_count(self, user_id: int) -> int:
        with Session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(UserSessionORM).where(
                UserSessionORM.user_id == user_id
            ))

    def test_login_sets_http_only_cookie_and_unlocks_business_routes(self) -> None:
        self.add_user("alice", email="alice@example.com")
        self.assertEqual(self.client.get("/tasks").status_code, 401)

        response = self.login(self.client, "  Alice@Example.com ")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["username"], "alice")
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn("Max-Age", cookie)
        self.assertEqual(self.client.get("/auth/me").json()["username"], "alice")
        self.assertEqual(self.client.get("/tasks").status_code, 200)

        # 数据库里只有令牌哈希，没有令牌原文。
        token = self.client.cookies[SESSION_COOKIE]
        with Session(self.engine) as session:
            stored = session.scalar(select(UserSessionORM.token_hash))
        self.assertNotEqual(stored, token)
        self.assertEqual(stored, auth_service.hash_token(token))

    def test_unknown_account_and_wrong_password_look_the_same(self) -> None:
        self.add_user("alice")
        wrong = self.login(self.client, "alice", "wrong-password")
        unknown = self.login(self.client, "nobody", PASSWORD)
        self.assertEqual((wrong.status_code, wrong.json()), (unknown.status_code, unknown.json()))
        self.assertEqual(wrong.json()["detail"], "账号或密码错误")

    def test_account_status_is_only_revealed_with_correct_password(self) -> None:
        for status, message in auth_service.STATUS_ERRORS.items():
            with self.subTest(status=status):
                self.add_user(f"u_{status}", status=status)
                self.assertEqual(self.login(self.client, f"u_{status}", "wrong-password").json()["detail"], "账号或密码错误")
                response = self.login(self.client, f"u_{status}")
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["detail"], message)
                self.assertNotIn(SESSION_COOKIE, response.cookies)

    def test_remember_and_idle_expiry(self) -> None:
        user_id = self.add_user("alice")
        remembered = self.new_client()
        self.assertIn("Max-Age=604800", self.login(remembered, "alice", remember=True).headers["set-cookie"])
        self.login(self.client, "alice")

        # 13 小时无操作：普通会话失效并被删除，保持登录的会话仍有效。
        with Session(self.engine) as session, session.begin():
            session.execute(update(UserSessionORM).values(last_seen_at=UserSessionORM.last_seen_at - IDLE_SECONDS - 3600))
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        self.assertEqual(remembered.get("/auth/me").status_code, 200)
        self.assertEqual(self.session_count(user_id), 1)

        with Session(self.engine) as session, session.begin():
            session.execute(update(UserSessionORM).values(expires_at=0))
        self.assertEqual(remembered.get("/auth/me").status_code, 401)

    def test_logout_and_disable_revoke_access(self) -> None:
        user_id = self.add_user("alice")
        self.login(self.client, "alice")
        token = self.client.cookies[SESSION_COOKIE]
        response = self.client.post("/auth/logout")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.session_count(user_id), 0)
        replay = self.new_client()
        replay.cookies.set(SESSION_COOKIE, token)
        self.assertEqual(replay.get("/auth/me").status_code, 401)

        self.login(self.client, "alice")
        with Session(self.engine) as session, session.begin():
            session.get(UserORM, user_id).status = "disabled"
        self.assertEqual(self.client.get("/tasks").status_code, 401)

    def test_change_password_keeps_only_current_session(self) -> None:
        user_id = self.add_user("alice")
        other = self.new_client()
        self.login(other, "alice")
        self.login(self.client, "alice")

        wrong = self.client.post("/auth/password", json={"old_password": "nope", "new_password": "new-password-1"})
        self.assertEqual(wrong.json()["detail"], "当前密码不正确")
        short = self.client.post("/auth/password", json={"old_password": PASSWORD, "new_password": "short"})
        self.assertEqual(short.json()["detail"], "密码长度须为 8–128 位")

        ok = self.client.post("/auth/password", json={"old_password": PASSWORD, "new_password": "new-password-1"})
        self.assertEqual(ok.status_code, 204)
        self.assertEqual(self.client.get("/auth/me").status_code, 200)
        self.assertEqual(other.get("/auth/me").status_code, 401)
        self.assertEqual(self.session_count(user_id), 1)
        self.assertEqual(self.login(self.new_client(), "alice", "new-password-1").status_code, 200)

    def test_reset_password_forces_change_before_business_access(self) -> None:
        self.add_user("alice")
        self.login(self.client, "alice")
        with Session(self.engine) as session:
            temporary = account_service.reset_password(
                session, target_id=account_service.user_id_by_username(session, "Alice"), actor_id=None,
            )
        self.assertEqual(self.client.get("/auth/me").status_code, 401)

        self.login(self.client, "alice", temporary)
        self.assertTrue(self.client.get("/auth/me").json()["must_change_password"])
        blocked = self.client.get("/tasks")
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()["detail"], "管理员已重置你的密码，请先设置新密码")

        self.client.post("/auth/password", json={"old_password": temporary, "new_password": "new-password-1"})
        self.assertFalse(self.client.get("/auth/me").json()["must_change_password"])
        self.assertEqual(self.client.get("/tasks").status_code, 200)

    def test_failed_logins_lock_the_account(self) -> None:
        self.add_user("alice")
        for _ in range(5):
            self.assertEqual(self.login(self.client, "alice", "wrong-password").status_code, 401)
        locked = self.login(self.client, "alice")
        self.assertEqual(locked.status_code, 429)
        self.assertEqual(locked.json()["detail"], "尝试次数过多，请稍后再试")

    def test_login_rate_limit_per_ip(self) -> None:
        self.add_user("alice")
        statuses = [self.login(self.client, f"nobody{i}").status_code for i in range(11)]
        self.assertEqual(statuses[:10], [401] * 10)
        self.assertEqual(statuses[10], 429)

    def test_write_requests_with_cookie_must_come_from_allowed_origin(self) -> None:
        self.add_user("alice")
        self.login(self.client, "alice")
        body = {"title": "任务", "due_at": "2030-01-02T10:00:00+08:00"}
        for origin in (None, "https://evil.example"):
            with self.subTest(origin=origin):
                headers = {"Idempotency-Key": str(uuid4()), "Origin": origin or ""}
                if origin is None:
                    self.client.headers.pop("Origin")
                response = self.client.post("/tasks", headers=headers, json=body)
                self.client.headers["Origin"] = ORIGIN
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["detail"], "请求来源不被允许")
        ok = self.client.post("/tasks", headers={"Idempotency-Key": str(uuid4())}, json=body)
        self.assertEqual(ok.status_code, 201, ok.text)

        bare = TestClient(app)
        self.addCleanup(bare.close)
        self.assertEqual(self.login(bare, "alice").status_code, 403)

    def test_sessions_per_user_are_capped(self) -> None:
        user_id = self.add_user("alice")
        for _ in range(12):
            login_ip_limiter.reset()
            self.login(self.new_client(), "alice")
        self.assertEqual(self.session_count(user_id), 10)

    def test_purge_removes_expired_sessions(self) -> None:
        user_id = self.add_user("alice")
        self.login(self.client, "alice")
        self.login(self.new_client(), "alice", remember=True)
        with Session(self.engine) as session, session.begin():
            session.execute(update(UserSessionORM).values(last_seen_at=0))
        with Session(self.engine) as session:
            self.assertEqual(auth_service.purge_expired_sessions(session), 1)
        self.assertEqual(self.session_count(user_id), 1)


class AccountServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()

    def tearDown(self) -> None:
        self.database.__exit__(None, None, None)

    def test_create_admin_validates_and_normalizes(self) -> None:
        with Session(self.engine) as session:
            user = account_service.create_admin(
                session, username="Owner", password="long-enough-1", email=" Owner@Example.COM ",
            )
        self.assertEqual((user.username, user.email, user.role), ("owner", "owner@example.com", "admin"))
        with Session(self.engine) as session:
            self.assertEqual(session.get(UserORM, user.id).status, "active")

        for kwargs, message in (
            ({"username": "ab", "password": "long-enough-1"}, "用户名须为 3–32 位小写字母、数字或下划线"),
            ({"username": "new_one", "password": "short"}, "密码长度须为 8–128 位"),
            ({"username": "new_one_1", "password": "new_one_1"}, "密码不能与用户名相同"),
            ({"username": "OWNER", "password": "long-enough-1"}, "用户名已被占用"),
            ({"username": "new_one", "password": "long-enough-1", "email": "owner@example.com"}, "邮箱已被使用"),
            ({"username": "new_one", "password": "long-enough-1", "email": "not-an-email"}, "邮箱格式不正确"),
        ):
            with self.subTest(kwargs=kwargs), Session(self.engine) as session:
                with self.assertRaises(HTTPException) as raised:
                    account_service.create_admin(session, **kwargs)
                self.assertEqual(raised.exception.detail, message)

    def test_promote_requires_active_account(self) -> None:
        with Session(self.engine) as session, session.begin():
            waiting = UserORM(username="waiting", password_hash="!", status="pending")
            member = UserORM(username="member", password_hash="!", status="active")
            session.add_all([waiting, member])
            session.flush()
            waiting_id, member_id = waiting.id, member.id
        with Session(self.engine) as session, self.assertRaises(HTTPException):
            account_service.promote_to_admin(session, user_id=waiting_id)
        with Session(self.engine) as session:
            account_service.promote_to_admin(session, user_id=member_id)
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(UserORM.role).where(UserORM.username == "member")), "admin")


if __name__ == "__main__":
    unittest.main()
