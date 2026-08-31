"""第二轮优化项测试（P1-1/P1-2/P1-5/P2-3/P2-4/P3-2/P3-4/P3-5）。"""
from askhanvon.db import get_db
from askhanvon.ops.strategies import strategies


# ---------------- P1-2 查询改写与多查询 ----------------
def test_rewrite_falls_back_offline():
    from askhanvon.rag.rewrite import rewrite_query

    # 测试环境无 LLM Key → 静默回退原查询
    assert rewrite_query("孙悟空为什么被压五行山下") == "孙悟空为什么被压五行山下"


def test_retrieve_multi_with_extra_queries(sample_book):
    from askhanvon.rag.retriever import get_retriever

    res = get_retriever().retrieve_multi(
        "桃园结义", top_k=8, extra_queries=["桃园三结义 关羽 张飞"],
    )
    assert res
    assert all(r.get("multi_query") for r in res)
    assert any("三国" in r["book_title"] for r in res)


def test_multi_query_strategy_off_still_works(sample_book):
    from askhanvon.rag.retriever import get_retriever

    strategies.set("retrieval.multi_query", False, by="test")
    try:
        res = get_retriever().retrieve_multi("桃园结义", top_k=5)
        assert res
    finally:
        strategies.set("retrieval.multi_query", True, by="test")


# ---------------- P1-1 父子块扩展 ----------------
def test_parent_expand_merges_neighbor_chunk(sample_book):
    import hashlib

    from askhanvon.rag.context import build_context

    db = get_db()
    ch = db.chapters_of_book(sample_book)[0]
    rows = db.get_chunks_by_book_chapter(sample_book, ch["no"])
    assert rows
    # 给该章补一个邻居块（只测上下文扩展，不须有 embedding/FTS）
    neighbor_text = "邻块补充内容：关云长千里走单骑，护嫂寻兄过五关斩六将。"
    cid2 = db.add_chunk({
        "book_id": sample_book, "chapter_id": ch["id"], "chunk_no": 99,
        "text": neighbor_text, "n_chars": len(neighbor_text),
        "vol": ch["vol"] or "", "chapter_no": ch["no"], "chapter_title": ch["title"],
        "para_start": 1, "para_end": 1, "page_start": 1, "page_end": 1,
        "content_hash": hashlib.blake2b(neighbor_text.encode(), digest_size=8).hexdigest(),
    })
    try:
        r0 = rows[0]
        item = {
            "chunk_id": r0["id"], "book_id": sample_book, "book_title": "三国演义精选导读",
            "vol": r0["vol"] or "", "chapter_no": r0["chapter_no"],
            "chapter_title": r0["chapter_title"], "para_start": 1, "para_end": 2,
            "page_start": 1, "page_end": 2, "text": r0["text"],
            "content_hash": "fake_hash_hash",
            "score": 0.9,
        }
        ctx = build_context("测试查询", [item])
        assert ctx.metas
        meta = ctx.metas[0]
        assert "邻块补充内容" in meta["context_text"]   # 扩展生效
        assert "邻块补充内容" not in meta["text"]        # 引用基准仍为原子块
        assert "邻块补充内容" in ctx.blocks
        # 策略关闭后不扩展
        strategies.set("retrieval.parent_expand", False, by="test")
        try:
            ctx2 = build_context("测试查询", [item])
            assert "邻块补充内容" not in ctx2.blocks
        finally:
            strategies.set("retrieval.parent_expand", True, by="test")
    finally:
        with db.transaction() as conn:
            conn.execute("DELETE FROM chunks WHERE id=?", (cid2,))


# ---------------- P1-5 Prompt 版本管理 ----------------
def test_prompt_version_service_and_messages():
    from askhanvon.generation.answer import get_answer_generator
    from askhanvon.generation.prompts import QA_SYSTEM as DEFAULT_QA
    from askhanvon.ops.prompts import prompt_service

    version0, template0 = prompt_service.get("qa", DEFAULT_QA)
    assert version0 == 0  # 默认即代码常量
    custom = "【自定义测试模板 v1】上下文：{context}\n用户：{profile}\n回答必须带引用。"
    new_ver = prompt_service.set("qa", custom, by="test")
    assert new_ver == 1
    ver, tpl = prompt_service.get("qa", DEFAULT_QA)
    assert ver == 1 and tpl == custom
    # 缺必需占位符 → 拒绝保存
    try:
        prompt_service.set("qa", "没有占位符的模板", by="test")
        assert False
    except ValueError:
        pass
    # 消息组装走版本模板
    class _Ctx:
        blocks = "资料"
        metas = []
    gen = get_answer_generator()
    msgs, v = gen._messages("问题", _Ctx(), "")
    assert v == 1 and "自定义测试模板 v1" in msgs[0]["content"]
    prompt_service.invalidate()


