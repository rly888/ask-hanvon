"""管理后台 API：策略 / 实验 / Campaign / 评测门禁 / 审计 / 内容管理 / 离线任务。

上传安全：样书上传不落盘，内容直接在内存解析入库；格式类别由服务端判定
（返回固定字面量），用户文件名不参与任何路径或 IO 操作。
"""
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from ..db import get_db, loads
from ..evals.runner import render_report, run_all_gates, run_suite
from ..modelhub import quota as quota_mod
from ..ops.ab import ab_service
from ..ops.campaigns import SLOT_HOMEPAGE, campaigns
from ..ops.strategies import strategies
from .routes_public import require_admin

router = APIRouter(prefix="/api/admin")


def _llm_ready() -> bool:
    from ..modelhub.gateway import get_gateway

    return get_gateway().llm_ready()


# ============ 策略配置 ============
@router.get("/strategies")
def get_strategies(user: dict = Depends(require_admin)):
    return {"strategies": strategies.all_effective(),
            "overrides": {r["key"]: loads(r["value"]) for r in get_db().strategy_all()}}


@router.put("/strategies/{key}")
def put_strategy(key: str, body: dict, user: dict = Depends(require_admin)):
    if "value" not in body:
        raise HTTPException(status_code=400, detail="缺少 value")
    strategies.set(key, body["value"], by=user["username"])
    return {"ok": True, "key": key, "value": strategies.get(key)}


# ============ Campaign / 优先图书 ============
@router.get("/campaigns")
def list_campaigns(user: dict = Depends(require_admin)):
    return {"campaigns": campaigns.list(), "priority": get_db().priority_list()}


class CampaignReq(BaseModel):
    name: str
    slot: str = SLOT_HOMEPAGE
    book_ids: list = []
    weight: float = 1.0
    start_at: str = ""
    end_at: str = ""
    enabled: bool = True


@router.post("/campaigns")
def create_campaign(req: CampaignReq, user: dict = Depends(require_admin)):
    cid = campaigns.create(req.name, req.slot, req.book_ids, req.weight,
                           req.start_at, req.end_at, req.enabled)
    return {"id": cid}


