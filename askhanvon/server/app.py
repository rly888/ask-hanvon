"""应用工厂：网关中间件（追踪/限流/指标）+ 路由 + 静态前端 + 事件消费线程。"""
import os
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .. import APP_NAME, __version__
from ..config import settings
from ..events import collector
from ..obs.metrics import metrics
from ..obs.tracing import get_trace_id, new_trace_id
from ..security.antifraud import ip_blacklisted, rate_limit
from . import routes_admin, routes_public

_SKIP_METRIC = {"/api/health", "/api/metrics", "/favicon.ico"}


class GatewayMiddleware(BaseHTTPMiddleware):
    """接入层网关职责的单体实现：IP 黑名单 / API 限流 / trace / 指标。"""

    async def dispatch(self, request, call_next):
        new_trace_id()
        path = request.url.path
        ip = request.client.host if request.client else "local"
        if ip_blacklisted(ip):
            return JSONResponse({"detail": "IP 已被封禁"}, status_code=403)
        if path.startswith("/api/") and path not in _SKIP_METRIC:
            ok, retry = rate_limit("ip", ip, settings.rate_limit_api_per_min)
            if not ok:
                return JSONResponse(
                    {"detail": "请求过于频繁，请 " + str(retry) + " 秒后再试"}, status_code=429
                )
        t0 = time.perf_counter()
        response = await call_next(request)
        if path not in _SKIP_METRIC:
            metrics.inc("http_requests_total",
                        {"method": request.method, "status": str(response.status_code)})
            metrics.observe("http_latency_ms", (time.perf_counter() - t0) * 1000)
        response.headers["X-Trace-Id"] = get_trace_id()
        return response


def create_app() -> FastAPI:
    app = FastAPI(title=APP_NAME, version=__version__)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )
    app.add_middleware(GatewayMiddleware)
    app.include_router(routes_public.router)
    app.include_router(routes_admin.router)

    web_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"
    )
    if os.path.isdir(web_dir):
        app.mount("/web", StaticFiles(directory=web_dir, html=True), name="web")

    @app.get("/")
    def root():
        return RedirectResponse(url="/web/index.html")

    @app.on_event("startup")
    def _startup():
        collector.start_consumer()
        if settings.scheduler_enabled:
            from ..ops.scheduler import scheduler as _sched

            _sched.start()

    @app.on_event("shutdown")
    def _shutdown():
        collector.stop_consumer()
        if settings.scheduler_enabled:
            from ..ops.scheduler import scheduler as _sched

            _sched.stop()

    return app


app = create_app()
