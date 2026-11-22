"""进程内的登录限流。单实例部署足够；多实例时需改为共享存储（如数据库或 Redis）。"""

import time
from collections import deque
from collections.abc import Callable
from threading import Lock


class SlidingWindowLimiter:
    """同一个 key 在 window_seconds 内最多 limit 次。"""

    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        """记录一次尝试；超过上限时返回 False（这次不计入）。"""
        now = self.clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - self.window}
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class FailureLock:
    """连续失败 max_failures 次后锁定 lock_seconds；成功一次即清零。"""

    def __init__(self, max_failures: int, lock_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self.clock = clock
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = Lock()

    def is_locked(self, key: str) -> bool:
        with self._lock:
            until = self._locked_until.get(key)
            if until is None:
                return False
            if until > self.clock():
                return True
            del self._locked_until[key]
            return False

    def record_failure(self, key: str) -> bool:
        """记录一次失败；这次失败导致锁定时返回 True。"""
        with self._lock:
            count = self._failures.get(key, 0) + 1
            if count >= self.max_failures:
                self._locked_until[key] = self.clock() + self.lock_seconds
                self._failures.pop(key, None)
                return True
            self._failures[key] = count
            return False

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()


# 登录：同一 IP 每分钟 10 次；同一账号连续失败 5 次锁定 15 分钟。注册：同一 IP 每小时 5 次（PRD 7.5）。
login_ip_limiter = SlidingWindowLimiter(limit=10, window_seconds=60)
login_failure_lock = FailureLock(max_failures=5, lock_seconds=15 * 60)
register_ip_limiter = SlidingWindowLimiter(limit=5, window_seconds=3600)
# 获取邮箱验证码：同一 IP 每小时 10 次（邮箱验证 PRD E5）。按邮箱的冷却与每小时上限在数据库里统计。
email_code_ip_limiter = SlidingWindowLimiter(limit=10, window_seconds=3600)
