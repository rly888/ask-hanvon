"""评测门禁测试：套件可运行、门禁判定结构正确、结果落库。"""
from askhanvon.evals.agent_eval import run_agent_eval
from askhanvon.evals.rag_eval import ensure_golden_seeded, run_rag_eval
from askhanvon.evals.rec_eval import run_rec_eval
from askhanvon.evals.runner import _gates_for, run_suite


def test_golden_set_seeded_and_large():
    n = ensure_golden_seeded()
    assert n >= 100, "黄金评测集应不少于 100 题"


def test_rag_eval_smoke(sample_book):
    report = run_rag_eval(limit=6, use_cache=False)
    m = report["metrics"]
    assert m["total"] == 6
    assert 0.0 <= m["citation_accuracy"] <= 1.0
    assert m["p95_latency_ms"] >= 0


def test_agent_eval_smoke(sample_book):
    report = run_agent_eval()
    m = report["metrics"]
    assert m["total"] == len(report["details"])
    assert 0.0 <= m["intent_accuracy"] <= 1.0


def test_rec_eval_smoke(sample_book):
    report = run_rec_eval(top_k=5)
    assert "ndcg_at_k" in report["metrics"]
    assert "coverage" in report["metrics"]


def test_gate_rules_structure():
    g = _gates_for("rag", {"citation_accuracy": 0.9, "answer_pass_rate": 0.8,
                           "refusal_rate_unanswerable": 0.95})
    assert all(v[2] for v in g.values())
    g2 = _gates_for("agent", {"tool_success_rate": 0.5, "hallucination_rate": 0.5})
    assert not all(v[2] for v in g2.values())


def test_run_suite_persists(sample_book):
    report = run_suite("agent")
    from askhanvon.db import get_db

    runs = get_db().eval_runs_list(suite="agent", limit=1)
    assert runs
