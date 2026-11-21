"""P4：注册审核、账号状态机、管理后台、个人设置与账号事件（PRD 13.2）。"""

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api import auth as auth_api
from backend.app.db.isolated import isolated_engine
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.models.account_event import AccountEventORM
from backend.app.models.user import UserORM
from backend.app.services import account_service
from backend.app.services.auth_service import hash_password
from backend.app.services.rate_limit import login_failure_lock, login_ip_limiter, register_ip_limiter

ORIGIN = "http://127.0.0.1:5173"
PASSWORD = "correct-horse-1"


class AccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        for limiter in (login_ip_limiter, login_failure_lock, register_ip_limiter):
            limiter.reset()

        def test_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_db] = test_db
        patcher = patch.object(auth_api, "COOKIE_SECURE", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin_id = self.add_user("boss", role="admin")
        self.admin = self.client_for("boss")

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.database.__exit__(None, None, None)

    # ---------- helpers ----------
    def new_client(self) -> TestClient:
        client = TestClient(app, headers={"Origin": ORIGIN})
        self.addCleanup(client.close)
        return client

    def add_user(self, username: str, *, status: str = "active", role: str = "user", email: str | None = None) -> int:
        with Session(self.engine) as session, session.begin():
            user = UserORM(
                username=username, email=email, password_hash=hash_password(PASSWORD), status=status, role=role,
            )
            session.add(user)
            session.flush()
            return user.id

    def login(self, client: TestClient, account: str, password: str = PASSWORD):
        return client.post("/auth/login", json={"account": account, "password": password})

    def client_for(self, account: str, password: str = PASSWORD) -> TestClient:
        client = self.new_client()
        response = self.login(client, account, password)
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def register(self, username: str, email: str | None = None, password: str = PASSWORD):
        return self.new_client().post("/auth/register", json={"username": username, "password": password, "email": email})

    def user_id(self, username: str) -> int:
        with Session(self.engine) as session:
            return session.scalar(select(UserORM.id).where(UserORM.username == username))

    def events(self, target_id: int) -> list[str]:
        with Session(self.engine) as session:
            return list(session.scalars(
                select(AccountEventORM.event_type).where(AccountEventORM.target_user_id == target_id)
                .order_by(AccountEventORM.id)
            ))

    # ---------- 注册与审核 ----------
    def test_register_then_approve(self) -> None:
        response = self.register("New_User", email=" New@Example.com ")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"username": "new_user", "status": "pending"})
        self.assertNotIn("liuhen_session", response.cookies)

        pending = self.login(self.new_client(), "new_user")
        self.assertEqual((pending.status_code, pending.json()["detail"]), (403, "账号正在审核中，审核通过后即可登录"))
        self.assertEqual(self.admin.get("/admin/users/pending-count").json(), {"count": 1})

        user_id = self.user_id("new_user")
        approved = self.admin.post(f"/admin/users/{user_id}/approve")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "active")
        self.assertIsNotNone(approved.json()["reviewed_at"])
        self.assertEqual(self.admin.post(f"/admin/users/{user_id}/approve").status_code, 409)
        self.client_for("new@example.com")
        self.assertEqual(
            self.events(user_id),
            ["user_registered", "user_approved", "login_succeeded"],
        )
        with Session(self.engine) as session:
            self.assertEqual(session.get(UserORM, user_id).reviewed_by, self.admin_id)

    def test_register_validation_and_duplicates(self) -> None:
        self.add_user("taken", email="taken@example.com")
        cases = (
            ({"username": "Taken"}, 409, "用户名已被占用"),
            ({"username": "fresh", "email": "TAKEN@example.com"}, 409, "邮箱已被使用"),
            ({"username": "ab"}, 422, "用户名须为 3–32 位小写字母、数字或下划线"),
            ({"username": "fresh", "password": "fresh"}, 422, "密码长度须为 8–128 位"),
        )
        for kwargs, code, message in cases:
            with self.subTest(kwargs=kwargs):
                response = self.register(**kwargs)
                self.assertEqual((response.status_code, response.json()["detail"]), (code, message))

    def test_register_rate_limit_per_ip(self) -> None:
        statuses = [self.register(f"user_{i}").status_code for i in range(6)]
        self.assertEqual(statuses, [201] * 5 + [429])

    def test_reject_then_delete_frees_username(self) -> None:
        self.register("spammer")
        user_id = self.user_id("spammer")
        rejected = self.admin.post(f"/admin/users/{user_id}/reject", json={"reason": "不认识"})
        self.assertEqual((rejected.json()["status"], rejected.json()["status_reason"]), ("rejected", "不认识"))
        self.assertEqual(self.login(self.new_client(), "spammer").json()["detail"], "注册申请未通过")

        self.assertEqual(self.admin.delete(f"/admin/users/{user_id}").status_code, 204)
        self.assertIsNone(self.user_id("spammer"))
        self.assertEqual(self.register("spammer").status_code, 201)

        events = self.admin.get("/admin/account-events", params={"user_id": user_id}).json()
        self.assertEqual([e["event_type"] for e in events], ["user_deleted", "user_rejected", "user_registered"])
        self.assertTrue(all(e["target_username"] == "spammer" for e in events))
        self.assertEqual(events[1]["reason"], "不认识")
        self.assertEqual(events[0]["actor_username"], "boss")

    def test_only_pending_or_rejected_can_be_deleted(self) -> None:
        member = self.add_user("member")
        response = self.admin.delete(f"/admin/users/{member}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "该账号当前状态为「正常」，不能执行这个操作")
        self.assertEqual(self.admin.delete("/admin/users/99999").status_code, 404)

    # ---------- 停用与启用 ----------
    def test_disable_revokes_sessions_and_enable_restores_login(self) -> None:
        member = self.add_user("member")
        member_client = self.client_for("member")
        self.assertEqual(member_client.get("/tasks").status_code, 200)

        disabled = self.admin.post(f"/admin/users/{member}/disable", json={"reason": "滥用"})
        self.assertEqual(disabled.json()["status"], "disabled")
        self.assertEqual(member_client.get("/tasks").status_code, 401)
        self.assertEqual(self.login(self.new_client(), "member").json()["detail"], "账号已被停用，请联系管理员")
        listed = self.admin.get("/admin/users", params={"status": "disabled"}).json()
        self.assertEqual([(u["username"], u["status_reason"]) for u in listed["items"]], [("member", "滥用")])

        self.assertEqual(self.admin.post(f"/admin/users/{member}/enable").json()["status"], "active")
        self.client_for("member")
        self.assertEqual(
            self.events(member),
            ["login_succeeded", "user_disabled", "user_enabled", "login_succeeded"],
        )

    def test_admin_cannot_disable_self_and_last_admin_survives(self) -> None:
        response = self.admin.post(f"/admin/users/{self.admin_id}/disable")
        self.assertEqual((response.status_code, response.json()["detail"]), (400, "不能停用自己的账号"))

        # 两个管理员互相停用：后执行的一方此时已被停用，操作被拒绝，系统仍保留一个正常的管理员。
        other = self.add_user("deputy", role="admin")
        with Session(self.engine) as session:
            account_service.disable(session, actor_id=self.admin_id, target_id=other, reason=None)
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            account_service.disable(session, actor_id=other, target_id=self.admin_id, reason=None)
        self.assertEqual(raised.exception.status_code, 403)
        with Session(self.engine) as session:
            self.assertEqual(session.get(UserORM, self.admin_id).status, "active")

    def test_admin_routes_require_admin(self) -> None:
        self.add_user("member")
        member = self.client_for("member")
        for method, path in (
            ("GET", "/admin/users"), ("GET", "/admin/users/pending-count"),
            ("POST", f"/admin/users/{self.admin_id}/disable"), ("GET", "/admin/account-events"),
        ):
            with self.subTest(path=path):
                response = member.request(method, path)
                self.assertEqual((response.status_code, response.json()["detail"]), (403, "需要管理员权限"))
        self.assertEqual(self.new_client().get("/admin/users").status_code, 401)

    # ---------- 重置密码 ----------
    def test_admin_reset_password(self) -> None:
        member = self.add_user("member")
        member_client = self.client_for("member")
        response = self.admin.post(f"/admin/users/{member}/reset-password")
        temporary = response.json()["temporary_password"]
        self.assertGreaterEqual(len(temporary), 16)
        self.assertEqual(member_client.get("/auth/me").status_code, 401)

        renewed = self.client_for("member", temporary)
        self.assertTrue(renewed.get("/auth/me").json()["must_change_password"])
        # 强制改密状态下个人设置接口同样不可用。
        self.assertEqual(renewed.get("/auth/sessions").status_code, 403)
        self.assertEqual(self.admin.get(f"/admin/users/{member}").json()["must_change_password"], True)

        self_reset = self.admin.post(f"/admin/users/{self.admin_id}/reset-password")
        self.assertEqual(self_reset.status_code, 400)
        with Session(self.engine) as session:
            details = session.scalar(
                select(AccountEventORM.details).where(AccountEventORM.event_type == "password_reset")
            )
        self.assertNotIn(temporary, str(details))

    # ---------- 账号列表 ----------
    def test_list_users_filters_counts_and_search(self) -> None:
        self.add_user("a_one", status="pending")
        self.add_user("axone", status="pending", email="x@example.com")
        self.add_user("cee", status="disabled")
        page = self.admin.get("/admin/users", params={"status": "pending"}).json()
        self.assertEqual([u["username"] for u in page["items"]], ["a_one", "axone"])
        self.assertEqual(page["counts"], {"pending": 2, "active": 1, "rejected": 0, "disabled": 1, "all": 4})
        # 下划线按字面匹配，不当作 LIKE 通配符。
        self.assertEqual([u["username"] for u in self.admin.get("/admin/users", params={"q": "a_"}).json()["items"]], ["a_one"])
        self.assertEqual(self.admin.get("/admin/users", params={"q": "X@EX"}).json()["total"], 1)
        paged = self.admin.get("/admin/users", params={"page": 2, "page_size": 3}).json()
        self.assertEqual((paged["total"], len(paged["items"])), (4, 1))

    # ---------- 个人设置 ----------
    def test_change_email(self) -> None:
        member = self.add_user("member", email="old@example.com")
        self.add_user("other", email="used@example.com")
        client = self.client_for("member")
        wrong = client.patch("/auth/me", json={"email": "new@example.com", "current_password": "nope"})
        self.assertEqual((wrong.status_code, wrong.json()["detail"]), (400, "当前密码不正确"))
        taken = client.patch("/auth/me", json={"email": "USED@example.com", "current_password": PASSWORD})
        self.assertEqual((taken.status_code, taken.json()["detail"]), (409, "邮箱已被使用"))

        ok = client.patch("/auth/me", json={"email": " New@Example.com", "current_password": PASSWORD})
        self.assertEqual(ok.json()["email"], "new@example.com")
        self.assertEqual(client.get("/auth/me").json()["email"], "new@example.com")
        self.client_for("new@example.com")
        self.assertEqual(self.login(self.new_client(), "old@example.com").status_code, 401)

        cleared = client.patch("/auth/me", json={"email": None, "current_password": PASSWORD})
        self.assertIsNone(cleared.json()["email"])
        self.assertEqual(self.events(member).count("email_changed"), 2)

    def test_sessions_list_and_revoke(self) -> None:
        member = self.add_user("member")
        current = self.client_for("member")
        laptop = self.client_for("member")
        phone = self.client_for("member")
        sessions = current.get("/auth/sessions").json()
        self.assertEqual(len(sessions), 3)
        self.assertEqual(sum(s["current"] for s in sessions), 1)
        current_id = next(s["id"] for s in sessions if s["current"])
        laptop_id = next(s["id"] for s in laptop.get("/auth/sessions").json() if s["current"])

        # 别人的会话、当前会话、不存在的会话都按 404 处理。
        self.assertEqual(self.admin.delete(f"/auth/sessions/{laptop_id}").status_code, 404)
        self.assertEqual(current.delete(f"/auth/sessions/{current_id}").status_code, 404)
        self.assertEqual(current.delete(f"/auth/sessions/{laptop_id}").status_code, 204)
        self.assertEqual(laptop.get("/auth/me").status_code, 401)
        self.assertEqual(phone.get("/auth/me").status_code, 200)

        self.assertEqual(current.post("/auth/sessions/revoke-others").json(), {"revoked": 1})
        self.assertEqual(phone.get("/auth/me").status_code, 401)
        self.assertEqual(current.get("/auth/me").status_code, 200)
        self.assertEqual(len(self.admin.get("/auth/sessions").json()), 1)
        self.assertEqual(self.events(member).count("session_revoked"), 2)

    def test_own_events_only_include_login_and_password(self) -> None:
        member = self.add_user("member")
        self.add_user("other")
        self.login(self.new_client(), "member", "wrong-password")
        self.login(self.new_client(), "nobody", "wrong-password")
        self.login(self.new_client(), "other", "wrong-password")
        client = self.client_for("member")
        client.patch("/auth/me", json={"email": "m@example.com", "current_password": PASSWORD})
        client.post("/auth/password", json={"old_password": PASSWORD, "new_password": "new-password-1"})

        events = client.get("/auth/events").json()
        self.assertEqual([e["event_type"] for e in events], ["password_changed", "login_succeeded", "login_failed"])
        self.assertTrue(all(e["ip"] == "testclient" for e in events if e["event_type"].startswith("login")))
        self.assertIn("email_changed", self.events(member))
        # 不存在的账号登录失败不入表。
        with Session(self.engine) as session:
            self.assertEqual(
                session.scalar(select(AccountEventORM.id).where(AccountEventORM.target_user_id == 0)), None,
            )

    def test_failed_logins_record_lock_event(self) -> None:
        member = self.add_user("member")
        for _ in range(5):
            self.login(self.new_client(), "member", "wrong-password")
        self.assertEqual(self.events(member), ["login_failed"] * 5 + ["login_locked"])
        self.assertEqual(self.login(self.new_client(), "member").status_code, 429)

    def test_cli_operations_record_events(self) -> None:
        with Session(self.engine) as session:
            created = account_service.create_admin(session, username="root2", password="long-enough-1")
        member = self.add_user("member")
        with Session(self.engine) as session:
            account_service.promote_to_admin(session, user_id=member)
        with Session(self.engine) as session:
            account_service.reset_password(session, target_id=member, actor_id=None)
        with Session(self.engine) as session:
            rows = session.execute(
                select(AccountEventORM.event_type, AccountEventORM.actor_user_id, AccountEventORM.details)
                .where(AccountEventORM.target_user_id.in_((created.id, member)))
                .order_by(AccountEventORM.id)
            ).all()
        self.assertEqual([r[0] for r in rows], ["user_registered", "role_changed", "password_reset"])
        self.assertTrue(all(actor is None and details["source"] == "cli" for _, actor, details in rows))


if __name__ == "__main__":
    unittest.main()
