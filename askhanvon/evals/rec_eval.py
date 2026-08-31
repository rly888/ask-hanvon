"""推荐评测：留出点击为真值 → NDCG@K / 命中率 / 覆盖率 / 多样性。"""
import math
from datetime import datetime, timedelta

from ..db import get_db, loads
from ..recommend.engine import get_rec_engine


def run_rec_eval(top_k: int = 6, verbose: bool = False) -> dict:
    db = get_db()
    engine = get_rec_engine()
    since = (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds")
    clicks = db.events_query(event_type="click", since=since, limit=20000)
    per_user: dict = {}
    for c in clicks:
        if c.get("user_id") and c.get("book_id"):
            per_user.setdefault(c["user_id"], []).append(c)

    users = [u for u, evs in per_user.items() if len(evs) >= 3]
    catalog_size = len(db.all_books())
    ndcgs, hits = [], []
    recommended_universe = set()
    diversity_list = []

    for u in users:
        evs = sorted(per_user[u], key=lambda e: e["id"])
        holdout = {e["book_id"] for e in evs[int(len(evs) * 0.8):]}
        if not holdout:
            continue
        items = engine.recommend(u, scene="homepage", top_k=top_k, track=False)
        ids = [i["book_id"] for i in items]
        recommended_universe.update(ids)
        diversity_list.append(len({i["category"] for i in items}) / max(1, len(items)))
        dcg = 0.0
        for rank, bid in enumerate(ids, start=1):
            if bid in holdout:
                dcg += 1.0 / math.log2(rank + 1)
        ideal = sum(1.0 / math.log2(r + 2) for r in range(min(len(holdout), top_k)))
        ndcgs.append(dcg / ideal if ideal else 0.0)
        hits.append(1.0 if set(ids) & holdout else 0.0)
        if verbose:
            print("  user " + str(u) + " ndcg=" + str(round(dcg / ideal if ideal else 0, 3)))

    metrics_out = {
        "users_evaluated": len(ndcgs),
        "ndcg_at_k": round(sum(ndcgs) / len(ndcgs), 4) if ndcgs else 0.0,
        "hit_rate_at_k": round(sum(hits) / len(hits), 4) if hits else 0.0,
        "coverage": round(len(recommended_universe) / max(1, catalog_size), 4),
        "diversity": round(sum(diversity_list) / len(diversity_list), 4)
        if diversity_list else 0.0,
        "top_k": top_k,
        "note": "样本少时指标仅作回归基线，A/B 在线 CTR 为最终标准（§3.3）",
    }
    return {"suite": "rec", "metrics": metrics_out, "details": {"ndcgs": ndcgs}}
