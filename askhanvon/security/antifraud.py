"""防刷与风控（§3.4）：滑动窗口限流 + 购买频次风控 + IP 黑名单。"""
import threading
import time
from collections import deque

from ..db import get_db
from ..obs.logging import get_logger, log_fields

logger = get_logger("askhanvon.security")


class SlidingWindowLimiter:
    """进程内滑动窗口限流器（Redis TokenBucket 的单体等价实现）。"""

    def __init__(self):
        self._hits: dict = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple:
        now = time.time()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] < now - window_s:
                q.popleft()
            if len(q) >= limit:
                retry = int(q[0] + window_s - now) + 1
                return False, retry
            q.append(now)
            return True, 0


_limiter = SlidingWindowLimiter()


def rate_limit(kind: str, key: str, limit: int, window_s: float = 60.0) -> tuple:
    """返回 (allowed, retry_after_seconds)。"""
    return _limiter.allow(kind + ":" + str(key), limit, window_s)


class _FailureTracker:
    """登录失败锁定（P0-6）：同 key 窗口内失败 ≥ limit 次则锁定。只记失败，成功清零。"""

    def __init__(self):
        self._hits: dict = {}
        self._lock = threading.Lock()

    def locked(self, key: str, limit: int, window_s: float) -> tuple:
        now = time.time()
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return False, 0
            while q and q[0] < now - window_s:
                q.popleft()
            if len(q) >= limit:
                return True, int(q[0] + window_s - now) + 1
            return False, 0

    def record(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._hits.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


_failures = _FailureTracker()


def login_locked(username: str, limit: int = 5, window_s: float = 900.0) -> tuple:
    """返回 (locked, retry_after_seconds)。"""
    return _failures.locked("login:" + str(username).lower(), limit, window_s)


def login_failure_record(username: str) -> None:
    _failures.record("login:" + str(username).lower())


def login_failure_reset(username: str) -> None:
    _failures.reset("login:" + str(username).lower())


def purchase_risk_check(user_id: int) -> dict:
    """下单前风控：1 小时内订单数速度检查。"""
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3600))
    recent = get_db().count_recent_orders(user_id, since)
    flags = []
    if recent >= 5:
        flags.append("velocity_high")
    if recent >= 8:
        log_fields(logger, 40, "risk.blocked", user_id=user_id, recent_orders=recent)
        return {"allowed": False, "flags": flags, "recent_orders": recent}
    return {"allowed": True, "flags": flags, "recent_orders": recent}


def ip_blacklisted(ip: str) -> bool:
    from ..ops.strategies import strategies

    blacklist = strategies.get("security.ip_blacklist", [])
    return ip in blacklist
