"""生成与回答引擎：LLM 生成 + 强制引用 + 低置信拒答 + 离线兜底 + 结果缓存。

设计要点（对应开发计划 §2 生成与回答 / §1.2 问答主链路）：
- 上下文由 RAG 引擎构建（带 locator），本层只负责「答 + 引用 + 拒答」；
- 无 LLM Key 或全部模型故障时走抽取式兜底（回答直接来自检索片段，天然带引用），
  保证整条链路在任何环境下可用（可用性优先）；
- 每个事实句的 [n] 引用与资料片段做 8gram 交叉验证，验证不过即剥除；
  全部剥除 = 无据可依 = 拒答（宁拒答不编造）。
"""
import re
import time
from dataclasses import dataclass, field

from ..config import settings
from ..modelhub import quota as quota_mod
from ..modelhub.gateway import LLMUnavailable, get_gateway
from ..nlp import get_local_embedder, tokenize
from ..obs.logging import get_logger, log_fields
from ..obs.metrics import metrics
from ..ops.strategies import strategies
from . import citation as cite_mod
from . import moderation
from .prompts import QA_SYSTEM

logger = get_logger("askhanvon.generation")

REFUSAL_TEXT = (
    "根据书库内容，我暂时无法有把握地回答这个问题。"
    "可以试试换个问法，或让我为你推荐相关图书。"
)

_NOANSWER_MARK = "无法回答"


@dataclass
class AnswerResult:
    text: str = ""
    citations: list = field(default_factory=list)
    refused: bool = False
    model: str = ""
    cached: bool = False
    semantic_cached: bool = False
    degraded: bool = False
    verified_ratio: float = 0.0
    latency_ms: float = 0.0
    prompt_version: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0


def _sentences_of(text: str) -> list:
    parts = re.split(r"(?<=[。！？!?])", text or "")
    return [p.strip() for p in parts if len(p.strip()) >= 8]


_GENERIC_TOKENS = {
    "时间", "内容", "什么", "怎么", "为什么", "如何", "讲", "了", "是", "的", "和",
    "在", "吗", "呢", "请", "帮", "我", "介绍", "说说", "几颗", "多少", "几天",
    "怎么样", "哪些", "哪个", "谁",
}


def _topic_coherent(query: str, answer: str, metas: list = None) -> bool:
    """主题一致性守卫（P2 补充）：防"关键词碎片诱导"漂移。

    规则：去掉泛词后的查询锚词，若既不出现在回答、也不出现在任何被引片段
    （context_text）中 → 判定回答漂移（如"时间简史"问题借"时间"碎片回答无关史实）。
    锚词为空时无法判定，交其他通道。
    """
    if not strategies.get("answer.topic_coherence_check", True):
        return True
    tokens = set(tokenize(query))
    anchors = {t for t in tokens if len(t) > 1 and t not in _GENERIC_TOKENS}
    if not anchors:
        return True
    haystack = answer or ""
    for m in (metas or []):
        haystack += (m.get("context_text") or "") + (m.get("text") or "")
    return any(a in haystack for a in anchors)


def _extractive_answer(query: str, ctx) -> str:
    """离线兜底：从检索片段中抽取与 query 最相关的句子，逐句带引用。"""
    q_tokens = set(t for t in tokenize(query) if len(t) > 1 or t.isascii())
    scored = []
    for m in ctx.metas:
        for sent in _sentences_of(m.get("text", "")):
            s_tokens = set(tokenize(sent))
            if not s_tokens:
                continue
            overlap = len(q_tokens & s_tokens) / max(1, len(q_tokens))
            scored.append((overlap, m["idx"], sent))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = []
    seen = set()
    for overlap, idx, sent in scored:
        if overlap <= 0.05:
            continue
        key = sent[:24]
        if key in seen:
            continue
        seen.add(key)
        picked.append((idx, sent))
        if len(picked) >= 3:
            break
    if not picked:
        # 无任何相关句 → 宁可拒答也不硬引无关片段（宁拒不编造）
        return ""
    lines = []
    for idx, sent in picked:
        clean = sent.split("[")[0].strip().rstrip("。！？")
        if clean:
            lines.append(clean + "[" + str(idx) + "]。")
    return "\n".join(lines)


