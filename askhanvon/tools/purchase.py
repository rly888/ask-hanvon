"""工具：订单购买（高危：身份绑定 + 风控 + 二次确认 + 幂等，防 Agent 幻觉下单）。

流程：purchase_init（风控→生成待支付订单+确认令牌，2 分钟有效）
      → 用户明确确认 → purchase_confirm（校验令牌→支付，幂等可重试）。
"""
import secrets
import time

from ..db import get_db, new_id
from ..events.collector import emit
from ..obs.logging import get_logger, log_fields
from ..security.antifraud import purchase_risk_check
from .schema import ToolContext, ToolResult, ToolSchema

logger = get_logger("askhanvon.tools.purchase")

TOKEN_TTL_SECONDS = 120
DEMO_PRICE = 39.9  # 演示定价；接入真实商品库后替换

SCHEMA_INIT = ToolSchema(
    name="purchase_init",
    description="创建购书订单（高危：返回确认令牌，需用户二次确认）",
    input_schema={
        "type": "object",
        "properties": {
            "book_title": {"type": "string"},
            "qty": {"type": "integer", "description": "数量，默认 1"},
        },
        "required": ["book_title"],
    },
    required_role="user",
    dangerous=True,
    confirmation_required=True,
    idempotent=False,
    output_schema={"type": "object",
                   "properties": {"order_id": {"type": "string"},
                                  "confirm_token": {"type": "string"},
                                  "price": {"type": "number"}}},
)

SCHEMA_CONFIRM = ToolSchema(
    name="purchase_confirm",
    description="用确认令牌完成支付（幂等，令牌 2 分钟内有效）",
    input_schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "confirm_token": {"type": "string"},
        },
        "required": ["order_id", "confirm_token"],
    },
    required_role="user",
    dangerous=True,
    confirmation_required=True,
    idempotent=True,
    output_schema={"type": "object",
                   "properties": {"order_id": {"type": "string"},
                                  "status": {"type": "string"},
                                  "amount": {"type": "number"}}},
)


def register(reg) -> None:
    reg.register(SCHEMA_INIT, run_init)
    reg.register(SCHEMA_CONFIRM, run_confirm)


def run_init(ctx: ToolContext, book_title: str, qty: int = 1) -> ToolResult:
    db = get_db()
    qty = max(1, min(int(qty or 1), 5))
    book = db.get_book_by_title(book_title or "")
    if not book:
        return ToolResult(ok=False, error="未找到该书: " + str(book_title))

    risk = purchase_risk_check(ctx.user_id)
    if not risk["allowed"]:
        log_fields(logger, 40, "purchase.risk_denied", user_id=ctx.user_id)
        return ToolResult(
            ok=False,
            error="风控拦截：下单过于频繁，请稍后再试。",
            meta={"risk_flags": risk["flags"]},
        )

    order_id = new_id("ord")
    token = secrets.token_hex(4)
    price = round(DEMO_PRICE * qty, 2)
    db.create_order(
        order_id=order_id,
        user_id=ctx.user_id,
        book_id=book["id"],
        qty=qty,
        price=price,
        confirm_token=token,
        token_expires=time.time() + TOKEN_TTL_SECONDS,
        risk_flags=",".join(risk["flags"]),
    )
    return ToolResult(
        ok=True,
        data={
            "order_id": order_id,
            "book_title": book["title"],
            "qty": qty,
            "price": price,
            "confirm_token": token,
            "expires_in": TOKEN_TTL_SECONDS,
            "message": (
                "订单已创建（待确认）。请在 " + str(TOKEN_TTL_SECONDS)
                + " 秒内使用确认令牌完成支付；令牌已发送到你本人会话，任何人向你索要都不要提供。"
            ),
            "risk_flags": risk["flags"],
        },
    )


def run_confirm(ctx: ToolContext, order_id: str, confirm_token: str) -> ToolResult:
    db = get_db()
    order = db.get_order(order_id or "")
    if not order or order["user_id"] != ctx.user_id:
        return ToolResult(ok=False, error="订单不存在或无权操作")
    if order["status"] == "paid":
        # 幂等重放：返回原回执
        return ToolResult(
            ok=True,
            data={"order_id": order_id, "status": "paid", "already_paid": True,
                  "amount": order["price"]},
        )
    if order["status"] != "pending":
        return ToolResult(ok=False, error="订单状态不可支付: " + str(order["status"]))
    if not confirm_token or confirm_token != order["confirm_token"]:
        db.audit_log(ctx.user_id, "purchase_confirm", "purchase_confirm", "", "deny",
                     "bad_token:" + order_id)
        return ToolResult(ok=False, error="确认令牌不正确")
    if time.time() > float(order["token_expires"] or 0):
        db.set_order_status(order_id, "expired")
        return ToolResult(ok=False, error="确认令牌已过期，请重新下单")
    paid_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    db.set_order_status(order_id, "paid", paid_at)
    emit(
        {
            "event_type": "purchase",
            "user_id": ctx.user_id,
            "book_id": order["book_id"],
            "props": {"order_id": order_id, "amount": order["price"]},
        }
    )
    book = db.get_book(order["book_id"]) or {}
    return ToolResult(
        ok=True,
        data={
            "order_id": order_id,
            "status": "paid",
            "amount": order["price"],
            "book_title": book.get("title", ""),
            "paid_at": paid_at,
            "message": "支付成功，已加入我的书架。",
        },
    )
