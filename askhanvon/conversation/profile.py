"""用户画像：偏好分布 / 最近阅读 / 画像摘要（注入对话与推荐）。"""
import json
import time

from ..db import get_db


class ProfileService:
    def _features(self, user_id) -> dict:
        return get_db().feature_user_map(user_id)

    def profile(self, user_id) -> dict:
        feats = self._features(user_id)
        _, cat_dist = feats.get("cat_click", (0.0, {}))
        _, recent = feats.get("recent_books", (0.0, []))
        clicks, _ = feats.get("total_clicks", (0.0, None))
        minutes, _ = feats.get("read_minutes", (0.0, None))
        cats = sorted((cat_dist or {}).items(), key=lambda x: x[1], reverse=True)
        db = get_db()
        recent_titles = []
        for bid in (recent or [])[:10]:
            b = db.get_book(bid)
            if b:
                recent_titles.append({"book_id": bid, "title": b["title"]})
        return {
            "pref_categories": [c for c, _ in cats[:3]],
            "category_dist": dict(cats),
            "recent_books": recent_titles,
            "total_clicks": int(clicks or 0),
            "read_minutes": round(float(minutes or 0.0), 1),
        }

    def record_click(self, user_id, book_id) -> None:
        """点击行为的画像增量更新（事件消费侧调用）。"""
        db = get_db()
        book = db.get_book(book_id)
        if not book:
            return
        feats = self._features(user_id)
        val, dist = feats.get("cat_click", (0.0, {}))
        dist = dict(dist or {})
        cat = book.get("category") or "未分类"
        dist[cat] = float(dist.get(cat, 0.0)) + 1.0
        db.feature_user_upsert(user_id, "cat_click", float(sum(dist.values())), json.dumps(dist, ensure_ascii=False))
        _, recent = feats.get("recent_books", (0.0, []))
        recent = list(recent or [])
        if book_id in recent:
            recent.remove(book_id)
        recent.insert(0, book_id)
        db.feature_user_upsert(
            user_id, "recent_books", float(len(recent[:10])),
            json.dumps(recent[:10], ensure_ascii=False),
        )

    def prompt_block(self, user_id) -> str:
        """注入 LLM 提示词的画像摘要段（最小授权：只给偏好类信息，不给隐私）。"""
        if not user_id:
            return "（游客）"
        p = self.profile(user_id)
        parts = []
        if p["pref_categories"]:
            parts.append("偏好分类: " + "、".join(p["pref_categories"]))
        if p["recent_books"]:
            parts.append("最近读过: " + "、".join("《" + b["title"] + "》" for b in p["recent_books"][:3]))
        if p["read_minutes"]:
            parts.append("累计阅读 " + str(p["read_minutes"]) + " 分钟")
        return "；".join(parts) if parts else "（暂无阅读画像）"
