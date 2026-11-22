"""邮箱验证码：生成并发送（send_code）、校验并消耗（consume）。规则见 docs/prd/prd-email-verification.md E4–E8。

两个函数都在调用方的事务里运行，调用方负责 `with session.begin()`：
- send_code 先插入记录再发信；发信失败时抛出 HTTPException，调用方的事务回滚，这次的记录和"作废旧码"都不会留下。
- consume 校验失败时只会累加失败次数并返回 False，不抛异常——调用方要让事务**正常提交**，
  再在事务外抛出 400，否则失败次数跟着回滚，"输错 5 次作废"就永远不会生效。
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.app.config.clock import now_utc
from backend.app.models.email_verification import EmailVerificationCodeORM
from backend.app.services import mailer

CODE_TTL = timedelta(minutes=10)
RESEND_AFTER = timedelta(seconds=60)
HOURLY_LIMIT = 5
MAX_FAILED_ATTEMPTS = 5

CODE_WRONG = "验证码错误或已过期"
TOO_FREQUENT = "获取验证码过于频繁，请稍后再试"

PURPOSE_LABELS = {"register": "注册账号", "change_email": "绑定邮箱", "reset_password": "找回密码"}
SUBJECT = "【小记】邮箱验证码"


def code_sent_response() -> dict:
    """发码接口统一的响应；找回密码在不发信时也返回它（E13）。"""
    return {"expires_in": int(CODE_TTL.total_seconds()), "resend_after": int(RESEND_AFTER.total_seconds())}


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _same_target(purpose: str, email: str, user_id: int | None):
    """同一用途、同一邮箱、同一用户的条件；register 的 user_id 为空，要用 IS NULL 比较。"""
    owner = EmailVerificationCodeORM.user_id.is_(None) if user_id is None else EmailVerificationCodeORM.user_id == user_id
    return (EmailVerificationCodeORM.purpose == purpose, EmailVerificationCodeORM.email == email, owner)


def _rate_limited(session: Session, email: str) -> bool:
    """同一邮箱（不分用途）60 秒冷却、每小时最多 HOURLY_LIMIT 次。"""
    now = now_utc()
    latest, recent = session.execute(
        select(func.max(EmailVerificationCodeORM.created_at), func.count())
        .where(EmailVerificationCodeORM.email == email, EmailVerificationCodeORM.created_at > now - timedelta(hours=1))
    ).one()
    return recent >= HOURLY_LIMIT or (latest is not None and latest > now - RESEND_AFTER)


def send_code(
    session: Session, *, purpose: str, email: str, user_id: int | None, ip: str | None, silent: bool = False,
) -> None:
    """生成验证码、作废同一目标的旧码、保存并发信。

    频率超限时抛 429；silent=True（找回密码，E13）时改为什么都不做直接返回，不让对方从 429 推断账号存在。
    """
    # 事务级咨询锁：同一邮箱的并发请求排队执行，避免两个请求同时通过冷却检查。事务结束自动释放。
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(email))))
    if _rate_limited(session, email):
        if silent:
            return
        raise HTTPException(429, TOO_FREQUENT)

    now = now_utc()
    session.execute(
        update(EmailVerificationCodeORM)
        .where(*_same_target(purpose, email, user_id),
               EmailVerificationCodeORM.used_at.is_(None), EmailVerificationCodeORM.invalidated_at.is_(None))
        .values(invalidated_at=now)
    )
    # secrets 而不是 random：random 是可预测的伪随机数，不能用于安全场景。
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(EmailVerificationCodeORM(
        purpose=purpose, email=email, user_id=user_id, code_hash=hash_code(code),
        created_at=now, expires_at=now + CODE_TTL, ip=ip,
    ))
    session.flush()

    try:
        mailer.send_mail(to=email, subject=SUBJECT, body=_body(purpose, code))
    except mailer.MailNotConfigured as exc:
        raise HTTPException(503, "邮件服务未配置") from exc
    except mailer.MailError as exc:
        raise HTTPException(502, "验证码邮件发送失败，请稍后再试") from exc


def _body(purpose: str, code: str) -> str:
    lines = [
        f"你正在{PURPOSE_LABELS[purpose]}，验证码：{code}",
        "",
        f"验证码 {int(CODE_TTL.total_seconds() // 60)} 分钟内有效，请勿告诉他人。",
        "如果不是你本人操作，请忽略本邮件。",
    ]
    if purpose == "reset_password":
        lines.append("不输入验证码，你的密码不会被修改。")
    return "\n".join(lines)


def consume(session: Session, *, purpose: str, email: str, user_id: int | None, code: str) -> bool:
    """校验该目标最新一条有效验证码；正确则标记已使用并返回 True。

    标记"已使用"与调用方的业务写入在同一事务里：后续步骤出错回滚时，验证码也恢复可用。
    """
    now = now_utc()
    row = session.scalar(
        select(EmailVerificationCodeORM)
        .where(*_same_target(purpose, email, user_id),
               EmailVerificationCodeORM.used_at.is_(None), EmailVerificationCodeORM.invalidated_at.is_(None),
               EmailVerificationCodeORM.expires_at > now)
        .order_by(EmailVerificationCodeORM.id.desc())
        .limit(1)
        # 行锁：同一个验证码的并发提交排队，只有一个能用掉它，失败次数也不会少算。
        .with_for_update()
    )
    if row is None:
        return False
    # compare_digest 的耗时与内容无关，不会因"前几位对了"而变慢，避免计时侧信道。
    if not hmac.compare_digest(row.code_hash, hash_code(code.strip())):
        row.failed_attempts += 1
        if row.failed_attempts >= MAX_FAILED_ATTEMPTS:
            row.invalidated_at = now
        return False
    row.used_at = now
    return True
