"""Rerank：粗召回 TopK → 精排 TopN。

本地词面重排为默认实现（bge-reranker 的占位）；配置 RERANK_API_URL 后
自动切换 API 重排（模型网关统一封装）。
"""
from ..config import settings
from ..modelhub.gateway import get_gateway


def rerank_chunks(query: str, retrieved: list, top_n: int | None = None) -> list:
    n = top_n or settings.rerank_top_n
    if not retrieved:
        return []
    gw = get_gateway()
    # 标题+正文 复合文本参与重排（章节标题是问答检索的强信号）
    docs = [
        (r.get("book_title", "") + " " + r.get("chapter_title", "") + "\n" + r["text"])
        for r in retrieved
    ]
    pairs = gw.rerank(query, docs, top_n=n)
    out = []
    for idx, score in pairs:
        item = dict(retrieved[idx])
        item["rerank_score"] = round(float(score), 4)
        out.append(item)
    out.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
    for i, item in enumerate(out):
        item["rank"] = i + 1
    return out
