"""工具：图书推荐（规则版/精排序引擎统一入口，结果可解释）。"""
from ..recommend.engine import get_rec_engine
from .schema import ToolContext, ToolResult, ToolSchema

SCHEMA = ToolSchema(
    name="recommend_books",
    description="为用户推荐图书，返回带推荐理由的可解释结果",
    input_schema={
        "type": "object",
        "properties": {
            "scene": {"type": "string", "description": "推荐位场景，默认 homepage"},
            "top_k": {"type": "integer", "description": "数量，默认 6"},
            "book_title": {"type": "string", "description": "参照书（找类似）"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {"items": {"type": "array"}, "variant": {"type": "string"}},
    },
)


def register(reg) -> None:
    reg.register(SCHEMA, run)


def run(ctx: ToolContext, scene: str = "homepage", top_k: int = 6,
        book_title: str = "") -> ToolResult:
    engine = get_rec_engine()
    items = engine.recommend(ctx.user_id, scene=scene, top_k=max(1, min(top_k, 12)),
                             session_id=ctx.session_id)
    if book_title:
        # 找类似：以参照书的分类与标签加权（内容通道强化）
        db = get_db_mod()
        ref = db.get_book_by_title(book_title)
        if ref:
            same_cat = [i for i in items if i["category"] == ref.get("category")]
            other = [i for i in items if i["category"] != ref.get("category")]
            items = same_cat + other
            items = items[:top_k]
    return ToolResult(
        ok=True,
        data={"items": items, "scene": scene, "user_id": ctx.user_id},
    )


def get_db_mod():
    from ..db import get_db

    return get_db()