@router.delete("/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int, user: dict = Depends(require_admin)):
    campaigns.delete(campaign_id)
    return {"ok": True}


class PriorityReq(BaseModel):
    book_id: str
    slot: str = SLOT_HOMEPAGE
    weight: float = 2.0
    reason: str = "编辑推荐"


@router.post("/priority")
def set_priority(req: PriorityReq, user: dict = Depends(require_admin)):
    campaigns.set_priority(req.slot, req.book_id, req.weight, req.reason)
    return {"ok": True}


@router.delete("/priority/{slot}/{book_id}")
def delete_priority(slot: str, book_id: str, user: dict = Depends(require_admin)):
    campaigns.delete_priority(slot, book_id)
    return {"ok": True}


# ============ A/B 实验 ============
class ExpReq(BaseModel):
    name: str
    description: str = ""
    variants: list  # [{"key":"A","weight":50,"params":{"rec.use_trained":false}}, ...]
    traffic_pct: float = 100.0


@router.get("/experiments")
def list_experiments(user: dict = Depends(require_admin)):
    return {"experiments": get_db().exp_list()}


@router.post("/experiments")
def create_experiment(req: ExpReq, user: dict = Depends(require_admin)):
    try:
        eid = ab_service.create(req.name, req.description, req.variants, req.traffic_pct)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": eid}


@router.get("/experiments/{name}/metrics")
def experiment_metrics(name: str, hours: int = 72, user: dict = Depends(require_admin)):
    return ab_service.metrics(name, hours=hours)


@router.post("/experiments/{name}/promote")
def promote_experiment(name: str, body: dict, user: dict = Depends(require_admin)):
    winner = (body or {}).get("winner", "A")
    ab_service.promote(name, winner)
    return {"ok": True, "winner": winner}


# ============ 评测门禁 ============
class EvalReq(BaseModel):
    suite: str = "all"
    limit: int | None = None


@router.post("/evals/run")
def run_evals(req: EvalReq, user: dict = Depends(require_admin)):
    if req.suite == "all":
        results = run_all_gates()
        return {**results, "report": render_report(results)}
    try:
        report = run_suite(req.suite, limit=req.limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return report


@router.get("/evals/runs")
def eval_runs(suite: str = "", limit: int = 20, user: dict = Depends(require_admin)):
    return {"runs": get_db().eval_runs_list(suite=suite, limit=limit)}


# ============ Prompt 版本管理（P1-5）============
@router.get("/prompts/{name}")
def prompt_get(name: str, user: dict = Depends(require_admin)):
    from ..generation.prompts import QA_SYSTEM
    from ..ops.prompts import prompt_service

    version, template = prompt_service.get(name, QA_SYSTEM)
    return {"name": name, "active_version": version,
            "active_template": template if version > 0 else None,
            "default_template": template if version == 0 else None,
            "history": prompt_service.history(name)}


class PromptReq(BaseModel):
    template: str


@router.put("/prompts/{name}")
def prompt_set(name: str, req: PromptReq, user: dict = Depends(require_admin)):
    from ..ops.prompts import prompt_service

    try:
        version = prompt_service.set(name, req.template, by=user["username"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "name": name, "version": version}


# ============ 审计 / 安全 / 模型治理 ============
@router.get("/audit")
def audit_logs(limit: int = 100, decision: str = "", user: dict = Depends(require_admin)):
    return {"logs": get_db().audit_list(limit=limit, decision=decision)}


@router.get("/injections")
def injection_hits(limit: int = 100, user: dict = Depends(require_admin)):
    return {"hits": get_db().injection_list(limit=limit)}


@router.get("/model_calls")
def model_calls(limit: int = 100, since_hours: int = 24,
                user: dict = Depends(require_admin)):
    since = (datetime.now() - timedelta(hours=since_hours)).isoformat(timespec="seconds")
    db = get_db()
    return {
        "by_model": db.mh_stats_by_model(since),
        "recent": db.mh_recent_calls(limit=limit),
        "quota_anonymous": quota_mod.quota_usage(None),
    }


# ============ 内容管理 ============
def _detect_ext(filename: str) -> str:
    """识别上传格式类别；只返回固定字面量，不含任何用户输入成分。"""
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith(".epub"):
        return "epub"
    if lower.endswith(".md") or lower.endswith(".markdown"):
        return "md"
    if lower.endswith(".txt"):
        return "txt"
    raise HTTPException(status_code=400, detail="仅支持 .md/.txt/.epub/.pdf")


@router.post("/books/upload")
async def upload_book(file: UploadFile, user: dict = Depends(require_admin)):
    """上传样书：内存解析入库（不落盘），绕开任何用户输入相关的路径构造。"""
    ext = _detect_ext(file.filename)
    # Content-Length 预检（无长度则分块读取时边读边限）——先于全量 read，
    # 防止超大请求先占满内存再判大小（DoS 面）
    cl = None
    if file.headers:
        raw = file.headers.get("content-length")
        if raw and raw.isdigit():
            cl = int(raw)
    if cl is not None and cl > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大（>50MB）")
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大（>50MB）")
    from ..pipeline.index_build import ingest_stream

    report = ingest_stream(ext, data, source_name=(file.filename or "")[:120])
    from ..rag.retriever import get_retriever

    get_retriever().invalidate()
    return {"ok": True, "report": report}


@router.post("/books/reindex")
def reindex_books(body: dict, user: dict = Depends(require_admin)):
    """从 data 目录重建样书索引。"""
    reindex = bool((body or {}).get("reindex"))
    from ..config import settings
    from ..pipeline.index_build import ingest_dir

    src = settings.data_dir
    reports = []
    books_dir = os.path.join(src, "books")
    if os.path.isdir(books_dir):
        reports = ingest_dir(books_dir, reindex=reindex)
    from ..rag.retriever import get_retriever

    get_retriever().invalidate()
    return {"ok": True, "reports": reports}


@router.get("/books")
def admin_books(user: dict = Depends(require_admin)):
    return {"books": get_db().all_books()}


# ============ 离线任务 ============
@router.post("/offline/features")
def recompute_features(user: dict = Depends(require_admin)):
    from ..offline.features import recompute_features as rf

    return rf()


@router.post("/offline/train")
def train_models(user: dict = Depends(require_admin)):
    from ..offline.features import recompute_features as rf

    rf()
    from ..offline.train import train_all

    return train_all()


@router.post("/offline/candidates")
def precompute(user: dict = Depends(require_admin)):
    from ..offline.train import precompute_candidates

    return precompute_candidates()


# ============ 运营总览 ============
@router.get("/overview")
def overview(user: dict = Depends(require_admin)):
    db = get_db()
    n_chunks, _ = db.count_chunks()
    return {
        "books": len(db.all_books()),
        "chunks": n_chunks,
        "users": len(db.list_users()),
        "llm_configured": _llm_ready(),
        "last_eval_runs": db.eval_runs_list(limit=3),
    }
