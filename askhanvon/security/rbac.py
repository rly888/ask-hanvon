"""工具级权限 RBAC（§3.4）：订单/购买类工具必须绑定登录用户身份。"""

ANONYMOUS_TOOLS = {
    "book_search",
    "book_qa",
    "recommend_books",
    "compare_books",
}

USER_ONLY_TOOLS = {
    "my_library",
    "user_profile",
    "purchase_init",
    "purchase_confirm",
}

ALL_KNOWN_TOOLS = ANONYMOUS_TOOLS | USER_ONLY_TOOLS


def can_use_tool(tool_name: str, role: str) -> bool:
    if role == "admin":
        return True
    if tool_name in ANONYMOUS_TOOLS:
        return True
    if role == "user" and tool_name in USER_ONLY_TOOLS:
        return True
    return False


def required_role(tool_name: str) -> str:
    if tool_name in ANONYMOUS_TOOLS:
        return "anonymous"
    return "user"
