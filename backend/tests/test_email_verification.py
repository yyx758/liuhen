"""邮箱验证（docs/prd/prd-email-verification.md）：验证码的发送、校验、频率限制，注册与改邮箱接入验证码，SMTP 发信。"""

import hashlib
import smtplib
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.app.api import auth as auth_api
from backend.app.db.isolated import isolated_engine
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.models.account_event import AccountEventORM
from backend.app.models.email_verification import EmailVerificationCodeORM
from backend.app.models.user import UserORM
from backend.app.services import mailer
from backend.app.services.auth_service import hash_password
from backend.app.services.rate_limit import (
    email_code_ip_limiter, login_failure_lock, login_ip_limiter, register_ip_limiter,
)
from backend.tests.mail_helpers import install_outbox, wrong_code

ORIGIN = "http://127.0.0.1:5173"
PASSWORD = "correct-horse-1"
CODE_WRONG = "验证码错误或已过期"
TOO_FREQUENT = "获取验证码过于频繁，请稍后再试"


class EmailVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        for limiter in (login_ip_limiter, login_failure_lock, register_ip_limiter, email_code_ip_limiter):
            limiter.reset()

        def test_db():
            with Session(self.engine) as session:
                yield session

        app.dependency_overrides[get_db] = test_db
        patcher = patch.object(auth_api, "COOKIE_SECURE", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.outbox = install_outbox(self)
        self.client = self.new_client()

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.database.__exit__(None, None, None)

    # ---------- helpers ----------
    def new_client(self) -> TestClient:
        client = TestClient(app, headers={"Origin": ORIGIN})
        self.addCleanup(client.close)
        return client

    def add_user(self, username: str, *, email: str | None = None, verified: bool = False,
                 status: str = "active", role: str = "user") -> int:
        with Session(self.engine) as session, session.begin():
            user = UserORM(
                username=username, email=email, password_hash=hash_password(PASSWORD), status=status, role=role,
                email_verified_at=session.scalar(select(func.now())) if verified else None,
            )
            session.add(user)
            session.flush()
            return user.id

    def client_for(self, account: str) -> TestClient:
        client = self.new_client()
        response = client.post("/auth/login", json={"account": account, "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def request_code(self, email: str, client: TestClient | None = None):
        return (client or self.client).post("/auth/register/email-code", json={"email": email})

    def register(self, username: str, email: str, code: str):
        return self.client.post(
            "/auth/register", json={"username": username, "password": PASSWORD, "email": email, "code": code},
        )

    def code_rows(self, email: str | None = None) -> list[EmailVerificationCodeORM]:
        with Session(self.engine) as session:
            stmt = select(EmailVerificationCodeORM).order_by(EmailVerificationCodeORM.id)
            if email is not None:
                stmt = stmt.where(EmailVerificationCodeORM.email == email)
            return list(session.scalars(stmt))

    def age_codes(self, email: str, seconds: int) -> None:
        """把某邮箱的验证码记录整体往前挪，模拟时间流逝（绕过 60 秒冷却）。"""
        delta = timedelta(seconds=seconds)
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(EmailVerificationCodeORM).where(EmailVerificationCodeORM.email == email).values(
                    created_at=EmailVerificationCodeORM.created_at - delta,
                    expires_at=EmailVerificationCodeORM.expires_at - delta,
                )
            )

    def user(self, username: str) -> UserORM:
        with Session(self.engine) as session:
            return session.scalar(select(UserORM).where(UserORM.username == username))

    # ---------- 获取注册验证码 ----------
    def test_register_code_is_mailed_and_only_hash_is_stored(self) -> None:
        response = self.request_code(" New@Example.com ")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"expires_in": 600, "resend_after": 60})

        self.assertEqual(len(self.outbox.sent), 1)
        mail = self.outbox.sent[0]
        self.assertEqual(mail["to"], "new@example.com")
        self.assertEqual(mail["subject"], "【小记】邮箱验证码")
        code = self.outbox.last_code()
        self.assertIn("10 分钟", mail["body"])

        [row] = self.code_rows()
        self.assertEqual((row.email, row.purpose, row.user_id), ("new@example.com", "register", None))
        self.assertEqual(row.code_hash, hashlib.sha256(code.encode()).hexdigest())
        self.assertEqual(row.expires_at - row.created_at, timedelta(minutes=10))
        self.assertIsNone(row.used_at)
        self.assertIsNone(row.invalidated_at)

    def test_invalid_or_taken_email_sends_nothing(self) -> None:
        self.add_user("taken", email="taken@example.com")
        invalid = self.request_code("not-an-email")
        self.assertEqual((invalid.status_code, invalid.json()["detail"]), (422, "邮箱格式不正确"))
        taken = self.request_code("TAKEN@example.com")
        self.assertEqual((taken.status_code, taken.json()["detail"]), (409, "邮箱已被使用"))
        self.assertEqual(self.outbox.sent, [])
        self.assertEqual(self.code_rows(), [])

    def test_resend_cooldown_and_hourly_limit_per_email(self) -> None:
        email = "busy@example.com"
        self.assertEqual(self.request_code(email).status_code, 200)
        again = self.request_code(email)
        self.assertEqual((again.status_code, again.json()["detail"]), (429, TOO_FREQUENT))

        # 每次都等过 60 秒冷却：一小时内同一邮箱最多 5 次。
        for _ in range(4):
            self.age_codes(email, 61)
            self.assertEqual(self.request_code(email).status_code, 200)
        self.age_codes(email, 61)
        sixth = self.request_code(email)
        self.assertEqual((sixth.status_code, sixth.json()["detail"]), (429, TOO_FREQUENT))
        self.assertEqual(len(self.outbox.sent), 5)

        # 最早的记录超过一小时后又可以获取。
        self.age_codes(email, 3600)
        self.assertEqual(self.request_code(email).status_code, 200)

    def test_ip_limit(self) -> None:
        statuses = [self.request_code(f"user{i}@example.com").status_code for i in range(11)]
        self.assertEqual(statuses, [200] * 10 + [429])

    def test_new_code_invalidates_previous_one(self) -> None:
        email = "new@example.com"
        self.request_code(email)
        first = self.outbox.last_code()
        self.age_codes(email, 61)
        self.request_code(email)
        second = self.outbox.last_code()
        self.assertNotEqual(first, second)

        old_row, new_row = self.code_rows(email)
        self.assertIsNotNone(old_row.invalidated_at)
        self.assertIsNone(new_row.invalidated_at)

        self.assertEqual(self.register("newbie", email, first).json()["detail"], CODE_WRONG)
        self.assertEqual(self.register("newbie", email, second).status_code, 201)

    def test_smtp_failure_does_not_keep_code_or_start_cooldown(self) -> None:
        email = "new@example.com"
        with patch.object(mailer, "send_mail", side_effect=mailer.MailError("connection refused")):
            failed = self.request_code(email)
        self.assertEqual((failed.status_code, failed.json()["detail"]), (502, "验证码邮件发送失败，请稍后再试"))
        self.assertEqual(self.code_rows(), [])
        # 没发出去的那次不算冷却，可以立即重试。
        self.assertEqual(self.request_code(email).status_code, 200)

    def test_smtp_failure_keeps_previous_code_valid(self) -> None:
        email = "new@example.com"
        self.request_code(email)
        code = self.outbox.last_code()
        self.age_codes(email, 61)
        with patch.object(mailer, "send_mail", side_effect=mailer.MailError("timeout")):
            self.assertEqual(self.request_code(email).status_code, 502)
        # 新码没发出去，旧码不能被作废。
        self.assertEqual(self.register("newbie", email, code).status_code, 201)

    def test_smtp_not_configured(self) -> None:
        with patch.object(mailer, "send_mail", side_effect=mailer.MailNotConfigured()):
            response = self.request_code("new@example.com")
        self.assertEqual((response.status_code, response.json()["detail"]), (503, "邮件服务未配置"))
        self.assertEqual(self.code_rows(), [])

    # ---------- 注册 ----------
    def test_register_with_code_marks_email_verified(self) -> None:
        self.request_code("New@Example.com")
        response = self.register("New_User", " new@example.com", self.outbox.last_code())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"username": "new_user", "status": "pending"})

        user = self.user("new_user")
        self.assertEqual(user.email, "new@example.com")
        self.assertIsNotNone(user.email_verified_at)
        [row] = self.code_rows()
        self.assertIsNotNone(row.used_at)
        with Session(self.engine) as session:
            details = session.scalar(
                select(AccountEventORM.details).where(AccountEventORM.target_user_id == user.id)
            )
        self.assertTrue(details["email_verified"])

        # 审核通过后，/auth/me 显示邮箱已验证。
        with Session(self.engine) as session, session.begin():
            session.execute(update(UserORM).where(UserORM.id == user.id).values(status="active"))
        self.assertTrue(self.client_for("new_user").get("/auth/me").json()["email_verified"])

    def test_register_requires_email_and_code(self) -> None:
        cases = (
            {"username": "fresh", "password": PASSWORD, "code": "123456"},
            {"username": "fresh", "password": PASSWORD, "email": "fresh@example.com"},
            {"username": "fresh", "password": PASSWORD, "email": "  ", "code": "123456"},
        )
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/auth/register", json=body).status_code, 422)
        self.assertIsNone(self.user("fresh"))

    def test_wrong_code_five_times_invalidates_it(self) -> None:
        email = "new@example.com"
        self.request_code(email)
        code = self.outbox.last_code()
        for attempt in range(5):
            response = self.register("newbie", email, wrong_code(code))
            self.assertEqual((response.status_code, response.json()["detail"]), (400, CODE_WRONG), attempt)
        [row] = self.code_rows()
        self.assertEqual(row.failed_attempts, 5)
        self.assertIsNotNone(row.invalidated_at)
        # 输错 5 次后，正确的验证码也不能再用。
        self.assertEqual(self.register("newbie", email, code).json()["detail"], CODE_WRONG)
        self.assertIsNone(self.user("newbie"))

    def test_expired_code_rejected(self) -> None:
        email = "new@example.com"
        self.request_code(email)
        self.age_codes(email, 601)
        self.assertEqual(self.register("newbie", email, self.outbox.last_code()).json()["detail"], CODE_WRONG)

    def test_code_cannot_be_reused_after_account_deleted(self) -> None:
        self.add_user("boss", role="admin")
        email = "new@example.com"
        self.request_code(email)
        code = self.outbox.last_code()
        self.assertEqual(self.register("first", email, code).status_code, 201)

        # 拒绝并删除后邮箱被释放，但同一个验证码不能再注册第二个账号。
        boss = self.client_for("boss")
        first_id = self.user("first").id
        boss.post(f"/admin/users/{first_id}/reject")
        self.assertEqual(boss.delete(f"/admin/users/{first_id}").status_code, 204)
        self.assertEqual(self.register("second", email, code).json()["detail"], CODE_WRONG)

    def test_code_is_bound_to_email_and_purpose(self) -> None:
        self.request_code("a@example.com")
        code_a = self.outbox.last_code()
        self.assertEqual(self.register("newbie", "b@example.com", code_a).json()["detail"], CODE_WRONG)

        # 改邮箱用途的验证码不能拿来注册。
        self.add_user("member")
        member = self.client_for("member")
        member.post("/auth/me/email-code", json={"email": "c@example.com", "current_password": PASSWORD})
        code_c = self.outbox.last_code("c@example.com")
        self.assertEqual(self.register("newbie", "c@example.com", code_c).json()["detail"], CODE_WRONG)

    def test_taken_email_is_checked_before_code(self) -> None:
        self.add_user("taken", email="taken@example.com")
        response = self.register("fresh", "taken@example.com", "123456")
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "邮箱已被使用"))

    # ---------- 个人设置：绑定 / 修改邮箱 ----------
    def test_change_email_with_code(self) -> None:
        member_id = self.add_user("member", email="old@example.com")
        member = self.client_for("member")
        self.assertFalse(member.get("/auth/me").json()["email_verified"])

        wrong = member.post("/auth/me/email-code", json={"email": "new@example.com", "current_password": "nope"})
        self.assertEqual((wrong.status_code, wrong.json()["detail"]), (400, "当前密码不正确"))
        self.assertEqual(self.outbox.sent, [])

        sent = member.post("/auth/me/email-code", json={"email": " New@Example.com", "current_password": PASSWORD})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(self.outbox.sent[-1]["to"], "new@example.com")
        [row] = self.code_rows()
        self.assertEqual((row.purpose, row.user_id), ("change_email", member_id))
        code = self.outbox.last_code()

        body = {"email": "new@example.com", "current_password": PASSWORD}
        missing = member.patch("/auth/me", json=body)
        self.assertEqual((missing.status_code, missing.json()["detail"]), (422, "请填写验证码"))
        bad = member.patch("/auth/me", json={**body, "code": wrong_code(code)})
        self.assertEqual((bad.status_code, bad.json()["detail"]), (400, CODE_WRONG))

        ok = member.patch("/auth/me", json={**body, "code": code})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual((ok.json()["email"], ok.json()["email_verified"]), ("new@example.com", True))
        self.assertTrue(member.get("/auth/me").json()["email_verified"])
        with Session(self.engine) as session:
            details = session.scalar(
                select(AccountEventORM.details).where(AccountEventORM.event_type == "email_changed")
            )
        self.assertEqual(details, {"from": "old@example.com", "to": "new@example.com", "verified": True})

    def test_change_email_code_request_rejections(self) -> None:
        self.add_user("member", email="mine@example.com")
        self.add_user("other", email="used@example.com")
        member = self.client_for("member")

        def ask(email: str):
            return member.post("/auth/me/email-code", json={"email": email, "current_password": PASSWORD})

        same = ask("MINE@example.com")
        self.assertEqual((same.status_code, same.json()["detail"]), (422, "新邮箱与当前邮箱相同"))
        taken = ask("used@example.com")
        self.assertEqual((taken.status_code, taken.json()["detail"]), (409, "邮箱已被使用"))
        self.assertEqual(self.outbox.sent, [])

    def test_change_email_code_is_bound_to_user(self) -> None:
        self.add_user("alice")
        self.add_user("bob")
        alice, bob = self.client_for("alice"), self.client_for("bob")
        alice.post("/auth/me/email-code", json={"email": "x@example.com", "current_password": PASSWORD})
        code = self.outbox.last_code()
        stolen = bob.patch("/auth/me", json={"email": "x@example.com", "current_password": PASSWORD, "code": code})
        self.assertEqual((stolen.status_code, stolen.json()["detail"]), (400, CODE_WRONG))
        self.assertIsNone(self.user("bob").email)

    def test_clear_email_needs_no_code_and_resets_verification(self) -> None:
        self.add_user("member", email="mine@example.com", verified=True)
        member = self.client_for("member")
        self.assertTrue(member.get("/auth/me").json()["email_verified"])
        cleared = member.patch("/auth/me", json={"email": None, "current_password": PASSWORD})
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual((cleared.json()["email"], cleared.json()["email_verified"]), (None, False))
        self.assertIsNone(self.user("member").email_verified_at)

    def test_admin_list_shows_email_verified(self) -> None:
        self.add_user("boss", role="admin")
        self.add_user("checked", email="checked@example.com", verified=True)
        self.add_user("unchecked", email="unchecked@example.com")
        items = self.client_for("boss").get("/admin/users", params={"status": "active"}).json()["items"]
        self.assertEqual(
            {u["username"]: u["email_verified"] for u in items},
            {"boss": False, "checked": True, "unchecked": False},
        )


