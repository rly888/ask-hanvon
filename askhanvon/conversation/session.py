"""会话存储与记忆：短期记忆（TTL）+ 多轮上下文 + 长期记忆挂点。

短期记忆 = Redis 带 TTL 的单体等价实现（memory_short 表 + 过期检查）；
长期记忆 = memory_long + 用户画像（由事件异步沉淀）。
"""
import json
import time
import uuid

from ..db import dumps, get_db, loads, now_iso
from ..obs.logging import get_logger, log_fields

logger = get_logger("askhanvon.conversation")

SESSION_TTL_SECONDS = 30 * 60
MEMORY_KEYS = ("last_book", "last_intent", "last_topic")


class SessionStore:
    def get_or_create(self, session_id: str, user_id, title: str = "") -> dict:
        db = get_db()
        sid = session_id or ("s_" + uuid.uuid4().hex[:16])
        row = db.get_session(sid)
        if not row:
            db.create_session(sid, user_id, title or "新会话")
            row = db.get_session(sid)
        elif title:
            db.touch_session(sid, title)
        else:
            db.touch_session(sid)
        return row

    def list_sessions(self, user_id) -> list:
        return get_db().list_sessions(user_id)

    def delete(self, session_id: str) -> None:
        get_db().delete_session(session_id)

    def add_message(self, session_id: str, role: str, content: str, meta: dict) -> None:
        get_db().add_message(session_id, role, content, dumps(meta))
        get_db().touch_session(session_id)

    def get_messages(self, session_id: str, limit: int = 50) -> list:
        rows = get_db().get_messages(session_id, limit)
        for r in rows:
            r["meta"] = loads(r.get("meta"), {}) or {}
        return rows

    # ---- 短期记忆（带 TTL）----
    def memory_set(self, session_id: str, key: str, value, ttl: float = SESSION_TTL_SECONDS) -> None:
        if key not in MEMORY_KEYS:
            return
        get_db().mem_short_set(
            session_id, key, json.dumps(value, ensure_ascii=False), time.time() + ttl
        )

    def memory_get(self, session_id: str, key: str, default=None):
        raw = get_db().mem_short_get(session_id, key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def last_book(self, session_id: str):
        return self.memory_get(session_id, "last_book", "")

    def history_for_llm(self, session_id: str, limit: int = 6) -> list:
        """最近几轮（user/assistant）作为多轮上下文。"""
        msgs = self.get_messages(session_id, limit)
        return [
            {"role": r["role"], "content": (r["content"] or "")[:300]}
            for r in msgs
            if r["role"] in ("user", "assistant") and (r["content"] or "").strip()
        ]

    def title_from_message(self, message: str) -> str:
        t = (message or "").strip().replace("\n", " ")
        return t[:30] if t else "新会话"
