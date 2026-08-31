"""Executor：按计划执行工具（注册表统一通道：RBAC/注入/审计/降级都在里面）。

多步骤时并行执行（P2-2）：注册表与 DB 层均线程安全（线程本地连接 + 写锁），
结果按计划顺序返回。
"""
from concurrent.futures import ThreadPoolExecutor
import time

from ..obs.logging import get_logger, log_fields
from ..tools.registry import get_registry
from ..tools.schema import ToolContext, ToolResult

logger = get_logger("askhanvon.agent")


class Executor:
    def run(self, plan, ctx: ToolContext) -> list:
        registry = get_registry()
        if len(plan.steps) <= 1:
            return [self._run_one(registry, step, ctx) for step in plan.steps]
        with ThreadPoolExecutor(max_workers=min(4, len(plan.steps))) as pool:
            return list(pool.map(lambda s: self._run_one(registry, s, ctx), plan.steps))

    def _run_one(self, registry, step, ctx: ToolContext) -> ToolResult:
        t0 = time.perf_counter()
        result = registry.invoke(step.tool, step.args, ctx)
        latency = (time.perf_counter() - t0) * 1000
        result.meta["reason"] = step.reason
        result.meta["latency_ms"] = round(latency, 1)
        log_fields(
            logger, 20 if result.ok else 30, "agent.tool_done",
            tool=step.tool, ok=result.ok, latency_ms=result.meta["latency_ms"],
        )
        return result
