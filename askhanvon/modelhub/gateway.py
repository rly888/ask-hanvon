"""模型网关与模型治理（开发计划 §3.2）。

- 统一接入：LLM / Embedding / Rerank 一套 API，屏蔽供应商差异（OpenAI 兼容协议）
- 多模型路由与分级：strong/weak 分层 + 降级链，故障逐级回落
- 配额与成本计量：每次调用记 mh_calls 审计与 mh_quota_daily 计量
- 无可用 LLM Key 时自动进入「离线兜底模式」：上层生成引擎用抽取式回答，
  整条产品链路依然可用（可用性优先于模型质量）。
"""
import json
import time
from dataclasses import dataclass, field

import numpy as np
import requests

from ..config import settings
from ..db import get_db
from ..nlp import get_local_embedder, gram_containment, tokenize
from ..obs.logging import get_logger, log_fields
from ..obs.metrics import metrics
from ..obs.tracing import get_trace_id
from . import quota as quota_mod

logger = get_logger("askhanvon.modelhub")

# 成本表：元 / 千 token（演示计量；真实价格按供应商计费页配置）
COST_PER_1K = {
    "glm-4-flash": (0.0, 0.0),
    "glm-4.5-flash": (0.0, 0.0),
    "glm-4-plus": (0.05, 0.05),
    "glm-4-air": (0.001, 0.001),
    "default": (0.002, 0.006),
}


class ProviderError(Exception):
    pass


class LLMUnavailable(ProviderError):
    pass


@dataclass
class ChatResult:
    text: str = ""
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    latency_ms: float = 0.0
    fallback_used: bool = False
    usage: dict = field(default_factory=dict)


def _cost_of(model: str, pt: int, ct: int) -> float:
    pin, pout = COST_PER_1K.get(model, COST_PER_1K["default"])
    return (pt / 1000.0) * pin + (ct / 1000.0) * pout


