"""优化项回归测试（P0-1~P0-6 / P1-3 / P2-2 / P3-1）。"""
import threading

from askhanvon.db import get_db
from askhanvon.events import collector
from askhanvon.ops.strategies import strategies


# ---------------- P0-2 事件消费认领 ----------------
def test_claim_disjoint_between_consumers(sample_book):
    collector.emit({"event_type": "click", "book_id": sample_book})
    collector.emit({"event_type": "click", "book_id": sample_book})
    collector.emit({"event_type": "click", "book_id": sample_book})
    db = get_db()
    b1 = db.event_claim_batch(2)
    b2 = db.event_claim_batch(2)
    ids1 = {r["id"] for r in b1}
    ids2 = {r["id"] for r in b2}
    assert ids1 and ids2
    assert not (ids1 & ids2), "两个消费者不得认领到同一事件"
    db.event_mark_done(list(ids1 | ids2))


def test_claim_lease_expiry_reuses_stale_batch(sample_book):
    import time

    db = get_db()
    collector.emit({"event_type": "click", "book_id": sample_book})
    first = db.event_claim_batch(10, lease_seconds=3600)
    assert first
    # 模拟消费者崩溃：租约过期后可被重新认领
    with db.transaction() as conn:
        for r in first:
            conn.execute("UPDATE event_queue SET claimed_at=? WHERE id=?",
                         (time.time() - 400, r["id"]))
    again = db.event_claim_batch(10, lease_seconds=300)
    assert {r["id"] for r in again} == {r["id"] for r in first}
    db.event_mark_done([r["id"] for r in again])


def test_flush_once_marks_done(sample_book):
    collector.emit({"event_type": "click", "book_id": sample_book})
    n1 = collector.flush_once()
    assert n1 >= 1
    # 已认领完成的事件不会被再次消费（队列排空后新增为 0）
    n2 = collector.flush_once()
    assert n2 == 0


# ---------------- P0-6 认证强化 ----------------
def test_password_policy_rejects_weak():
    from fastapi.testclient import TestClient

    from askhanvon.server.app import app

    client = TestClient(app)
    r = client.post("/api/auth/register",
                    json={"username": "weakpw_user", "password": "12345678"})
    assert r.status_code == 400
    assert "常见" in r.json()["detail"] or "密码" in r.json()["detail"]


def test_login_lockout_after_failures():
    import secrets as _secrets

    from fastapi.testclient import TestClient

    from askhanvon.server.app import app

    client = TestClient(app)
    username = "lock_" + _secrets.token_hex(3)
    real_password = "real-" + _secrets.token_hex(8)
    client.post("/api/auth/register",
                json={"username": username, "password": real_password})
    for _ in range(5):
        rr = client.post("/api/auth/login",
                         json={"username": username, "password": "wrong-" + _secrets.token_hex(4)})
        assert rr.status_code == 401
    rr = client.post("/api/auth/login", json={"username": username,
                                              "password": real_password})
    assert rr.status_code == 429  # 即使密码正确也锁定


def test_refresh_rotation_and_logout():
    import secrets as _secrets

    from fastapi.testclient import TestClient

    from askhanvon.server.app import app

    client = TestClient(app)
    username = "rot_" + _secrets.token_hex(3)
    password = "rot-" + _secrets.token_hex(8)
    reg = client.post("/api/auth/register",
                      json={"username": username, "password": password}).json()
    refresh = reg["refresh_token"]
    # 轮换：旧 refresh 作废，新 refresh 可用
    r1 = client.post("/api/auth/refresh", json={"refresh_token": refresh}).json()
    assert r1.get("token") and r1.get("refresh_token")
    r2 = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert r2.status_code == 401  # 旧令牌已轮换作废
    # 登出后新 refresh 不可用
    client.post("/api/auth/logout", json={"refresh_token": r1["refresh_token"]})
    r3 = client.post("/api/auth/refresh", json={"refresh_token": r1["refresh_token"]})
    assert r3.status_code == 401


# ---------------- P0-3 短语匹配 + RRF ----------------
def test_fts_query_contains_phrase_channel():
    from askhanvon.nlp import fts_match_query

    expr = fts_match_query("孙悟空大闹天宫")
    assert '"孙悟空"' in expr
    # 相邻内容词元构成二元组短语通道
    assert " 大闹" in expr or "天宫\"" in expr and " OR \"" in expr
    assert expr.count(" OR ") >= 1


def test_rrf_fusion_returns_results(sample_book):
    strategies.set("retrieval.fusion", "rrf", by="test")
    try:
        from askhanvon.rag.retriever import get_retriever

        res = get_retriever().retrieve("桃园结义 关羽", top_k=8)
        assert res
        assert all(0 <= r["score"] <= 1.0001 for r in res)
        # 切回 linear 仍可用
        strategies.set("retrieval.fusion", "linear", by="test")
        res2 = get_retriever().retrieve("桃园结义 关羽", top_k=8)
        assert res2
    finally:
        strategies.set("retrieval.fusion", "rrf", by="test")


