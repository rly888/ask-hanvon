"""Prompt injection 检测（§3.4）。

覆盖两类入口：用户消息与检索到的书内容（资料区）。资料区命中时该片段被
标记为不可信并在上下文构建阶段剔除；用户输入命中超过阈值则整条拒绝。
"""
import re

from ..db import get_db
from ..obs.logging import get_logger, log_fields

logger = get_logger("askhanvon.security")

PATTERNS = [
    (re.compile(r"忽略(之前|上面|以上|前面|既往)?(的)?(所有|全部)?(指令|规则|提示|设定)"), 0.9,
     "ignore_instructions_zh"),
    (re.compile(r"(忽略|无视|忘记|丢弃|清除|清除).{0,8}(指令|规则|提示|设定|要求|约束)"), 0.85,
     "ignore_instructions_flex"),
    (re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts?|rules)",
                re.I), 0.9, "ignore_instructions_en"),
    (re.compile(r"(输出|打印|泄露|告诉我|show\s+me)(你的)?(系统提示|system\s*prompt|初始指令)",
                re.I), 0.8, "prompt_exfiltration"),
    (re.compile(r"(输出|打印|告诉我|给我)(你的)?(api\s*key|密钥|秘钥|口令|密码)", re.I), 0.85,
     "secret_exfiltration"),
    (re.compile(r"(开发者模式|developer\s*mode|DAN\s*模式|越狱|jailbreak)", re.I), 0.6,
     "jailbreak"),
    (re.compile(r"(直接|偷偷|立即|帮我)?(调用|执行|触发).{0,8}(购买|下单|支付).{0,12}(不要|无需|不用|跳过)(确认|询问|二次)"),
     0.95, "tool_abuse_purchase"),
    (re.compile(r"(你必须|你要|请)(立刻|马上|直接)?(下单|购买|支付|扣款)"), 0.7,
     "forced_purchase"),
    (re.compile(r"\{\{.*system.*\}\}|<\|.*im_start.*\|>", re.I), 0.7, "template_escape"),
]


def scan(text: str) -> dict:
    """返回 {score, hits:[{label, weight}]}；score = 命中权重和（截断到 1）。"""
    hits = []
    score = 0.0
    for regex, weight, label in PATTERNS:
        if regex.search(text or ""):
            hits.append({"label": label, "weight": weight})
            score += weight
    return {"score": min(1.0, score), "hits": hits}


def check_user_message(text: str, user_id=None, threshold: float = 0.7) -> dict:
    """用户消息安全检查。命中并超阈值：记审计并拒绝。"""
    result = scan(text)
    blocked = result["score"] >= threshold
    if result["hits"]:
        get_db().injection_hit(
            user_id=user_id,
            source="user_message",
            snippet=(text or "")[:180],
            patterns=",".join(h["label"] for h in result["hits"]),
            score=result["score"],
            blocked=1 if blocked else 0,
        )
        log_fields(
            logger, 30 if blocked else 20, "injection.scan",
            source="user_message", score=result["score"], blocked=blocked,
        )
    return {"blocked": blocked, **result}


def check_retrieved(text: str) -> dict:
    """检索片段安全检查：命中即标记不可信（不拦截整体请求）。"""
    result = scan(text)
    return {"untrusted": result["score"] >= 0.5, **result}