class OpenAICompatClient:
    """OpenAI 兼容协议客户端（chat 与 embeddings），支持 SSE 流式。"""

    def __init__(self, base_url: str, api_key_fn, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.api_key_fn = api_key_fn
        self.timeout = timeout

    def _headers(self, key: str) -> dict:
        return {"Authorization": "Bearer " + key, "Content-Type": "application/json"}

    def chat(self, model: str, messages: list, temperature: float, max_tokens: int,
             stream: bool = False):
        key = self.api_key_fn()
        if not key:
            raise LLMUnavailable("未配置 LLM API Key")
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        try:
            resp = requests.post(
                self.base_url + "/chat/completions",
                headers=self._headers(key),
                json=payload,
                timeout=self.timeout,
                stream=stream,
            )
        except requests.RequestException as e:
            raise ProviderError("LLM 请求失败: " + str(e)[:120]) from e
        if resp.status_code != 200:
            raise ProviderError("LLM HTTP " + str(resp.status_code) + ": " + resp.text[:150])
        if not stream:
            data = resp.json()
            if "choices" not in data:
                raise ProviderError("LLM 响应异常: " + json.dumps(data, ensure_ascii=False)[:150])
            return data
        return self._iter_sse(resp)

    def _iter_sse(self, resp):
        """解析 SSE；产出 {"delta": str}，结尾产出 {"usage": {...}} 或 {"error": ...}。"""
        text_seen = False
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            body = raw[5:].strip()
            if body == "[DONE]":
                break
            try:
                d = json.loads(body)
            except ValueError:
                continue
            if "error" in d:
                yield {"error": d["error"]}
                return
            choices = d.get("choices") or [{}]
            delta = (choices[0].get("delta") or {}).get("content") or ""
            if delta:
                text_seen = True
                yield {"delta": delta}
            if d.get("usage"):
                yield {"usage": d["usage"]}
        if not text_seen:
            yield {"error": "流式响应为空"}

    def embeddings(self, model: str, texts: list) -> list:
        key = self.api_key_fn()
        if not key:
            raise LLMUnavailable("未配置 Embedding API Key")
        try:
            resp = requests.post(
                self.base_url + "/embeddings",
                headers=self._headers(key),
                json={"model": model, "input": texts},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ProviderError("Embedding 请求失败: " + str(e)[:120]) from e
        if resp.status_code != 200:
            raise ProviderError("Embedding HTTP " + str(resp.status_code))
        data = resp.json()
        if "data" not in data or not data["data"]:
            raise ProviderError("Embedding 响应异常")
        out = [None] * len(texts)
        for item in data["data"]:
            out[item.get("index", 0)] = item["embedding"]
        return out


def _lexical_rerank(query: str, docs: list) -> list:
    """本地词面重排：内容词元覆盖（>1字）+ 8gram 覆盖组合分。

    docs 可传 "标题\\n正文" 复合文本（标题是问答检索的强信号）。
    """
    q_tokens = [t for t in tokenize(query) if len(t) > 1 or t.isascii()]
    scores = []
    for doc in docs:
        cover = 0.0
        if q_tokens:
            d_tokens = set(tokenize(doc))
            cover = sum(1 for t in q_tokens if t in d_tokens) / len(q_tokens)
        scores.append(0.7 * cover + 0.3 * gram_containment(query, doc, n=8))
    return scores


class ModelGateway:
    """统一模型入口。所有 LLM/Embedding/Rerank 调用必须经此（审计+配额+降级）。"""

    def __init__(self):
        self.chat_client = OpenAICompatClient(
            settings.llm_base_url, settings.llm_api_key, settings.llm_timeout
        )
        self.embed_client = None
        if settings.embed_base_url and settings.embed_model:
            self.embed_client = OpenAICompatClient(
                settings.embed_base_url, settings.embed_api_key, settings.llm_timeout
            )
        # 最近一次 embed() 的实际来源（api | local），供入库打标使用：
        # 配置了 API 但调用失败时向量实际来自本地，chunk 必须按真实来源打标，
        # 否则 API 恢复后新旧向量混算，P0-1 的模型变更重建防线会失效。
        self.embed_last_source = "local"

    # ---------- LLM ----------
    def _chain(self, tier: str) -> list:
        primary = settings.llm_strong_model if tier == "strong" else settings.llm_weak_model
        chain = [primary]
        other = settings.llm_weak_model if tier == "strong" else settings.llm_strong_model
        for m in list(settings.llm_fallback_models) + [other]:
            if m and m not in chain:
                chain.append(m)
        return chain

    def _audit(self, service: str, provider: str, model: str, tier: str, user_id,
               pt: int, ct: int, cost: float, latency_ms: float, status: str,
               error: str = "") -> None:
        get_db().mh_call_log(
            ts=quota_mod.date.today().isoformat() + "T" + time.strftime("%H:%M:%S"),
            trace_id=get_trace_id(),
            user_id=user_id,
            service=service,
            provider=provider,
            model=model,
            tier=tier,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost=round(cost, 6),
            latency_ms=round(latency_ms, 1),
            status=status,
            error=(error or "")[:200],
        )
        get_db().mh_quota_incr(
            day=quota_mod.date.today().isoformat(),
            user_id=user_id,
            calls=1 if service == "chat" else 0,
            tokens=pt + ct,
            cost=cost,
        )
        metrics.inc("mh_calls_total", {"service": service, "model": model, "status": status})

    def chat(self, messages: list, tier: str = "weak", user_id=None, scene: str = "generic",
             max_tokens: int = 1024, temperature: float | None = None,
             model: str | None = None) -> ChatResult:
        """非流式对话：按层级路由 + 降级链。全部失败抛 LLMUnavailable。

        model 显式指定时优先生效（如评测 judge 与被测模型解耦，P0-5），
        降级链仍按 tier 补齐。
        """
        quota_mod.check_quota(user_id)
        temperature = settings.llm_temperature if temperature is None else temperature
        chain = self._chain(tier)
        if model:
            chain = [model] + [m for m in chain if m != model]
        errors = []
        for model_name in chain:
            t0 = time.perf_counter()
            try:
                data = self.chat_client.chat(model_name, messages, temperature, max_tokens)
                choice = (data.get("choices") or [{}])[0]
                text = ((choice.get("message") or {}).get("content")) or ""
                usage = data.get("usage") or {}
                pt = int(usage.get("prompt_tokens", quota_mod.estimate_tokens(
                    "".join(m.get("content", "") for m in messages))))
                ct = int(usage.get("completion_tokens", quota_mod.estimate_tokens(text)))
                cost = _cost_of(model_name, pt, ct)
                latency = (time.perf_counter() - t0) * 1000
                self._audit("chat", "openai-compat", model_name, tier, user_id, pt, ct, cost,
                            latency, "ok")
                metrics.observe("llm_latency_ms", latency, {"model": model_name})
                return ChatResult(text=text, model=model_name, provider="openai-compat",
                                  prompt_tokens=pt, completion_tokens=ct, cost=cost,
                                  latency_ms=latency, usage=usage)
            except ProviderError as e:
                latency = (time.perf_counter() - t0) * 1000
                errors.append(model_name + ": " + str(e)[:80])
                self._audit("chat", "openai-compat", model_name, tier, user_id, 0, 0, 0.0,
                            latency, "error", str(e)[:150])
                log_fields(logger, 30, "llm.fallback", model=model_name, error=str(e)[:120],
                           scene=scene)
        raise LLMUnavailable("所有模型不可用: " + " | ".join(errors))

    def chat_stream(self, messages: list, tier: str = "weak", user_id=None,
                    scene: str = "generic", max_tokens: int = 1024,
                    temperature: float | None = None):
        """流式对话：产出 {"delta": str}，结束产出 {"meta": ChatResult} 或 {"error": ...}。"""
        quota_mod.check_quota(user_id)
        temperature = settings.llm_temperature if temperature is None else temperature
        for model in self._chain(tier):
            t0 = time.perf_counter()
            try:
                gen = self.chat_client.chat(model, messages, temperature, max_tokens,
                                            stream=True)
                acc = []

                def _wrap():
                    usage = {}
                    had_error = None
                    for item in gen:
                        if "delta" in item:
                            acc.append(item["delta"])
                            yield item
                        elif "usage" in item:
                            usage = item["usage"]
                        elif "error" in item:
                            had_error = item["error"]
                    text = "".join(acc)
                    pt = int(usage.get("prompt_tokens", quota_mod.estimate_tokens(
                        "".join(m.get("content", "") for m in messages))))
                    ct = int(usage.get("completion_tokens", quota_mod.estimate_tokens(text)))
                    cost = _cost_of(model, pt, ct)
                    latency = (time.perf_counter() - t0) * 1000
                    status = "error" if had_error else "ok"
                    self._audit("chat", "openai-compat", model, tier, user_id, pt, ct, cost,
                                latency, status, had_error or "")
                    metrics.observe("llm_latency_ms", latency, {"model": model})
                    meta = ChatResult(text=text, model=model, provider="openai-compat",
                                      prompt_tokens=pt, completion_tokens=ct, cost=cost,
                                      latency_ms=latency, usage=usage)
                    if had_error and not text:
                        yield {"error": had_error}
                    else:
                        yield {"meta": meta}

                return _wrap()
            except ProviderError as e:
                self._audit("chat", "openai-compat", model, tier, user_id, 0, 0, 0.0,
                            (time.perf_counter() - t0) * 1000, "error", str(e)[:150])
                log_fields(logger, 30, "llm.stream_fallback", model=model, error=str(e)[:120])
        raise LLMUnavailable("所有模型不可用（流式）")

    def llm_ready(self) -> bool:
        return bool(settings.llm_api_key())

    def embed_model_name(self) -> str:
        """当前 embedding 模型标识（P0-1：入库时记录，模型变更即强制重建索引）。"""
        if self.embed_client is not None and settings.embed_model:
            return settings.embed_model
        return "local-hash-embed-" + str(settings.embed_dim)

    def effective_embed_model_name(self) -> str:
        """按 embed() 最近一次真实来源返回标识：API 降级到本地时不虚报模型名。"""
        if self.embed_client is not None and settings.embed_model \
                and self.embed_last_source == "api":
            return settings.embed_model
        return "local-hash-embed-" + str(settings.embed_dim)

    # ---------- Embedding ----------
    def embed(self, texts: list, user_id=None) -> np.ndarray:
        """优先 API embedding；否则本地向量。返回 float32 [n, dim]。"""
        if not texts:
            dim = settings.embed_dim
            return np.zeros((0, dim), dtype=np.float32)
        t0 = time.perf_counter()
        if self.embed_client is not None:
            try:
                vecs = []
                for i in range(0, len(texts), 16):
                    batch = texts[i : i + 16]
                    out = self.embed_client.embeddings(settings.embed_model, batch)
                    vecs.extend(out)
                arr = np.array(vecs, dtype=np.float32)
                norms = np.linalg.norm(arr, axis=1, keepdims=True)
                arr = arr / np.maximum(norms, 1e-9)
                self.embed_last_source = "api"
                self._audit("embed", "openai-compat", settings.embed_model, "embed",
                            user_id, 0, int(sum(len(t) for t in texts) / 2), 0.0,
                            (time.perf_counter() - t0) * 1000, "ok")
                return arr
            except ProviderError as e:
                self.embed_last_source = "local"
                self._audit("embed", "openai-compat", settings.embed_model, "embed",
                            user_id, 0, 0, 0.0, (time.perf_counter() - t0) * 1000,
                            "error", str(e)[:150])
                log_fields(logger, 30, "embed.fallback_local", error=str(e)[:120])
        self.embed_last_source = "local"
        arr = get_local_embedder(settings.embed_dim).embed(texts)
        self._audit("embed", "local", "local-hash-embed", "embed", user_id, 0,
                    int(sum(len(t) for t in texts) / 2), 0.0,
                    (time.perf_counter() - t0) * 1000, "ok")
        return arr

    # ---------- Rerank ----------
    def rerank(self, query: str, docs: list, top_n: int = 6) -> list:
        """返回 [(原下标, 分数)] 按分降序。API rerank 未配置时用本地词面重排。"""
        if settings.rerank_api_url and settings.rerank_model:
            try:
                key = settings.rerank_api_key()
                resp = requests.post(
                    settings.rerank_api_url,
                    headers={"Authorization": "Bearer " + key},
                    json={"model": settings.rerank_model, "input": query,
                          "documents": docs, "top_n": top_n},
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("results") or data.get("output", {}).get("results") or []
                    out = [(r["index"], float(r["relevance_score"])) for r in results]
                    self._audit("rerank", "api", settings.rerank_model, "rerank", None,
                                0, 0, 0.0, 0.0, "ok")
                    return out[:top_n]
                self._audit("rerank", "api", settings.rerank_model, "rerank", None,
                            0, 0, 0.0, 0.0, "error", "HTTP " + str(resp.status_code))
            except requests.RequestException as e:
                self._audit("rerank", "api", settings.rerank_model, "rerank", None,
                            0, 0, 0.0, 0.0, "error", str(e)[:150])
        scores = _lexical_rerank(query, docs)
        order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
        return [(i, scores[i]) for i in order[:top_n]]


_gateway: ModelGateway | None = None


def get_gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway
