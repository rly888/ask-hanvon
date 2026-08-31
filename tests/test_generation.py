"""生成引擎测试：引用校验 / 版权护栏 / 离线兜底回答 / 拒答。"""
from askhanvon.generation.answer import get_answer_generator
from askhanvon.generation.citation import refs_in, validate_citations
from askhanvon.generation.moderation import copyright_guard
from askhanvon.ops.strategies import strategies


def _make_ctx():
    metas = [
        {"idx": 1, "chunk_id": 101, "book_id": "b_test", "book_title": "测试之书",
         "vol": "", "chapter_no": 2, "chapter_title": "探索火星之旅",
         "para_start": 1, "para_end": 2, "page_start": 3, "page_end": 4, "score": 0.9,
         "text": "火星因为表面的氧化铁而呈现红色，奥林帕斯山是太阳系最高的火山。"},
    ]
    metas.append(dict(metas[0], idx=2, chunk_id=102,
                      text="嫦娥四号在2019年实现人类首次月球背面软着陆。"))
    return metas


def test_validate_citations_verified():
    metas = _make_ctx()
    raw = "火星因氧化铁呈红色 [1]。而嫦娥四号实现了月背软着陆 [2]。"
    clean, citations, stats = validate_citations(raw, metas)
    assert stats["used"] == 2
    assert stats["verified"] == 2
    assert len(citations) == 2
    assert citations[0]["chapter_no"] == 2
    assert citations[0]["pages"] == "p3-4"


def test_validate_citations_drops_unverified():
    metas = _make_ctx()
    raw = "这是一个资料里完全没有的说法哦 [1]。"
    clean, citations, stats = validate_citations(raw, metas)
    assert stats["verified"] == 0 and stats["removed"] == 1
    assert "[1]" not in clean


def test_copyright_guard_trims_long_quote():
    metas = _make_ctx()
    chunk_text = metas[0]["text"]
    long_copy = chunk_text * 10  # 大段抄写
    clean, flags = copyright_guard("论述：" + long_copy, metas, max_quote=20)
    assert "copyright_trimmed" in flags
    assert "（原文引用已节选" in clean


def test_answer_cites_in_offline_mode(sample_book):
    """离线兜底模式：回答必须来自检索片段并带引用。"""
    strategies.invalidate()
    from askhanvon.tools.book_qa import ask_rag

    result = ask_rag("孙悟空在哪里出世", use_cache=False)
    assert result["degraded"] is True
    assert result["refused"] is False
    assert result["citations"], "离线兜底回答应带引用"
    assert any(c["book_title"] for c in result["citations"])


def test_answer_refuses_unanswerable(sample_book):
    from askhanvon.tools.book_qa import ask_rag

    result = ask_rag("量子纠缠在量子通信中如何应用呢", use_cache=False)
    # 书库没有量子通信内容 → 低置信或拒答（宁拒不编造）
    assert result["refused"] or result["confidence"] < 0.5


def test_answer_generator_caches(sample_book):
    gen = get_answer_generator()
    from askhanvon.tools.book_qa import ask_rag

    r1 = ask_rag("真假美猴王的真相是什么", use_cache=True)
    r2 = ask_rag("真假美猴王的真相是什么", use_cache=True)
    assert r2["cached"] is True or r1["refused"]
