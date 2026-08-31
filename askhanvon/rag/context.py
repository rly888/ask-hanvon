"""上下文构建：引用块格式化 + locator 元数据 + token 预算 + 去重 + 低置信判定。

父子块扩展（P1-1）：检索/引用以子块（原 chunk）为准；prompt 块可并入同章相邻块
（small-to-big），让回答拥有更完整的论述上下文。策略 retrieval.parent_expand。
"""
from dataclasses import dataclass, field

from ..config import settings
from ..ops.strategies import strategies


@dataclass
class ContextResult:
    blocks: str = ""
    metas: list = field(default_factory=list)
    confidence: float = 0.0
    low_confidence: bool = True
    dropped_untrusted: int = 0
    dropped_dup: int = 0


def _dedup(items: list, per_book_cap: int = 3) -> tuple:
    """同 hash 去重 + 每书最多 per_book_cap 块（保证多书覆盖）。"""
    seen_hash = set()
    book_count: dict = {}
    kept, dropped = [], 0
    for r in items:
        h = r.get("content_hash")
        if h and h in seen_hash:
            dropped += 1
            continue
        seen_hash.add(h)
        c = book_count.get(r["book_id"], 0)
        if c >= per_book_cap:
            dropped += 1
            continue
        book_count[r["book_id"]] = c + 1
        kept.append(r)
    return kept, dropped


def _expand_chunk(item: dict, excluded_ids: set) -> str:
    """父块扩展：并入同章相邻块文本（跳过已入选块），单侧长度受限（P1-1）。"""
    if not strategies.get("retrieval.parent_expand", True):
        return item["text"]
    try:
        from ..db import get_db

        rows = get_db().get_chunks_by_book_chapter(item["book_id"], item["chapter_no"])
    except Exception:  # noqa: BLE001 — 扩展失败回退原文
        return item["text"]
    if len(rows) <= 1:
        return item["text"]
    idx = next((i for i, x in enumerate(rows) if x["id"] == item["chunk_id"]), None)
    if idx is None:
        return item["text"]
    parts = [rows[idx]["text"]]
    for j in (idx - 1, idx + 1):
        if 0 <= j < len(rows) and rows[j]["id"] not in excluded_ids:
            neighbor = rows[j]["text"]
            if len(neighbor) <= max(len(item["text"]) * 1.5, 200):
                parts.append(neighbor)
    return "\n……\n".join(parts) if len(parts) > 1 else item["text"]


def build_context(query: str, retrieved: list, budget: int | None = None,
                  untrusted_ids: set | None = None) -> ContextResult:
    """retrieved 应已过 rerank；untrusted_ids 为注入检测标记的 chunk_id 集合。"""
    untrusted_ids = untrusted_ids or set()
    dropped_untrusted = sum(1 for r in retrieved if r["chunk_id"] in untrusted_ids)
    safe = [r for r in retrieved if r["chunk_id"] not in untrusted_ids]
    safe, dropped_dup = _dedup(safe)

    budget = budget or settings.context_char_budget
    safe_ids = {r["chunk_id"] for r in safe}
    lines: list = []
    metas: list = []
    used = 0
    for i, r in enumerate(safe, start=1):
        vol_part = ("卷" + r["vol"]) if r.get("vol") else ""
        head = "[{i}] 《{t}》第{c}章 {ct}（{v}·页{p1}-{p2}）".format(
            i=i, t=r["book_title"], c=r["chapter_no"], ct=r["chapter_title"],
            v=vol_part, p1=r["page_start"], p2=r["page_end"],
        )
        # P1-1：prompt 块用扩展文本；meta["text"] 保留原子块（引用交叉验证基准）
        body = _expand_chunk(r, safe_ids - {r["chunk_id"]})
        block = head + "\n" + body
        if used + len(block) > budget and lines:
            break
        if used + len(block) > budget:
            body = body[: max(0, budget - used - len(head))] + "……"
            block = head + "\n" + body
        lines.append(block)
        metas.append(
            {
                "idx": i,
                "chunk_id": r["chunk_id"],
                "book_id": r["book_id"],
                "book_title": r["book_title"],
                "vol": r["vol"],
                "chapter_no": r["chapter_no"],
                "chapter_title": r["chapter_title"],
                "para_start": r["para_start"],
                "para_end": r["para_end"],
                "page_start": r["page_start"],
                "page_end": r["page_end"],
                "score": r.get("score", 0),
                "text": r["text"],
                "context_text": body,
            }
        )
        used += len(block)

    top_score = safe[0].get("score", 0.0) if safe else 0.0
    low = (not metas) or top_score < settings.min_confidence
    return ContextResult(
        blocks="\n\n".join(lines),
        metas=metas,
        confidence=round(float(top_score), 4),
        low_confidence=low,
        dropped_untrusted=dropped_untrusted,
        dropped_dup=dropped_dup,
    )
