"""Synthesizer：把工具结果整合为面向用户的多模态回复（文本/卡片/对比表/订单）。"""
from ..generation.answer import get_answer_generator
from ..modelhub.gateway import LLMUnavailable, get_gateway
from ..modelhub import quota as quota_mod
from ..obs.logging import get_logger, log_fields
from ..tools.schema import DEGRADE_MESSAGE, ToolResult

logger = get_logger("askhanvon.agent")


class Synthesizer:
    def synthesize(self, intent: str, message: str, results: list, profile_block: str = "",
                   user_id=None) -> dict:
        """返回 {type, text, items, citations, data, steps_ok}。"""
        def first_ok(kind: str):
            for r in results:
                if r.ok and kind in r.meta.get("reason", "") + r.meta.get("tool", ""):
                    return r
            return results[0] if results else None

        def by_tool(tool: str):
            for r in results:
                if r.meta.get("tool") == tool:
                    return r
            return None

        # ---- RAG 问答 ----
        r = by_tool("book_qa")
        if r is not None:
            if r.ok:
                d = r.data
                return {
                    "type": "qa",
                    "text": d.get("answer", ""),
                    "citations": d.get("citations", []),
                    "refused": d.get("refused", False),
                    "confidence": d.get("confidence", 0),
                    "model": d.get("model", ""),
                    "degraded": d.get("degraded", False),
                    "verified_ratio": d.get("verified_ratio", 0),
                    "usage": d.get("usage", {}),
                    "prompt_version": d.get("prompt_version", 0),
                    "retrieval": d.get("retrieval", []),
                }
            return self._fallback(r)

        # ---- 推荐 ----
        r = by_tool("recommend_books")
        if r is not None and r.ok:
            items = r.data.get("items", [])
            lead = self._intro(items, user_id)
            return {"type": "cards", "text": lead, "items": items,
                    "card_kind": "recommend"}
        if r is not None:
            return self._fallback(r)

        # ---- 搜索 ----
        r = by_tool("book_search")
        if r is not None and r.ok:
            items = r.data.get("results", [])
            if intent == "purchase" and items:
                return {
                    "type": "text",
                    "text": "为你找到以下图书，回复「买《书名》」即可下单（下单需登录并二次确认）：",
                    "items": items, "card_kind": "search",
                }
            text = "找到 " + str(len(items)) + " 本相关图书：" if items else "没有找到相关图书，换个关键词试试？"
            return {"type": "cards", "text": text, "items": items, "card_kind": "search"}
        if r is not None:
            return self._fallback(r)

        # ---- 比较 ----
        r = by_tool("compare_books")
        if r is not None:
            if r.ok:
                return {"type": "comparison", "text": "对比结果如下：",
                        "data": r.data.get("comparison", {})}
            return self._fallback(r)

        # ---- 书架 ----
        r = by_tool("my_library")
        if r is not None:
            if r.ok:
                d = r.data
                return {
                    "type": "cards" if d.get("items") else "text",
                    "text": d.get("message", "") or ("书架共 " + str(len(d.get("items", []))) + " 本"),
                    "items": d.get("items", []), "card_kind": "library",
                }
            return self._fallback(r)

        # ---- 下单 ----
        r = by_tool("purchase_init")
        if r is not None:
            if r.ok:
                d = r.data
                return {
                    "type": "order",
                    "text": d.get("message", ""),
                    "data": {k: d.get(k) for k in
                             ("order_id", "book_title", "qty", "price", "confirm_token",
                              "expires_in")},
                }
            return self._fallback(r)

        # ---- 闲聊 / 无工具 ----
        return self._chitchat(message, user_id)

    def _fallback(self, r: ToolResult) -> dict:
        return {"type": "text", "text": r.data.get("message") or r.error or DEGRADE_MESSAGE,
                "degraded": True}

    def _intro(self, items: list, user_id) -> str:
        if not items:
            return "暂时没有合适的推荐，稍后再来看看。"
        top = items[0]
        reasons = "、".join(top.get("reasons", [])[:2]) or "为你精选"
        return "为你挑选了 " + str(len(items)) + " 本（首推《" + top["title"] + "》：" + reasons + "）："

    def _chitchat(self, message: str, user_id) -> dict:
        gw = get_gateway()
        if gw.llm_ready():
            try:
                res = gw.chat(
                    [
                        {"role": "system",
                         "content": "你是「问小汉」，图书阅读助手。友好简短地回应用户，并自然引导到图书话题。60 字以内。"},
                        {"role": "user", "content": (message or "")[:120]},
                    ],
                    tier="weak", user_id=user_id, scene="chitchat", max_tokens=120,
                )
                return {"type": "text", "text": res.text.strip()}
            except (LLMUnavailable, quota_mod.QuotaExceeded) as e:
                log_fields(logger, 30, "synth.chitchat_degrade", error=str(e)[:100])
        return {
            "type": "text",
            "text": "你好，我是问小汉 📚 可以问我书的内容（如「《西游记》讲了什么」），或让我推荐几本好书。",
        }
