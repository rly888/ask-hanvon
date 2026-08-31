"""离线训练：精排 LTR（逻辑回归）+ item-item 协同过滤 → rec_models。

精排：样本 = (用户, 书) 曝光对，标签 = 是否点击；特征与在线打分同源
（同 rank.FEATURE_NAMES，保证离线/在线一致性）；按时间切分 80/20，
产出 AUC/NDCG 指标入模型库，供评测门禁与 A/B 晋升使用。
"""
import math
from datetime import timedelta, datetime

import numpy as np

from ..db import dumps, get_db, loads
from ..obs.logging import get_logger, log_fields
from ..recommend.rank import FEATURE_NAMES

logger = get_logger("askhanvon.offline")

CF_TOPK = 50
EPOCHS = 400
LR = 0.12
L2 = 0.01


def _build_pairs(events: list, catalog: dict):
    """曝光→标签对；标签 = 曝光后 7 天内是否点击（时间窗，防泄漏）。"""
    from datetime import datetime as _dt, timedelta as _td

    book_cat = {b["id"]: (b.get("category") or "未分类") for b in catalog.values()}
    click_ts: dict = {}
    impressions: list = []
    for e in events:
        uid, bid = e.get("user_id"), e.get("book_id") or ""
        if not uid or not bid or bid not in book_cat:
            continue
        if e["event_type"] == "click":
            click_ts.setdefault((uid, bid), []).append(e["ts"])
        elif e["event_type"] == "impression":
            impressions.append((e["ts"], uid, bid))
    impressions.sort(key=lambda x: x[0])

    window = _td(days=7)

    def _label(uid, bid, ts):
        t0 = _dt.fromisoformat(ts)
        for ct in click_ts.get((uid, bid), []):
            if t0 <= _dt.fromisoformat(ct) <= t0 + window:
                return 1.0
        return 0.0

    split = int(len(impressions) * 0.8)
    train_impr, test_impr = impressions[:split], impressions[split:]
    labels = {}
    for (ts, uid, bid) in impressions:
        labels[(ts, uid, bid)] = _label(uid, bid, ts)
    boundary = train_impr[-1][0] if train_impr else ""
    return train_impr, test_impr, labels, book_cat, boundary


def _feature_vec(uid, bid, clicks_in_train, book_pop, book_cat, cf_val: float = 0.0):
    cat_pref = 0.0
    user_cats: dict = {}
    for (u, b) in clicks_in_train:
        if u == uid:
            c = book_cat.get(b, "未分类")
            user_cats[c] = user_cats.get(c, 0) + 1
    total = sum(user_cats.values())
    if total:
        cat_pref = user_cats.get(book_cat.get(bid, "未分类"), 0) / total
    pop_max = max(list(book_pop.values()) or [1.0])
    return [
        min(1.0, float(cf_val)),
        cat_pref,
        math.log1p(book_pop.get(bid, 0.0)) / math.log1p(max(pop_max, 1.0)),
        cat_pref,
        0.0,
        0.0,
    ]


def _auc(scores: list, labels: list) -> float:
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return 0.0
    import random

    random.seed(7)
    wins = 0.0
    for _ in range(4000):
        p = pos[random.randrange(len(pos))]
        n = neg[random.randrange(len(neg))]
        wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return wins / 4000.0


