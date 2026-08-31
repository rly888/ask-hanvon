"""精排：特征拼接 → 打分。

规则权重为默认（策略中心可配）；离线训练出 LTR 权重后由 A/B 决定启用
（rec.use_trained），实现「离线训练 → 在线打分」全链路（§3.1）。
"""
import math

from ..db import get_db, loads
from ..ops.strategies import strategies

FEATURE_NAMES = ["cf", "content", "popularity", "category_pref", "freshness", "editorial"]


def _features(book_id: str, channels: dict, book_feats: dict, cat_pref: float) -> dict:
    f = {k: 0.0 for k in FEATURE_NAMES}
    if "cf" in channels:
        f["cf"] = min(1.0, channels["cf"]["score"])
    if "content" in channels:
        f["content"] = min(1.0, channels["content"]["score"])
    clicks = float(book_feats.get("clicks", 0.0))
    impressions = float(book_feats.get("impressions", 0.0))
    f["popularity"] = math.log1p(clicks) / math.log1p(max(clicks, 50.0))
    f["category_pref"] = cat_pref
    f["freshness"] = 0.0  # 样书无时间维度；接入新书入库时间后启用
    if "editorial" in channels:
        f["editorial"] = min(1.0, channels["editorial"]["score"] / 3.0)
    if impressions >= 5 and clicks > 0:
        f["popularity"] = 0.5 * f["popularity"] + 0.5 * (clicks / impressions)
    return f


def rank_candidates(candidates: dict, user_id) -> list:
    """返回 [{book_id, score, features, channels}]，按分降序。"""
    db = get_db()
    weights = strategies.get("rec.feature_weights", {})
    use_trained = bool(strategies.get("rec.use_trained", False))
    model = db.rec_model_latest("rank") if use_trained else None
    if model:
        artifact = loads(model["artifact"], {})
        weights = dict(zip(artifact.get("feature_names", FEATURE_NAMES),
                           artifact.get("weights", [1.0] * len(FEATURE_NAMES))))
        bias = float(artifact.get("bias", 0.0))
    else:
        bias = 0.0

    book_feats_all = db.feature_book_map()
    user_feats = db.feature_user_map(user_id) if user_id else {}
    _, cat_dist = user_feats.get("cat_click", (0.0, {}))
    cat_dist = cat_dist or {}
    catalog = {b["id"]: b for b in db.all_books()}

    scored = []
    for book_id, entry in candidates.items():
        book = catalog.get(book_id)
        if not book:
            continue
        cat_pref = 0.0
        if cat_dist:
            total = sum(cat_dist.values()) or 1.0
            cat_pref = cat_dist.get(book.get("category") or "未分类", 0.0) / total
        feats = _features(book_id, entry["channels"], book_feats_all.get(book_id, {}), cat_pref)
        score = bias + sum(float(weights.get(k, 0.0)) * v for k, v in feats.items())
        scored.append(
            {
                "book_id": book_id,
                "score": round(score, 4),
                "features": {k: round(v, 4) for k, v in feats.items()},
                "channels": entry["channels"],
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