# ---------------- P0-4 语义缓存 ----------------
def test_semantic_cache_hits_paraphrase(sample_book):
    from askhanvon.generation.answer import get_answer_generator
    from askhanvon.tools.book_qa import ask_rag

    gen = get_answer_generator()
    gen._semantic.clear()
    gen._cache.clear()
    q1 = "孙悟空为什么被压五行山下？"
    q2 = "孙悟空为什么被压五行山下呢？"
    r1 = ask_rag(q1, user_id=-1, use_cache=True)
    r2 = ask_rag(q2, user_id=-1, use_cache=True)
    if r1["refused"]:
        return  # 拒答不参与缓存语义
    assert r2["cached"] or r2["refused"]
    if r2["cached"]:
        assert r2.get("semantic_cached") or r2["answer"] == r1["answer"]


# ---------------- P1-3 引用验证向量通道 ----------------
def test_vector_support_positive_and_gate():
    from askhanvon.generation.citation import _vector_support

    chunk = ("火星因为表面的氧化铁而呈现红色，被称为红色星球。"
             "奥林帕斯山是太阳系最高的火山，高约二十一公里。")
    # 默认关闭（实测会放入关键词诱导型幻觉，见优化文档 P1-3 注记）
    assert not _vector_support("火星呈现红色是因为氧化铁", chunk)
    # 开启后（本地向量用 0.45 阈值）同话题改写应判有据
    strategies.set("answer.citation_vector_check", True, by="test")
    strategies.set("answer.citation_vec_threshold", 0.45, by="test")
    try:
        assert _vector_support("火星呈现红色是因为氧化铁", chunk)
    finally:
        strategies.set("answer.citation_vector_check", False, by="test")


# ---------------- P0-1 Embedding 模型版本化 ----------------
def _sanguo_path() -> str:
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "books", "三国演义精选导读.md")


def test_embedding_model_recorded_and_rebuild_on_change(sample_book):
    from askhanvon.pipeline import index_build
    from askhanvon.modelhub.gateway import get_gateway

    db = get_db()
    book = db.get_book(sample_book)
    current = get_gateway().embed_model_name()
    assert (book["embedding_model"] or "") == current
    # 模拟换模型 → ingest 强制重建
    gw = get_gateway()
    original = gw.embed_model_name
    gw.embed_model_name = lambda: "fake-embed-v2"
    try:
        report = index_build.ingest_book(_sanguo_path())
        assert report["chunks"] > 0
        book2 = db.get_book(sample_book)
        assert book2["embedding_model"] == "fake-embed-v2"
    finally:
        gw.embed_model_name = original
    # 还原真实模型并重建，避免污染其他用例
    index_build.ingest_book(_sanguo_path())
    from askhanvon.rag.retriever import get_retriever

    get_retriever().invalidate()


# ---------------- P3-1 曝光去重 + 探索位 ----------------
def test_exposure_dedup_defers_overexposed(sample_book):
    from askhanvon.recommend.engine import get_rec_engine
    from askhanvon.db import get_db

    users = get_db().list_users()
    uid = users[0]["id"] if users else 1
    # 该用户对第一本书制造 5 次近 7 天曝光
    for _ in range(5):
        collector.emit({"event_type": "impression", "user_id": uid,
                        "book_id": sample_book, "props": {"scene": "test"}})
    collector.flush_once()
    items = get_rec_engine().recommend(uid, top_k=6, track=False)
    assert len(items) == 6  # 列表长度不缩水
    head_ids = [i["book_id"] for i in items[:3]]
    assert sample_book not in head_ids, "过曝光书应被延后"


def test_explore_slot_rule_tagged():
    from askhanvon.recommend.engine import get_rec_engine

    # top_k=4：6 本书中留出 2 本进尾部探索池
    items = get_rec_engine().recommend(None, top_k=4, track=False)
    if len(items) < 4:
        return
    tail_rules = items[-1].get("breakdown", {}).get("rules_applied", [])
    assert tail_rules and tail_rules[0] in ("longtail_slot", "explore_slot")


# ---------------- P2-2 工具并行执行 ----------------
def test_executor_parallel_preserves_order(sample_book):
    from askhanvon.agent.executor import Executor
    from askhanvon.agent.schema import Plan, PlanStep
    from askhanvon.tools.schema import ToolContext

    plan = Plan(intent="test", steps=[
        PlanStep(tool="book_search", args={"query": "三国"}),
        PlanStep(tool="recommend_books", args={"top_k": 3}),
        PlanStep(tool="book_search", args={"query": "西游"}),
    ])
    results = Executor().run(plan, ToolContext(user_id=None, role="anonymous"))
    assert len(results) == 3
    assert [r.meta["tool"] for r in results] == ["book_search", "recommend_books",
                                                 "book_search"]
    assert all(r.ok for r in results)
