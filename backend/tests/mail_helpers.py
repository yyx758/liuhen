"""测试用的发信替身：不连接真实 SMTP，把每封邮件记下来，供测试从正文里取验证码。

业务代码通过模块属性调用 `mailer.send_mail(...)`，所以替换 `mailer.send_mail` 这个属性就能截获所有发信。
（如果业务代码写成 `from mailer import send_mail`，名字在导入时就被复制走了，替换模块属性不再生效。）
"""

import re
import unittest
from unittest.mock import patch

from backend.app.services import mailer

CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")


class MailOutbox:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def __call__(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})

    def last_code(self, to: str | None = None) -> str:
        """最近一封（可指定收件人）邮件里的 6 位验证码。"""
        for mail in reversed(self.sent):
            if to is None or mail["to"] == to:
                match = CODE_PATTERN.search(mail["body"])
                if match:
                    return match.group(1)
        raise AssertionError(f"没有发给 {to or '任何人'} 的验证码邮件")


def install_outbox(test: unittest.TestCase) -> MailOutbox:
    outbox = MailOutbox()
    patcher = patch.object(mailer, "send_mail", outbox)
    patcher.start()
    test.addCleanup(patcher.stop)
    return outbox


def wrong_code(code: str) -> str:
    """和给定验证码不同的另一个 6 位数字。"""
    return f"{(int(code) + 1) % 1_000_000:06d}"
