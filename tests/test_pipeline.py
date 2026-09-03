"""pipeline 测试：解析 / 去重 / chunk / locator。"""
from askhanvon.pipeline.chunk import chunk_chapters
from askhanvon.pipeline.dedup import dedup_paragraphs
from askhanvon.pipeline.parse import cn2int, parse_markdown

SAMPLE = """# 测试之书 | 测试作者 | 科普 | 测试标签 | 一本用于测试的书。

### 第一章 天体运行的奥秘
太阳系有八大行星，木星是其中最大的行星，质量远超其他行星的总和。
土星的光环由冰粒组成，美丽而壮观。

月球距离地球约三十八万公里，是人类踏足过的唯一地外天体。

### 第二章 探索火星之旅
火星因为表面的氧化铁而呈现红色，被称为红色星球。
奥林帕斯山是太阳系最高的火山，高约二十一公里。
火星一天约二十四点六小时，与地球十分接近。
"""


def test_cn2int():
    assert cn2int("一") == 1
    assert cn2int("十二") == 12
    assert cn2int("21") == 21
    assert cn2int("三") == 3


def test_parse_markdown():
    book = parse_markdown(SAMPLE)
    assert book.title == "测试之书"
    assert book.author == "测试作者"
    assert book.category == "科普"
    assert len(book.chapters) == 2
    ch1 = book.chapters[0]
    assert ch1.no == 1 and "天体" in ch1.title
    assert len(ch1.paragraphs) == 3  # 两行连续段落 + 空行分隔的月球段


def test_dedup_paragraphs():
    paras = ["甲乙丙丁测试段落", "甲乙丙丁测试段落", "完全不同的另一段内容"]
    out = dedup_paragraphs(paras)
    assert len(out) == 2


def test_chunking_locator():
    book = parse_markdown(SAMPLE)
    chunks = chunk_chapters(book)
    assert chunks, "应有 chunk 产出"
    for c in chunks:
        # locator 完整性：引用溯源的生命线
        assert c["chapter_no"] in (1, 2)
        assert c["chapter_title"]
        assert c["page_start"] >= 1 and c["page_end"] >= c["page_start"]
        assert c["para_start"] >= 1 and c["para_end"] >= c["para_start"]
        assert c["content_hash"]
        assert c["n_chars"] == len(c["text"])
    # 页码单调不减
    pages = [(c["page_start"], c["page_end"]) for c in chunks]
    assert pages == sorted(pages, key=lambda p: p[0])


def test_ingest_and_index(sample_book):
    from askhanvon.db import get_db

    db = get_db()
    book = db.get_book(sample_book)
    assert book and book["n_chunks"] > 0
    rows = db.get_chunks_of_book(sample_book)
    assert all(r["embedding"] for r in rows), "embedding 应已生成"


def test_reingest_without_reindex_leaves_no_orphans(sample_book):
    """重入库（reindex=False）后章节全部重建，旧 chunk 必须清理，不留孤儿块。"""
    import os

    from askhanvon.db import get_db
    from askhanvon.pipeline.index_build import ingest_book

    db = get_db()
    book = db.get_book(sample_book)
    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "books", book["source_file"],
    )
    before = len(db.get_chunks_of_book(sample_book))
    report = ingest_book(source, reindex=False)
    after = len(db.get_chunks_of_book(sample_book))
    assert report["chunks"] == after, "落库块数应与本次 ingest 产出一致"
    assert after <= before, "重入库不应累积旧块"
    # 无孤儿：每个 chunk 的 chapter_id 都在当前章节表中
    valid_ids = {c["id"] for c in db.chapters_of_book(sample_book)}
    for r in db.get_chunks_of_book(sample_book):
        assert r["chapter_id"] in valid_ids, "存在指向已删除章节的孤儿 chunk"
