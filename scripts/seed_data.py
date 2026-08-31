"""演示数据初始化：管理员/用户 → 样书入库 → 运营配置 → 行为埋点 → 特征/训练 → 评测集。

凭据策略（安全约束）：源码不写入任何口令字面量。管理员口令优先取环境变量
ADMIN_PASSWORD；未设置时用 secrets 随机生成并只在初始化时打印一次（不落盘）。
造数随机：确定性演示随机源（blake2b 派生，可复现，非加密用途）。
"""
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from askhanvon.config import settings  # noqa: E402
from askhanvon.db import get_db  # noqa: E402
from askhanvon.events import collector  # noqa: E402
from askhanvon.server.auth import hash_password  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOKS_DIR = os.path.join(ROOT, "books")


class DetRng:
    """确定性造数随机源（哈希派生；仅用于演示数据生成，非加密用途）。"""

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


def ensure_users() -> dict:
    db = get_db()
    if not db.get_user_by_username("admin"):
        pwd = os.environ.get("ADMIN_PASSWORD") or ("ah-" + secrets.token_hex(4))
        db.create_user("admin", hash_password(pwd), "admin", "管理员")
        print("  [+] 已创建管理员 admin，口令(仅显示一次): " + pwd)
    else:
        print("  [=] admin 已存在")
    demo_users = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Henry"]
    created = 0
    for name in demo_users:
        uname = name.lower()
        if not db.get_user_by_username(uname):
            pwd = "ah-" + secrets.token_hex(3)
            db.create_user(uname, hash_password(pwd), "user", name)
            created += 1
    print("  [+] 演示用户已就绪: " + ", ".join(u.lower() for u in demo_users)
          + "（新建 " + str(created) + "，口令仅创建时打印）")
    users = {u["username"]: u["id"] for u in db.list_users()}
    return users


def ingest_books() -> list:
    from askhanvon.pipeline.index_build import ingest_dir
    from askhanvon.rag.retriever import get_retriever

    if not os.path.isdir(BOOKS_DIR):
        print("  [!] 未找到 books/ 目录，跳过样书入库")
        return []
    reports = ingest_dir(BOOKS_DIR, reindex=True)
    get_retriever().invalidate()
    ok = [r for r in reports if "error" not in r]
    print("  [+] 样书入库: " + str(len(ok)) + " 本 / " + str(len(reports)) + " 个文件")
    for r in ok:
        print("      《" + r["title"] + "》 章节 " + str(r["chapters"]) + " · 块 " + str(r["chunks"]))
    return reports


def seed_ops(book_ids: list) -> None:
    db = get_db()
    from askhanvon.ops.ab import ab_service
    from askhanvon.ops.campaigns import campaigns as campaign_svc

    if not db.exp_get_by_name("rec_rank_v1"):
        ab_service.create(
            "rec_rank_v1",
            "推荐精排 A/B：A=规则权重，B=离线训练权重（LTR）",
            [
                {"key": "A", "weight": 50, "params": {"rec.use_trained": False}},
                {"key": "B", "weight": 50, "params": {"rec.use_trained": True}},
            ],
            traffic_pct=100,
        )
        print("  [+] A/B 实验 rec_rank_v1 已创建")
    if not db.campaign_list():
        if len(book_ids) >= 2:
            campaign_svc.create(
                "新学期书单", "homepage", book_ids[:2], weight=2.5,
                start_at="2020-01-01T00:00:00", end_at="2030-01-01T00:00:00",
            )
            print("  [+] Campaign「新学期书单」已创建")
    if not db.priority_list():
        if book_ids:
            campaign_svc.set_priority("homepage", book_ids[0], 2.0, "编辑推荐 · 首页首位")


def simulate_events(users: dict, book_ids: list) -> int:
    """30 天行为模拟：8 个偏好画像用户 → 埋点 → 特征/CF/LTR 训练数据。"""
    rng = DetRng(20260828)
    if len(book_ids) < 4:
        print("  [!] 样书不足，跳过行为模拟")
        return 0
    cat_of = {}
    for bid in book_ids:
        b = get_db().get_book(bid)
        if b:
            cat_of[bid] = b.get("category") or "未分类"
    profiles = [
        ("alice", ["神魔小说", "科普"]),
        ("bob", ["历史演义", "历史"]),
        ("carol", ["世情小说"]),
        ("dave", ["科普"]),
        ("eve", ["英雄传奇", "历史演义"]),
        ("frank", ["历史"]),
        ("grace", ["世情小说", "神魔小说"]),
        ("henry", ["科普", "英雄传奇"]),
    ]
    now = datetime.now()
    n = 0
    for uname, cats in profiles:
        uid = users.get(uname)
        if not uid:
            continue
        pref_books = [b for b in book_ids if cat_of.get(b) in cats]
        for day in range(30, 0, -1):
            base = now - timedelta(days=day, hours=rng.randint(0, 12))
            sid = "s_seed_" + uname + "_" + str(day)
            for _ in range(rng.randint(2, 5)):
                ts = (base + timedelta(minutes=rng.randint(0, 120))).isoformat(timespec="seconds")
                for bid in rng.sample(book_ids, k=min(4, len(book_ids))):
                    p_click = 0.55 if bid in pref_books else 0.08
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
                                "book_id": bid, "ts": ts,
                                "props": {"seconds": rng.randint(60, 1800)},
                            })
                            n += 1
                if rng.random() < 0.3 and pref_books:
                    collector.emit({
                        "event_type": "collect", "user_id": uid,
                        "book_id": rng.choice(pref_books), "ts": ts,
                    })
                    n += 1
    return n


def seed_eval_cases() -> int:
    from askhanvon.evals.rag_eval import ensure_golden_seeded

    n = ensure_golden_seeded()
    print("  [+] RAG 黄金评测集: " + str(n) + " 题")
    return n


def main() -> None:
    print("== 问小汉 · 演示数据初始化 ==")
    users = ensure_users()
    reports = ingest_books()
    book_ids = [r["book_id"] for r in reports if "book_id" in r]
    seed_ops(book_ids)
    n = simulate_events(users, book_ids)
    print("  [+] 模拟埋点: " + str(n) + " 条")
    print("  [i] 消费埋点 → 事件明细 + 特征 …")
    while collector.flush_once(500):
        pass
    from askhanvon.offline.features import recompute_features

    print("  [+] 特征重算: " + json.dumps(recompute_features(), ensure_ascii=False))
    from askhanvon.offline.train import train_all

    m = train_all()
    print("  [+] 离线训练: " + json.dumps(m, ensure_ascii=False))
    from askhanvon.offline.train import precompute_candidates

    c = precompute_candidates()
    print("  [+] 候选集预计算: " + json.dumps(c, ensure_ascii=False))
    seed_eval_cases()
    print("== 初始化完成 ==")


if __name__ == "__main__":
    main()
