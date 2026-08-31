"""埋点 → 消费 → 特征 → 训练 的离线链路测试。"""
from askhanvon.db import get_db
from askhanvon.events import collector
from askhanvon.events.schema import validate_event


def test_event_schema_validation():
    ok, _ = validate_event({"event_type": "click", "book_id": "b1"})
    assert ok
    ok2, errors = validate_event({"event_type": "click"})
    assert not ok2 and errors
    ok3, _ = validate_event({"event_type": "unknown_type"})
    assert not ok3


def test_emit_consume_features(sample_book):
    db = get_db()
    users = db.list_users()
    uid = users[0]["id"] if users else None
    collector.emit({"event_type": "click", "user_id": uid, "book_id": sample_book,
                    "props": {"scene": "test"}})
    collector.emit({"event_type": "impression", "user_id": uid, "book_id": sample_book})
    collector.flush_once()
    row = db.get_book(sample_book)
    feats = db.feature_book_map().get(sample_book, {})
    assert "clicks" in feats or "impressions" in feats
    # 画像（ProfileService 由消费侧更新）
    from askhanvon.conversation.profile import ProfileService

    if uid:
        p = ProfileService().profile(uid)
        assert isinstance(p["pref_categories"], list)


def test_recompute_features(sample_book):
    from askhanvon.offline.features import recompute_features

    stats = recompute_features()
    assert stats["events"] > 0


def test_train_offline_models(sample_book):
    from askhanvon.offline.train import train_all

    m = train_all()
    db = get_db()
    assert db.rec_model_latest("cf") is not None
    # 样本充足时 rank 模型应产出 AUC
    if not m["rank"].get("skipped"):
        assert db.rec_model_latest("rank") is not None
