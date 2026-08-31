"""查询改写（优化项 P1-2）：LLM 弱模型 JSON 改写，失败静默回退原查询。

用于多查询检索（multi-query retrieval）：原查询 + 改写查询各检一路，
RRF 融合（见 retriever.retrieve_multi）。规则先行原则不变——
指代消解等规则改写仍在 conversation/intent 层完成，本模块只做词汇层面扩展。
"""
from ..config import settings
from ..modelhub.gateway import get_gateway
from ..obs.logging import get_logger, log_fields

logger = get_logger("askhanvon.rag")

_REWRITE_SYSTEM = (
    "你是图书检索查询改写器。把用户问题改写成更适合书库关键词检索的查询："
    "补全指代、去掉口语词、补充同义词。只输出 JSON："
    '{"rewritten":"改写后的查询"}。不要输出其他内容。'
)


def rewrite_query(query: str, book_title: str = "", user_id=None) -> str:
    """返回改写后的查询；任何失败都回退原查询（不阻塞主链路）。"""
    if not strategies_enabled():
        return query
    gw = get_gateway()
    if not gw.llm_ready():
        return query
    prompt = query
    if book_title:
        prompt = f"（关于《{book_title}》）{query}"
    try:
        res = gw.chat(
            [
                {"role": "system", "content": _REWRITE_SYSTEM},
                {"role": "user", "content": prompt[:200]},
            ],
            tier="weak", user_id=user_id, scene="query_rewrite",
            max_tokens=120, temperature=0.1,
        )
        import json

        text = res.text.strip()
        s, e = text.find("{"), text.rfind("}")
        if s < 0 or e <= s:
            return query
        rewritten = (json.loads(text[s : e + 1]).get("rewritten") or "").strip()
        if not rewritten or len(rewritten) > 120:
            return query
        return rewritten
    except Exception as e:  # noqa: BLE001 — 改写失败不阻塞主链路
        log_fields(logger, 20, "rewrite.fallback", error=str(e)[:100])
        return query


def strategies_enabled() -> bool:
    from ..ops.strategies import strategies

    return bool(strategies.get("retrieval.query_rewrite", True))
