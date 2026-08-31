"""推荐引擎编排：召回 → 精排 → 规则重排 → 解释 → 曝光埋点（A/B 分桶标注）。"""
from ..db import get_db
from ..events.collector import emit
from ..obs.logging import get_logger, log_fields
from ..obs.metrics import metrics
from ..ops.ab import ab_service
from ..ops.strategies import strategies
from .explain import breakdown, explain_item
from .rank import rank_candidates
from .recall import recall_candidates
from .rules import apply_rules

logger = get_logger("askhanvon.recommend")

AB_EXPERIMENT = "rec_rank_v1"


class RecommendEngine:
    def recommend(self, user_id, scene: str = "homepage", top_k: int = 6,
                  book_hint: str = "", track: bool = True,
                  session_id: str = "") -> list:
        """返回可解释推荐列表；track=False 用于离线评测（不产生曝光事件）。

        session_id（P3-4）：会话内实时信号——用户刚问过的书（会话记忆 last_book）
        的同类/同作者作品加权重排，实现"刚看完西游记 → 立刻推同类"。
        """
        t0_ident = None
        import time

        t0_ident = time.perf_counter()
        db = get_db()
        catalog = {b["id"]: b for b in db.all_books()}

        candidates = recall_candidates(user_id, scene)
        ranked = rank_candidates(candidates, user_id)

        # P3-4：会话实时信号（不改变精排特征空间，只做在线加权，避免模型版本错位）
        if session_id and strategies.get("rec.session_boost", True):
            from ..conversation.session import SessionStore

            last_title = SessionStore().last_book(session_id)
            if last_title:
                last_book = db.get_book_by_title(last_title)
                if last_book:
                    last_tags = {t for t in (last_book.get("tags") or "").split(",") if t}
                    for it in ranked:
                        b = catalog.get(it["book_id"], {})
                        b_tags = {t for t in (b.get("tags") or "").split(",") if t}
                        same_cat = b.get("category") == last_book.get("category")
                        same_author = b.get("author") and \
                            b.get("author") == last_book.get("author")
                        shared_tag = bool(last_tags & b_tags)
                        if (same_cat or same_author or shared_tag) \
                                and b.get("id") != last_book["id"]:
                            it["score"] = round(float(it["score"]) + 0.25, 4)
                            it["_session_seed"] = last_book["title"]
                    ranked.sort(key=lambda x: x["score"], reverse=True)

        # A/B：B 组启用离线训练权重（strategy 由 assign 的 params 决定，
        # 这里只读取分配结果并打标，不直接改全局策略）
        variant = ab_service.assign(AB_EXPERIMENT, user_id)["variant"]

        final = apply_rules(ranked, top_k=top_k, user_id=user_id)

        items = []
        for pos, item in enumerate(final, start=1):
            book = catalog.get(item["book_id"], {})
            exp = explain_item(item, catalog)
            reasons = exp["reasons"]
            if item.get("_session_seed"):
                reasons.insert(0, "你刚在看《" + str(item["_session_seed"]) + "》")
            payload = {
                "position": pos,
                "book_id": item["book_id"],
                "title": book.get("title", ""),
                "author": book.get("author", ""),
                "category": book.get("category", ""),
                "cover_emoji": book.get("cover_emoji", "📘"),
                "description": book.get("description", ""),
                "score": item["score"],
                "reasons": reasons,
                "channels": exp["channels"],
                "breakdown": breakdown(item),
                "variant": variant,
            }
            items.append(payload)
            if track:
                try:
                    emit(
                        {
                            "event_type": "impression",
                            "user_id": user_id,
                            "book_id": item["book_id"],
                            "props": {"scene": scene, "variant": variant, "position": pos},
                        }
                    )
                except ValueError:
                    pass
        latency = (time.perf_counter() - t0_ident) * 1000
        metrics.observe("recommend_latency_ms", latency)
        log_fields(
            logger, 20, "recommend.done", user_id=user_id, scene=scene, n=len(items),
            variant=variant, latency_ms=round(latency, 1),
        )
        return items


_engine: RecommendEngine | None = None


def get_rec_engine() -> RecommendEngine:
    global _engine
    if _engine is None:
        _engine = RecommendEngine()
    return _engine