# ---------------- P2-3 拒答改写重检 ----------------
def test_retry_event_on_refusal():
    from askhanvon.agent.loop import get_agent

    r = get_agent().handle("量子纠缠在量子通信中如何应用呢", session_id="s_r2_retry")
    step_types = [s.get("type") for s in r.get("steps", [])]
    assert "retry" in step_types  # 触发了改写重检
    assert r.get("refused", True)  # 改写后仍无据 → 保持拒答（宁拒不编造）


# ---------------- P2-4 建议与 usage ----------------
def test_done_payload_has_usage_and_suggestions():
    from askhanvon.agent.loop import get_agent

    r = get_agent().handle("给我推荐几本历史书", session_id="s_r2_sugg")
    assert "suggestions" in r  # 建议键存在（推荐类至少 1 条）
    assert r.get("suggestions")
    if r.get("intent") == "recommend":
        assert any("买" in s or "换" in s for s in r["suggestions"])
    qr = get_agent().handle("《三国演义》里赤壁之战怎么赢的", session_id="s_r2_usage")
    assert "usage" in qr

    # 引用类建议应含书名
    if qr.get("citations"):
        assert any("三国演义精选导读" in s for s in qr.get("suggestions", []))


# ---------------- P3-4 会话实时信号 ----------------
def test_session_boost_reason():
    from askhanvon.conversation.session import SessionStore
    from askhanvon.recommend.engine import get_rec_engine

    sid = "s_r2_sessionboost"
    SessionStore().get_or_create(sid, None)
    SessionStore().memory_set(sid, "last_book", "三国演义精选导读")
    items = get_rec_engine().recommend(None, top_k=6, track=False, session_id=sid)
    boosted = [i for i in items if any("你刚在看" in r for r in i["reasons"])]
    assert boosted, "会话内信号应产生同类加权推荐"


# ---------------- P3-2 MMR ----------------
def test_mmr_lambda_runs_and_tags():
    from askhanvon.recommend.engine import get_rec_engine

    strategies.set("rec.mmr_lambda", 0.8, by="test")
    try:
        items = get_rec_engine().recommend(None, top_k=5, track=False)
        assert items
        # 主序列（除尾部探索位被替换外）应带 mmr 标记
        tagged = [i for i in items
                  if "mmr" in i.get("breakdown", {}).get("rules_applied", [])]
        assert tagged, "MMR 装配应产生带 mmr 标记的结果"
    finally:
        strategies.set("rec.mmr_lambda", 0.0, by="test")


def test_topic_coherence_refuses_drift():
    from askhanvon.generation.answer import _topic_coherent

    # 关键词碎片诱导（"时间"命中无关史实内容，片段中无"简史"锚词）→ 判漂移
    metas = [{"context_text": "从解决温饱到全面建成小康社会，中国用几十年时间走完了工业化历程。"}]
    assert _topic_coherent("《时间简史》讲了什么内容？",
                           "从解决温饱到全面建成小康社会。", metas) is False
    # 正常同题回答（片段含锚词）→ 放行
    metas2 = [{"context_text": "如来翻掌将悟空压在五行山下，一压便是五百年。"}]
    assert _topic_coherent("孙悟空为什么被压五行山下？",
                           "如来翻掌将悟空压在五行山下。", metas2) is True
    strategies.set("answer.topic_coherence_check", False, by="test")
    try:
        assert _topic_coherent("《时间简史》讲了什么内容？",
                               "从解决温饱到全面建成小康社会。", metas) is True
    finally:
        strategies.set("answer.topic_coherence_check", True, by="test")


# ---------------- P3-5 内置调度器 ----------------
def test_scheduler_cycle_runs_due_jobs_only():
    from askhanvon.ops.scheduler import scheduler

    executed = scheduler.run_cycle()
    assert "features_hourly" in executed  # 首轮全部到期
    again = scheduler.run_cycle()
    assert again == []  # 未到期不执行
    scheduler._next_run.clear()
