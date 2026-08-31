"""API 网关层（公开端点）：认证 / 会话 / 问答(SSE 流式) / 图书 / 搜索 / 推荐 /
埋点 / MCP 开放端点 / 指标与健康检查。"""
import json
import queue
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agent.loop import get_agent
from ..config import settings
from ..db import dumps, get_db, loads, new_id
from ..events import collector
from ..modelhub import quota as quota_mod
from ..recommend.engine import get_rec_engine
from ..security.antifraud import (
    ip_blacklisted,
    login_failure_record,
    login_failure_reset,
    login_locked,
    rate_limit,
)
from ..security.injection import check_user_message
from ..tools.registry import get_registry
from ..tools.schema import ToolContext
from .auth import (
    create_token,
    hash_password,
    issue_refresh_token,
    password_policy,
    revoke_refresh_token,
    rotate_refresh_token,
    user_from_token,
    verify_password,
)

router = APIRouter()

_LOGIN_LOCK_LIMIT = 5
_LOGIN_LOCK_WINDOW = 900.0  # 15 分钟内失败 5 次锁定


def current_user(request: Request):
    """从 Authorization 头解析用户（可匿名）。"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return user_from_token(auth[7:])
    return None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ============ 认证 ============
class RegisterReq(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    nickname: str = ""


class LoginReq(BaseModel):
    username: str
    password: str


@router.post("/api/auth/register")
def register(req: RegisterReq):
    ok, msg = password_policy(req.password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    db = get_db()
    if db.get_user_by_username(req.username):
        raise HTTPException(status_code=409, detail="用户名已存在")
    uid = db.create_user(req.username, hash_password(req.password), "user",
                         req.nickname or req.username)
    return {"user_id": uid, "username": req.username,
            "token": create_token(uid, req.username, "user"),
            "refresh_token": issue_refresh_token(uid)}


@router.post("/api/auth/login")
def login(req: LoginReq):
    # 登录失败锁定（P0-6）：15 分钟内失败 5 次 → 锁定
    locked, retry = login_locked(req.username, _LOGIN_LOCK_LIMIT, _LOGIN_LOCK_WINDOW)
    if locked:
        raise HTTPException(status_code=429,
                            detail="失败次数过多，账号已临时锁定，请 " + str(retry) + " 秒后再试")
    db = get_db()
    user = db.get_user_by_username(req.username)
    if not user or not verify_password(req.password, user["password_hash"]):
        login_failure_record(req.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    login_failure_reset(req.username)
    return {
        "user_id": user["id"], "username": user["username"], "role": user["role"],
        "nickname": user["nickname"],
        "token": create_token(user["id"], user["username"], user["role"]),
        "refresh_token": issue_refresh_token(user["id"]),
    }


class RefreshReq(BaseModel):
    refresh_token: str


@router.post("/api/auth/refresh")
def refresh(req: RefreshReq):
    """轮换刷新令牌：旧 refresh 作废，签发新 access + refresh（P0-6）。"""
    user_id, new_refresh = rotate_refresh_token(req.refresh_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="刷新令牌无效或已过期")
    user = get_db().get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return {
        "token": create_token(user["id"], user["username"], user["role"]),
        "refresh_token": new_refresh,
    }


@router.post("/api/auth/logout")
def logout(req: RefreshReq):
    """登出：吊销 refresh token（access token 自然过期）。"""
    revoke_refresh_token(req.refresh_token)
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: dict = Depends(require_user)):
    return user


# ============ 对话（SSE 流式） ============
class ChatReq(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = ""
    stream: bool = True


@router.post("/api/chat")
def chat(req: ChatReq, request: Request):
    user = current_user(request)
    user_id = user["user_id"] if user else None
    role = user["role"] if user else "anonymous"

    client_ip = request.client.host if request.client else "local"
    if ip_blacklisted(client_ip):
        raise HTTPException(status_code=403, detail="IP 已被封禁")
    ok, retry = rate_limit("chat", user_id or client_ip,
                           settings.rate_limit_chat_per_min)
    if not ok:
        raise HTTPException(status_code=429, detail="请求过于频繁，请 " + str(retry) + " 秒后再试")

    if not req.stream:
        result = get_agent().handle(req.message, user_id=user_id, role=role,
                                    session_id=req.session_id)
        return result

    def gen():
        q: queue.Queue = queue.Queue()

        def cb(t, p=None):
            # type 字段放最后覆盖，保证事件类型不被 payload 内的 type 顶掉
            q.put({**(p or {}), "type": t})

        def run():
            try:
                get_agent().handle_stream(req.message, user_id=user_id, role=role,
                                          session_id=req.session_id, on_event=cb)
            except Exception as e:  # noqa: BLE001 — 兜底降级话术
                q.put({"type": "delta", "text": "我暂时查不到，请稍后再试。"})
                q.put({"type": "error", "error": str(e)[:150]})
            finally:
                q.put({"type": "__end__"})

        threading.Thread(target=run, daemon=True).start()
        while True:
            try:
                item = q.get(timeout=180)
            except queue.Empty:
                yield "data: " + json.dumps({"type": "error", "error": "响应超时"}) + "\n\n"
                break
            if item.get("type") == "__end__":
                yield "data: " + json.dumps({"type": "end"}) + "\n\n"
                break
            yield "data: " + json.dumps(item, ensure_ascii=False, default=str) + "\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============ 会话 ============
@router.get("/api/sessions")
def list_sessions(user: dict = Depends(require_user)):
    return {"sessions": get_db().list_sessions(user["user_id"])}


@router.get("/api/sessions/{session_id}/messages")
def session_messages(session_id: str, user: dict = Depends(require_user)):
    sess = get_db().get_session(session_id)
    if not sess or sess["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = get_db().get_messages(session_id, 200)
    for m in msgs:
        m["meta"] = loads(m.get("meta"), {}) or {}
    return {"messages": msgs}


@router.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, user: dict = Depends(require_user)):
    sess = get_db().get_session(session_id)
    if not sess or sess["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="会话不存在")
    get_db().delete_session(session_id)
    return {"ok": True}


# ============ 图书 / 阅读 ============
@router.get("/api/books")
def list_books(query: str = "", category: str = "", page: int = 1, size: int = 24):
    items = get_db().list_books(keyword=query, category=category,
                                limit=size, offset=(page - 1) * size)
    cats = sorted({b.get("category") for b in get_db().all_books() if b.get("category")})
    return {"books": items, "categories": cats, "page": page}


@router.get("/api/books/{book_id}")
def book_detail(book_id: str):
    book = get_db().get_book(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="图书不存在")
    chapters = get_db().chapters_of_book(book_id)
    return {"book": book, "chapters": chapters}


@router.get("/api/books/{book_id}/content")
def book_content(book_id: str, chapter_no: int):
    """阅读视图：合并章节 chunk 成正文段落（电纸书/网页阅读场景）。"""
    chunks = get_db().get_chunks_by_book_chapter(book_id, chapter_no)
    if not chunks:
        raise HTTPException(status_code=404, detail="章节不存在")
    book = get_db().get_book(book_id)
    paragraphs = []
    for c in chunks:
        for p in c["text"].split("\n"):
            p = p.strip()
            if p and p not in paragraphs:
                paragraphs.append(p)
    return {"book": book, "chapter_no": chapter_no,
            "chapter_title": chunks[0]["chapter_title"], "vol": chunks[0]["vol"],
            "page_start": chunks[0]["page_start"], "page_end": chunks[-1]["page_end"],
            "paragraphs": paragraphs}


# ============ 搜索 / 推荐 ============
@router.get("/api/search")
def search(q: str, top_k: int = 8):
    reg = get_registry()
    res = reg.invoke("book_search", {"query": q, "top_k": top_k},
                     ToolContext(user_id=None, role="anonymous"))
    return res.to_dict()


@router.get("/api/recommend")
def recommend(request: Request, scene: str = "homepage", top_k: int = 6):
    user = current_user(request)
    user_id = user["user_id"] if user else None
    items = get_rec_engine().recommend(user_id, scene=scene, top_k=top_k)
    return {"items": items}


# ============ 埋点 ============
class EventReq(BaseModel):
    event_type: str
    book_id: str = ""
    query: str = ""
    session_id: str = ""
    props: dict = {}


@router.post("/api/events")
def track_events(req: list[EventReq] | EventReq, request: Request):
    user = current_user(request)
    user_id = user["user_id"] if user else None
    items = req if isinstance(req, list) else [req]
    accepted = 0
    errors = []
    for e in items:
        try:
            collector.emit(
                {
                    "event_type": e.event_type,
                    "user_id": user_id,
                    "session_id": e.session_id,
                    "book_id": e.book_id,
                    "query": e.query,
                    "props": e.props,
                }
            )
            accepted += 1
        except ValueError as ex:
            errors.append(str(ex))
    return {"accepted": accepted, "errors": errors}


# ============ 工具直调 & MCP 开放端点（§3.5 Phase 3 项） ============
class ToolCallReq(BaseModel):
    arguments: dict = {}


@router.post("/api/tools/{name}")
def invoke_tool(name: str, req: ToolCallReq, request: Request):
    user = current_user(request)
    ctx = ToolContext(
        user_id=user["user_id"] if user else None,
        role=user["role"] if user else "anonymous",
    )
    return get_registry().invoke(name, req.arguments, ctx).to_dict()


@router.get("/api/mcp/tools/list")
def mcp_tools_list(request: Request):
    user = current_user(request)
    manifest = get_registry().manifest()
    if not user or user["role"] != "admin":
        # 匿名/普通用户只见可用工具（权限边界）
        manifest = [m for m in manifest
                    if m["annotations"]["required_role"] == "anonymous"]
    return {"tools": manifest, "protocol": "mcp-like/1.0"}


@router.post("/api/mcp/tools/call")
def mcp_tools_call(req: dict, request: Request):
    user = current_user(request)
    ctx = ToolContext(
        user_id=user["user_id"] if user else None,
        role=user["role"] if user else "anonymous",
    )
    name = (req or {}).get("name", "")
    arguments = (req or {}).get("arguments", {})
    return get_registry().invoke(name, arguments, ctx).to_dict()


# ============ 配额 / 指标 / 健康 ============
@router.get("/api/quota")
def my_quota(request: Request):
    user = current_user(request)
    return quota_mod.quota_usage(user["user_id"] if user else None)


@router.get("/api/metrics")
def prometheus_metrics():
    from ..obs.metrics import metrics as m

    return StreamingResponse(iter([m.render()]), media_type="text/plain")


@router.get("/api/health")
def health():
    from ..modelhub.gateway import get_gateway

    gw = get_gateway()
    db = get_db()
    n_chunks, _ = db.count_chunks()
    return {
        "status": "ok",
        "llm_configured": gw.llm_ready(),
        "mode": "llm" if gw.llm_ready() else "offline-fallback",
        "books": len(db.all_books()),
        "chunks": n_chunks,
        "version": "1.0.0",
    }
