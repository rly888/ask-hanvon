"""SQLite 存储层（领域仓储模式，SQLite / PostgreSQL 双后端）。

单体多模块：按域前缀分表（books_/users_/orders_/ops_/mh_/eval_/audit_），
对应《开发计划》§3.5 目标服务拓扑的数据所有权边界，为后续按域拆库留缝。

安全约定：所有 SQL 均为方法体内的完整字面量，无任何字符串拼接/插值/连接符
进入 SQL 文本；用户可控数据一律通过 ? 占位符绑定；只读方法仅接受
SELECT/WITH。LIKE 过滤在 Python 侧完成（图书量级小，全量取回内存过滤）。
PostgreSQL 模式（DB_ENGINE=postgres + PG_DSN）：占位符经 pgcompat 适配，
FTS5 由 tsvector 方案替代（中文分词先 jieba 预处理，检索行为一致）。
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

from .config import settings
from .pg_repository import PG_DOMAIN_METHODS

_SELECT_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:12]


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


class Database:
    """线程安全 SQLite 仓储（每线程一连接 + 写锁）。"""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    # ---------- 连接与事务 ----------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    def init_schema(self) -> None:
        with self._write_lock:
            self._conn().executescript(
                """
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS books(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT, publisher TEXT, pub_year TEXT,
  isbn TEXT, category TEXT, tags TEXT, cover_emoji TEXT, description TEXT,
  source_file TEXT, version INTEGER DEFAULT 1, status TEXT DEFAULT 'active',
  n_chunks INTEGER DEFAULT 0, embedding_model TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS chapters(
  id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT, vol TEXT, no INTEGER, title TEXT,
  sort_order INTEGER);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT, chapter_id INTEGER,
  chunk_no INTEGER, text TEXT, n_chars INTEGER,
  vol TEXT, chapter_no INTEGER, chapter_title TEXT,
  para_start INTEGER, para_end INTEGER, page_start INTEGER, page_end INTEGER,
  content_hash TEXT, embedding BLOB, embedding_model TEXT DEFAULT '', created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_chunks_book ON chunks(book_id, chapter_no);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(content_hash);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(tokens);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, role TEXT DEFAULT 'user', nickname TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS user_profiles(
  user_id INTEGER PRIMARY KEY, pref_categories TEXT, pref_tags TEXT,
  recent_books TEXT, summary TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS chat_sessions(
  id TEXT PRIMARY KEY, user_id INTEGER, title TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS chat_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
  meta TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id);
CREATE TABLE IF NOT EXISTS memory_short(
  session_id TEXT, key TEXT, value TEXT, expires_at REAL,
  PRIMARY KEY(session_id, key));
CREATE TABLE IF NOT EXISTS memory_long(
  user_id INTEGER, key TEXT, value TEXT, weight REAL DEFAULT 1.0, updated_at TEXT,
  PRIMARY KEY(user_id, key));
CREATE TABLE IF NOT EXISTS user_library(
  user_id INTEGER, book_id TEXT, action TEXT, created_at TEXT,
  PRIMARY KEY(user_id, book_id));
CREATE TABLE IF NOT EXISTS auth_tokens(
  token_hash TEXT PRIMARY KEY, user_id INTEGER, kind TEXT,
  expires_at REAL, revoked INTEGER DEFAULT 0, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id);
CREATE TABLE IF NOT EXISTS orders(
  id TEXT PRIMARY KEY, user_id INTEGER, book_id TEXT, qty INTEGER DEFAULT 1,
  price REAL DEFAULT 0, status TEXT DEFAULT 'pending',
  confirm_token TEXT, token_expires REAL, risk_flags TEXT,
  created_at TEXT, paid_at TEXT);
CREATE TABLE IF NOT EXISTS ops_strategies(
  key TEXT PRIMARY KEY, value TEXT, updated_by TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS ops_campaigns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, slot TEXT, book_ids TEXT,
  weight REAL DEFAULT 1.0, start_at TEXT, end_at TEXT,
  enabled INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS ops_priority_books(
  book_id TEXT, slot TEXT, weight REAL DEFAULT 1.0, reason TEXT,
  PRIMARY KEY(slot, book_id));
CREATE TABLE IF NOT EXISTS ops_experiments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, description TEXT,
  traffic_pct REAL DEFAULT 100, variants TEXT, status TEXT DEFAULT 'running',
  winner TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS ops_prompts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, version INTEGER DEFAULT 1,
  template TEXT, updated_by TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_ops_prompts_name ON ops_prompts(name, version);
CREATE TABLE IF NOT EXISTS ops_assignments(
  exp_id INTEGER, user_id INTEGER, variant TEXT, assigned_at TEXT,
  PRIMARY KEY(exp_id, user_id));
CREATE TABLE IF NOT EXISTS event_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT, status TEXT DEFAULT 'new',
  claimed_at REAL, created_at TEXT, consumed_at TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, session_id TEXT,
  event_type TEXT, book_id TEXT, query TEXT, props TEXT);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, ts);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE TABLE IF NOT EXISTS features_user(
  user_id INTEGER, feature_key TEXT, value REAL DEFAULT 0, value_json TEXT,
  updated_at TEXT, PRIMARY KEY(user_id, feature_key));
CREATE TABLE IF NOT EXISTS features_book(
  book_id TEXT, feature_key TEXT, value REAL DEFAULT 0,
  updated_at TEXT, PRIMARY KEY(book_id, feature_key));
CREATE TABLE IF NOT EXISTS rec_models(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, version TEXT, artifact TEXT,
  metrics TEXT, trained_at TEXT);
CREATE TABLE IF NOT EXISTS rec_candidates(
  user_id INTEGER, model_version TEXT, book_ids TEXT, scores TEXT,
  updated_at TEXT, PRIMARY KEY(user_id, model_version));
CREATE TABLE IF NOT EXISTS mh_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, trace_id TEXT, user_id INTEGER,
  service TEXT, provider TEXT, model TEXT, tier TEXT,
  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
  cost REAL DEFAULT 0, latency_ms REAL DEFAULT 0, status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS mh_quota_daily(
  day TEXT, user_id INTEGER, calls INTEGER DEFAULT 0, tokens INTEGER DEFAULT 0,
  cost REAL DEFAULT 0, PRIMARY KEY(day, user_id));
CREATE TABLE IF NOT EXISTS eval_cases(
  id INTEGER PRIMARY KEY AUTOINCREMENT, suite TEXT, question TEXT, gold_answer TEXT,
  gold_citations TEXT, expect_refusal INTEGER DEFAULT 0, tags TEXT);
CREATE TABLE IF NOT EXISTS eval_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, suite TEXT, total INTEGER,
  passed INTEGER, metrics TEXT, gates TEXT, gate_passed INTEGER, details TEXT);
CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, trace_id TEXT,
  action TEXT, tool TEXT, params TEXT, decision TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS injection_hits(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, source TEXT,
  snippet TEXT, patterns TEXT, score REAL, blocked INTEGER);
"""
            )
            self._migrate()
            self._conn().commit()

    def _migrate(self) -> None:
        """存量库的轻量迁移：缺列则补（表名/列名均为下方字面量）。"""
        conn = self._conn()
        eq = [r["name"] for r in conn.execute("PRAGMA table_info(event_queue)")]
        if "claimed_at" not in eq:
            conn.execute("ALTER TABLE event_queue ADD COLUMN claimed_at REAL")
        ck = [r["name"] for r in conn.execute("PRAGMA table_info(chunks)")]
        if "embedding_model" not in ck:
            conn.execute("ALTER TABLE chunks ADD COLUMN embedding_model TEXT DEFAULT ''")
        bk = [r["name"] for r in conn.execute("PRAGMA table_info(books)")]
        if "embedding_model" not in bk:
            conn.execute("ALTER TABLE books ADD COLUMN embedding_model TEXT DEFAULT ''")

    @contextmanager
    def transaction(self):
        with self._write_lock:
            try:
                yield self._conn()
                self._conn().commit()
            except Exception:
                self._conn().rollback()
                raise

    @staticmethod
    def _rows(cur) -> list:
        return [dict(r) for r in cur.fetchall()]

    def _read(self, sql: str, params: tuple = ()) -> list:
        """只读入口：仅接受 SELECT/WITH 字面量（运行期兜底校验）。"""
        if not _SELECT_RE.match(sql):
            raise ValueError("只读入口仅接受 SELECT/WITH")
        return self._rows(self._conn().execute(sql, params))

    # ---------- kv ----------
    def kv_get(self, key: str, default=None):
        rows = self._read("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def kv_set(self, key: str, value: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO kv(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn().commit()

    # ---------- 图书域 ----------
    def upsert_book(self, meta: dict) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO books(id, title, author, publisher, pub_year, isbn, category,"
                " tags, cover_emoji, description, source_file, n_chunks, embedding_model,"
                " status, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
                " author=excluded.author, publisher=excluded.publisher,"
                " pub_year=excluded.pub_year, isbn=excluded.isbn,"
                " category=excluded.category, tags=excluded.tags,"
                " cover_emoji=excluded.cover_emoji, description=excluded.description,"
                " source_file=excluded.source_file, n_chunks=excluded.n_chunks,"
                " embedding_model=excluded.embedding_model,"
                " status=excluded.status, version=books.version+1,"
                " updated_at=excluded.updated_at",
                (
                    meta["id"], meta["title"], meta.get("author", ""),
                    meta.get("publisher", ""), meta.get("pub_year", ""), meta.get("isbn", ""),
                    meta.get("category", ""), meta.get("tags", ""),
                    meta.get("cover_emoji", "📘"), meta.get("description", ""),
                    meta.get("source_file", ""), meta.get("n_chunks", 0),
                    meta.get("embedding_model", ""),
                    meta.get("status", "active"), meta.get("created_at", now_iso()), now_iso(),
                ),
            )
            self._conn().commit()

    def get_book(self, book_id: str):
        rows = self._read("SELECT * FROM books WHERE id=?", (book_id,))
        return rows[0] if rows else None

    def get_book_by_title(self, title: str):
        rows = self._read("SELECT * FROM books WHERE title=? LIMIT 1", (title,))
        if rows:
            return rows[0]
        for b in self.all_books():
            if title and (title in b["title"] or title in (b["author"] or "")):
                return b
        return None

    def list_books(self, keyword: str = "", category: str = "", limit: int = 50,
                   offset: int = 0) -> list:
        items = self.all_books()
        if category:
            items = [b for b in items if b.get("category") == category]
        if keyword:
            kw = keyword.lower()
            items = [
                b for b in items
                if kw in (b["title"] or "").lower()
                or kw in (b["author"] or "").lower()
                or kw in (b["tags"] or "").lower()
            ]
        return items[offset : offset + limit]

    def all_books(self) -> list:
        return self._read("SELECT * FROM books WHERE status='active' ORDER BY title")

    def delete_book(self, book_id: str) -> None:
        with self._write_lock:
            self._conn().execute("DELETE FROM chunks WHERE book_id=?", (book_id,))
            self._conn().execute(
                "DELETE FROM chunks_fts WHERE rowid NOT IN (SELECT id FROM chunks)"
            )
            self._conn().execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
            self._conn().execute("DELETE FROM books WHERE id=?", (book_id,))
            self._conn().commit()

    def add_chapter(self, book_id: str, vol: str, no: int, title: str, sort_order: int) -> int:
        with self._write_lock:
            cur = self._conn().execute(
                "INSERT INTO chapters(book_id, vol, no, title, sort_order) VALUES(?,?,?,?,?)",
                (book_id, vol, no, title, sort_order),
            )
            self._conn().commit()
            return cur.lastrowid

    def delete_chapters(self, book_id: str) -> None:
        with self._write_lock:
            self._conn().execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
            self._conn().commit()

    def chapters_of_book(self, book_id: str) -> list:
        return self._read(
            "SELECT * FROM chapters WHERE book_id=? ORDER BY sort_order", (book_id,)
        )

    def add_chunk(self, c: dict) -> int:
        with self._write_lock:
            cur = self._conn().execute(
                "INSERT INTO chunks(book_id, chapter_id, chunk_no, text, n_chars, vol,"
                " chapter_no, chapter_title, para_start, para_end, page_start, page_end,"
                " content_hash, embedding_model, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    c["book_id"], c["chapter_id"], c["chunk_no"], c["text"], c["n_chars"],
                    c["vol"], c["chapter_no"], c["chapter_title"], c["para_start"],
                    c["para_end"], c["page_start"], c["page_end"], c["content_hash"],
                    c.get("embedding_model", ""),
                    now_iso(),
                ),
            )
            self._conn().commit()
            return cur.lastrowid

    def delete_chunks(self, book_id: str) -> None:
        with self._write_lock:
            rows = self._conn().execute(
                "SELECT id FROM chunks WHERE book_id=?", (book_id,)
            ).fetchall()
            for r in rows:
                self._conn().execute("DELETE FROM chunks_fts WHERE rowid=?", (r["id"],))
            self._conn().execute("DELETE FROM chunks WHERE book_id=?", (book_id,))
            self._conn().commit()

    def update_chunk_embedding(self, chunk_id: int, blob: bytes) -> None:
        with self._write_lock:
            self._conn().execute(
                "UPDATE chunks SET embedding=? WHERE id=?", (blob, chunk_id)
            )
            self._conn().commit()

    def set_fts(self, chunk_id: int, tokens: str) -> None:
        with self._write_lock:
            # FTS5 虚拟表不支持 UPSERT，用 DELETE + INSERT 保证幂等
            self._conn().execute("DELETE FROM chunks_fts WHERE rowid=?", (chunk_id,))
            self._conn().execute(
                "INSERT INTO chunks_fts(rowid, tokens) VALUES(?,?)",
                (chunk_id, tokens),
            )
            self._conn().commit()

    def delete_fts(self, chunk_ids: list) -> None:
        with self._write_lock:
            for cid in chunk_ids:
                self._conn().execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
            self._conn().commit()

    def fts_search(self, match_expr: str, limit: int = 20) -> list:
        # match_expr 由 nlp.fts_match_query() 构造（词元加引号），经 ? 绑定；
        # JOIN chunks 确保只返回仍存在的知识块（防陈旧索引行）
        return self._read(
            "SELECT chunks_fts.rowid AS chunk_id, bm25(chunks_fts) AS score"
            " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            " WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
            (match_expr, limit),
        )

    def get_chunk(self, chunk_id: int):
        rows = self._read("SELECT * FROM chunks WHERE id=?", (chunk_id,))
        return rows[0] if rows else None

    def get_chunks_all(self) -> list:
        return self._read("SELECT * FROM chunks ORDER BY id")

    def get_chunks_of_book(self, book_id: str) -> list:
        return self._read("SELECT * FROM chunks WHERE book_id=? ORDER BY id", (book_id,))

    def get_chunks_by_book_chapter(self, book_id: str, chapter_no: int) -> list:
        return self._read(
            "SELECT * FROM chunks WHERE book_id=? AND chapter_no=? ORDER BY chunk_no",
            (book_id, chapter_no),
        )

    def count_chunks(self) -> tuple:
        rows = self._read("SELECT COUNT(*) AS n, MAX(id) AS mx FROM chunks")
        if not rows:
            return (0, 0)
        return (rows[0]["n"], rows[0]["mx"] or 0)

    def content_hash_exists(self, content_hash: str) -> bool:
        rows = self._read(
            "SELECT id FROM chunks WHERE content_hash=? LIMIT 1", (content_hash,)
        )
        return bool(rows)

    # ---------- 用户域 ----------
    def create_user(self, username: str, password_hash: str, role: str, nickname: str) -> int:
        with self._write_lock:
            cur = self._conn().execute(
                "INSERT INTO users(username, password_hash, role, nickname, created_at)"
                " VALUES(?,?,?,?,?)",
                (username, password_hash, role, nickname, now_iso()),
            )
            self._conn().commit()
            return cur.lastrowid

    def get_user_by_username(self, username: str):
        rows = self._read("SELECT * FROM users WHERE username=?", (username,))
        return rows[0] if rows else None

    def get_user(self, user_id: int):
        rows = self._read("SELECT * FROM users WHERE id=?", (user_id,))
        return rows[0] if rows else None

    def list_users(self) -> list:
        return self._read(
            "SELECT id, username, role, nickname, created_at FROM users ORDER BY id"
        )

    def get_profile(self, user_id: int):
        rows = self._read("SELECT * FROM user_profiles WHERE user_id=?", (user_id,))
        return rows[0] if rows else None

    def upsert_profile(self, user_id: int, pref_categories: str, pref_tags: str,
                       recent_books: str, summary: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO user_profiles(user_id, pref_categories, pref_tags,"
                " recent_books, summary, updated_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " pref_categories=excluded.pref_categories, pref_tags=excluded.pref_tags,"
                " recent_books=excluded.recent_books, summary=excluded.summary,"
                " updated_at=excluded.updated_at",
                (user_id, pref_categories, pref_tags, recent_books, summary, now_iso()),
            )
            self._conn().commit()

    # ---------- 会话与消息 ----------
    def create_session(self, session_id: str, user_id, title: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO chat_sessions(id, user_id, title, created_at, updated_at)"
                " VALUES(?,?,?,?,?)",
                (session_id, user_id, title, now_iso(), now_iso()),
            )
            self._conn().commit()

    def touch_session(self, session_id: str, title: str = "") -> None:
        with self._write_lock:
            if title:
                self._conn().execute(
                    "UPDATE chat_sessions SET updated_at=?,"
                    " title=CASE WHEN title='' THEN ? ELSE title END WHERE id=?",
                    (now_iso(), title[:40], session_id),
                )
            else:
                self._conn().execute(
                    "UPDATE chat_sessions SET updated_at=? WHERE id=?",
                    (now_iso(), session_id),
                )
            self._conn().commit()

    def get_session(self, session_id: str):
        rows = self._read("SELECT * FROM chat_sessions WHERE id=?", (session_id,))
        return rows[0] if rows else None

    def list_sessions(self, user_id: int) -> list:
        return self._read(
            "SELECT * FROM chat_sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
            (user_id,),
        )

    def delete_session(self, session_id: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "DELETE FROM chat_messages WHERE session_id=?", (session_id,)
            )
            self._conn().execute(
                "DELETE FROM memory_short WHERE session_id=?", (session_id,)
            )
            self._conn().execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
            self._conn().commit()

    def add_message(self, session_id: str, role: str, content: str, meta: str) -> int:
        with self._write_lock:
            cur = self._conn().execute(
                "INSERT INTO chat_messages(session_id, role, content, meta, created_at)"
                " VALUES(?,?,?,?,?)",
                (session_id, role, content, meta, now_iso()),
            )
            self._conn().commit()
            return cur.lastrowid

    def get_messages(self, session_id: str, limit: int = 100) -> list:
        rows = self._read(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return list(reversed(rows))

    # ---------- 记忆 ----------
    def mem_short_get(self, session_id: str, key: str):
        rows = self._read(
            "SELECT value, expires_at FROM memory_short WHERE session_id=? AND key=?",
            (session_id, key),
        )
        if not rows:
            return None
        if rows[0]["expires_at"] and rows[0]["expires_at"] < time.time():
            return None
        return rows[0]["value"]

    def mem_short_set(self, session_id: str, key: str, value: str, expires_at: float) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO memory_short(session_id, key, value, expires_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value,"
                " expires_at=excluded.expires_at",
                (session_id, key, value, expires_at),
            )
            self._conn().commit()

    def mem_long_get(self, user_id: int, key: str):
        rows = self._read(
            "SELECT value FROM memory_long WHERE user_id=? AND key=?", (user_id, key)
        )
        return rows[0]["value"] if rows else None

    def mem_long_set(self, user_id: int, key: str, value: str, weight: float = 1.0) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO memory_long(user_id, key, value, weight, updated_at)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value,"
                " weight=excluded.weight, updated_at=excluded.updated_at",
                (user_id, key, value, weight, now_iso()),
            )
            self._conn().commit()

    # ---------- 藏书库 ----------
    def library_upsert(self, user_id: int, book_id: str, action: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO user_library(user_id, book_id, action, created_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(user_id, book_id) DO UPDATE SET action=excluded.action,"
                " created_at=excluded.created_at",
                (user_id, book_id, action, now_iso()),
            )
            self._conn().commit()

    def library_list(self, user_id: int, action: str = "") -> list:
        if action:
            return self._read(
                "SELECT * FROM user_library WHERE user_id=? AND action=?"
                " ORDER BY created_at DESC",
                (user_id, action),
            )
        return self._read(
            "SELECT * FROM user_library WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        )

    # ---------- 刷新令牌（认证强化 P0-6）----------
    def auth_token_save(self, token_hash: str, user_id: int, kind: str,
                        expires_at: float) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO auth_tokens(token_hash, user_id, kind, expires_at, revoked,"
                " created_at) VALUES(?,?,?,?,0,?)"
                " ON CONFLICT(token_hash) DO UPDATE SET revoked=0,"
                " expires_at=excluded.expires_at",
                (token_hash, user_id, kind, expires_at, now_iso()),
            )
            self._conn().commit()

    def auth_token_get_valid(self, token_hash: str, kind: str):
        rows = self._read(
            "SELECT * FROM auth_tokens WHERE token_hash=? AND kind=? AND revoked=0"
            " AND expires_at > ? LIMIT 1",
            (token_hash, kind, time.time()),
        )
        return rows[0] if rows else None

    def auth_token_revoke(self, token_hash: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "UPDATE auth_tokens SET revoked=1 WHERE token_hash=?", (token_hash,)
            )
            self._conn().commit()

    def auth_token_revoke_all(self, user_id: int, kind: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "UPDATE auth_tokens SET revoked=1 WHERE user_id=? AND kind=?",
                (user_id, kind),
            )
            self._conn().commit()

    # ---------- 订单域 ----------
    def create_order(self, order_id: str, user_id: int, book_id: str, qty: int, price: float,
                     confirm_token: str, token_expires: float, risk_flags: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO orders(id, user_id, book_id, qty, price, status,"
                " confirm_token, token_expires, risk_flags, created_at)"
                " VALUES(?,?,?,?,?,'pending',?,?,?,?)",
                (order_id, user_id, book_id, qty, price, confirm_token, token_expires,
                 risk_flags, now_iso()),
            )
            self._conn().commit()

    def get_order(self, order_id: str):
        rows = self._read("SELECT * FROM orders WHERE id=?", (order_id,))
        return rows[0] if rows else None

    def set_order_status(self, order_id: str, status: str, paid_at: str = "") -> None:
        with self._write_lock:
            self._conn().execute(
                "UPDATE orders SET status=?,"
                " paid_at=CASE WHEN ?='' THEN paid_at ELSE ? END WHERE id=?",
                (status, paid_at, paid_at, order_id),
            )
            self._conn().commit()

    def count_recent_orders(self, user_id: int, since_ts: str) -> int:
        rows = self._read(
            "SELECT COUNT(*) AS n FROM orders WHERE user_id=? AND created_at>=?",
            (user_id, since_ts),
        )
        return rows[0]["n"] if rows else 0

    # ---------- 运营域 ----------
    def strategy_get(self, key: str):
        rows = self._read("SELECT value FROM ops_strategies WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def strategy_set(self, key: str, value: str, by: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO ops_strategies(key, value, updated_by, updated_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (key, value, by, now_iso()),
            )
            self._conn().commit()

    def strategy_all(self) -> list:
        return self._read("SELECT * FROM ops_strategies ORDER BY key")

    def campaign_create(self, name: str, slot: str, book_ids: str, weight: float,
                        start_at: str, end_at: str, enabled: int) -> int:
        with self._write_lock:
            cur = self._conn().execute(
                "INSERT INTO ops_campaigns(name, slot, book_ids, weight, start_at, end_at,"
                " enabled, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (name, slot, book_ids, weight, start_at, end_at, enabled, now_iso()),
            )
            self._conn().commit()
            return cur.lastrowid

    def campaign_update(self, campaign_id: int, enabled: int) -> None:
        with self._write_lock:
            self._conn().execute(
                "UPDATE ops_campaigns SET enabled=? WHERE id=?", (enabled, campaign_id)
            )
            self._conn().commit()

    def campaign_delete(self, campaign_id: int) -> None:
        with self._write_lock:
            self._conn().execute("DELETE FROM ops_campaigns WHERE id=?", (campaign_id,))
            self._conn().commit()

    def campaign_list(self) -> list:
        return self._read("SELECT * FROM ops_campaigns ORDER BY id DESC")

    def campaigns_active(self, now: str) -> list:
        return self._read(
            "SELECT * FROM ops_campaigns WHERE enabled=1"
            " AND (start_at='' OR start_at IS NULL OR start_at<=?)"
            " AND (end_at='' OR end_at IS NULL OR end_at>=?)",
            (now, now),
        )

    def priority_upsert(self, slot: str, book_id: str, weight: float, reason: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO ops_priority_books(book_id, slot, weight, reason)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(slot, book_id) DO UPDATE SET weight=excluded.weight,"
                " reason=excluded.reason",
                (book_id, slot, weight, reason),
            )
            self._conn().commit()

    def priority_list(self, slot: str = "") -> list:
        if slot:
            return self._read(
                "SELECT * FROM ops_priority_books WHERE slot=? ORDER BY weight DESC", (slot,)
            )
        return self._read("SELECT * FROM ops_priority_books ORDER BY slot, weight DESC")

    def priority_delete(self, slot: str, book_id: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "DELETE FROM ops_priority_books WHERE slot=? AND book_id=?", (slot, book_id)
            )
            self._conn().commit()

    def exp_create(self, name: str, description: str, traffic_pct: float,
                   variants: str) -> int:
        with self._write_lock:
            cur = self._conn().execute(
                "INSERT INTO ops_experiments(name, description, traffic_pct, variants,"
                " created_at) VALUES(?,?,?,?,?)",
                (name, description, traffic_pct, variants, now_iso()),
            )
            self._conn().commit()
            return cur.lastrowid

    def exp_list(self) -> list:
        return self._read("SELECT * FROM ops_experiments ORDER BY id DESC")

    def exp_get_by_name(self, name: str):
        rows = self._read("SELECT * FROM ops_experiments WHERE name=?", (name,))
        return rows[0] if rows else None

    def exp_get(self, exp_id: int):
        rows = self._read("SELECT * FROM ops_experiments WHERE id=?", (exp_id,))
        return rows[0] if rows else None

    def exp_finish(self, exp_id: int, status: str, winner: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "UPDATE ops_experiments SET status=?, winner=? WHERE id=?",
                (status, winner, exp_id),
            )
            self._conn().commit()

    def exp_assign(self, exp_id: int, user_id: int, variant: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO ops_assignments(exp_id, user_id, variant, assigned_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(exp_id, user_id) DO UPDATE SET variant=excluded.variant",
                (exp_id, user_id, variant, now_iso()),
            )
            self._conn().commit()

    def exp_get_assignment(self, exp_id: int, user_id: int):
        rows = self._read(
            "SELECT variant FROM ops_assignments WHERE exp_id=? AND user_id=?",
            (exp_id, user_id),
        )
        return rows[0]["variant"] if rows else None

    # ---------- Prompt 版本管理（P1-5）----------
    def prompt_latest(self, name: str):
        rows = self._read(
            "SELECT * FROM ops_prompts WHERE name=? ORDER BY version DESC LIMIT 1",
            (name,),
        )
        return rows[0] if rows else None

    def prompt_save(self, name: str, version: int, template: str, by: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO ops_prompts(name, version, template, updated_by, updated_at)"
                " VALUES(?,?,?,?,?)",
                (name, version, template, by, now_iso()),
            )
            self._conn().commit()

    # ---------- 埋点与离线域 ----------
    def event_enqueue(self, payload: str) -> int:
        with self._write_lock:
            cur = self._conn().execute(
                "INSERT INTO event_queue(payload, status, created_at) VALUES(?, 'new', ?)",
                (payload, now_iso()),
            )
            self._conn().commit()
            return cur.lastrowid

    def event_claim_batch(self, limit: int = 200, lease_seconds: float = 300.0) -> list:
        """认领一批事件（多实例安全）：

        UPDATE ... RETURNING 原子认领 status='new' 或租约过期的 'processing' 行，
        两个消费者不可能认领到同一批（P0-2 优化项）。
        """
        deadline = time.time() - lease_seconds
        with self._write_lock:
            cur = self._conn().execute(
                "UPDATE event_queue SET status='processing', claimed_at=?"
                " WHERE id IN (SELECT id FROM event_queue"
                " WHERE status='new'"
                " OR (status='processing' AND (claimed_at IS NULL OR claimed_at < ?))"
                " ORDER BY id LIMIT ?)"
                " RETURNING id, payload",
                (time.time(), deadline, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
            self._conn().commit()
            return rows

    def event_mark_done(self, ids: list) -> None:
        with self._write_lock:
            for i in ids:
                self._conn().execute(
                    "UPDATE event_queue SET status='done', consumed_at=? WHERE id=?",
                    (now_iso(), i),
                )
            self._conn().commit()

    def event_release(self, ids: list) -> None:
        """消费失败回退为 new（等待下次认领重试）。"""
        with self._write_lock:
            for i in ids:
                self._conn().execute(
                    "UPDATE event_queue SET status='new', claimed_at=NULL WHERE id=?",
                    (i,),
                )
            self._conn().commit()

    def event_insert(self, ts: str, user_id, session_id: str, event_type: str, book_id: str,
                     query: str, props: str) -> int:
        with self._write_lock:
            cur = self._conn().execute(
                "INSERT INTO events(ts, user_id, session_id, event_type, book_id, query,"
                " props) VALUES(?,?,?,?,?,?,?)",
                (ts, user_id, session_id, event_type, book_id, query, props),
            )
            self._conn().commit()
            return cur.lastrowid

    def events_query(self, event_type: str = "", since: str = "", user_id=None,
                     limit: int = 1000) -> list:
        if event_type and since and user_id is not None:
            return self._read(
                "SELECT * FROM events WHERE event_type=? AND ts>=? AND user_id=?"
                " ORDER BY id LIMIT ?",
                (event_type, since, user_id, limit),
            )
        if event_type and since:
            return self._read(
                "SELECT * FROM events WHERE event_type=? AND ts>=? ORDER BY id LIMIT ?",
                (event_type, since, limit),
            )
        if since:
            return self._read(
                "SELECT * FROM events WHERE ts>=? ORDER BY id LIMIT ?", (since, limit)
            )
        return self._read("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def events_count(self, event_type: str, since: str = "") -> int:
        if since:
            rows = self._read(
                "SELECT COUNT(*) AS n FROM events WHERE event_type=? AND ts>=?",
                (event_type, since),
            )
        else:
            rows = self._read(
                "SELECT COUNT(*) AS n FROM events WHERE event_type=?", (event_type,)
            )
        return rows[0]["n"] if rows else 0

    def feature_user_upsert(self, user_id: int, key: str, value: float,
                            value_json: str = "") -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO features_user(user_id, feature_key, value, value_json,"
                " updated_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(user_id, feature_key) DO UPDATE SET value=excluded.value,"
                " value_json=excluded.value_json, updated_at=excluded.updated_at",
                (user_id, key, value, value_json, now_iso()),
            )
            self._conn().commit()

    def feature_user_map(self, user_id: int) -> dict:
        rows = self._read(
            "SELECT feature_key, value, value_json FROM features_user WHERE user_id=?",
            (user_id,),
        )
        return {r["feature_key"]: (r["value"], loads(r["value_json"])) for r in rows}

    def feature_book_upsert(self, book_id: str, key: str, value: float) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO features_book(book_id, feature_key, value, updated_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(book_id, feature_key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (book_id, key, value, now_iso()),
            )
            self._conn().commit()

    def feature_book_map(self) -> dict:
        rows = self._read("SELECT book_id, feature_key, value FROM features_book")
        out: dict = {}
        for r in rows:
            out.setdefault(r["book_id"], {})[r["feature_key"]] = r["value"]
        return out

    def rec_model_save(self, kind: str, version: str, artifact: str,
                       metrics_json: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO rec_models(kind, version, artifact, metrics, trained_at)"
                " VALUES(?,?,?,?,?)",
                (kind, version, artifact, metrics_json, now_iso()),
            )
            self._conn().commit()

    def rec_model_latest(self, kind: str):
        rows = self._read(
            "SELECT * FROM rec_models WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)
        )
        return rows[0] if rows else None

    def rec_candidates_save(self, user_id: int, version: str, book_ids: str,
                            scores: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO rec_candidates(user_id, model_version, book_ids, scores,"
                " updated_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(user_id, model_version) DO UPDATE SET"
                " book_ids=excluded.book_ids, scores=excluded.scores,"
                " updated_at=excluded.updated_at",
                (user_id, version, book_ids, scores, now_iso()),
            )
            self._conn().commit()

    def rec_candidates_get(self, user_id: int, version: str):
        rows = self._read(
            "SELECT * FROM rec_candidates WHERE user_id=? AND model_version=?",
            (user_id, version),
        )
        return rows[0] if rows else None

    # ---------- 模型治理域 ----------
    def mh_call_log(self, ts: str, trace_id: str, user_id, service: str, provider: str,
                    model: str, tier: str, prompt_tokens: int, completion_tokens: int,
                    cost: float, latency_ms: float, status: str, error: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO mh_calls(ts, trace_id, user_id, service, provider, model,"
                " tier, prompt_tokens, completion_tokens, cost, latency_ms, status, error)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, trace_id, user_id, service, provider, model, tier, prompt_tokens,
                 completion_tokens, cost, latency_ms, status, error),
            )
            self._conn().commit()

    def mh_quota_incr(self, day: str, user_id, calls: int, tokens: int, cost: float) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO mh_quota_daily(day, user_id, calls, tokens, cost)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(day, user_id) DO UPDATE SET calls=mh_quota_daily.calls+?,"
                " tokens=mh_quota_daily.tokens+?, cost=mh_quota_daily.cost+?",
                (day, user_id if user_id is not None else 0, calls, tokens, cost, calls,
                 tokens, cost),
            )
            self._conn().commit()

    def mh_quota_get(self, day: str, user_id):
        rows = self._read(
            "SELECT calls, tokens, cost FROM mh_quota_daily WHERE day=? AND user_id=?",
            (day, user_id if user_id is not None else 0),
        )
        return rows[0] if rows else None

    def mh_stats_by_model(self, since: str) -> list:
        return self._read(
            "SELECT service, provider, model, COUNT(*) AS calls, SUM(prompt_tokens) AS pt,"
            " SUM(completion_tokens) AS ct, SUM(cost) AS cost, AVG(latency_ms) AS avg_ms"
            " FROM mh_calls WHERE ts>=? GROUP BY service, provider, model ORDER BY calls DESC",
            (since,),
        )

    def mh_recent_calls(self, limit: int = 100) -> list:
        return self._read("SELECT * FROM mh_calls ORDER BY id DESC LIMIT ?", (limit,))

    # ---------- 评测域 ----------
    def eval_cases_replace(self, suite: str, cases: list) -> None:
        with self._write_lock:
            self._conn().execute("DELETE FROM eval_cases WHERE suite=?", (suite,))
            for c in cases:
                self._conn().execute(
                    "INSERT INTO eval_cases(suite, question, gold_answer, gold_citations,"
                    " expect_refusal, tags) VALUES(?,?,?,?,?,?)",
                    (
                        suite, c["question"], c.get("gold_answer", ""),
                        dumps(c.get("gold_citations", [])),
                        1 if c.get("expect_refusal") else 0,
                        dumps(c.get("tags", [])),
                    ),
                )
            self._conn().commit()

    def eval_cases_get(self, suite: str) -> list:
        rows = self._read(
            "SELECT * FROM eval_cases WHERE suite=? ORDER BY id", (suite,)
        )
        for r in rows:
            r["gold_citations"] = loads(r["gold_citations"], [])
            r["tags"] = loads(r["tags"], [])
        return rows

    def eval_run_save(self, suite: str, total: int, passed: int, metrics_json: str,
                      gates_json: str, gate_passed: int, details_json: str) -> int:
        with self._write_lock:
            cur = self._conn().execute(
                "INSERT INTO eval_runs(ts, suite, total, passed, metrics, gates,"
                " gate_passed, details) VALUES(?,?,?,?,?,?,?,?)",
                (now_iso(), suite, total, passed, metrics_json, gates_json, gate_passed,
                 details_json),
            )
            self._conn().commit()
            return cur.lastrowid

    def eval_runs_list(self, suite: str = "", limit: int = 20) -> list:
        if suite:
            return self._read(
                "SELECT id, ts, suite, total, passed, metrics, gates, gate_passed"
                " FROM eval_runs WHERE suite=? ORDER BY id DESC LIMIT ?",
                (suite, limit),
            )
        return self._read(
            "SELECT id, ts, suite, total, passed, metrics, gates, gate_passed"
            " FROM eval_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def eval_run_get(self, run_id: int):
        rows = self._read("SELECT * FROM eval_runs WHERE id=?", (run_id,))
        return rows[0] if rows else None

    # ---------- 安全审计域 ----------
    def audit_log(self, user_id, action: str, tool: str, params: str, decision: str,
                  reason: str) -> None:
        from .obs.tracing import get_trace_id

        with self._write_lock:
            self._conn().execute(
                "INSERT INTO audit_logs(ts, user_id, trace_id, action, tool, params,"
                " decision, reason) VALUES(?,?,?,?,?,?,?,?)",
                (now_iso(), user_id, get_trace_id(), action, tool, params, decision, reason),
            )
            self._conn().commit()

    def audit_list(self, limit: int = 100, decision: str = "") -> list:
        if decision:
            return self._read(
                "SELECT * FROM audit_logs WHERE decision=? ORDER BY id DESC LIMIT ?",
                (decision, limit),
            )
        return self._read("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))

    def injection_hit(self, user_id, source: str, snippet: str, patterns: str, score: float,
                      blocked: int) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO injection_hits(ts, user_id, source, snippet, patterns, score,"
                " blocked) VALUES(?,?,?,?,?,?,?)",
                (now_iso(), user_id, source, snippet[:200], patterns, score, blocked),
            )
            self._conn().commit()

    def injection_list(self, limit: int = 100) -> list:
        return self._read(
            "SELECT * FROM injection_hits ORDER BY id DESC LIMIT ?", (limit,)
        )


_db: Database | None = None
_db_lock = threading.Lock()


class HybridStore:
    """按域路由的存储门面（方案 A 多引擎分工）：

    - PostgreSQL 后端开启时（DB_ENGINE=postgres）：业务域方法委托给 PgRepository，
      检索域方法（图书/章节/知识块/FTS）留在 SQLite 检索库；
    - SQLite 后端（默认）：全部委托给本进程 SQLite 仓储（原单体行为不变）。
    """

    def __init__(self, sqlite_db: Database, pg_repo=None):
        self._sqlite = sqlite_db
        self._pg = pg_repo

    def __getattr__(self, name: str):
        if self._pg is not None and name in PG_DOMAIN_METHODS:
            return getattr(self._pg, name)
        return getattr(self._sqlite, name)


def get_db():
    """返回领域存储门面。调用方无感：DB_ENGINE 切换只改环境变量。"""
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                sqlite_store = Database(settings.db_path)
                sqlite_store.init_schema()
                store = HybridStore(sqlite_store)
                if settings.db_engine == "postgres":
                    from . import pg_repository

                    store._pg = pg_repository.get_pg()
                _db = store
    return _db
