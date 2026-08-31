"""trace_id 上下文与轻量 span。"""
import contextvars
import time
import uuid
from contextlib import contextmanager

_trace_id: contextvars.ContextVar = contextvars.ContextVar("trace_id", default="")
_span_root: contextvars.ContextVar = contextvars.ContextVar("span_root", default="")


def new_trace_id() -> str:
    tid = uuid.uuid4().hex[:16]
    _trace_id.set(tid)
    return tid


def set_trace_id(tid: str) -> None:
    _trace_id.set(tid or "")


def get_trace_id() -> str:
    return _trace_id.get()


@contextmanager
def span(name: str, **fields):
    """记录一个耗时区间（写入结构化日志 + 指标）。"""
    from .logging import get_logger, log_fields
    from .metrics import metrics

    logger = get_logger("askhanvon.span")
    t0 = time.perf_counter()
    try:
        yield
        dur = (time.perf_counter() - t0) * 1000
        log_fields(logger, 20 - 10 + 10, "span.done", span=name, dur_ms=round(dur, 1), **fields)
        metrics.observe(f"span_{name}_ms", dur)
    except Exception as e:
        dur = (time.perf_counter() - t0) * 1000
        log_fields(logger, 40, "span.error", span=name, dur_ms=round(dur, 1), error=str(e), **fields)
        metrics.inc("span_errors_total", {"span": name})
        raise
