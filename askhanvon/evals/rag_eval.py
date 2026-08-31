"""RAG 评测：黄金标注集回归（问题-答案-引用位置），指标即上线门禁。"""
import json
import os
import time

from ..config import settings
from ..db import get_db
from ..generation.citation import locator_match
from ..nlp import tokenize
from ..tools.book_qa import ask_rag

GOLDEN_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "evals", "rag_golden.json",
)


def ensure_golden_seeded() -> int:
    """黄金集入库（表空时从 data/evals/rag_golden.json 导入）。"""
    db = get_db()
    if db.eval_cases_get("rag"):
        return len(db.eval_cases_get("rag"))
    if not os.path.exists(GOLDEN_FILE):
        return 0
    with open(GOLDEN_FILE, "r", encoding="utf-8") as f:
        cases = json.load(f)
    db.eval_cases_replace("rag", cases)
    return len(cases)


def _heuristic_judge(answer: str, gold: str) -> float:
    """离线评测裁判：答案与标准答案的词元重合度（LLM judge 的降级实现）。"""
    a = set(tokenize(answer))
    g = set(tokenize(gold))
    if not g:
        return 0.0
    return len(a & g) / len(g)


def _llm_judge(question: str, gold: str, answer: str):
    """LLM-as-judge。JUDGE_MODEL 配置时与被测模型解耦（P0-5）。"""
    from ..generation.prompts import JUDGE_SYSTEM
    from ..modelhub import quota as quota_mod
    from ..modelhub.gateway import LLMUnavailable, get_gateway

    gw = get_gateway()
    if not gw.llm_ready():
        return None
    try:
        res = gw.chat(
            [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user",
                 "content": "【问题】" + question + "\n【标准答案】" + gold
                            + "\n【系统回答】" + answer[:600]},
            ],
            tier="weak", user_id=-1, scene="eval_judge", max_tokens=150, temperature=0.1,
            model=settings.judge_model or None,
        )
        text = res.text
        s, e = text.find("{"), text.rfind("}")
        if s < 0 or e <= s:
            return None
        data = json.loads(text[s : e + 1])
        return max(1, min(5, int(data.get("score", 3))))
    except (LLMUnavailable, quota_mod.QuotaExceeded, ValueError):
        return None


def run_rag_eval(limit: int | None = None, use_cache: bool = False,
                 verbose: bool = False) -> dict:
    ensure_golden_seeded()
    cases = get_db().eval_cases_get("rag")
    if limit:
        cases = cases[:limit]
    use_llm_judge = _judge_ready()
    details = []
    latencies = []
    for c in cases:
        t0 = time.perf_counter()
        result = ask_rag(c["question"], user_id=-1, use_cache=use_cache)
        latency = (time.perf_counter() - t0) * 1000
        latencies.append(latency)

        gold_cites = c.get("gold_citations", [])
        expect_refusal = bool(c.get("expect_refusal"))
        if gold_cites:
            cited_ok = any(
                any(locator_match(cit, g) for g in gold_cites)
                for cit in result.get("citations", [])
            )
        else:
            cited_ok = not expect_refusal
        retrieval_hit = any(
            any(g.get("book") == r.get("book_title")
                and (g.get("chapter_no") is None
                     or int(g.get("chapter_no")) == int(r.get("chapter_no") or -1))
                for g in gold_cites)
            for r in result.get("retrieval", [])
        )
        if expect_refusal:
            refusal_ok = result["refused"]
            answer_score = 5 if refusal_ok else 1
        else:
            refusal_ok = True
            judge = _llm_judge(c["question"], c.get("gold_answer", ""), result["answer"]) \
                if use_llm_judge else None
            if judge is None:
                answer_score = 5 if _heuristic_judge(
                    result["answer"], c.get("gold_answer", "")) >= 0.28 else 3
            else:
                answer_score = judge
        details.append(
            {
                "id": c.get("id"),
                "question": c["question"],
                "expect_refusal": expect_refusal,
                "cited_ok": cited_ok,
                "retrieval_hit": retrieval_hit,
                "refusal_ok": refusal_ok,
                "answer_score": answer_score,
                "refused": result["refused"],
                "latency_ms": round(latency, 1),
                "model": result.get("model", ""),
                "prompt_version": result.get("prompt_version", 0),
            }
        )
        if verbose:
            ok = (cited_ok and answer_score >= 3) if not expect_refusal else refusal_ok
            print("  [" + ("✓" if ok else "✗") + "] " + c["question"][:40])

    answerable = [d for d in details if not d["expect_refusal"]]
    unanswerable = [d for d in details if d["expect_refusal"]]
    lat_sorted = sorted(latencies)
    p95 = lat_sorted[max(0, int(len(lat_sorted) * 0.95) - 1)] if lat_sorted else 0.0
    prompt_versions = {d.get("prompt_version", 0) for d in details}

    def rate(items, key):
        return round(sum(1 for d in items if d[key]) / len(items), 4) if items else 1.0

    metrics_out = {
        "total": len(details),
        "citation_accuracy": (
            round(sum(1 for d in answerable if d["cited_ok"]) / len(answerable), 4)
            if answerable else 0.0
        ),
        "retrieval_recall": rate(answerable, "retrieval_hit"),
        "answer_pass_rate": (
            round(sum(1 for d in answerable if d["answer_score"] >= 3) / len(answerable), 4)
            if answerable else 1.0
        ),
        "refusal_rate_unanswerable": rate(unanswerable, "refused"),
        "false_refusal_rate": (
            round(sum(1 for d in answerable if d["refused"]) / len(answerable), 4)
            if answerable else 0.0
        ),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "p95_latency_ms": round(p95, 1),
        "judge": "llm" if use_llm_judge else "heuristic",
        "prompt_version": max(prompt_versions) if prompt_versions else 0,
    }
    return {"suite": "rag", "metrics": metrics_out, "details": details}


def _judge_ready() -> bool:
    from ..modelhub.gateway import get_gateway

    return get_gateway().llm_ready()
