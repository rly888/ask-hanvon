"""工具：用户画像（查询/手动补充偏好；最小授权，只暴露偏好类字段）。"""
from ..db import dumps, get_db
from ..conversation.profile import ProfileService
from .schema import ToolContext, ToolResult, ToolSchema

SCHEMA = ToolSchema(
    name="user_profile",
    description="查看或设置用户阅读偏好画像（需登录）",
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "set_pref"]},
            "categories": {"type": "array", "description": "set_pref 时：偏好分类列表"},
        },
        "required": ["action"],
    },
    output_schema={"type": "object"},
    required_role="user",
)


def register(reg) -> None:
    reg.register(SCHEMA, run)


def run(ctx: ToolContext, action: str = "get", categories: list = None) -> ToolResult:
    svc = ProfileService()
    if action == "set_pref":
        cats = [str(c).strip() for c in (categories or []) if str(c).strip()][:5]
        if not cats:
            return ToolResult(ok=False, error="请提供偏好分类，如 [\"科幻\", \"历史\"]")
        get_db().mem_long_set(ctx.user_id, "pref_manual", dumps(cats), weight=1.5)
        return ToolResult(ok=True, data={"pref_categories": cats,
                                         "message": "偏好已更新: " + "、".join(cats)})
    profile = svc.profile(ctx.user_id)
    manual = get_db().mem_long_get(ctx.user_id, "pref_manual")
    import json as _json

    if manual:
        try:
            profile["manual_pref"] = _json.loads(manual)
        except ValueError:
            profile["manual_pref"] = []
    return ToolResult(ok=True, data={"profile": profile})
