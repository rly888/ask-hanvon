"""Agent 评测：任务用例集（意图路由正确性 / 工具调用成功率 / 幻觉率）。"""
from ..agent.loop import get_agent

CASES = [
    {"query": "《西游记》里孙悟空为什么大闹天宫？", "intent": "qa", "tools": ["book_qa"]},
    {"query": "《红楼梦》的作者是谁？", "intent": "qa", "tools": ["book_qa"]},
    {"query": "帮我推荐几本历史类的书", "intent": "recommend", "tools": ["recommend_books"]},
    {"query": "我最近想看点科普，有什么推荐？", "intent": "recommend", "tools": ["recommend_books"]},
    {"query": "帮我找一下《水浒传》", "intent": "search", "tools": ["book_search"]},
    {"query": "比较一下《西游记》和《三国演义》", "intent": "compare", "tools": ["compare_books"]},
    {"query": "我想买《宇宙探索简史》", "intent": "purchase", "tools": ["purchase_init"]},
    {"query": "你好呀，你是谁？", "intent": "chitchat", "tools": []},
    {"query": "讲讲官渡之战", "intent": "qa", "tools": ["book_qa"]},
    {"query": "《中国历史十五讲》里怎么讲丝绸之路的？", "intent": "qa", "tools": ["book_qa"]},
]


def run_agent_eval(verbose: bool = False) -> dict:
    agent = get_agent()
    details = []
    for c in CASES:
        result = agent.handle(c["query"], user_id=None, role="anonymous",
                              session_id="eval_agent_" + c["query"][:8])
        called = [s.get("tool") for s in result.get("steps", [])
                  if s.get("type") == "tool" and s.get("status") == "done"]
        # 兼容：tool done 事件可能不重复携带工具名，从 plan 取
        if not called:
            called = [s.get("tool") for s in result.get("steps", []) if s.get("type") == "plan"
                      for s in s.get("steps", [])]
        intent_ok = result.get("intent") == c["intent"]
        tools_ok = all(t in called for t in c["tools"]) and (
            len(called) == len(c["tools"])
        )
        # 幻觉率：QA 类回答必须带引用或明确拒答（无据可依的回答视为幻觉风险）
        hallucination = False
        if c["intent"] == "qa" and result.get("type") == "qa":
            has_citations = bool(result.get("citations"))
            hallucination = (not result.get("refused")) and (not has_citations)
        details.append(
            {
                "query": c["query"],
                "intent_ok": intent_ok,
                "tools_ok": tools_ok,
                "called": called,
                "expected": c["tools"],
                "hallucination": hallucination,
            }
        )
        if verbose:
            print("  [" + ("✓" if intent_ok and tools_ok else "✗") + "] " + c["query"][:30]
                  + " → " + str(called))
    n = len(details)
    metrics_out = {
        "total": n,
        "intent_accuracy": round(sum(1 for d in details if d["intent_ok"]) / n, 4),
        "tool_selection_accuracy": round(sum(1 for d in details if d["tools_ok"]) / n, 4),
        "tool_success_rate": round(
            sum(1 for d in details if d["tools_ok"] and not d["hallucination"]) / n, 4
        ),
        "hallucination_rate": round(sum(1 for d in details if d["hallucination"]) / n, 4),
    }
    return {"suite": "agent", "metrics": metrics_out, "details": details}
