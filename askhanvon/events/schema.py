"""埋点事件模型（开发计划 §8.5：事件表 行为 → 特征 → 推荐）。

事件 = {event_type, user_id?, session_id?, book_id?, query?, props{}}
定义即契约：新增事件类型必须先在这里登记（校验不通过直接拒绝入队）。
"""
EVENT_MODEL = {
    "search": {"required": ["query"], "props": []},
    "impression": {"required": ["book_id"], "props": ["scene", "variant", "position"]},
    "click": {"required": ["book_id"], "props": ["scene", "source", "position"]},
    "read_duration": {"required": ["book_id", "seconds"], "props": ["chapter_no"]},
    "collect": {"required": ["book_id"], "props": []},
    "uncollect": {"required": ["book_id"], "props": []},
    "purchase": {"required": ["book_id"], "props": ["order_id", "amount"]},
    "chat_message": {"required": [], "props": ["intent"]},
    "feedback": {"required": ["book_id", "action"], "props": []},
}


def validate_event(evt: dict) -> tuple:
    """返回 (ok, errors)。"""
    errors = []
    et = evt.get("event_type")
    if not et:
        return False, ["缺少 event_type"]
    spec = EVENT_MODEL.get(et)
    if not spec:
        errors.append("未知事件类型: " + str(et))
        return False, errors
    props = evt.get("props") or {}
    for req in spec["required"]:
        if req in props and props[req] not in (None, ""):
            continue
        if evt.get(req) not in (None, ""):
            continue
        errors.append("缺少必填字段: " + req)
    return (not errors), errors
