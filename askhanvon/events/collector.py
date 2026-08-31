"""事件采集与消费（Kafka/RabbitMQ 的单体等价实现）。

- emit(): 校验 → 入 event_queue（等价 MQ produce）
- Consumer 后台线程：批量消费 → events 明细表 → 增量特征更新（等价 Flink 消费）
- 测试可用 flush_once() 同步消费。
"""
import json
import threading
from datetime import datetime, timedelta

from ..config import settings
from ..db import dumps, get_db, loads, now_iso
from ..obs.logging import get_logger, log_fields
from ..obs.metrics import metrics
from .schema import validate_event

logger = get_logger("askhanvon.events")

_consumer_thread = None
_stop_flag = threading.Event()


def emit(event: dict) -> dict:
    """埋点入口：校验失败抛 ValueError（埋点即契约）。"""
    ok, errors = validate_event(event)
    if not ok:
        raise ValueError("事件校验失败: " + "; ".join(errors))
    payload = {
        "event_type": event.get("event_type"),
        "user_id": event.get("user_id"),
        "session_id": event.get("session_id"),
        "book_id": event.get("book_id"),
        "query": event.get("query"),
        "props": event.get("props") or {},
        "ts": event.get("ts") or now_iso(),
        "client_ts": event.get("client_ts"),
    }
    get_db().event_enqueue(dumps(payload))
    metrics.inc("events_enqueued_total", {"type": payload["event_type"]})
    return payload


def _consume_one(payload: dict) -> None:
    db = get_db()
    db.event_insert(
        ts=payload["ts"],
        user_id=payload.get("user_id"),
        session_id=payload.get("session_id"),
        event_type=payload["event_type"],
        book_id=payload.get("book_id") or "",
        query=payload.get("query") or "",
        props=dumps(payload.get("props") or {}),
    )
    _incremental_features(payload)


def _incremental_features(p: dict) -> None:
    """消费侧增量特征（离线特征的实时补充；全量重算由 offline.features 承担）。"""
    db = get_db()
    et = p["event_type"]
    book_id = p.get("book_id") or ""
    user_id = p.get("user_id")
    if et in ("impression", "click", "purchase") and book_id:
        key = {"impression": "impressions", "click": "clicks", "purchase": "purchases"}[et]
        row = db.feature_book_map().get(book_id, {})
        db.feature_book_upsert(book_id, key, float(row.get(key, 0.0)) + 1)
    if et == "click" and user_id and book_id:
        # 用户侧画像增量：写 profile（偏好分布/最近阅读）
        from ..conversation.profile import ProfileService

        ProfileService().record_click(user_id, book_id)


def flush_once(limit: int = 500, lease_seconds: float = 300.0) -> int:
    """同步消费一批（测试/CLI/后台线程共用）。

    认领式消费（UPDATE...RETURNING 原子认领 + 租约回收）：
    多实例/多 worker 部署时不会重复消费同一事件（P0-2 优化项）。
    单条坏事件记录日志后标记完成，不阻断队列。
    """
    db = get_db()
    batch = db.event_claim_batch(limit, lease_seconds)
    failed: list = []
    for row in batch:
        try:
            _consume_one(loads(row["payload"], {}))
        except Exception as e:  # noqa: BLE001 — 单条坏事件不阻断队列
            log_fields(logger, 40, "event.consume_error", error=str(e)[:120])
            failed.append(row["id"])
    done = [r["id"] for r in batch if r["id"] not in failed]
    if done:
        db.event_mark_done(done)
    if failed:
        db.event_release(failed)
    metrics.inc("events_consumed_total", {}, len(done))
    return len(done)


def _worker():
    while not _stop_flag.is_set():
        try:
            n = flush_once()
            if n == 0:
                _stop_flag.wait(1.0)
        except Exception as e:  # noqa: BLE001
            log_fields(logger, 40, "event.worker_error", error=str(e)[:150])
            _stop_flag.wait(2.0)


def start_consumer() -> None:
    global _consumer_thread
    if _consumer_thread is not None and _consumer_thread.is_alive():
        return
    _stop_flag.clear()
    _consumer_thread = threading.Thread(target=_worker, name="event-consumer", daemon=True)
    _consumer_thread.start()
    log_fields(logger, 20, "event.consumer_started")


def stop_consumer() -> None:
    _stop_flag.set()


def purge_old_events(days: int = 180) -> None:
    """数据治理：清理过期明细（保留聚合特征）。"""
    db = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