def train_rank(events: list, catalog: list) -> dict:
    db = get_db()
    catalog_map = {b["id"]: b for b in catalog}
    train_impr, test_impr, labels, book_cat, boundary = _build_pairs(events, catalog_map)

    # 训练期点击（用于 CF / 流行度 / 用户偏好特征，不包含验证期）
    clicks_train = set()
    for (ts, uid, bid) in train_impr:
        if labels[(ts, uid, bid)] == 1.0:
            clicks_train.add((uid, bid))
    pop: dict = {}
    for (u, b) in clicks_train:
        pop[b] = pop.get(b, 0) + 1

    # 简化 CF 特征：共现（训练窗口内同用户点击对）
    user_books: dict = {}
    for (u, b) in clicks_train:
        user_books.setdefault(u, set()).add(b)
    co: dict = {}
    for u, bs in user_books.items():
        bs = list(bs)
        for i in range(len(bs)):
            for j in range(len(bs)):
                if i != j:
                    co[(bs[i], bs[j])] = co.get((bs[i], bs[j]), 0) + 1

    def cf_of(uid, bid):
        seeds = user_books.get(uid, set())
        if not seeds:
            return 0.0
        vals = [co.get((s, bid), 0) for s in seeds]
        mx = max(vals) if vals else 0
        return min(1.0, mx / 5.0)

    X, y = [], []
    for (ts, uid, bid) in train_impr:
        X.append(_feature_vec(uid, bid, clicks_train, pop, book_cat,
                              cf_val=cf_of(uid, bid)))
        y.append(labels[(ts, uid, bid)])
    if not X or sum(y) == 0:
        log_fields(logger, 30, "train.skipped", reason="无有效样本")
        return {"skipped": True}

    Xa = np.array(X, dtype=np.float64)
    ya = np.array(y)
    w = np.zeros(len(FEATURE_NAMES))
    b = 0.0
    for _ in range(EPOCHS):
        z = Xa @ w + b
        p = 1.0 / (1.0 + np.exp(-z))
        gw = Xa.T @ (p - ya) / len(ya) + L2 * w
        gb = float(np.mean(p - ya))
        w -= LR * gw
        b -= LR * gb

    # 验证
    Xt, yt = [], []
    for (ts, uid, bid) in test_impr:
        Xt.append(_feature_vec(uid, bid, clicks_train, pop, book_cat,
                               cf_val=cf_of(uid, bid)))
        yt.append(labels[(ts, uid, bid)])
    metrics_out = {"n_train": len(y), "n_test": len(yt),
                   "pos_ratio": round(float(np.mean(ya)), 4)}
    if Xt:
        Xta = np.array(Xt)
        scores = list(Xta @ w + b)
        metrics_out["auc"] = round(_auc(scores, yt), 4)
        metrics_out["ndcg10"] = _ndcg(Xta, yt, w, b)
    db.rec_model_save(
        kind="rank",
        version="v1",
        artifact=dumps({"feature_names": FEATURE_NAMES, "weights": w.tolist(), "bias": b}),
        metrics_json=dumps(metrics_out),
    )
    log_fields(logger, 20, "train.rank_done", **metrics_out)
    return metrics_out


def _ndcg(X: np.ndarray, y: list, w: np.ndarray, b: float, k: int = 10) -> float:
    scores = X @ w + b
    order = np.argsort(-scores)[:k]
    dcg = sum(y[i] / math.log2(idx + 2) for idx, i in enumerate(order))
    n_pos = min(sum(1 for v in y if v > 0), k)
    idcg = sum(1 / math.log2(i + 2) for i in range(n_pos)) if n_pos else 0.0
    return round(dcg / idcg, 4) if idcg else 0.0


def train_cf(events: list, catalog: list) -> dict:
    """item-item 协同：共现点击 → 余弦归一 → top50 邻居。"""
    db = get_db()
    user_books: dict = {}
    for e in events:
        if e["event_type"] == "click" and e.get("user_id") and e.get("book_id"):
            user_books.setdefault(e["user_id"], set()).add(e["book_id"])
    co: dict = {}
    item_cnt: dict = {}
    valid = {b["id"] for b in catalog}
    for u, bs in user_books.items():
        bs = [b for b in bs if b in valid]
        for b in bs:
            item_cnt[b] = item_cnt.get(b, 0) + 1
        for i in range(len(bs)):
            for j in range(len(bs)):
                if i != j:
                    co[(bs[i], bs[j])] = co.get((bs[i], bs[j]), 0) + 1
    table: dict = {}
    for (a, b), c in co.items():
        denom = math.sqrt(item_cnt.get(a, 1) * item_cnt.get(b, 1))
        table.setdefault(a, {})[b] = round(c / denom, 4)
    for a in table:
        neighbors = sorted(table[a].items(), key=lambda x: x[1], reverse=True)[:CF_TOPK]
        table[a] = dict(neighbors)
    db.rec_model_save(
        kind="cf",
        version="v1",
        artifact=dumps(table),
        metrics_json=dumps({"items": len(table), "pairs": len(co)}),
    )
    log_fields(logger, 20, "train.cf_done", items=len(table))
    return {"items": len(table), "pairs": len(co)}


def train_all() -> dict:
    db = get_db()
    events = db.events_query(limit=200000)
    catalog = db.all_books()
    rank_m = train_rank(events, catalog)
    cf_m = train_cf(events, catalog)
    return {"rank": rank_m, "cf": cf_m}


def precompute_candidates(k: int = 200) -> dict:
    """召回候选集预计算：活跃用户的每路候选离线算好落库。"""
    from ..recommend.recall import recall_candidates

    db = get_db()
    since = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    active = {e["user_id"] for e in db.events_query(since=since, limit=50000) if e.get("user_id")}
    n = 0
    for uid in active:
        candidates = recall_candidates(uid, "homepage", k_per_channel=k)
        ranked = sorted(
            candidates.items(),
            key=lambda kv: max((c["score"] for c in kv[1]["channels"].values()), default=0),
            reverse=True,
        )[:k]
        if ranked:
            db.rec_candidates_save(
                uid, "precomputed_v1",
                dumps([bid for bid, _ in ranked]),
                dumps({bid: round(max((c["score"] for c in e["channels"].values()), default=0), 4)
                        for bid, e in ranked}),
            )
            n += 1
    return {"users": n}