class AnswerGenerator:
    CACHE_TTL = 3600
    REFUSAL_TTL = 600

    def __init__(self):
        self._cache: dict = {}
        # 语义缓存（P0-4）：(query 向量, 精确缓存 key, Top chunk id 集)
        self._semantic: list = []
        self._redis = None

    def _rclient(self):
        """结果缓存后端：REDIS_URL 配置时用 Redis（多实例共享 TTL），否则进程内。"""
        if self._redis is None and settings.redis_url:
            try:
                import redis as _redis

                self._redis = _redis.Redis.from_url(settings.redis_url,
                                                    decode_responses=True)
            except Exception:  # noqa: BLE001
                self._redis = False
        return self._redis or None

    def _cache_key(self, query: str, ctx) -> str:
        ids = ",".join(str(m["chunk_id"]) for m in ctx.metas[:6])
        import hashlib

        return hashlib.blake2b((query + "|" + ids).encode("utf-8"), digest_size=16).hexdigest()

    def _semantic_lookup(self, query: str, ctx):
        """语义缓存：query 向量相似 + 证据块重叠 ≥50% → 复用精确缓存结果。

        相似度用本地向量（确定性、零成本，字符 bigram 对近义改写敏感），
        不占用 API embedding 配额。
        """
        if not strategies.get("answer.semantic_cache", True):
            return None
        thr = float(strategies.get("answer.semantic_cache_threshold", 0.93))
        if not self._semantic:
            return None
        qv = get_local_embedder(settings.embed_dim).embed_one(query)
        cur_ids = {m["chunk_id"] for m in ctx.metas[:6]}
        best, best_sim = None, 0.0
        for entry in self._semantic:
            sim = float(qv @ entry["vec"])
            if sim > best_sim:
                best, best_sim = entry, sim
        if best is None or best_sim < thr:
            return None
        overlap = len(cur_ids & best["chunk_ids"]) / max(1, len(cur_ids))
        if cur_ids and overlap < 0.5:
            return None
        hit = self._get_cache(best["key"])
        if hit is None:
            return None
        hit.cached = True
        hit.semantic_cached = True
        return hit

    def _semantic_store(self, query: str, key: str, ctx) -> None:
        if not strategies.get("answer.semantic_cache", True):
            return
        if len(self._semantic) >= 500:
            self._semantic = self._semantic[250:]
        vec = get_local_embedder(settings.embed_dim).embed_one(query)
        self._semantic.append(
            {"vec": vec, "key": key, "chunk_ids": {m["chunk_id"] for m in ctx.metas[:6]}}
        )

    def _get_cache(self, key: str):
        redis = self._rclient()
        if redis is not None:
            try:
                raw = redis.get("ans:" + key)
            except Exception:  # noqa: BLE001 — Redis 故障回退进程内
                raw = None
                self._redis = None
            if raw:
                data = __import__("json").loads(raw)
                return AnswerResult(**data)
        item = self._cache.get(key)
        if not item:
            return None
        ts, res = item
        ttl = self.REFUSAL_TTL if res.refused else self.CACHE_TTL
        if time.time() - ts > ttl:
            self._cache.pop(key, None)
            return None
        return res

    def _put_cache(self, key: str, res: AnswerResult):
        redis = self._rclient()
        if redis is not None:
            import json as _json

            ttl = self.REFUSAL_TTL if res.refused else self.CACHE_TTL
            try:
                redis.setex("ans:" + key, ttl,
                            _json.dumps(res.__dict__, ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001 — Redis 故障回退进程内
                self._redis = None
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[key] = (time.time(), res)

    def _messages(self, query: str, ctx, profile_block: str = "") -> tuple:
        """返回 (messages, prompt_version)。模板走 Prompt 版本服务（P1-5）。

        ① 结构式隔离：资料区一律由系统包裹 <context> 标签（内容本身已过
        sanitize_context_tags，不可信内容中的伪造标签被转义/剔块）。
        """
        from ..ops.prompts import prompt_service

        version, template = prompt_service.get("qa", QA_SYSTEM)
        context_block = "<context>\n" + (ctx.blocks or "（无资料）") + "\n</context>"
        system = template.format(context=context_block,
                                 profile=profile_block or "（无）")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ]
        return messages, version

    def _post(self, raw: str, ctx, model: str, degraded: bool, cached: bool,
              latency: float, query: str = "") -> AnswerResult:
        if ctx.low_confidence or not ctx.metas:
            return AnswerResult(
                text=REFUSAL_TEXT, citations=[], refused=True, model=model,
                cached=cached, degraded=degraded, latency_ms=latency,
            )
        if not (raw or "").strip() or _NOANSWER_MARK in (raw or ""):
            return AnswerResult(
                text=(raw.strip() or REFUSAL_TEXT), citations=[], refused=True,
                model=model, cached=cached, degraded=degraded, latency_ms=latency,
            )
        # 主题一致性：问题锚词在回答与引用片段中均缺失 = 漂移（有引用也不放行）
        if query and not _topic_coherent(query, raw, ctx.metas):
            return AnswerResult(
                text=REFUSAL_TEXT, citations=[], refused=True, model=model,
                cached=cached, degraded=degraded, latency_ms=latency,
            )
        clean, citations, stats = cite_mod.validate_citations(raw or "", ctx.metas)
        # 无任何可验证引用 → 无据可依 → 拒答（宁拒不编造，防幻觉）
        if stats["used"] == 0 or (stats["used"] > 0 and stats["verified"] == 0):
            return AnswerResult(
                text=REFUSAL_TEXT, citations=[], refused=True, model=model,
                cached=cached, degraded=degraded, latency_ms=latency,
            )
        clean, flags = moderation.copyright_guard(clean, ctx.metas)
        clean, banned = moderation.banned_check(clean)
        verified_ratio = (
            stats["verified"] / stats["used"] if stats["used"] else (1.0 if citations else 0.0)
        )
        if flags or banned:
            log_fields(logger, 30, "moderation.applied", flags=flags, banned=banned)
        return AnswerResult(
            text=clean.strip(), citations=citations, refused=False, model=model,
            cached=cached, degraded=degraded, verified_ratio=round(verified_ratio, 3),
            latency_ms=latency,
        )

    # ---------- 非流式 ----------
    def generate(self, query: str, ctx, user_id=None, profile_block: str = "",
                 use_cache: bool = True) -> AnswerResult:
        t0 = time.perf_counter()
        key = self._cache_key(query, ctx)
        if use_cache:
            hit = self._get_cache(key)
            if hit is not None:
                hit.cached = True
                return hit
            sem = self._semantic_lookup(query, ctx)
            if sem is not None:
                metrics.inc("answer_semantic_cache_hits_total")
                return sem
        gw = get_gateway()
        model = "offline-extractive"
        degraded = True
        raw = ""
        prompt_version = 0
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        if gw.llm_ready():
            try:
                messages, prompt_version = self._messages(query, ctx, profile_block)
                res = gw.chat(
                    messages,
                    tier="strong" if len(query) > 40 else "weak",
                    user_id=user_id,
                    scene="book_qa",
                    max_tokens=800,
                    temperature=0.3,
                )
                raw, model, degraded = res.text, res.model, False
                usage = {"prompt_tokens": res.prompt_tokens,
                         "completion_tokens": res.completion_tokens,
                         "cost": round(res.cost, 6)}
            except (LLMUnavailable, quota_mod.QuotaExceeded) as e:
                log_fields(logger, 30, "answer.llm_degrade", error=str(e)[:120])
        if not raw:
            raw = _extractive_answer(query, ctx)
        latency = (time.perf_counter() - t0) * 1000
        result = self._post(raw, ctx, model, degraded, False, latency, query=query)
        result.prompt_version = prompt_version
        result.prompt_tokens = usage["prompt_tokens"]
        result.completion_tokens = usage["completion_tokens"]
        result.cost = usage["cost"]
        metrics.observe("answer_latency_ms", latency)
        if use_cache:
            self._put_cache(key, result)
            self._semantic_store(query, key, ctx)
        return result

    # ---------- 流式 ----------
    def generate_stream(self, query: str, ctx, user_id=None, profile_block: str = ""):
        """产出 {"delta": str}，最后产出 {"final": AnswerResult}。

        流式阶段先原样下发增量；结束后统一做引用校验/版权护栏/拒答判定，
        final.text 才是入库与评测口径的最终文本。
        """
        t0 = time.perf_counter()
        key = self._cache_key(query, ctx)
        hit = self._get_cache(key)
        if hit is not None:
            hit.cached = True
            yield {"delta": hit.text}
            yield {"final": hit}
            return
        if ctx.low_confidence or not ctx.metas:
            final = self._post("", ctx, "offline-extractive", True, False,
                               (time.perf_counter() - t0) * 1000, query=query)
            yield {"delta": final.text}
            yield {"final": final}
            return
        gw = get_gateway()
        emitted = []
        model = "offline-extractive"
        degraded = True
        prompt_version = 0
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        if gw.llm_ready():
            try:
                messages, prompt_version = self._messages(query, ctx, profile_block)
                gen = gw.chat_stream(
                    messages,
                    tier="strong" if len(query) > 40 else "weak",
                    user_id=user_id,
                    scene="book_qa",
                    max_tokens=800,
                    temperature=0.3,
                )
                got_meta = None
                for item in gen:
                    if "delta" in item:
                        emitted.append(item["delta"])
                        yield {"delta": item["delta"]}
                    elif "meta" in item:
                        got_meta = item["meta"]
                    elif "error" in item:
                        break
                if got_meta is not None:
                    model, degraded = got_meta.model, False
                    usage = {"prompt_tokens": got_meta.prompt_tokens,
                             "completion_tokens": got_meta.completion_tokens,
                             "cost": round(got_meta.cost, 6)}
            except (LLMUnavailable, quota_mod.QuotaExceeded) as e:
                log_fields(logger, 30, "answer.stream_degrade", error=str(e)[:120])
        raw = "".join(emitted)
        if not raw.strip():
            raw = _extractive_answer(query, ctx)
            for sent in re.split(r"(?<=[。！？!?])", raw):
                if sent.strip():
                    yield {"delta": sent}
        latency = (time.perf_counter() - t0) * 1000
        final = self._post(raw, ctx, model, degraded, False, latency, query=query)
        final.prompt_version = prompt_version
        final.prompt_tokens = usage["prompt_tokens"]
        final.completion_tokens = usage["completion_tokens"]
        final.cost = usage["cost"]
        if final.refused and not final.text:
            final.text = REFUSAL_TEXT
            yield {"delta": REFUSAL_TEXT}
        self._put_cache(key, final)
        yield {"final": final}


_gen: AnswerGenerator | None = None


def get_answer_generator() -> AnswerGenerator:
    global _gen
    if _gen is None:
        _gen = AnswerGenerator()
    return _gen
