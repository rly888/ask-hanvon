# -*- coding: utf-8 -*-
"""任务 5 · 推荐多路召回 + 用户画像注入：元数据入库 → 行为模拟 → 离线特征/CF/LTR
→ 多路召回评测（冷启动 / 画像用户 / 分类覆盖 / 混合语料技术书+小说）。

- 推荐引擎只用「书目元数据 + 行为」，不依赖 chunk 向量：
  txt/ 网文无元信息头，从文件名+规则 enrich（作者/分类/标签/介绍）；
  epub/ 走 parse_file 取 OPF 元数据。均不切块、不嵌入，快速绕开 RAG 的 embedding 瓶颈。
- 行为按「书目真实分类」模拟（技术书/小说分池），检验你关心的"混合语料覆盖偏"。
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from askhanvon.config import settings            # noqa: E402
from askhanvon.db import get_db                  # noqa: E402
from askhanvon.events import collector           # noqa: E402
from askhanvon.server.auth import hash_password  # noqa: E402
from askhanvon.pipeline.index_build import book_slug  # noqa: E402
from askhanvon.pipeline.parse import parse_file  # noqa: E402

USER_NAMES = ["alice", "bob", "carol", "dave", "eve", "frank", "grace", "henry", "ivy", "jack"]

# ---- txt/ 网文元数据 enrich 规则（文件名 → 作者/分类/标签/介绍）----
KNOWN_AUTHORS = {
    "斗罗大陆": "唐家三少", "酒神": "唐家三少", "琴帝": "唐家三少",
    "生肖守护神": "唐家三少", "空速星痕": "唐家三少", "冰火魔厨": "唐家三少",
    "善良的死神": "唐家三少",
    "星辰变": "我吃西红柿", "盘龙": "我吃西红柿", "寸芒": "我吃西红柿",
    "九鼎记": "我吃西红柿",
    "斗破苍穹": "天蚕土豆",
    "长生界": "辰东", "神魔": "辰东",
    "阳神": "梦入神机", "佛本是道": "梦入神机", "龙蛇演义": "梦入神机",
    "兽血沸腾": "静官",
    "庆余年": "猫腻",
    "亵渎": "烟雨江南",
    "紫川": "老猪",
    "回到明朝当王爷": "月关", "步步生莲": "月关",
    "无限恐怖": "zhttty", "王牌进化": "zhttty",
    "仙葫": "忘语", "凡人修仙传": "忘语",
    "吞噬星空": "我吃西红柿",
    "诛仙": "萧鼎", "飘渺之旅": "萧潜", "升龙道": "血红",
}
# 分类规则：书名子串关键词 → (分类, [标签])，先匹配先用
CATEGORY_RULES = [
    (("网游", "106网游", "屠龙巫师", "模拟城市", "职业人生", "近战法师", "梦幻现实", "魔兽剑圣"),
     "网游竞技", ["网游", "竞技", "游戏"]),
    (("篮球", "足球", "冠军教父", "我们是冠军", "宇皇星", "校园篮球"),
     "体育竞技", ["体育", "竞技", "校园"]),
    (("无限恐怖", "王牌进化", "进化空间"), "无限流", ["无限流", "惊悚", "轮回"]),
    # 军事须在"都市"之前，避免"谍变/横刀立马"被都市抢先匹配
    (("狙击", "终身制", "横刀立马", "谍变"), "军事谍战", ["军事", "特种", "谍战"]),
    (("三国", "明朝", "康熙", "初唐", "大唐", "战国", "大争之世", "步步生莲",
      "随波逐流", "混在三国", "军师", "军阀", "调教初唐", "迷失在", "惟我独尊",
      "商业三国", "大汉", "庆余年", "帝国风云录"),
     "历史", ["穿越", "历史", "权谋"]),
    (("高手寂寞", "冒牌大英雄", "无敌幸运星", "狗运战神", "恶汉", "江山美色",
      "红尘有梦", "花开堪折", "纨绔才子", "变脸武士", "骗艳记",
      "大亨传说", "猛龙过江", "天王", "命运的抉择", "邪气凛然", "天下之弱者", "血色梦游", "起点"),
     "都市", ["都市", "现代", "情感"]),
    (("仙", "道缘", "儒仙", "莲花宝鉴", "无极魔道", "惟我独仙", "逍行纪",
      "中华仙魔录", "张三丰", "黑山老妖", "仙葫", "邪风曲", "仙路"),
     "仙侠", ["修仙", "修真", "仙侠"]),
    (("异界", "法师", "佣兵天下", "迦南之心", "紫川", "亵渎", "恶魔法则",
      "圣斗士", "善良的死神", "魔兽", "异世界", "奶爸"),
     "西方奇幻", ["魔法", "异界", "西方"]),
    (("星际", "小兵传奇", "龙战星野", "机动风暴", "武装风暴", "天擎", "光脑", "太空", "亡灵帝国"),
     "科幻", ["星际", "机甲", "科幻"]),
    (("浪子江湖", "金庸", "道士", "江湖"), "武侠", ["武侠", "江湖", "恩怨"]),
]
DEFAULT_CATEGORY = "玄幻"  # 未命中关键词的兜底（网文主流类型）
DEFAULT_TAGS = ["玄幻", "修行", "热血"]


def norm_title(filename: str) -> str:
    """书名取自文件名（去扩展名，_ 转空格），与入库 slug 保持一致。"""
    return os.path.splitext(filename)[0].replace("_", " ").strip()


def classify(title: str):
    for kws, cat, tags in CATEGORY_RULES:
        for kw in kws:
            if kw in title:
                return cat, tags
    return DEFAULT_CATEGORY, DEFAULT_TAGS


def author_for(title: str) -> str:
    if title in KNOWN_AUTHORS:
        return KNOWN_AUTHORS[title]
    # 确定性笔名：哈希取值 + 姓氏池，保证可复现且尽量互异
    given = ["风清扬", "紫衣侯", "北冥客", "南山狐", "云上客", "夜未央",
             "青衫客", "白衣卿", "天机子", "纵横客", "星河客", "断桥客"]
    dig = hashlib.blake2b(title.encode("utf-8"), digest_size=4).digest()
    return given[int.from_bytes(dig[:2], "little") % len(given)]


def describe(title: str, cat: str, tags: list) -> str:
    tag = tags[0] if tags else cat
    return "《%s》是一部讲%s故事的%s题材长篇小说。" % (title, tag, cat)


class DetRng:
    """确定性演示随机源（blake2b 哈希链派生，可复现，非加密用途；与 seed_data.py 同源）。"""

    def __init__(self, seed: int):
        self._state = seed

    def _next(self) -> float:
        self._state += 1
        h = hashlib.blake2b(str(self._state).encode(), digest_size=8).digest()
        return int.from_bytes(h, "little") / 2.0 ** 64

    def randint(self, a: int, b: int) -> int:
        return a + int(self._next() * (b - a + 1)) % (b - a + 1)

    def random(self) -> float:
        return self._next()

    def sample(self, seq: list, k: int) -> list:
        pool = list(seq)
        out = []
        for _ in range(min(k, len(pool))):
            i = int(self._next() * len(pool)) % len(pool)
            out.append(pool.pop(i))
        return out

    def choice(self, seq: list):
        return seq[int(self._next() * len(seq)) % len(seq)]


def det_rng(seed: int) -> DetRng:
    return DetRng(seed)


def ensure_users() -> dict:
    db = get_db()
    out = {}
    for n in USER_NAMES:
        if not db.get_user_by_username(n):
            db.create_user(n, hash_password("ah-" + n), "user", n)
    return {u["username"]: u["id"] for u in db.list_users() if u["username"] in USER_NAMES}


def sync_catalog() -> int:
    """仅元数据入库（不切块不嵌入）。books/ 已在库，跳过；补 txt/ + epub/。

    txt/ 网文无元信息头（裸正文），书名取自文件名，作者/分类/标签/介绍
    由 enrich 规则就地生成（不落盘、不解析正文）；epub/ 走 parse_file 取 OPF 元数据。
    """
    db = get_db()
    existing = {b["id"] for b in db.all_books()}
    added = 0
    for sub in ("txt", "epub"):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith((".txt", ".epub", ".md")):
                continue
            if sub == "txt":
                title = norm_title(name)
                cat, tags = classify(title)
                book = {"title": title, "author": author_for(title),
                        "category": cat, "tags": tags,
                        "description": describe(title, cat, tags)}
            else:
                try:
                    parsed = parse_file(os.path.join(d, name))
                except Exception as e:  # noqa: BLE001
                    print("  [!] 解析失败 %s: %s" % (name, str(e)[:60]))
                    continue
                book = {"title": parsed.title, "author": parsed.author,
                        "category": parsed.category or "未分类", "tags": parsed.tags,
                        "description": parsed.description}
            bid = book_slug(book["title"])
            if bid in existing:
                continue
            existing.add(bid)
            db.upsert_book({
                "id": bid, "title": book["title"], "author": book["author"],
                "category": book["category"] or "未分类", "tags": ",".join(book["tags"]),
                "description": book["description"], "cover_emoji": "📖",
                "source_file": name, "n_chunks": 0, "embedding_model": "",
            })
            added += 1
    return added


def simulate_events(users: dict) -> int:
    """30 天行为：每用户 1-2 个真实分类偏好，技术书/小说分池，点击概率随偏好走高。"""
    db = get_db()
    catalog = db.all_books()
    by_cat: dict = {}
    for b in catalog:
        by_cat.setdefault(b.get("category") or "未分类", []).append(b["id"])
    cats = list(by_cat.keys())
    rng = det_rng(20260828)
    now = datetime.now()
    n = 0
    for uname, uid in users.items():
        pref = rng.sample(cats, k=min(2, len(cats)))
        pref_books = [b for b in catalog if (b.get("category") or "未分类") in pref]
        pref_ids = {b["id"] for b in pref_books}
        for day in range(30, 0, -1):
            base = now - timedelta(days=day, hours=rng.randint(0, 12))
            sid = "s5_" + uname + "_" + str(day)
            for _ in range(rng.randint(2, 5)):
                ts = (base + timedelta(minutes=rng.randint(0, 120))).isoformat(timespec="seconds")
                for bid in rng.sample(catalog, k=min(4, len(catalog))):
                    bid = bid["id"]
                    p_click = 0.55 if bid in pref_ids else 0.08
                    collector.emit({
                        "event_type": "impression", "user_id": uid, "session_id": sid,
                        "book_id": bid, "ts": ts,
                        "props": {"scene": "homepage", "variant": "A"},
                    })
                    n += 1
                    if rng.random() < p_click:
                        collector.emit({
                            "event_type": "click", "user_id": uid, "session_id": sid,
                            "book_id": bid, "ts": ts, "props": {"scene": "homepage"},
                        })
                        n += 1
                        if rng.random() < 0.35:
                            collector.emit({
                                "event_type": "read_duration", "user_id": uid,
                                "book_id": bid, "ts": ts, "props": {"seconds": rng.randint(60, 1800)},
                            })
                            n += 1
                if rng.random() < 0.3 and pref_ids:
                    collector.emit({
                        "event_type": "collect", "user_id": uid,
                        "book_id": rng.choice(list(pref_ids)), "ts": ts,
                    })
                    n += 1
    return n


def cat_coverage(items: list) -> dict:
    cov: dict = {}
    for it in items:
        cov[it.get("category") or "未分类"] = cov.get(it.get("category") or "未分类", 0) + 1
    return cov


def show_recs(label: str, items: list) -> None:
    print("\n### " + label)
    for it in items:
        print("  #%-2d %-24s [%s] | %s | %s" % (
            it["position"], (it.get("title") or "")[:24], it.get("category", ""),
            it["score"], " / ".join(it.get("reasons", []))[:40]))
    cov = cat_coverage(items)
    print("  分类覆盖: " + str(cov) + "  类别数=%d" % len(cov))


def main() -> None:
    print("== 1) 书目元数据入库（txt+epub，不切块）==")
    added = sync_catalog()
    db = get_db()
    catalog = db.all_books()
    print("  新增 %d 本；现存目录 %d 本" % (added, len(catalog)))
    cats = sorted({b.get("category") or "未分类" for b in catalog})
    print("  分类分布: %s" % {c: sum(1 for b in catalog if (b.get("category") or "未分类") == c)
                              for c in cats})

    print("\n== 2) 行为模拟（30 天，按真实分类偏好）==")
    users = ensure_users()
    n = simulate_events(users)
    print("  模拟埋点: %d 条" % n)
    while collector.flush_once(500):
        pass

    print("\n== 3) 离线特征重算 ==")
    from askhanvon.offline.features import recompute_features
    print("  " + json.dumps(recompute_features(), ensure_ascii=False))

    print("\n== 4) 离线训练（LTR + 协同过滤）==")
    from askhanvon.offline.train import train_all, precompute_candidates
    m = train_all()
    print("  " + json.dumps(m, ensure_ascii=False, default=str))
    print("  候选集预计算: " + json.dumps(precompute_candidates(), ensure_ascii=False))

    print("\n== 5) 多路召回评测 ==")
    from askhanvon.recommend.engine import get_rec_engine

    eng = get_rec_engine()
    # 冷启动：无任何行为的全新用户
    db.create_user("cold_new", hash_password("ah-cold"), "user", "冷启动")
    cold_id = db.get_user_by_username("cold_new")["id"]
    show_recs("冷启动用户（无行为）", eng.recommend(cold_id, "homepage", top_k=6, track=False))
    # 画像用户（有行为，走 CF + 内容偏好）
    for uname in ("alice", "eve"):
        show_recs("画像用户 %s" % uname,
                  eng.recommend(users[uname], "homepage", top_k=6, track=False))

    print("\n== 6) 运营位 / 策略（若已配）==")
    from askhanvon.ops.campaigns import campaigns
    print("  活动位: %s" % campaigns.active_for_slot("homepage"))


if __name__ == "__main__":
    main()
