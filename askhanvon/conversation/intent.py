"""意图识别与 Query 理解（§2 对话与交互：最重要的一次分流）。

规则优先（低延迟、可解释、可测试）；规则不确定时用弱模型 JSON 分类兜底；
多轮指代消解基于会话短期记忆（last_book）做 Query 改写。
"""
import json
import re
from dataclasses import dataclass, field

from ..generation.prompts import INTENT_SYSTEM
from ..modelhub.gateway import get_gateway
from ..obs.logging import get_logger, log_fields

logger = get_logger("askhanvon.conversation")

INTENTS = ("qa", "recommend", "search", "compare", "library", "purchase", "chitchat")

_BOOK_RE = re.compile(r"《(.+?)》")

_RULES = [
    ("compare", re.compile(r"比较|对比|哪个好|区别|相差|VS")),
    ("recommend", re.compile(r"推荐|书单|看什么|读什么|猜你喜欢|类似|同类|还有什么书|介绍几本")),
    ("library", re.compile(r"我的书架|书架|藏书|我的收藏|收藏的")),
    ("purchase", re.compile(r"买|下单|购买|订阅|价格|多少钱|怎么购")),
    ("search", re.compile(r"^搜|搜索|查找|找一下|帮我找|有没有.*书")),
    ("chitchat", re.compile(r"^(你好|您好|hi|hello|嗨|你是谁|谢谢|再见|拜拜|早上好|晚上好)")),
    ("qa", re.compile(r"讲了什么|什么内容|主要内容|剧情|情节|作者|谁写的|简介|怎么样|好不好|为什么|如何|是什么|读后感|适合")),
]

_DEICTIC_RE = re.compile(r"(它|这本书|那本|该书|这书|里面|其中|主角|主人公|书里|文中)")


@dataclass
class IntentResult:
    intent: str = "qa"
    book_title: str = ""
    rewritten: str = ""
    confidence: float = 0.5
    source: str = "rule"  # rule | llm | default
    slots: dict = field(default_factory=dict)


def extract_book_title(message: str) -> str:
    m = _BOOK_RE.search(message or "")
    return m.group(1) if m else ""


def route_intent(message: str, store=None, session_id: str = "",
                 use_llm: bool = True) -> IntentResult:
    msg = (message or "").strip()
    if not msg:
        return IntentResult(intent="chitchat", rewritten="", confidence=1.0, source="rule")

    book_title = extract_book_title(msg)
    last_book = store.last_book(session_id) if (store and session_id) else ""

    # 规则路由（书名优先级最高的信号：有书名的默认 qa/compare/搜索）
    for intent, regex in _RULES:
        if regex.search(msg):
            result = IntentResult(
                intent=intent, book_title=book_title, rewritten=msg, confidence=0.8,
                source="rule",
            )
            return _rewrite(result, msg, last_book)

    if book_title:
        # 有明确书名但无动词信号：问书的内容
        return _rewrite(
            IntentResult(intent="qa", book_title=book_title, rewritten=msg, confidence=0.7),
            msg, last_book,
        )

    # 弱模型分类兜底（规则未命中时）
    if use_llm:
        result = _llm_classify(msg)
        if result is not None:
            return _rewrite(result, msg, last_book)

    # 默认走 RAG 问答（宁可检索不可瞎聊）
    return _rewrite(
        IntentResult(intent="qa", book_title=book_title, rewritten=msg, confidence=0.4,
                     source="default"),
        msg, last_book,
    )


def _rewrite(result: IntentResult, msg: str, last_book: str) -> IntentResult:
    """多轮改写：指代词 + 会话记忆中的上一本书 → 扩写查询（便于检索）。"""
    rewritten = msg
    if result.intent == "qa" and not result.book_title and last_book and _DEICTIC_RE.search(msg):
        rewritten = msg + "（关于《" + last_book + "》）"
        result.book_title = last_book
        result.slots["rewritten_from_memory"] = True
    result.rewritten = rewritten
    return result


def _llm_classify(msg: str):
    gw = get_gateway()
    if not gw.llm_ready():
        return None
    try:
        res = gw.chat(
            [
                {"role": "system", "content": INTENT_SYSTEM},
                {"role": "user", "content": msg[:200]},
            ],
            tier="weak", scene="intent", max_tokens=120, temperature=0.1,
        )
        text = res.text.strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(text[start : end + 1])
        intent = data.get("intent")
        if intent not in INTENTS:
            return None
        title = (data.get("book_title") or "").strip()
        m = _BOOK_RE.search(title) or _BOOK_RE.search(msg)
        return IntentResult(
            intent=intent, book_title=(title.strip("《》") or (m.group(1) if m else "")),
            rewritten=msg, confidence=0.65, source="llm",
        )
    except Exception as e:  # noqa: BLE001 — 意图兜底失败不影响主链路
        log_fields(logger, 30, "intent.llm_error", error=str(e)[:120])
        return None
