"""每日对话用量：按 UTC+08:00 自然日统计对话轮数，并在新建轮次前检查上限（PRD 第 9 节）。

一轮 = chat_turns 里一个新的 request_id；用同一 request_id 重试不新建轮次，所以不重复计数。
删除会话会删掉 chat_turns，被删的轮次保留在 deleted_chat_requests（带原创建时间），两处相加才是真实用量。
直接从这两张表统计，不另建统计表（D12）。
"""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.config.env import get_usage_config
from backend.app.models.request import ChatTurnORM, DeletedChatRequestORM
from backend.app.models.user import UserORM

DAY_SECONDS = 24 * 3600
# 自然日按 UTC+08:00 划分：时间戳加 8 小时后整除一天，得到"第几天"。
OFFSET_SECONDS = 8 * 3600
WEEK_DAYS = 7
LIMIT_REACHED = "今日对话次数已用完，明天再来"

DEFAULT_DAILY_CHAT_LIMIT = get_usage_config()["default_daily_chat_limit"]


def current_timestamp() -> int:
    """与 chat_turns.created_at 同一口径的当前时间（真实时钟，不受测评冻结时间影响）。"""
    return int(datetime.now(timezone.utc).timestamp())


def day_index(timestamp: int) -> int:
    return (timestamp + OFFSET_SECONDS) // DAY_SECONDS


def day_start(timestamp: int) -> int:
    """timestamp 所在自然日（UTC+08:00）0 点的 UTC 时间戳。"""
    return day_index(timestamp) * DAY_SECONDS - OFFSET_SECONDS


def effective_limit(role: str, daily_chat_limit: int | None) -> int | None:
    """管理员不限量（D16），返回 None；其他用户为自定义上限或默认值。"""
    if role == "admin":
        return None
    return daily_chat_limit if daily_chat_limit is not None else DEFAULT_DAILY_CHAT_LIMIT


def _day_counts(session: Session, *, since: int, user_ids: list[int] | None) -> dict[tuple[int, int], int]:
    """(user_id, 第几天) → 轮数，统计 since 之后的轮次。"""
    counts: dict[tuple[int, int], int] = {}
    for model, created in ((ChatTurnORM, ChatTurnORM.created_at), (DeletedChatRequestORM, DeletedChatRequestORM.turn_created_at)):
        day = (created + OFFSET_SECONDS) // DAY_SECONDS
        stmt = (
            select(model.user_id, day, func.count())
            .where(created >= since)
            .group_by(model.user_id, day)
        )
        if user_ids is not None:
            stmt = stmt.where(model.user_id.in_(user_ids))
        for user_id, index, count in session.execute(stmt):
            counts[(user_id, int(index))] = counts.get((user_id, int(index)), 0) + count
    return counts


def today_count(session: Session, *, user_id: int, now: int) -> int:
    since = day_start(now)
    return sum(_day_counts(session, since=since, user_ids=[user_id]).values())


def week_counts(session: Session, *, user_ids: list[int], now: int) -> dict[int, list[int]]:
    """每个用户最近 7 个自然日（含今天）的轮数，从早到晚排列，最后一项是今天。"""
    if not user_ids:
        return {}
    today = day_index(now)
    since = day_start(now) - (WEEK_DAYS - 1) * DAY_SECONDS
    counts = _day_counts(session, since=since, user_ids=user_ids)
    return {
        user_id: [counts.get((user_id, today - offset), 0) for offset in range(WEEK_DAYS - 1, -1, -1)]
        for user_id in user_ids
    }


def totals(session: Session, *, now: int) -> tuple[int, int]:
    """全部用户今天与最近 7 天的轮数合计。"""
    today = day_index(now)
    since = day_start(now) - (WEEK_DAYS - 1) * DAY_SECONDS
    counts = _day_counts(session, since=since, user_ids=None)
    return (
        sum(count for (_, day), count in counts.items() if day == today),
        sum(counts.values()),
    )


def ensure_quota(session: Session, *, user_id: int, now: int) -> None:
    """新建轮次前检查今日上限；调用方负责事务，检查与新建轮次须在同一事务。

    先锁住用户行（SELECT ... FOR UPDATE）：同一用户的并发请求在这里排队，
    后一个请求拿到锁时，前一个请求新建的轮次已经提交、能被统计到，所以并发也不能突破上限。
    """
    user = session.get(UserORM, user_id, with_for_update=True)
    if user is None:
        raise HTTPException(401, "请先登录")
    limit = effective_limit(user.role, user.daily_chat_limit)
    if limit is None:
        return
    if today_count(session, user_id=user_id, now=now) >= limit:
        raise HTTPException(429, LIMIT_REACHED)


def usage_of(session: Session, *, user_id: int, role: str, daily_chat_limit: int | None) -> tuple[int, int | None]:
    """(今天已用轮数, 生效上限)。"""
    return (
        today_count(session, user_id=user_id, now=current_timestamp()),
        effective_limit(role, daily_chat_limit),
    )
