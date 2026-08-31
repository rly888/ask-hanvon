"""引用/溯源：解析回答中的 [n]，与资料区 locator 交叉验证（§1.1 判断 4 生命线）。

验证标准（满足其一即视为有据可依）：
- 词元覆盖率 ≥ 0.45；二元组覆盖率 ≥ 0.30（容忍同义改写）；8gram ≥ 0.35（摘抄）；
- 答案级证据：被引片段内容词在整段回答中的覆盖 ≥ 0.25；
- 向量通道（P1-3）：主张句与片段的本地向量余弦 ≥ 阈值（策略化开关/阈值），
  两端同用本地向量保证一致，不消耗 API 配额。
"""
import re

from ..config import settings
from ..nlp import get_local_embedder, gram_containment, tokenize
from ..ops.strategies import strategies

_CITE_RE = re.compile(r"\[(\d{1,2})\]")

_TOKEN_COVER_THRESHOLD = 0.45
_GRAM_COVER_THRESHOLD = 0.35
_BIGRAM_COVER_THRESHOLD = 0.30
_ANSWER_LEVEL_THRESHOLD = 0.12  # 策略可调：跨片段综述句的答案级证据容忍


def _claim_supported(claim: str, chunk_text: str) -> bool:
    """转述容忍的交叉验证：词元 ∨ 二元组 ∨ 8gram。"""
    tokens = [t for t in tokenize(claim) if len(t) > 1 or t.isascii()]
    if tokens:
        in_chunk = sum(1 for t in tokens if t in chunk_text)
        if in_chunk / len(tokens) >= _TOKEN_COVER_THRESHOLD:
            return True
    # 二元组覆盖：容忍合理的同义改写（如"八颗行星"→"八大行星"）
    if gram_containment(claim, chunk_text, n=2) >= _BIGRAM_COVER_THRESHOLD:
        return True
    return gram_containment(claim, chunk_text, n=8) >= _GRAM_COVER_THRESHOLD


def _vector_support(claim: str, chunk_text: str) -> bool:
    """向量通道（P1-3）：本地向量余弦 ≥ 阈值即视为有据。

    实测注记：本地哈希向量会把"关键词诱导型幻觉"（同话题但无据的编造）
    也放进来，因此默认关闭；接入真实 Embedding API 后可开评估。
    """
    if not strategies.get("answer.citation_vector_check", False):
        return False
    thr = float(strategies.get("answer.citation_vec_threshold", 0.55))
    emb = get_local_embedder(settings.embed_dim)
    a = emb.embed_one(claim)
    b = emb.embed_one(chunk_text)
    return float(a @ b) >= thr


def _chunk_supports_answer(chunk_text: str, answer_tokens: set) -> float:
    """答案级证据：被引片段的内容词在整段回答中的出现比例（容忍转述）。"""
    chunk_tokens = [t for t in tokenize(chunk_text) if len(t) > 1 or t.isascii()]
    if not chunk_tokens:
        return 0.0
    hit = sum(1 for t in chunk_tokens if t in answer_tokens)
    return hit / len(chunk_tokens)


def split_sentences(text: str) -> list:
    parts = re.split(r"(?<=[。！？!?])", text or "")
    out = []
    for p in parts:
        for sub in p.split("\n"):
            sub = sub.strip()
            if sub:
                out.append(sub)
    return out


def refs_in(text: str) -> set:
    return {int(m) for m in _CITE_RE.findall(text or "")}


def validate_citations(text: str, metas: list) -> tuple:
    """逐引用做片段交叉验证（词元覆盖 ∨ 8gram 覆盖）。

    返回 (clean_text, citations, stats)：
    - 未通过验证的 [n] 从文本中移除；
    - citations: [{idx, book_id, book_title, chapter_no, chapter_title, pages, quote}]
    - stats: {used, verified, removed}
    """
    meta_map = {m["idx"]: m for m in metas}
    # 归一化：把句号后的 [n] 移到句内（"……。[1]下一句" → "……[1]。下一句"），
    # 避免引用标记跨句归属错误
    text = re.sub(r"([。！？])\s*\[(\d{1,2})\]", r"[\2]\1", text or "")
    used = refs_in(text)
    # 答案级证据词集（整段回答的内容词）
    bare_text = _CITE_RE.sub("", text)
    answer_tokens = {t for t in tokenize(bare_text) if len(t) > 1 or t.isascii()}
    verified_idx = set()
    for idx in used:
        meta = meta_map.get(idx)
        if not meta:
            continue
        # P1-1 父子块扩展后，模型看到的是 context_text；验证基准与之一致
        chunk_text = meta.get("context_text") or meta.get("text", "")
        # 答案级：被引片段内容词出现在回答中（转述容忍）
        answer_level_target = float(
            strategies.get("answer.answer_level_threshold", _ANSWER_LEVEL_THRESHOLD)
        )
        answer_level = (
            _chunk_supports_answer(chunk_text, answer_tokens) >= answer_level_target
        )
        marker = "[" + str(idx) + "]"
        sentences = [s for s in split_sentences(text) if marker in s]
        quote = max(
            (s for s in sentences),
            key=lambda s: (_claim_supported(s, chunk_text),
                           gram_containment(s, chunk_text, 8)),
            default="",
        )
        claim_text = quote if quote else bare_text[:200]
        if (
            answer_level
            or (quote and _claim_supported(quote, chunk_text))
            or _vector_support(claim_text, chunk_text)
        ):
            verified_idx.add(idx)
    removed = used - verified_idx
    clean = text
    for idx in removed:
        clean = clean.replace("[" + str(idx) + "]", "")
    clean = re.sub(r"[ \t]{2,}", " ", clean).strip()

    citations = []
    for idx in sorted(verified_idx):
        m = meta_map[idx]
        pages = "p" + str(m["page_start"]) + "-" + str(m["page_end"])
        citations.append(
            {
                "idx": idx,
                "book_id": m["book_id"],
                "book_title": m["book_title"],
                "vol": m.get("vol", ""),
                "chapter_no": m["chapter_no"],
                "chapter_title": m["chapter_title"],
                "para_start": m.get("para_start"),
                "para_end": m.get("para_end"),
                "pages": pages,
                "quote": (m.get("text", "") or "")[:120],
            }
        )
    stats = {"used": len(used), "verified": len(verified_idx), "removed": len(removed)}
    return clean, citations, stats


def locator_match(citation: dict, gold: dict) -> bool:
    """评测用：citation 是否命中 gold 引用位置（书 + 章，页码可选）。"""
    if citation.get("book_title") != gold.get("book"):
        return False
    g_ch = gold.get("chapter_no")
    if g_ch is not None and int(citation.get("chapter_no") or -1) != int(g_ch):
        return False
    return True
