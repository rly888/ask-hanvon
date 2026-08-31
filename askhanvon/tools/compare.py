"""工具：图书比较（最多 3 本，基于各自检索要点生成对比）。"""
from ..db import get_db
from ..rag.retriever import get_retriever
from .schema import ToolContext, ToolResult, ToolSchema

SCHEMA = ToolSchema(
    name="compare_books",
    description="比较多本图书的作者/分类/核心内容/适合人群，输出对比表",
    input_schema={
        "type": "object",
        "properties": {
            "titles": {
                "type": "array",
                "description": "书名列表（2-3 本）",
            },
        },
        "required": ["titles"],
    },
    output_schema={"type": "object", "properties": {"comparison": {"type": "object"}}},
)


def register(reg) -> None:
    reg.register(SCHEMA, run)


def _key_points(text: str, n: int = 2) -> list:
    import re

    parts = re.split(r"(?<=[。！？!?])", text or "")
    return [p.strip() for p in parts if len(p.strip()) >= 10][:n]


def run(ctx: ToolContext, titles: list) -> ToolResult:
    titles = [str(t) for t in titles][:3]
    if len(titles) < 2:
        return ToolResult(ok=False, error="比较至少需要两本书")
    db = get_db()
    retriever = get_retriever()
    columns = []
    for t in titles:
        book = db.get_book_by_title(t)
        if not book:
            columns.append({"title": t, "found": False})
            continue
        retrieved = retriever.retrieve(t, top_k=8, book_ids=[book["id"]])
        points = []
        for r in retrieved[:2]:
            points.extend(_key_points(r["text"]))
        columns.append(
            {
                "title": book["title"],
                "found": True,
                "author": book.get("author", ""),
                "category": book.get("category", ""),
                "description": book.get("description", ""),
                "key_points": points or [(retrieved[0]["text"][:80] + "……") if retrieved else ""],
            }
        )
    return ToolResult(ok=True, data={"comparison": {"columns": columns, "fields": [
        "author", "category", "key_points"
    ]}})
