"""Agent 编排循环：意图 → 计划 → 执行 → 综合 → 记忆 → 埋点（§1.2 主链路实现）。"""
import time

from ..conversation.intent import route_intent
from ..conversation.profile import ProfileService
from ..conversation.session import SessionStore
from ..db import get_db
from ..events.collector import emit
from ..obs.logging import get_logger, log_fields
from ..obs.metrics import metrics
from ..obs.tracing import new_trace_id
from ..ops.strategies import strategies
from ..security.injection import check_user_message
from ..tools.schema import ToolContext
from .executor import Executor
from .planner import Planner
from .synthesizer import Synthesizer

logger = get_logger("askhanvon.agent")

_INJECTION_REFUSAL = (
    "这条请求里包含了我不能执行的指令内容。你可以继续问我书的内容、"
    "让我推荐图书，或管理你的书架。"
)


class AgentLoop:
    def __init__(self):
        self.store = SessionStore()
        self.planner = Planner()
        self.executor = Executor()
        self.synth = Synthesizer()

    # ---------- 非流式 ----------
    def handle(self, message: str, user_id=None, role: str = "anonymous",
               session_id: str = "") -> dict:
        events = []
        final = self._run(message, user_id, role, session_id,
                          on_event=lambda t, p=None: events.append({**(p or {}), "type": t}))
        final["steps"] = events
        return final

    # ---------- 流式（SSE 事件回调）----------
    def handle_stream(self, message: str, user_id=None, role: str = "anonymous",
                      session_id: str = "", on_event=None) -> dict:
        return self._run(message, user_id, role, session_id, on_event, stream=True)

    # ---------- 核心 ----------
    def _run(self, message: str, user_id, role, session_id, on_event, stream: bool = False) -> dict:
        t0 = time.perf_counter()
        new_trace_id()
        db = get_db()
        emit_step = on_event or (lambda t, p: None)
        store = self.store

        # 会话
        session = store.get_or_create(session_id, user_id,
                                      store.title_from_message(message))
        sid = session["id"]

        # 1) 用户输入安全闸（prompt injection）
        inj = check_user_message(message, user_id=user_id,
                                 threshold=0.7)
        if inj["blocked"]:
            db.audit_log(user_id, "chat_blocked", "", "", "deny", "injection")
            payload = {"type": "text", "text": _INJECTION_REFUSAL,
                       "intent": "blocked", "citations": [], "items": []}
            _persist(store, sid, user_id, message, payload)
            emit_step("done", payload)
            return payload

        # 2) 意图识别（含多轮改写）
        intent = route_intent(message, store, sid)
        store.memory_set(sid, "last_intent", intent.intent)
        emit_step("intent", {"intent": intent.intent, "book_title": intent.book_title,
                             "rewritten": intent.rewritten, "source": intent.source,
                             "confidence": intent.confidence})
        emit(
            {"event_type": "chat_message", "user_id": user_id, "session_id": sid,
             "query": message, "props": {"intent": intent.intent, "role": role}}
        )

        # 3) 计划
        plan = self.planner.plan(intent, user_id)
        emit_step("plan", {"steps": [{"tool": s.tool, "args": s.args, "reason": s.reason}
                                     for s in plan.steps]})

        ctx = ToolContext(user_id=user_id, role=role, session_id=sid)

        # 4) 执行（QA 走流式工具）
        synth_input: list = []
        for step in plan.steps:
            emit_step("tool", {"tool": step.tool, "args": step.args, "reason": step.reason,
                               "status": "running"})
            if step.tool == "book_qa":
                from ..tools.book_qa import ask_rag_stream

                profile_block = ProfileService().prompt_block(user_id)

                def _bridge(t, p):
                    # 工具内事件 → Agent 流事件（delta 透传给前端打字机效果）
                    if t in ("retrieval", "context"):
                        emit_step(t, {"tool": "book_qa", **p})
                    elif t == "delta":
                        emit_step("delta", {"text": p.get("text", "")})

                data = ask_rag_stream(
                    step.args.get("query", message),
                    user_id=user_id,
                    book_hint=step.args.get("book_title", ""),
                    profile_block=profile_block,
                    on_event=_bridge,
                )
                from ..tools.schema import ToolResult

                result = ToolResult(ok=True, data=data, meta={"tool": "book_qa",
                                                              "reason": step.reason})
                emit_step("citations", {"citations": data.get("citations", [])})
            else:
                result = self.executor_run_one(step.tool, step.args, ctx)
            synth_input.append(result)
            emit_step("tool", {"tool": step.tool, "status": "done", "ok": result.ok})

        # 5) 综合
        payload = self.synth.synthesize(intent.intent, message, synth_input,
                                        user_id=user_id)
        payload["intent"] = intent.intent
        payload["session_id"] = sid
        payload.setdefault("citations", [])
        payload.setdefault("items", [])

        # 5b) 拒答后改写重检一次（P2-3，非流式路径：流式已发出内容不便推翻）
        if (not stream and payload.get("refused")
                and strategies.get("answer.retry_on_refusal", True)):
            emit_step("retry", {"phase": "rewrite_and_reretrieve"})
            from ..rag.rewrite import rewrite_query
            from ..tools.book_qa import ask_rag

            rw = rewrite_query(intent.rewritten or message,
                               book_title=intent.book_title or "", user_id=user_id)
            retry = ask_rag(rw, user_id=user_id, book_hint=intent.book_title,
                            profile_block=ProfileService().prompt_block(user_id),
                            use_cache=False)
            if not retry.get("refused"):
                payload.update({
                    "type": "qa",
                    "text": retry["answer"],
                    "citations": retry.get("citations", []),
                    "refused": False,
                    "confidence": retry.get("confidence", 0),
                    "verified_ratio": retry.get("verified_ratio", 0),
                    "model": retry.get("model", ""),
                    "usage": retry.get("usage", {}),
                    "prompt_version": retry.get("prompt_version", 0),
                })
                payload["retried"] = True
                emit_step("retry", {"query": rw, "status": "ok"})

        # 5c) 追问建议（P2-4：规则生成，附于 done payload）
        payload["suggestions"] = self._suggestions(intent.intent, payload)

        # 6) 记忆更新（书名追踪，供多轮指代消解）
        book_ref = intent.book_title
        if not book_ref:
            for c in payload.get("citations", [])[:1]:
                book_ref = c.get("book_title") or ""
        if not book_ref and payload.get("items"):
            book_ref = payload["items"][0].get("title", "")
        if book_ref:
            store.memory_set(sid, "last_book", book_ref)

        # 7) 持久化 + 指标
        _persist(store, sid, user_id, message, payload)
        latency = (time.perf_counter() - t0) * 1000
        payload["latency_ms"] = round(latency, 1)
        metrics.observe("chat_latency_ms", latency)
        emit_step("done", payload)
        return payload

    def executor_run_one(self, tool: str, args: dict, ctx: ToolContext):
        from ..tools.registry import get_registry

        return get_registry().invoke(tool, args, ctx)

    @staticmethod
    def _suggestions(intent: str, payload: dict) -> list:
        """追问建议（规则生成，P2-4）：随 done payload 下发，前端渲染为可点 chips。"""
        book = ""
        if payload.get("citations"):
            book = payload["citations"][0].get("book_title") or ""
        elif payload.get("items"):
            book = payload["items"][0].get("title") or ""
        if intent == "qa" and book:
            return [
                "《" + book + "》里最精彩的情节是什么",
                "推荐和《" + book + "》类似的书",
            ]
        if intent in ("recommend", "search") and payload.get("items"):
            top = payload["items"][0].get("title") or ""
            out = ["换一个分类推荐一下"]
            if top:
                out.insert(0, "我想买《" + top + "》")
            return out
        if intent == "library":
            return ["帮我推荐几本新书"]
        return []


def _persist(store: SessionStore, sid: str, user_id, message: str, payload: dict) -> None:
    store.add_message(sid, "user", message, {"intent": payload.get("intent")})
    store.add_message(
        sid,
        "assistant",
        payload.get("text", ""),
        {
            "type": payload.get("type"),
            "citations": payload.get("citations", []),
            "items": payload.get("items", []),
            "degraded": payload.get("degraded", False),
        },
    )


_agent: AgentLoop | None = None


def get_agent() -> AgentLoop:
    global _agent
    if _agent is None:
        _agent = AgentLoop()
    return _agent
