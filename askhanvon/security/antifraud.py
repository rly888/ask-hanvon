"""防刷与风控（§3.4）：滑动窗口限流 + 购买频次风控 + IP 黑名单。"""
import threading
import time
from collections import deque

from ..config import settings
from ..db import get_db
from ..obs.logging import get_logger, log_fields

logger = get_logger("askhanvon.security")


class SlidingWindowLimiter:
    """限流三级纵深（问题 3 修复）：

    1. Redis SortedSet（多 worker 原子共享）—— 正常路径；
    2. DB 原子计数（rate_counter 表，窗口 id 为桶）—— Redis 故障时兜底，
       跨 worker 依然严格（不会"每 worker 各算一份"）；
    3. 进程内 deque —— DB 也故障时的最后防线（单 worker 语义正确）。
    """

    def __init__(self, db_fallback: bool | None = None):
        self._hits: dict = {}
        self._lock = threading.Lock()
        self._redis = None
        self._db_ticks = 0
        # DB 兜底：PG 模式自动启用（多 worker 部署形态必配 PG+Redis）；
        # 测试可显式强制；SQLite 单体默认进程内（单进程语义即正确）
        if db_fallback is None:
            db_fallback = (settings.db_engine == "postgres")
        self._db_enabled = db_fallback

    def _client(self):
        if self._redis is None and settings.redis_url:
            import redis as _redis

            self._redis = _redis.Redis.from_url(settings.redis_url, decode_responses=True)
        return self._redis

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple:
        now = time.time()
        redis = self._client()
        if redis is not None:
            try:
                return self._allow_redis(redis, key, limit, window_s, now)
            except Exception:  # noqa: BLE001 — Redis 故障 → 降级 DB 兜底
                self._redis = None
        db_result = None
        if self._db_enabled:
            db_result = self._allow_db(key, limit, window_s, now)
        if db_result is not None:
            return db_result
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] < now - window_s:
                q.popleft()
            if len(q) >= limit:
                retry = int(q[0] + window_s - now) + 1
                return False, retry
            q.append(now)
            return True, 0

    def _allow_redis(self, redis, key, limit, window_s, now) -> tuple:
        rkey = "rate:" + str(key)
        pipe = redis.pipeline()
        pipe.zremrangebyscore(rkey, 0, now - window_s)
        pipe.zadd(rkey, {str(now): now})
        pipe.zcard(rkey)
        pipe.expire(rkey, int(window_s) + 10)
        results = pipe.execute()
        count = int(results[2])
        if count > limit:
            pipe2 = redis.pipeline()
            pipe2.zremrangebyscore(rkey, 0, now - window_s)
            pipe2.zrange(rkey, 0, 0, withscores=True)
            oldest = pipe2.execute()[1]
            retry = int(oldest[0][1] + window_s - now) + 1 if oldest else 1
            return False, retry
        return True, 0

    def _allow_db(self, key: str, limit: int, window_s: float, now):
        """DB 原子计数兜底：窗口 id = 时间桶，跨进程严格共享总限。"""
        try:
            from ..db import get_db

            window_id = str(int(now // window_s))
            ok = get_db().rate_bump("rl:" + str(key), window_id, limit)
            self._db_ticks += 1
            if self._db_ticks % 200 == 0:
                try:
                    get_db().rate_purge(str(int((now - 7200) // window_s)))
                except Exception:  # noqa: BLE001
                    pass
            if ok:
                return True, 0
            return False, int(window_s - (now % window_s)) + 1
        except Exception:  # noqa: BLE001 — DB 也故障 → 返回 None 走进程内
            return None


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
