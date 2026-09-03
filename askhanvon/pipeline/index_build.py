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
        else:
            # 非 reindex：章节已重建（新 id），旧 chunk 全部成为孤儿，
            # 清理后 FTS/向量矩阵不留脏块（数据一致性，P 优化）
            orphaned = db.delete_orphan_chunks(book_id)
            if orphaned:
                log_fields(logger, 30, "ingest.orphan_cleanup",
                           book=parsed.title, removed=orphaned)
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
    embedded, eff_model = embed_chunks(new_chunk_rows)
    if eff_model != embed_model:
        # API 配置了但实际降级到本地向量：块级/书级模型标识按真实来源修正，
        # 下次 API 恢复时 embed_model_name() 变化会触发强制重建（P0-1 防线）。
        db.set_chunks_embedding_model(book_id, eff_model)
        db.set_book_embedding_model(book_id, eff_model)
        embed_model = eff_model
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
