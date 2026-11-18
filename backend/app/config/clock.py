"""Business clock; evaluations can freeze it for one async execution context."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterator


_evaluation_now: ContextVar[datetime | None] = ContextVar("evaluation_now", default=None)


def now_utc() -> datetime:
    return _evaluation_now.get() or datetime.now(timezone.utc)


@contextmanager
def fixed_now(value: datetime) -> Iterator[None]:
    if value.utcoffset() is None:
        raise ValueError("测评时间必须包含时区")
    token = _evaluation_now.set(value.astimezone(timezone.utc))
    try:
        yield
    finally:
        _evaluation_now.reset(token)
