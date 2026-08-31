"""去重：精确 hash 去重 + 段内 n-gram 近似去重（解析 pipeline 第 2 步）。"""
import hashlib
import re

from ..nlp import ngrams

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub("", text or "")


def content_hash(text: str) -> str:
    return hashlib.blake2b(normalize(text).encode("utf-8"), digest_size=16).hexdigest()


def is_near_dup(a: str, b: str, threshold: float = 0.92) -> bool:
    ga, gb = ngrams(a, 4), ngrams(b, 4)
    if not ga or not gb:
        return False
    return len(ga & gb) / len(ga | gb) > threshold


def dedup_paragraphs(paragraphs: list) -> list:
    """保持顺序去重：先精确 hash，再与相邻段做近似去重。"""
    seen_hash = set()
    kept: list = []
    for p in paragraphs:
        h = content_hash(p)
        if h in seen_hash:
            continue
        if kept and is_near_dup(p, kept[-1]):
            continue
        seen_hash.add(h)
        kept.append(p)
    return kept
