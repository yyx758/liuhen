"""通过 SMTP（SSL）发送纯文本邮件。只负责"发出去"，验证码等业务逻辑在 email_verification_service。

调用方请写 `mailer.send_mail(...)`，不要 `from mailer import send_mail`：测试替换的是模块属性。
"""

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from backend.app.config.env import get_smtp_config

SENDER_NAME = "小记"
TIMEOUT_SECONDS = 10


class MailNotConfigured(Exception):
    """没有配置 SMTP 服务器或账号。"""


class MailError(Exception):
    """连接、认证或发送失败。"""


def send_mail(*, to: str, subject: str, body: str) -> None:
    config = get_smtp_config()
    if not (config["host"] and config["username"] and config["password"]):
        raise MailNotConfigured()

    message = EmailMessage()
    message["From"] = formataddr((SENDER_NAME, config["username"]))
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        # SMTP_SSL：连接一建立就是 TLS（465 端口），QQ 邮箱推荐的方式。
        with smtplib.SMTP_SSL(config["host"], config["port"], timeout=TIMEOUT_SECONDS) as smtp:
            smtp.login(config["username"], config["password"])
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        # OSError 覆盖连接被拒、超时、DNS 失败；SMTPException 覆盖认证失败、收件人被拒等。
        raise MailError(str(exc)) from exc
