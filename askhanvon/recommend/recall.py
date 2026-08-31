"""多路召回：编辑位 / 协同过滤 / 内容 / 热门 / 分类偏好。

冷启动策略（§7 风险）：图书无行为数据是天然难点 → 新用户靠编辑位 + 热门 +
分类兜底；行为丰富后 CF/内容通道权重自然上升。
"""
import math

from ..db import get_db, loads


def _user_recent_books(user_id, limit: int = 10) -> list:
    if not user_id:
        return []
    rows = get_db().events_query(event_type="click", user_id=user_id, limit=200)
    seen = []
    for r in reversed(rows):  # 时间正序
        bid = r.get("book_id")
        if bid and bid not in seen:
            seen.append(bid)
    return seen[-limit:][::-1]


def recall_candidates(user_id, scene: str, k_per_channel: int = 20) -> dict:
    """返回 {book_id: {"channels": {channel: {"score": s, "meta": {...}}}}}。"""
    db = get_db()
    catalog = {b["id"]: b for b in db.all_books()}
    out: dict = {}

    def add(book_id, channel, score, meta):
        if book_id not in catalog:
            return
        entry = out.setdefault(book_id, {"channels": {}})
        entry["channels"][channel] = {"score": float(score), "meta": meta}

    # 1) 编辑位：Campaign + 优先图书
    from ..ops.campaigns import campaigns as campaign_svc

    for bid, name, weight in campaign_svc.active_for_slot(scene):
        add(bid, "editorial", weight, {"campaign": name})
    for p in campaign_svc.priority_books(scene):
        add(p["book_id"], "editorial", p["weight"], {"reason": p["reason"] or "编辑推荐"})

    # 2) 协同过滤：读过 X 的读者也在读（离线训练的 item-item CF）
    cf_model = db.rec_model_latest("cf")
    recent = _user_recent_books(user_id)
    if cf_model and recent:
        table = loads(cf_model["artifact"], {}) or {}
        for i, seed in enumerate(recent[:5]):
            for nb, score in (table.get(seed) or {}).items():
                if nb == seed:
                    continue
                decay = 0.85 ** i
                add(nb, "cf", float(score) * decay, {"seed_book": seed})

    # 3) 内容通道：用户分类偏好 × 书目分类
    feats = db.feature_user_map(user_id) if user_id else {}
    _, cat_dist = feats.get("cat_click", (0.0, {}))
    cat_dist = cat_dist or {}
    if cat_dist:
        max_c = max(cat_dist.values()) or 1.0
        for bid, b in catalog.items():
            pref = cat_dist.get(b.get("category") or "未分类", 0.0) / max_c
            if pref > 0:
                add(bid, "content", pref, {"category": b.get("category")})

    # 4) 热门通道：features_book.popularity
    book_feats = db.feature_book_map()
    pop_list = []
    for bid in catalog:
        clicks = float(book_feats.get(bid, {}).get("clicks", 0.0))
        pop_list.append((bid, math.log1p(clicks)))
    pop_list.sort(key=lambda x: x[1], reverse=True)
    for rank, (bid, s) in enumerate(pop_list[:k_per_channel]):
        add(bid, "popular", max(s, 0.1), {"rank": rank + 1})

    # 5) 分类兜底（匿名/新用户主力通道）：全书按分类轮转
    for i, (bid, b) in enumerate(sorted(catalog.items())):
        add(bid, "category", 1.0 / (i + 1), {"category": b.get("category")})

    return out
