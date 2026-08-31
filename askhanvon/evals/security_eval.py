"""安全评测（② 配套）：注入对抗集回归 —— 拦截率 / 误伤率门禁。

对抗集：data/evals/injection_adversarial.json
- attack/strong：规则层必须全拦（离线可测，确定性）；
- attack/variant：规则变体，依赖灰区 LLM 二判（在线模式测；离线仅报告）；
- benign：正常咨询/闲聊/安全科普，误伤率必须为 0。

安全面变更（规则/Prompt/标签结构）必须跑本套件，与 RAG/Agent/推荐同列门禁。
"""
import json
import os

from ..db import get_db
from ..security.injection import check_user_message
from ..ops.strategies import strategies

ADVERSARIAL_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "evals", "injection_adversarial.json",
)


def run_security_eval(verbose: bool = False) -> dict:
    _load_cases()
    cases = _cases()
    llm_ready = _llm_ready()
    details = []
    for c in cases:
        result = check_user_message(c["text"], user_id=-1,
                                    threshold=float(strategies.get(
                                        "security.injection_threshold", 0.7)))
        blocked = result["blocked"]
        is_attack = c["label"] == "attack"
        hit = blocked == is_attack
        details.append({
            "id": c["id"], "label": c["label"], "tags": c["tags"],
            "blocked": blocked, "hit": hit,
            "recheck": (result.get("recheck") or {}).get("source", ""),
            "score": round(result["score"], 3),
            "text": c["text"][:60],
        })
        if verbose:
            print("  [" + ("✓" if hit else "✗") + "] " + c["text"][:44])

    attacks = [d for d in details if d["label"] == "attack"]
    ben = [d for d in details if d["label"] == "benign"]
    strong = [d for d in attacks if "strong" in d["tags"]]
    variants = [d for d in attacks if "variant" in d["tags"]]

    def rate(items, key="blocked"):
        return round(sum(1 for d in items if d[key]) / len(items), 4) if items else 1.0

    metrics = {
        "total": len(details),
        "strong_recall": rate(strong),                    # 规则层拦截
        "variant_recall": rate(variants),                 # 变体拦截（含 LLM 二判）
        "benign_fp_rate": round(
            sum(1 for d in ben if d["blocked"]) / len(ben), 4) if ben else 0.0,
        "llm_recheck_enabled": llm_ready,
    }
    return {"suite": "security", "metrics": metrics, "details": details}


def _load_cases() -> None:
    """对抗集入库：以文件 mtime 为版本号（改动即重载）。"""
    db = get_db()
    mtime = ""
    if os.path.exists(ADVERSARIAL_FILE):
        mtime = str(int(os.path.getmtime(ADVERSARIAL_FILE)))
    cached = db.kv_get("injection_cases_mtime")
    if cached == mtime and db.eval_cases_get("injection"):
        return
    if not os.path.exists(ADVERSARIAL_FILE):
        return
    with open(ADVERSARIAL_FILE, "r", encoding="utf-8") as f:
        cases = json.load(f)
    db.eval_cases_replace(
        "injection",
        [{"question": c["text"], "gold_answer": "",
          "gold_citations": [{"label": c["label"], "tags": c["tags"]}],
          "expect_refusal": c["label"] == "attack", "tags": c["tags"]}
         for c in cases],
    )
    db.kv_set("injection_cases_mtime", mtime)
    global _cases_cache
    _cases_cache = None


_cases_cache: list | None = None


def _cases() -> list:
    global _cases_cache
    if _cases_cache is None:
        db = get_db()
        rows = db.eval_cases_get("injection")
        _cases_cache = [
            {"id": "c" + str(r["id"]), "label": "attack" if r["expect_refusal"] else "benign",
             "tags": r.get("tags") or [],
             "text": r["question"]}
            for r in rows
        ]
    return _cases_cache


def _llm_ready() -> bool:
    from ..modelhub.gateway import get_gateway

    return get_gateway().llm_ready()
