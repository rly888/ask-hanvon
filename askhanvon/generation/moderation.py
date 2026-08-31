"""输出内容审核（§3.4）：版权护栏（连续引用截断）+ 敏感词 + 青少年模式。"""
import re

from ..nlp import gram_containment
from ..ops.strategies import strategies

_BANNED = [
    "暴力教程", "制毒", "自残方法",
]


def _longest_common_run(a: str, b: str) -> int:
    """最长公共连续子串长度（O(n*m)，片段短，可接受）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ca = a[i - 1]
        for j in range(1, len(b) + 1):
            if ca == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def copyright_guard(text: str, metas: list, max_quote: int | None = None) -> tuple:
    """单处连续引用超限则截断为节选（版权红线：不做原文镜像）。

    P1-1 父子块扩展后，对照模型实际看到的 context_text（含相邻块）。
    """
    limit = max_quote or strategies.get(
        "answer.copyright_max_quote", 200
    )
    flags = []
    sentences = re.split(r"(?<=[。！？!?])", text or "")
    out = []
    for s in sentences:
        clipped = False
        for m in metas:
            source = m.get("context_text") or m.get("text", "")
            if _longest_common_run(s, source) > limit:
                cut = s[:limit] + "……（原文引用已节选，完整内容请阅读原书）"
                out.append(cut)
                clipped = True
                flags.append("copyright_trimmed")
                break
        if not clipped:
            out.append(s)
    return "".join(out), flags


def banned_check(text: str) -> tuple:
    """返回 (clean_text, hits)。命中词直接剔除。"""
    hits = [w for w in _BANNED if w in (text or "")]
    clean = text or ""
    for w in hits:
        clean = clean.replace(w, "*")
    return clean, hits


def youth_mode_filter(categories: list) -> bool:
    """青少年模式：结果类别是否放行（示范策略）。"""
    if not strategies.get("security.youth_mode", False):
        return True
    blocked = {"恐怖", "惊悚"}
    return not any(c in blocked for c in categories)
