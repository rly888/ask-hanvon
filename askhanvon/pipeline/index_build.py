"""索引构建：解析 → 去重 → chunk → FTS/向量索引 → 元数据落库（幂等可重跑）。"""
import hashlib
import os

from ..db import get_db
from ..modelhub.gateway import get_gateway
from ..nlp import tokenize
from ..obs.logging import get_logger, log_fields
from .chunk import chunk_chapters
from .embed import embed_chunks
from .parse import parse_file

logger = get_logger("askhanvon.pipeline")


def book_slug(title: str) -> str:
    return "b_" + hashlib.blake2b(title.encode("utf-8"), digest_size=6).hexdigest()


def ingest_book(path: str, reindex: bool = False) -> dict:
    """ingest 一本书（幂等：同书重跑=更新版本）。"""
    parsed = parse_file(path)
    return _ingest_parsed(parsed, os.path.basename(path), reindex)


def ingest_stream(ext: str, data: bytes, source_name: str = "upload") -> dict:
    """内存 ingest（上传不落盘）。ext 为服务端判定的固定类别。"""
    from .parse import parse_stream

    parsed = parse_stream(ext, data)
    return _ingest_parsed(parsed, source_name, reindex=True)


def _ingest_parsed(parsed, source_name: str, reindex: bool) -> dict:
    db = get_db()
    if not parsed.title or not parsed.chapters:
        raise ValueError("解析失败或无章节: " + source_name)
    book_id = book_slug(parsed.title)
    embed_model = get_gateway().embed_model_name()

    existing = db.get_book(book_id)
    db.delete_chapters(book_id)
    if existing:
        # embedding 模型变更 → 强制重建（防新旧向量混算，P0-1）
        if (existing.get("embedding_model") or "") != embed_model:
            reindex = True
        if reindex:
            db.delete_chunks(book_id)
    n_new_chunks = 0
    new_chunk_rows: list = []
    from .chunk import chunk_chapters  # 局部导入避免环

    all_chunks = chunk_chapters(parsed)  # 全书切分一次，按章节归属挂载
    for order, ch in enumerate(parsed.chapters):
        chapter_id = db.add_chapter(book_id, ch.vol, ch.no, ch.title, order)
        mine = [c for c in all_chunks if c["chapter_no"] == ch.no and c["vol"] == ch.vol]
        seen_no = 0
        for c in mine:
            seen_no += 1
            c["chunk_no"] = seen_no
            c["embedding_model"] = embed_model  # 向量模型版本随块落库（P0-1）
            chunk_id = db.add_chunk({**c, "book_id": book_id, "chapter_id": chapter_id})
            # 标题词 ×2 加权：章节标题是问答检索的强信号
            title_tokens = tokenize(c["chapter_title"])
            db.set_fts(chunk_id, " ".join(title_tokens + title_tokens + tokenize(c["text"])))
            row = db.get_chunk(chunk_id)
            new_chunk_rows.append(row)
            n_new_chunks += 1

    db.upsert_book(
        {
            "id": book_id,
            "title": parsed.title,
            "author": parsed.author,
            "category": parsed.category,
            "tags": ",".join(parsed.tags),
            "description": parsed.description,
            "source_file": source_name,
            "n_chunks": n_new_chunks,
            "embedding_model": embed_model,
        }
    )
    embedded = embed_chunks(new_chunk_rows)
    log_fields(
        logger, 20, "ingest.done", book=parsed.title, chapters=len(parsed.chapters),
        chunks=n_new_chunks, embedded=embedded,
    )
    return {
        "book_id": book_id,
        "title": parsed.title,
        "chapters": len(parsed.chapters),
        "chunks": n_new_chunks,
        "embedded": embedded,
        "reindexed": bool(existing and reindex),
    }


def ingest_dir(dir_path: str, reindex: bool = False) -> list:
    reports = []
    for name in sorted(os.listdir(dir_path)):
        if name.lower().endswith((".md", ".txt", ".epub", ".pdf")):
            try:
                reports.append(ingest_book(os.path.join(dir_path, name), reindex=reindex))
            except Exception as e:  # noqa: BLE001 — 单本失败不阻断批量导入
                log_fields(logger, 40, "ingest.error", file=name, error=str(e)[:150])
                reports.append({"file": name, "error": str(e)[:150]})
    return reports


def index_stats() -> dict:
    db = get_db()
    n, mx = db.count_chunks()
    return {"chunks": n, "max_chunk_id": mx, "books": len(db.all_books())}
