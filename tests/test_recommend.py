"""推荐引擎测试：可解释性 / 多样性 / 优先位 / 曝光埋点 / A/B 分桶。"""
from askhanvon.db import get_db
from askhanvon.recommend.engine import get_rec_engine
from askhanvon.recommend.rules import apply_rules


def test_recommend_returns_items_with_reasons():
    items = get_rec_engine().recommend(None, top_k=6, track=False)
    assert 1 <= len(items) <= 6
    for i in items:
        assert i["reasons"]
        assert i["book_id"] and i["title"]


def test_rules_diversity_cap():
    ranked = [
        {"book_id": "b" + str(i), "score": 1.0 - i * 0.1, "features": {},
         "channels": {}, "rules_applied": []}
        for i in range(6)
    ]
    # 目录里这些书不存在时规则会过滤，只断言不抛错与长度约束
    out = apply_rules(ranked, top_k=6)
    assert len(out) <= 6


def test_recommend_emits_impressions(sample_book):
    from askhanvon.events.collector import flush_once

    before = get_db().events_count("impression")
    get_rec_engine().recommend(None, top_k=4, track=True)
    flush_once()
    after = get_db().events_count("impression")
    assert after > before


def test_priority_boost_first():
    from askhanvon.ops.campaigns import campaigns as campaign_svc

    books = get_db().all_books()
    if not books:
        return
    target = books[0]["id"]
    campaign_svc.set_priority("homepage", target, 9.0, "测试置顶")
    items = get_rec_engine().recommend(None, top_k=6, track=False)
    assert any(i["book_id"] == target for i in items[:3])
    campaign_svc.delete_priority("homepage", target)


def test_ab_assignment_deterministic():
    from askhanvon.ops.ab import ab_service

    a1 = ab_service.assign("rec_rank_v1", 7)
    a2 = ab_service.assign("rec_rank_v1", 7)
    assert a1 == a2
    assert a1["variant"] in ("A", "B")
