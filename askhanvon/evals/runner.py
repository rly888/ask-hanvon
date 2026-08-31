"""评测门禁 runner：三套评测 + 门禁判定 + 结果落库（没有门禁不上线，§3.3）。"""
import json
import math

from ..config import settings
from ..db import dumps, get_db
from ..obs.logging import get_logger, log_fields
from .agent_eval import run_agent_eval
from .rag_eval import run_rag_eval
from .rec_eval import run_rec_eval

logger = get_logger("askhanvon.evals")

SUITES = {"rag": run_rag_eval, "agent": run_agent_eval, "rec": run_rec_eval}


def _gates_for(suite: str, m: dict) -> dict:
    """门禁规则：每次模型/索引/策略变更必须全绿。"""
    if suite == "rag":
        return {
            "citation_accuracy>=": [
                settings.gate_citation_accuracy, m.get("citation_accuracy", 0.0),
                m.get("citation_accuracy", 0.0) >= settings.gate_citation_accuracy,
            ],
            "answer_pass_rate>=": [
                settings.gate_answer_pass_rate, m.get("answer_pass_rate", 0.0),
                m.get("answer_pass_rate", 0.0) >= settings.gate_answer_pass_rate,
            ],
            "refusal_rate_unanswerable>=": [
                settings.gate_refusal_rate, m.get("refusal_rate_unanswerable", 0.0),
                m.get("refusal_rate_unanswerable", 0.0) >= settings.gate_refusal_rate,
            ],
        }
    if suite == "agent":
        return {
            "tool_success_rate>=": [
                settings.gate_tool_success_rate, m.get("tool_success_rate", 0.0),
                m.get("tool_success_rate", 0.0) >= settings.gate_tool_success_rate,
            ],
            "hallucination_rate<=": [0.05, m.get("hallucination_rate", 1.0),
                                     m.get("hallucination_rate", 1.0) <= 0.05],
        }
    if suite == "rec":
        return {
            "ndcg_at_k>0": [0.0, m.get("ndcg_at_k", 0.0), m.get("ndcg_at_k", 0.0) > 0.0],
            "coverage>0": [0.0, m.get("coverage", 0.0), m.get("coverage", 0.0) > 0.0],
        }
    return {}


def run_suite(name: str, verbose: bool = False, limit: int | None = None) -> dict:
    fn = SUITES.get(name)
    if not fn:
        raise ValueError("未知评测套件: " + name)
    kwargs = {}
    if name == "rag":
        kwargs = {"verbose": verbose, "limit": limit, "use_cache": False}
    else:
        kwargs = {"verbose": verbose}
    report = fn(**kwargs)
    metrics = report["metrics"]
    gates = _gates_for(name, metrics)
    gate_passed = all(g[2] for g in gates.values()) if gates else True
    report["gates"] = gates
    report["gate_passed"] = gate_passed

    passed = sum(1 for g in gates.values() if g[2])
    db = get_db()
    db.eval_run_save(
        suite=name, total=metrics.get("total", 0), passed=passed,
        metrics_json=dumps(metrics), gates_json=dumps(gates),
        gate_passed=1 if gate_passed else 0, details_json=dumps(report.get("details", {})),
    )
    log_fields(logger, 20 if gate_passed else 40, "eval.suite_done",
               suite=name, gate_passed=gate_passed, metrics=metrics)
    return report


def run_all_gates(verbose: bool = False) -> dict:
    results = {}
    all_pass = True
    for name in ("rag", "agent", "rec"):
        try:
            r = run_suite(name, verbose=verbose)
            results[name] = r
            all_pass = all_pass and r["gate_passed"]
        except Exception as e:  # noqa: BLE001 — 单套件失败=门禁不通过
            logger.error("eval suite failed: %s", e)
            results[name] = {"suite": name, "error": str(e)[:200], "gate_passed": False}
            all_pass = False
    return {"all_pass": all_pass, "suites": results}


def render_report(results: dict) -> str:
    lines = []
    lines.append("=" * 62)
    lines.append("问小汉 · 评测门禁报告（不达标不上线）")
    lines.append("=" * 62)
    for name, r in results.get("suites", {}).items():
        if "error" in r:
            lines.append("[" + name + "] 执行失败: " + r["error"])
            continue
        m = r["metrics"]
        lines.append("[" + name + "] 门禁: " + ("✅ 通过" if r["gate_passed"] else "❌ 未通过"))
        for k, v in m.items():
            lines.append("    " + k + " = " + str(v))
        for gk, g in (r.get("gates") or {}).items():
            lines.append("    gate " + gk + ": 期望 " + str(g[0]) + " 实际 " + str(g[1])
                         + " → " + ("✅" if g[2] else "❌"))
    lines.append("-" * 62)
    lines.append("总体: " + ("✅ 全部门禁通过" if results.get("all_pass") else "❌ 存在未通过门禁"))
    return "\n".join(lines)
