"""业务规则重排：多样性约束、优先位保障、曝光去重、长尾/探索位（运营策略落地点）。"""
import hashlib
from datetime import datetime, timedelta

from ..db import get_db
from ..ops.strategies import strategies


def _recent_exposure_counts(user_id, days: int = 7) -> dict:
    """近 N 天每本书的曝光次数（P3-1 曝光去重）。"""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = get_db().events_query(event_type="impression", since=since,
                                 user_id=user_id, limit=3000)
    counts: dict = {}
    for r in rows:
        bid = r.get("book_id")
        if bid:
            counts[bid] = counts.get(bid, 0) + 1
    return counts


def _mmr_select(ranked: list, catalog: dict, top_k: int, lam: float,
                exclude_ids: set = None) -> list:
    """MMR 选择（P3-2）：λ·相关度 − (1−λ)·最大已选相似度，控制同质化。

    相似度用本地向量（书名+简介），零 API 成本；rec.mmr_lambda=0 时走原装配。
    """
    exclude_ids = exclude_ids or set()
    from ..config import settings
    from ..nlp import get_local_embedder

    emb = get_local_embedder(settings.embed_dim)

    def _vec(item_id: str):
        b = catalog.get(item_id, {})
        return emb.embed_one(str(b.get("title", "")) + str(b.get("description", ""))
                             + str(b.get("category", "")))

    pool = [i for i in ranked if i["book_id"] not in exclude_ids]
    if not pool:
        return []
    selected: list = []
    candidates = [(i, _vec(i["book_id"])) for i in pool]
    for _ in range(min(top_k, len(pool))):
        if not candidates:
            break
        best_i, best_score = None, -1e9
        for i, v in candidates:
            sim = 0.0
            if selected:
                sim = max(float(v @ sv) for _, sv in selected)
            score = lam * float(i["score"]) - (1.0 - lam) * sim
            if score > best_score:
                best_i, best_score = i, score
        out = dict(best_i)
        out["rules_applied"] = (out.get("rules_applied", []) + ["mmr"])[:3]
        selected.append((out, _vec(best_i["book_id"])))
    return [i for i, _ in selected]


def apply_rules(ranked: list, top_k: int = 6, user_id=None) -> list:
    """输入精排结果，输出最终列表（每项带 rules_applied 标记，规则命中 100% 可追踪）。"""
    db_mod = get_db()
    catalog = {b["id"]: b for b in db_mod.all_books()}
    max_per_cat = int(strategies.get("rec.diversity_max_per_category", 2))
    longtail_on = bool(strategies.get("rec.longtail_enabled", True))
    dedup_max = int(strategies.get("rec.exposure_dedup_max", 3))

    priority_ids = []
    from ..ops.campaigns import campaigns as campaign_svc

    for p in campaign_svc.priority_books("homepage")[:2]:
        priority_ids.append(p["book_id"])

    # 0) 曝光去重（P3-1）：近 7 天曝光 ≥ N 次的书延后（不剔除，列表长度不变；
    #    优先位书与长尾池不受影响）
    heavy: list = []
    if user_id and dedup_max > 0:
        counts = _recent_exposure_counts(user_id)
        fresh, heavy = [], []
        for i in ranked:
            if counts.get(i["book_id"], 0) >= dedup_max and i["book_id"] not in priority_ids:
                heavy.append(i)
            else:
                fresh.append(i)
        ranked = fresh

    final: list = []
    cat_count: dict = {}
    seen = set()

    # 0.5) MMR 装配（P3-2）：λ>0 时替代类目多样性装配，控制内容同质化
    mmr_lambda = float(strategies.get("rec.mmr_lambda", 0.0))
    if mmr_lambda > 0.0:
        mmr_pool = [i for i in ranked if i["book_id"] not in priority_ids]
        final = _mmr_select(mmr_pool, catalog, max(0, top_k - len(priority_ids)),
                            lam=mmr_lambda)
        seen = {i["book_id"] for i in final}

    # 1) 优先位保障（最多 2 个，插在最前）
    for pid in priority_ids:
        for item in ranked:
            if item["book_id"] == pid and pid not in seen:
                item = dict(item)
                item["rules_applied"] = ["priority_boost"]
                final.append(item)
                seen.add(pid)
                cat = catalog.get(pid, {}).get("category") or "未分类"
                cat_count[cat] = cat_count.get(cat, 0) + 1
                break

    # 2) 主序列 + 类目多样性
    for item in ranked:
        if len(final) >= top_k:
            break
        bid = item["book_id"]
        if bid in seen:
            continue
        cat = catalog.get(bid, {}).get("category") or "未分类"
        if cat_count.get(cat, 0) >= max_per_cat:
            item = dict(item)
            item["rules_applied"] = ["diversity_deferred"]
            continue
        item = dict(item)
        item["rules_applied"] = item.get("rules_applied", [])
        final.append(item)
        seen.add(bid)
        cat_count[cat] = cat_count.get(cat, 0) + 1

    # 3) 被多样性延迟的项回填，再回填被曝光去重延后的项
    heavy_ids = {i["book_id"] for i in heavy}
    if len(final) < top_k:
        for item in ranked + heavy:
            if len(final) >= top_k:
                break
            if item["book_id"] not in seen:
                item = dict(item)
                item["rules_applied"] = (
                    ["exposure_dedup_deferred"]
                    if item["book_id"] in heavy_ids else ["diversity_backfill"]
                )
                final.append(item)
                seen.add(item["book_id"])

    # 4) 尾部探索位：长尾池按 ε-greedy 抽样（P3-1）——
    #    以 (user, 日期) 为种子的确定性随机：同用户当日结果稳定，跨天有探索性
    if longtail_on and len(final) >= top_k:
        feats = db_mod.feature_book_map()
        tail_pool = [
            bid for bid, b in catalog.items()
            if bid not in seen and float(feats.get(bid, {}).get("clicks", 0.0)) < 3
        ]
        if tail_pool:
            seed_material = (str(user_id) + datetime.now().date().isoformat()).encode("utf-8")
            seed_bytes = hashlib.blake2b(seed_material, digest_size=8).digest()
            seed_int = int.from_bytes(seed_bytes, "little")
            epsilon = float(strategies.get("rec.explore_epsilon", 0.25))
            if seed_int / 2.0 ** 64 < epsilon:
                pick, rule = tail_pool[seed_int % len(tail_pool)], "explore_slot"
            else:
                pick, rule = tail_pool[0], "longtail_slot"
            tail = next((i for i in ranked if i["book_id"] == pick),
                        {"book_id": pick, "score": 0.0, "features": {}, "channels": {}})
            final[-1] = {
                **tail,
                "rules_applied": [rule],
            }
            seen.add(pick)
    return final[:top_k]
