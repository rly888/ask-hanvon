"""工具：图书阅读问答（RAG 封装）——检索→安全过滤→重排→上下文→生成。

ask_rag / ask_rag_stream 同时服务于 Agent 工具调用、评测回归与 API 直连。
"""
from ..config import settings
from ..db import get_db
from ..generation.answer import get_answer_generator
from ..ops.strategies import strategies
from ..rag.context import build_context
from ..rag.rerank import rerank_chunks
from ..rag.retriever import get_retriever
from ..security.injection import check_retrieved
from .schema import ToolContext, ToolResult, ToolSchema

SCHEMA = ToolSchema(
    name="book_qa",
    description="图书阅读问答：基于书库内容回答书中内容/作者/情节等问题，答案带书内引用",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "问题"},
            "book_title": {"type": "string", "description": "限定书名（可选）"},
        },
        "required": ["query"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "citations": {"type": "array"},
            "confidence": {"type": "number"},
            "refused": {"type": "boolean"},
        },
    },
    timeout_s=30.0,
)


def register(reg) -> None:
    reg.register(SCHEMA, run)


def run(ctx: ToolContext, query: str, book_title: str = "",
        profile_block: str = "") -> ToolResult:
    result = ask_rag(query, user_id=ctx.user_id, book_hint=book_title,
                     profile_block=profile_block)
    return ToolResult(ok=True, data=result)


def _resolve_book_ids(book_hint: str) -> list:
    if not book_hint:
        return []
    book = get_db().get_book_by_title(book_hint)
    return [book["id"]] if book else []


def ask_rag(query: str, user_id=None, book_hint: str = "", profile_block: str = "",
            use_cache: bool = True) -> dict:
    retriever = get_retriever()
    top_k = int(strategies.get("retrieval.top_k", settings.retrieval_top_k))
    # P1-2：多查询检索（原查询 + LLM 改写，RRF 融合；策略可关）
    retrieved = retriever.retrieve_multi(query, top_k=top_k,
                                         book_ids=_resolve_book_ids(book_hint))

    # 资料区注入扫描（书内容是不可信数据）
    untrusted = set()
    for r in retrieved:
        if check_retrieved(r["text"])["untrusted"]:
            untrusted.add(r["chunk_id"])

    reranked = rerank_chunks(query, retrieved)
    ctx = build_context(query, reranked, untrusted_ids=untrusted)
    answer = get_answer_generator().generate(
        query, ctx, user_id=user_id, profile_block=profile_block, use_cache=use_cache
    )
    return {
        "answer": answer.text,
        "citations": answer.citations,
        "refused": answer.refused,
        "confidence": ctx.confidence,
        "verified_ratio": answer.verified_ratio,
        "model": answer.model,
        "degraded": answer.degraded,
        "cached": answer.cached,
        "semantic_cached": answer.semantic_cached,
        "prompt_version": answer.prompt_version,
        "usage": {"prompt_tokens": answer.prompt_tokens,
                  "completion_tokens": answer.completion_tokens,
                  "cost": answer.cost},
        "retrieval": [
            {
                "book_title": m["book_title"],
                "chapter_no": m["chapter_no"],
                "chapter_title": m["chapter_title"],
                "score": m["score"],
                "chunk_id": m["chunk_id"],
            }
            for m in ctx.metas
        ],
        "dropped_untrusted": ctx.dropped_untrusted,
    }


def ask_rag_stream(query: str, user_id=None, book_hint: str = "",
                   profile_block: str = "", on_event=None):
    """流式版：on_event(type, payload) 回调 step/delta/final；返回最终 dict。"""
    retriever = get_retriever()
    top_k = int(strategies.get("retrieval.top_k", settings.retrieval_top_k))
    retrieved = retriever.retrieve_multi(query, top_k=top_k,
                                         book_ids=_resolve_book_ids(book_hint))
    untrusted = set()
    for r in retrieved:
        if check_retrieved(r["text"])["untrusted"]:
            untrusted.add(r["chunk_id"])
    if on_event:
        on_event("retrieval", {"n": len(retrieved), "untrusted": len(untrusted),
                               "top": [r["book_title"] + "·" + r["chapter_title"]
                                       for r in retrieved[:3]]})
    reranked = rerank_chunks(query, retrieved)
    ctx = build_context(query, reranked, untrusted_ids=untrusted)
    if on_event:
        on_event("context", {"confidence": ctx.confidence,
                             "chunks": [m["book_title"] + "·第" + str(m["chapter_no"])
                                        + "章" for m in ctx.metas]})
    gen = get_answer_generator()
    final = None
    for item in gen.generate_stream(query, ctx, user_id=user_id,
                                    profile_block=profile_block):
        if "delta" in item:
            if on_event:
                on_event("delta", {"text": item["delta"]})
        elif "final" in item:
            final = item["final"]
    if final is None:  # 防御：生成器异常中断
        from ..generation.answer import AnswerResult, REFUSAL_TEXT

        final = AnswerResult(text=REFUSAL_TEXT, refused=True)
    result = {
        "answer": final.text,
        "citations": final.citations,
        "refused": final.refused,
        "confidence": ctx.confidence,
        "verified_ratio": final.verified_ratio,
        "model": final.model,
        "degraded": final.degraded,
        "cached": final.cached,
        "semantic_cached": final.semantic_cached,
        "prompt_version": final.prompt_version,
        "usage": {"prompt_tokens": final.prompt_tokens,
                  "completion_tokens": final.completion_tokens,
                  "cost": final.cost},
        "retrieval": [
            {"book_title": m["book_title"], "chapter_no": m["chapter_no"],
             "chapter_title": m["chapter_title"], "score": m["score"],
             "chunk_id": m["chunk_id"]}
            for m in ctx.metas
        ],
        "dropped_untrusted": ctx.dropped_untrusted,
    }
    if on_event:
        on_event("final", result)
    return result
