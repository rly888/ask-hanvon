"""特征平台：行为 → 特征（§3.1 离线链路起步）。

特征注册表 = 文档 + 血缘：每个特征写明来源事件与口径，离线/在线同源
（在线增量更新见 events.collector._incremental_features，全量重算在本模块）。
"""
from ..db import dumps, get_db, loads
from ..obs.logging import get_logger, log_fields

logger = get_logger("askhanvon.offline")

FEATURE_REGISTRY = {
    "user.cat_click": "来源: click 事件 × 书目分类 → {分类: 次数} 偏好分布",
    "user.recent_books": "来源: click 事件时间序 → 最近点击书目（去重，最多 10 本）",
    "user.total_clicks": "来源: click 事件计数",
    "user.read_minutes": "来源: read_duration 事件秒数累加 / 60",
    "book.clicks": "来源: click 事件计数",
    "book.impressions": "来源: impression 事件计数",
    "book.purchases": "来源: purchase 事件计数",
    "book.ctr": "派生: clicks / max(impressions, 1)",
    "book.popularity": "派生: ln(1 + clicks)",
    "book.collects": "来源: collect 事件计数",
}


def recompute_features() -> dict:
    """全量重算：events 明细 → features_user / features_book。"""
    db = get_db()
    events = db.events_query(limit=100000)
    user_cat: dict = {}
    user_recent: dict = {}
    user_clicks: dict = {}
    user_read: dict = {}
    book_clicks: dict = {}
    book_impr: dict = {}
    book_purch: dict = {}
    book_coll: dict = {}
    book_cat: dict = {}
    for b in db.all_books():
        book_cat[b["id"]] = b.get("category") or "未分类"
    for e in events:
        et = e["event_type"]
        uid = e.get("user_id")
        bid = e.get("book_id") or ""
        if et == "click":
            if uid:
                user_clicks[uid] = user_clicks.get(uid, 0) + 1
                if bid:
                    user_cat.setdefault(uid, {})
                    cat = book_cat.get(bid, "未分类")
                    user_cat[uid][cat] = user_cat[uid].get(cat, 0) + 1
                    lst = user_recent.setdefault(uid, [])
                    if bid in lst:
                        lst.remove(bid)
                    lst.insert(0, bid)
                    del lst[10:]
            if bid:
                book_clicks[bid] = book_clicks.get(bid, 0) + 1
        elif et == "impression" and bid:
            book_impr[bid] = book_impr.get(bid, 0) + 1
        elif et == "purchase" and bid:
            book_purch[bid] = book_purch.get(bid, 0) + 1
        elif et == "collect" and bid:
            book_coll[bid] = book_coll.get(bid, 0) + 1
        elif et == "read_duration" and uid:
            props = loads(e.get("props"), {}) or {}
            user_read[uid] = user_read.get(uid, 0.0) + float(props.get("seconds", 0)) / 60.0

    n_user = n_book = 0
    for uid, dist in user_cat.items():
        db.feature_user_upsert(uid, "cat_click", float(sum(dist.values())), dumps(dist))
        n_user += 1
    for uid, lst in user_recent.items():
        db.feature_user_upsert(uid, "recent_books", float(len(lst)), dumps(lst))
    for uid, n in user_clicks.items():
        db.feature_user_upsert(uid, "total_clicks", float(n))
    for uid, mins in user_read.items():
        db.feature_user_upsert(uid, "read_minutes", round(mins, 1))
    for bid in set(list(book_clicks) + list(book_impr) + list(book_purch) + list(book_coll)):
        clicks = book_clicks.get(bid, 0)
        impr = book_impr.get(bid, 0)
        import math

        db.feature_book_upsert(bid, "clicks", float(clicks))
        db.feature_book_upsert(bid, "impressions", float(impr))
        db.feature_book_upsert(bid, "purchases", float(book_purch.get(bid, 0)))
        db.feature_book_upsert(bid, "collects", float(book_coll.get(bid, 0)))
        db.feature_book_upsert(bid, "ctr", round(clicks / max(impr, 1), 4))
        db.feature_book_upsert(bid, "popularity", round(math.log1p(clicks), 4))
        n_book += 1
    log_fields(logger, 20, "features.recomputed", users=n_user, books=n_book)
    return {"users": n_user, "books": n_book, "events": len(events)}
