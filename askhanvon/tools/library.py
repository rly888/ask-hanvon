"""工具：藏书库（我的书架/收藏/阅读历史）。"""
from ..db import get_db, new_id
from ..events.collector import emit
from .schema import ToolContext, ToolResult, ToolSchema

SCHEMA = ToolSchema(
    name="my_library",
    description="我的书架：查看收藏与阅读历史，收藏/取消收藏图书（需登录）",
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "collect", "uncollect", "history"]},
            "book_title": {"type": "string", "description": "收藏/取消的书名（collect/uncollect 必填）"},
        },
        "required": ["action"],
    },
    output_schema={"type": "object", "properties": {"items": {"type": "array"}}},
    required_role="user",
)


def register(reg) -> None:
    reg.register(SCHEMA, run)


def run(ctx: ToolContext, action: str = "list", book_title: str = "") -> ToolResult:
    db = get_db()
    if action in ("collect", "uncollect"):
        book = db.get_book_by_title(book_title or "")
        if not book:
            return ToolResult(ok=False, error="未找到该书: " + str(book_title))
        db.library_upsert(ctx.user_id, book["id"], "collect" if action == "collect" else "dropped")
        emit(
            {
                "event_type": "collect" if action == "collect" else "uncollect",
                "user_id": ctx.user_id,
                "book_id": book["id"],
            }
        )
        return ToolResult(
            ok=True,
            data={"action": action, "book_id": book["id"], "title": book["title"],
                  "message": ("已加入书架: " if action == "collect" else "已移出书架: ") + book["title"]},
        )
    if action == "history":
        events = db.events_query(event_type="click", user_id=ctx.user_id, limit=50)
        items = []
        seen = set()
        for e in reversed(events):
            bid = e.get("book_id")
            if not bid or bid in seen:
                continue
            seen.add(bid)
            b = db.get_book(bid)
            if b:
                items.append({"book_id": bid, "title": b["title"], "author": b["author"],
                              "cover_emoji": b.get("cover_emoji", "📘")})
        return ToolResult(ok=True, data={"items": items[:20], "action": "history"})
    # list
    rows = db.library_list(ctx.user_id, "collect")
    items = []
    for r in rows:
        b = db.get_book(r["book_id"])
        if b:
            items.append({"book_id": b["id"], "title": b["title"], "author": b["author"],
                          "category": b["category"], "cover_emoji": b.get("cover_emoji", "📘")})
    return ToolResult(ok=True, data={"items": items, "action": "list"})
