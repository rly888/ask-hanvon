"""工具注册表：注册 / 校验 / RBAC / 注入扫描 / 限流 / 审计 / 降级 的统一入口。

Agent 与外部 MCP 客户端都只能通过 invoke() 使用工具——这是工具级安全
（§3.4）与审计（§3.2）的强制通道。
"""
import time

from ..db import dumps, get_db
from ..obs.logging import get_logger, log_fields
from ..obs.metrics import metrics
from ..security.injection import scan
from ..security.rbac import can_use_tool
from .schema import DEGRADE_MESSAGE, ToolContext, ToolResult, ToolSchema

logger = get_logger("askhanvon.tools")

_TYPE_CHECK = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
}


def validate_args(input_schema: dict, args: dict) -> tuple:
    """极简 JSON-Schema 校验（type/required/enum，工具契约够用）。"""
    props = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    for r in required:
        if r not in args or args[r] in (None, ""):
            return False, "缺少必填参数: " + r
    for k, v in (args or {}).items():
        spec = props.get(k)
        if not spec:
            continue  # 未声明的参数忽略
        t = spec.get("type", "string")
        check = _TYPE_CHECK.get(t)
        if check and not check(v):
            return False, "参数 " + k + " 类型应为 " + t
        if "enum" in spec and v not in spec["enum"]:
            return False, "参数 " + k + " 取值不合法"
    return True, ""


class ToolRegistry:
    def __init__(self):
        self._schemas: dict = {}
        self._impls: dict = {}

    def register(self, schema: ToolSchema, impl) -> None:
        self._schemas[schema.name] = schema
        self._impls[schema.name] = impl

    def get(self, name: str):
        return self._schemas.get(name)

    def names(self) -> list:
        return list(self._schemas.keys())

    def manifest(self) -> list:
        return [s.to_mcp() for s in self._schemas.values()]

    def invoke(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        db = get_db()
        schema = self._schemas.get(name)
        t0 = time.perf_counter()

        def finish(result: ToolResult, decision: str, reason: str = "") -> ToolResult:
            result.meta.setdefault("tool", name)
            result.meta.setdefault("latency_ms",
                                   round((time.perf_counter() - t0) * 1000, 1))
            db.audit_log(
                user_id=ctx.user_id, action="tool_invoke", tool=name,
                params=dumps(args or {}), decision=decision, reason=reason,
            )
            metrics.inc("tool_calls_total",
                        {"tool": name, "status": "ok" if result.ok else "error"})
            return result

        if not schema:
            return finish(ToolResult(ok=False, error="未知工具: " + name), "deny",
                          "unknown_tool")

        # 1) RBAC
        if not can_use_tool(name, ctx.role):
            return finish(
                ToolResult(ok=False, error="无权限使用该工具，请先登录或升级账户。",
                           meta={"degraded": False}),
                "deny", "rbac",
            )

        # 2) 参数注入扫描（工具调用参数是注入攻击的主要入口）
        joined = " ".join(str(v) for v in (args or {}).values() if isinstance(v, str))
        inj = scan(joined)
        if inj["score"] >= 0.7:
            db.injection_hit(
                user_id=ctx.user_id, source="tool_args:" + name, snippet=joined[:180],
                patterns=",".join(h["label"] for h in inj["hits"]), score=inj["score"],
                blocked=1,
            )
            return finish(
                ToolResult(ok=False, error="参数包含被禁止的指令内容，已拒绝执行。",
                           meta={"degraded": False}),
                "deny", "injection",
            )

        # 3) 契约校验
        ok, err = validate_args(schema.input_schema, args or {})
        if not ok:
            return finish(ToolResult(ok=False, error=err), "deny", "bad_args")

        # 4) 执行（异常 → 降级话术）
        try:
            result = self._impls[name](ctx, **(args or {}))
            if not isinstance(result, ToolResult):
                result = ToolResult(ok=True, data=result or {})
        except Exception as e:  # noqa: BLE001 — 工具故障统一降级
            log_fields(logger, 40, "tool.error", tool=name, error=str(e)[:200])
            result = ToolResult(
                ok=False, error=str(e)[:200], data={"message": DEGRADE_MESSAGE},
                meta={"degraded": True},
            )
        return finish(result, "allow")


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        _register_all(_registry)
    return _registry


def _register_all(reg: ToolRegistry) -> None:
    from . import book_qa, book_search, compare, library, profile_tool, purchase, recommend

    book_search.register(reg)
    book_qa.register(reg)
    recommend.register(reg)
    compare.register(reg)
    library.register(reg)
    profile_tool.register(reg)
    purchase.register(reg)
