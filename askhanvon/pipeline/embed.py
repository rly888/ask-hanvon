"""embedding 生成：批量经模型网关（API 或本地向量），结果以 BLOB 落库。"""
import numpy as np

from ..config import settings
from ..db import get_db
from ..modelhub.gateway import get_gateway


def _to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def embed_chunks(rows: list, user_id=None, batch: int = 32):
    """rows: 含 id/text 的 chunk dict 列表。逐批生成并写回 embedding。

    返回 (done, effective_model)：effective_model 为本次向量真实来源标识
    （任一批次降级到本地向量即整体按本地来源打标，防新旧向量混算）。
    """
    gw = get_gateway()
    done = 0
    used_local = False
    for i in range(0, len(rows), batch):
        part = rows[i : i + batch]
        vecs = gw.embed([r["text"] for r in part], user_id=user_id)
        used_local = used_local or gw.embed_last_source == "local"
        for r, v in zip(part, vecs):
            get_db().update_chunk_embedding(r["id"], _to_blob(v))
            done += 1
    eff = gw.effective_embed_model_name()
    if used_local:
        eff = "local-hash-embed-" + str(settings.embed_dim)
    return done, eff