class MailerTests(unittest.TestCase):
    """SMTP 发信本身：不连网，替换 smtplib.SMTP_SSL 检查调用方式。"""

    CONFIG = {"host": "smtp.qq.com", "port": 465, "username": "sender@qq.com", "password": "auth-code"}

    def test_sends_over_ssl_with_login(self) -> None:
        smtp = MagicMock()
        with patch.object(mailer, "get_smtp_config", return_value=self.CONFIG), \
                patch.object(mailer.smtplib, "SMTP_SSL", return_value=smtp) as smtp_ssl:
            mailer.send_mail(to="user@example.com", subject="【小记】邮箱验证码", body="验证码 123456")

        smtp_ssl.assert_called_once_with("smtp.qq.com", 465, timeout=10)
        conn = smtp.__enter__.return_value
        conn.login.assert_called_once_with("sender@qq.com", "auth-code")
        [message], _ = conn.send_message.call_args
        self.assertIn("sender@qq.com", message["From"])
        self.assertEqual(message["To"], "user@example.com")
        self.assertEqual(message["Subject"], "【小记】邮箱验证码")
        self.assertIn("123456", message.get_content())

    def test_smtp_errors_become_mail_error(self) -> None:
        smtp = MagicMock()
        smtp.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad auth")
        with patch.object(mailer, "get_smtp_config", return_value=self.CONFIG), \
                patch.object(mailer.smtplib, "SMTP_SSL", return_value=smtp):
            with self.assertRaises(mailer.MailError):
                mailer.send_mail(to="user@example.com", subject="s", body="b")

    def test_connection_errors_become_mail_error(self) -> None:
        with patch.object(mailer, "get_smtp_config", return_value=self.CONFIG), \
                patch.object(mailer.smtplib, "SMTP_SSL", side_effect=OSError("network unreachable")):
            with self.assertRaises(mailer.MailError):
                mailer.send_mail(to="user@example.com", subject="s", body="b")

    def test_missing_config_raises_not_configured(self) -> None:
        empty = {"host": None, "port": 465, "username": None, "password": None}
        with patch.object(mailer, "get_smtp_config", return_value=empty), \
                patch.object(mailer.smtplib, "SMTP_SSL") as smtp_ssl:
            with self.assertRaises(mailer.MailNotConfigured):
                mailer.send_mail(to="user@example.com", subject="s", body="b")
        smtp_ssl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
