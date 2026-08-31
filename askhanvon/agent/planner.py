"""Planner：意图 → 工具调用计划（轻量自研编排起步，复杂 DAG 再上框架）。"""
import re
from dataclasses import dataclass, field

from ..conversation.intent import IntentResult
from .schema import Plan, PlanStep

_BOOKS_RE = re.compile(r"《(.+?)》")


class Planner:
    def plan(self, intent: IntentResult, user_id) -> Plan:
        steps: list = []
        if intent.intent == "qa":
            steps.append(
                PlanStep(
                    tool="book_qa",
                    args={"query": intent.rewritten,
                          "book_title": intent.book_title or ""},
                    reason="内容问答 → RAG 检索增强回答",
                )
            )
        elif intent.intent == "recommend":
            args = {"scene": "homepage", "top_k": 6}
            if intent.book_title:
                args["book_title"] = intent.book_title
            steps.append(
                PlanStep(tool="recommend_books", args=args, reason="推荐诉求 → 推荐引擎")
            )
        elif intent.intent == "search":
            steps.append(
                PlanStep(
                    tool="book_search",
                    args={"query": intent.rewritten or "书"},
                    reason="搜索诉求 → 书籍搜索工具",
                )
            )
        elif intent.intent == "compare":
            titles = _BOOKS_RE.findall(intent.rewritten or "")
            if len(titles) < 2:
                titles = (intent.rewritten or "").replace("比较", "").replace("对比", "").split("和")
                titles = [t.strip("《》?？ 。") for t in titles if t.strip("《》?？ 。")]
            steps.append(
                PlanStep(
                    tool="compare_books",
                    args={"titles": titles[:3]},
                    reason="比较诉求 → 图书比较工具",
                )
            )
        elif intent.intent == "library":
            action = "list"
            msg = intent.rewritten or ""
            if re.search(r"收藏|加入书架|想看", msg):
                action = "collect"
            elif re.search(r"取消收藏|移出|移除", msg):
                action = "uncollect"
            elif re.search(r"历史|最近读|读过", msg):
                action = "history"
            args = {"action": action}
            if action in ("collect", "uncollect"):
                title = intent.book_title or _BOOKS_RE.search(msg)
                args["book_title"] = title.group(1) if hasattr(title, "group") else (title or "")
            steps.append(
                PlanStep(tool="my_library", args=args, reason="书架操作 → 藏书库工具")
            )
        elif intent.intent == "purchase":
            if intent.book_title:
                steps.append(
                    PlanStep(
                        tool="purchase_init",
                        args={"book_title": intent.book_title},
                        reason="购买诉求 → 下单工具（高危，需二次确认）",
                    )
                )
            else:
                steps.append(
                    PlanStep(
                        tool="book_search",
                        args={"query": intent.rewritten},
                        reason="购买意向但未指明书 → 先搜索再引导",
                    )
                )
        # chitchat / unknown → 无工具步骤，Synthesizer 直接生成
        return Plan(intent=intent.intent, steps=steps)
