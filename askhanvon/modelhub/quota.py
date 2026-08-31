"""配额与预算控制（模型治理 §3.2：按用户/日配额，超限降级）。"""
from datetime import date

from ..config import settings
from ..db import get_db
from ..obs.logging import get_logger, log_fields

logger = get_logger("askhanvon.modelhub.quota")


class QuotaExceeded(Exception):
    """超出当日配额——调用方应走降级链路而非报错。"""


ANON_USER_ID = 0  # 匿名请求共享一个较紧的配额桶
EVAL_USER_ID = -1  # 系统评测专用：不受用户配额限制（受全局模型层审计监督）
ANON_CALLS_PER_DAY = 60


def estimate_tokens(text: str) -> int:
    """中文/混合文本的轻量 token 估算（≈2 字符/token）。"""
    return max(1, (len(text or "") + 1) // 2)


def check_quota(user_id) -> None:
    """调用前检查当日配额，超限抛 QuotaExceeded。"""
    if user_id == EVAL_USER_ID:
        return  # 系统评测豁免
    uid = user_id if user_id else ANON_USER_ID
    day = date.today().isoformat()
    row = get_db().mh_quota_get(day, uid)
    calls_limit = settings.quota_llm_calls_per_user_day
    tokens_limit = settings.quota_llm_tokens_per_user_day
    if uid == ANON_USER_ID:
        calls_limit = min(calls_limit, ANON_CALLS_PER_DAY)
    if row:
        if row["calls"] >= calls_limit or row["tokens"] >= tokens_limit:
            log_fields(
                logger, 30, "quota.exceeded", user_id=uid, calls=row["calls"],
                tokens=row["tokens"],
            )
            raise QuotaExceeded(f"当日配额已用尽: calls={row['calls']}/{calls_limit}")


def quota_usage(user_id) -> dict:
    uid = user_id if user_id else ANON_USER_ID
    day = date.today().isoformat()
    row = get_db().mh_quota_get(day, uid) or {"calls": 0, "tokens": 0, "cost": 0.0}
    calls_limit = settings.quota_llm_calls_per_user_day
    if uid == ANON_USER_ID:
        calls_limit = min(calls_limit, ANON_CALLS_PER_DAY)
    used_ratio = row["calls"] / calls_limit if calls_limit else 0
    if used_ratio >= 0.8:
        log_fields(logger, 30, "quota.budget_alert", user_id=uid, used_ratio=round(used_ratio, 2))
    return {
        "day": day,
        "calls": row["calls"],
        "tokens": row["tokens"],
        "cost": round(row["cost"], 4),
        "calls_limit": calls_limit,
        "tokens_limit": tokens_limit,
    }
