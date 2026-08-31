"""中文分词 / 本地向量 / 相似度工具（RAG 与推荐的公共底座）。"""
import hashlib
import math
import re
from collections import Counter

import jieba
import numpy as np

jieba.setLogLevel(60)  # 静音jieba日志

_WS = re.compile(r"\s+")


def tokenize(text: str) -> list:
    """jieba 分词，去除空白。"""
    if not text:
        return []
    return [w for w in jieba.lcut(_WS.sub(" ", text)) if w.strip()]


def fts_match_query(text: str, max_terms: int = 24) -> str:
    """构造 FTS5 MATCH 表达式：词元 OR + 相邻词元短语通道。

    - 过滤单字 CJK 词元（"在/什么"等），避免通用虚词稀释 BM25 排序；
    - 短语通道（"长坂 坡桥"式二元组短语）提升词序敏感问题（书名/事件名）的精度。
    """
    terms = []
    for t in tokenize(text):
        if len(t) == 1 and not t.isascii():
            continue  # 单字中文多为虚词
        t = t.replace('"', "")
        if t and t not in terms:
            terms.append(t)
        if len(terms) >= max_terms:
            break
    parts = ['"' + t + '"' for t in terms]
    # 短语通道：内容词元的相邻二元组（P0-3 优化项）
    for i in range(min(len(terms) - 1, 12)):
        parts.append('"' + terms[i] + " " + terms[i + 1] + '"')
    return " OR ".join(parts)


def normalize_text(s: str) -> str:
    return _WS.sub("", s or "")


def ngrams(s: str, n: int) -> set:
    s = normalize_text(s)
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def gram_containment(needle: str, haystack: str, n: int = 8) -> float:
    """needle 的 n-gram 被 haystack 覆盖的比例（引用交叉验证用）。"""
    grams = ngrams(needle, n)
    if not grams:
        return 0.0
    big = ngrams(haystack, n)
    return len(grams & big) / len(grams)


def token_overlap(a: str, b: str) -> float:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta:
        return 0.0
    return len(ta & tb) / len(ta)


def minmax(values: list) -> list:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0 if hi <= 0 else 1.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


class LocalEmbedder:
    """本地轻量向量：词 + 字符 bigram 特征哈希，tf 加权，L2 归一化。

    作为无外部 embedding API 时的默认 provider；接口与 OpenAI 兼容 API 对齐，
    配置 EMBED_BASE_URL/EMBED_MODEL 后自动切换真实向量模型。
    """

    def __init__(self, dim: int = 256, cache_size: int = 20000):
        self.dim = dim
        self._cache: dict = {}
        self._cache_order: list = []
        self._cache_size = cache_size

    def _features(self, text: str) -> Counter:
        feats = Counter()
        text = _WS.sub("", text or "")
        if not text:
            return feats
        for w in tokenize(text):
            feats["w:" + w] += 1
        for i in range(len(text) - 1):
            feats["c:" + text[i : i + 2]] += 1
        return feats

    def _bucket(self, feat: str) -> int:
        h = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(h, "little") % self.dim

    def embed_one(self, text: str) -> np.ndarray:
        key = hashlib.blake2b((text or "").encode("utf-8"), digest_size=16).hexdigest()
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        vec = np.zeros(self.dim, dtype=np.float32)
        for feat, tf in self._features(text).items():
            if tf <= 0:
                continue
            vec[self._bucket(feat)] += (1.0 + math.log(tf)) * (
                2.0 if feat.startswith("w:") else 1.0
            )
        norm = float(np.linalg.norm(vec))
        if norm > 1e-9:
            vec /= norm
        if len(self._cache) >= self._cache_size:
            self._cache.pop(self._cache_order.pop(0), None)
        self._cache[key] = vec
        self._cache_order.append(key)
        return vec

    def embed(self, texts: list) -> np.ndarray:
        return np.stack([self.embed_one(t) for t in texts]) if texts else np.zeros(
            (0, self.dim), dtype=np.float32
        )


_default_embedder = None


def get_local_embedder(dim: int = 256) -> LocalEmbedder:
    global _default_embedder
    if _default_embedder is None or _default_embedder.dim != dim:
        _default_embedder = LocalEmbedder(dim=dim)
    return _default_embedder
