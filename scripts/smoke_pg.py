"""PG 模式全链路 smoke（方案 A 验收）。

用法（需先 docker compose -f docker-compose.prod.yml up -d 或手工起容器）：
    export DB_ENGINE=postgres
    export PG_DSN=postgresql://askhanvon:...@127.0.0.1:5432/askhanvon
    export DATA_DIR=<临时目录>       # SQLite 检索库位置
    python scripts/smoke_pg.py

覆盖：schema / 用户注册登录刷新 / 样书入库(检索域) / RAG 问答 / 推荐 /
购买两步 / 埋点消费 / 策略/A-B/Prompt / 评测集与运行 / 审计。
"""
import os
import secrets
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("LOG_LEVEL", "ERROR")
os.environ.setdefault("DATA_DIR", os.path.join(tempfile.gettempdir(), "ah_pg_smoke"))


def main() -> int:
    if os.environ.get("DB_ENGINE") != "postgres":
        print("请设置 DB_ENGINE=postgres 与 PG_DSN")
        return 1
    from askhanvon.events import collector
    from askhanvon.events.collector import flush_once
    from askhanvon.pipeline.index_build import ingest_dir
    from askhanvon.recommend.engine import get_rec_engine
    from askhanvon.server.auth import hash_password
    from askhanvon.tools.book_qa import ask_rag
    from askhanvon.tools.registry import get_registry
    from askhanvon.tools.schema import ToolContext
    from askhanvon.config import settings
    from askhanvon.db import get_db

    db = get_db()
    ok = []

    def check(name, cond):
        ok.append((name, bool(cond)))
        print(("  [PASS] " if cond else "  [FAIL] ") + name)

    print("== PG 模式 smoke（DSN host 见环境变量）==")

    # 1) 用户注册/登录/刷新令牌（PG users/auth_tokens/kv）
    smoke_user = "pgsmoke_" + secrets.token_hex(3)
    uid = db.create_user(smoke_user, hash_password("ok-" + secrets.token_hex(6)), "user",
                         "PG 冒烟")
    check("users 写入", uid > 0)
    u = db.get_user_by_username(smoke_user)
    check("users 读取", u["id"] == uid)
    raw = secrets.token_urlsafe(16)
    db.auth_token_save(__import__("hashlib").sha256(raw.encode()).hexdigest(), uid,
                       "refresh", 9999999999)
    row = db.auth_token_get_valid(__import__("hashlib").sha256(raw.encode()).hexdigest(),
                                  "refresh")
    check("auth_tokens 生效", row is not None)
    db.kv_set("smoke_probe", "pg-ok")
    check("kv 读写", db.kv_get("smoke_probe") == "pg-ok")

    # 2) 样书入库（检索域 → SQLite）
    reports = ingest_dir("books", reindex=True)
    check("样书入库", len([r for r in reports if "book_id" in r]) == 6)

    # 3) RAG 问答（检索 sqlite + 引用）
    r = ask_rag("《西游记》里孙悟空为什么被压五行山下？", user_id=-1, use_cache=False)
    check("RAG 问答带引用", (not r["refused"]) and r["citations"])

    # 4) 推荐（特征读 PG）
    items = get_rec_engine().recommend(uid, top_k=4, track=False)
    check("推荐可解释", items and all(i["reasons"] for i in items))

    # 5) 购买两步（orders）
    reg = get_registry()
    ctx = ToolContext(user_id=uid, role="user")
    book = db.all_books()[0]
    o = reg.invoke("purchase_init", {"book_title": book["title"]}, ctx)
    check("下单", o.ok and o.data.get("confirm_token"))
    p = reg.invoke("purchase_confirm",
                   {"order_id": o.data["order_id"],
                    "confirm_token": o.data["confirm_token"]}, ctx)
    check("确认支付", p.ok and p.data.get("status") == "paid")

    # 6) 埋点消费（event_queue / events / features → PG）
    collector.emit({"event_type": "click", "user_id": uid, "book_id": book["id"]})
    collector.emit({"event_type": "impression", "user_id": uid, "book_id": book["id"]})
    flush_once()
    check("事件消费", db.events_count("click") >= 1)
    feats = db.feature_book_map().get(book["id"], {})
    check("特征落库", "clicks" in feats or "impressions" in feats)

    # 7) 策略 / A-B / Prompt（ops_* → PG）
    from askhanvon.ops.ab import ab_service
    from askhanvon.ops.prompts import prompt_service
    from askhanvon.ops.strategies import strategies

    strategies.set("smoke.probe", {"a": 1}, by="smoke")
    check("策略写入", strategies.get("smoke.probe") == {"a": 1})
    exp_name = "pg_smoke_exp_" + secrets.token_hex(3)
    ab_service.create(exp_name, "smoke", [{"key": "A", "weight": 100, "params": {}}])
    check("A-B 写入", ab_service.assign(exp_name, uid)["variant"] == "A")
    from askhanvon.generation.prompts import QA_SYSTEM

    ver = prompt_service.set("qa", QA_SYSTEM, by="smoke")
    check("Prompt 版本", ver >= 1)

    # 8) 评测集与运行（eval_* → PG）
    from askhanvon.evals.runner import run_suite

    report = run_suite("agent")
    check("评测运行落 PG", report["gate_passed"] and db.eval_runs_list(suite="agent"))

    # 9) 审计（audit_* → PG）
    db.audit_log(uid, "smoke", "smoke_tool", "{}", "allow", "test")
    check("审计落库", len(db.audit_list(decision="allow")) >= 1)

    print("== 结果:", "ALL PASS ✅" if all(c for _, c in ok) else "SOME FAILED ❌")
    return 0 if all(c for _, c in ok) else 1


if __name__ == "__main__":
    sys.exit(main())
