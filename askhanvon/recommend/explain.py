"""推荐解释：每个结果标注命中通道与理由（规则命中 100% 可追踪）。"""


def explain_item(item: dict, catalog: dict) -> dict:
    channels = item.get("channels") or {}
    reasons = []
    priority_map = ["editorial", "cf", "content", "popular", "category"]
    for ch in priority_map:
        if ch not in channels:
            continue
        meta = channels[ch].get("meta", {})
        if ch == "editorial":
            reasons.append(meta.get("campaign") or meta.get("reason") or "编辑推荐")
        elif ch == "cf":
            seed = meta.get("seed_book")
            seed_title = catalog.get(seed, {}).get("title", seed or "你读过的书")
            reasons.append("读过《" + str(seed_title) + "》的读者也在读")
        elif ch == "content":
            reasons.append("与你常读的「" + str(meta.get("category", "")) + "」相符")
        elif ch == "popular":
            reasons.append("本周热门 · 第" + str(meta.get("rank", "?")) + "位")
        elif ch == "category":
            reasons.append("「" + str(meta.get("category", "")) + "」分类精选")
    if not reasons:
        reasons.append("为你探索")
    return {"reasons": reasons, "channels": list(channels.keys())}


def breakdown(item: dict) -> dict:
    """打分明细（特征 × 权重贡献），用于前端展开「为什么推荐」。"""
    feats = item.get("features") or {}
    return {
        "score": item.get("score"),
        "features": feats,
        "rules_applied": item.get("rules_applied", []),
    }
