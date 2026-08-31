"""工具：书籍搜索（书名/作者/标签/内容全文混合搜索）。"""
from ..db import get_db
from ..rag.retriever import get_retriever
from .schema import ToolContext, ToolResult, ToolSchema

SCHEMA = ToolSchema(
    name="book_search",
    description="按书名/作者/关键词搜索书库图书，返回带命中摘要的图书列表",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "top_k": {"type": "integer", "description": "返回数量，默认 8"},
            "category": {"type": "string", "description": "分类过滤（可选）"},
        },
        "required": ["query"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "results": {"type": "array", "description": "图书卡片列表"},
            "total": {"type": "integer"},
        },
    },
)


def register(reg) -> None:
    reg.register(SCHEMA, run)


def run(ctx: ToolContext, query: str, top_k: int = 8, category: str = "") -> ToolResult:
    db = get_db()
    # 1) 元数据匹配（书名/作者/标签，Python 侧过滤）
    meta_books = db.list_books(keyword=query, category=category, limit=top_k)
    results = []
    seen = set()
    for b in meta_books:
        results.append(
            {
                "book_id": b["id"],
                "title": b["title"],
                "author": b["author"],
                "category": b["category"],
                "cover_emoji": b.get("cover_emoji", "📘"),
                "description": (b.get("description") or "")[:120],
                "snippet": "",
                "score": 1.0,
                "match": "meta",
            }
        )
        seen.add(b["id"])

    # 2) 内容命中（RAG 检索，按书聚合取最优片段）
    retrieved = get_retriever().retrieve(query, top_k=top_k * 4)
    by_book: dict = {}
    for r in retrieved:
        if r["book_id"] in seen or (category and r.get("category") != category):
            continue
        if r["book_id"] not in by_book or r["score"] > by_book[r["book_id"]]["score"]:
            by_book[r["book_id"]] = r
    for book_id, r in sorted(by_book.items(), key=lambda x: x[1]["score"], reverse=True):
        if len(results) >= top_k:
            break
        b = db.get_book(book_id) or {}
        results.append(
            {
                "book_id": book_id,
                "title": r["book_title"] or b.get("title", ""),
                "author": b.get("author", ""),
                "category": b.get("category", ""),
                "cover_emoji": b.get("cover_emoji", "📘"),
                "description": (b.get("description") or "")[:120],
                "snippet": r["text"][:100] + "……",
                "score": r["score"],
                "match": "content",
            }
        )
    return ToolResult(
        ok=True,
        data={"results": results[:top_k], "total": len(results[:top_k]), "query": query},
    )
