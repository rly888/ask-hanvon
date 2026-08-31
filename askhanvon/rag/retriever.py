"""RAG 检索：BM25(SQLite FTS5) + 向量 双路召回 → 加权融合。

- BM25：jieba 分词后写入 FTS5，bm25() 排序（Elasticsearch 的单体等价实现）
- 向量：chunk embedding 存 SQLite BLOB，内存矩阵暴力余弦（数据量 < 百万级够用，
  接 Milvus 只需替换 VectorSearch 实现）
- 融合权重经策略中心可配（ops.strategies: retrieval.weights）
"""
import threading

import numpy as np

from ..config import settings
from ..db import get_db
from ..modelhub.gateway import get_gateway
from ..nlp import fts_match_query, minmax
from ..ops.strategies import strategies


class HybridRetriever:
    def __init__(self):
        self._lock = threading.Lock()
        self._vec_sig = None
        self._vec_ids: list = []
        self._vec_matrix: np.ndarray | None = None
        self._book_map: dict = {}

    # ---------- 向量索引缓存 ----------
    def _refresh_if_needed(self):
        n, mx = get_db().count_chunks()
        sig = (n, mx)
        if self._vec_sig == sig and self._vec_matrix is not None:
            return
        with self._lock:
            if self._vec_sig == sig and self._vec_matrix is not None:
                return
            rows = get_db().get_chunks_all()
            ids, vecs = [], []
            dim = settings.embed_dim
            for r in rows:
                blob = r.get("embedding")
                if not blob:
                    continue
                v = np.frombuffer(blob, dtype=np.float32)
                if v.size != dim or not np.any(v):
                    continue
                ids.append(r["id"])
                vecs.append(v)
            self._vec_ids = ids
            self._vec_matrix = (
                np.stack(vecs) if vecs else np.zeros((0, dim), dtype=np.float32)
            )
            self._book_map = {b["id"]: b for b in get_db().all_books()}
            self._vec_sig = sig

    def invalidate(self):
        with self._lock:
            self._vec_sig = None

    # ---------- 单路召回 ----------
    def bm25_search(self, query: str, top_k: int) -> list:
        expr = fts_match_query(query)
        if not expr:
            return []
        rows = get_db().fts_search(expr, top_k)
        # SQLite bm25() 越负越相关 → 取负得「越大越好」
        out = [(r["chunk_id"], -float(r["score"])) for r in rows if r["score"] < 0]
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    def vector_search(self, query: str, top_k: int) -> list:
        self._refresh_if_needed()
        if not self._vec_ids:
            return []
        qv = get_gateway().embed([query])[0]
        sims = self._vec_matrix @ qv
        order = np.argsort(-sims)[:top_k]
        return [(self._vec_ids[i], float(sims[i])) for i in order]

    # ---------- 混合检索 ----------
    def retrieve(self, query: str, top_k: int | None = None, book_ids: list | None = None,
                 user_id=None) -> list:
        """单查询混合检索（双路召回 → 策略化融合 → 过滤）。"""
        return self._retrieve_single(query, top_k=top_k, book_ids=book_ids, user_id=user_id)

    def retrieve_multi(self, query: str, top_k: int | None = None,
                       book_ids: list | None = None, user_id=None,
                       extra_queries: list | None = None) -> list:
        """多查询检索（P1-2）：原查询 + LLM 改写查询各检一路，RRF 融合。

        extra_queries 显式给出时直接使用（测试/上游已改写）；
        否则按策略 retrieval.multi_query 走 rewrite_query。
        """
        queries = [query]
        if extra_queries is None:
            if strategies.get("retrieval.multi_query", True):
                from .rewrite import rewrite_query

                rw = rewrite_query(query, user_id=user_id)
                if rw and rw != query:
                    queries.append(rw)
        else:
            queries += [q for q in extra_queries if q and q != query]
        if len(queries) == 1:
            return self._retrieve_single(query, top_k=top_k, book_ids=book_ids,
                                         user_id=user_id)
        # 各路检索 → RRF 融合
        k = top_k or settings.retrieval_top_k
        per_query = [
            self._retrieve_single(q, top_k=k, book_ids=book_ids, user_id=user_id)
            for q in queries
        ]
        rrf: dict = {}
        items: dict = {}
        for results in per_query:
            for rank, item in enumerate(results, start=1):
                cid = item["chunk_id"]
                rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60.0 + rank)
                if cid not in items or item["score"] > items[cid]["score"]:
                    items[cid] = item
        if not rrf:
            return []
        norm = minmax(list(rrf.values()))
        ranked = sorted(zip(rrf.keys(), norm), key=lambda x: x[1], reverse=True)
        out = []
        for cid, score in ranked[:k]:
            item = dict(items[cid])
            item["score"] = round(float(score), 4)
            item["multi_query"] = True
            out.append(item)
        return out

    def _retrieve_single(self, query: str, top_k: int | None = None,
                         book_ids: list | None = None, user_id=None) -> list:
        """双路召回 → 策略化融合（rrf | linear）→ 过滤。

        RRF（Reciprocal Rank Fusion，P0-3）：score = Σ 1/(k+rank)，按通道内名次融合，
        消除两路分数量纲差异；输出前做 minmax 归一以保持置信度语义。
        """
        self._refresh_if_needed()
        k = top_k or settings.retrieval_top_k
        w = strategies.get("retrieval.weights", None) or {
            "bm25": settings.bm25_weight, "vector": settings.vector_weight
        }
        fusion = str(strategies.get("retrieval.fusion", "rrf")).lower()
        bm = self.bm25_search(query, k)
        vs = self.vector_search(query, k)

        fused: dict = {}
        if bm and vs:
            if fusion == "rrf":
                rrf_k = float(strategies.get("retrieval.rrf_k", 60))
                rrf: dict = {}
                for channel in (bm, vs):
                    for rank, (cid, _s) in enumerate(channel, start=1):
                        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (rrf_k + rank)
                norm = minmax(list(rrf.values()))
                fused = dict(zip(rrf.keys(), norm))
            else:
                bm_n = dict(zip([x[0] for x in bm], minmax([x[1] for x in bm])))
                vs_n = dict(zip([x[0] for x in vs], minmax([x[1] for x in vs])))
                for cid, s in bm_n.items():
                    fused[cid] = fused.get(cid, 0.0) + w.get("bm25", 0.6) * s
                for cid, s in vs_n.items():
                    fused[cid] = fused.get(cid, 0.0) + w.get("vector", 0.4) * s
        elif bm:
            for cid, s in zip([x[0] for x in bm], minmax([x[1] for x in bm])):
                fused[cid] = s
        elif vs:
            for cid, s in zip([x[0] for x in vs], minmax([x[1] for x in vs])):
                fused[cid] = s

        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
        if book_ids:
            book_ids = set(book_ids)
        out = []
        for cid, score in ranked:
            row = get_db().get_chunk(cid)
            if row is None:
                continue
            if book_ids and row["book_id"] not in book_ids:
                continue
            book = self._book_map.get(row["book_id"]) or get_db().get_book(row["book_id"]) or {}
            out.append(
                {
                    "chunk_id": cid,
                    "book_id": row["book_id"],
                    "book_title": book.get("title", ""),
                    "category": book.get("category", ""),
                    "vol": row["vol"],
                    "chapter_no": row["chapter_no"],
                    "chapter_title": row["chapter_title"],
                    "para_start": row["para_start"],
                    "para_end": row["para_end"],
                    "page_start": row["page_start"],
                    "page_end": row["page_end"],
                    "text": row["text"],
                    "content_hash": row["content_hash"],
                    "score": round(score, 4),
                    "bm25_score": next((s for c, s in bm if c == cid), None),
                    "vector_score": next((s for c, s in vs if c == cid), None),
                }
            )
            if len(out) >= k:
                break
        return out


_retriever: HybridRetriever | None = None


def get_retriever() -> HybridRetriever:
    global _retriever
    if _retriever is None:
        _retriever = HybridRetriever()
    return _retriever
