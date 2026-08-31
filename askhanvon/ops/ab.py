"""A/B 实验服务（运营与策略中心）：确定性分桶、曝光记录、指标回流、赢家晋升。"""
import hashlib
from datetime import datetime, timedelta

from ..db import dumps, get_db, loads, now_iso


def _bucket(exp_name: str, user_id: int, salt: str = "v1") -> int:
    key = exp_name + ":" + str(user_id) + ":" + salt
    return int(hashlib.blake2b(key.encode(), digest_size=8).hexdigest(), 16) % 100


class ExperimentService:
    def create(self, name: str, description: str, variants: list, traffic_pct: float = 100.0,
               by: str = "admin") -> int:
        """variants: [{"key":"A","weight":50,"params":{...}}, ...] 权重和可为 100。"""
        total = sum(v.get("weight", 0) for v in variants)
        if not variants or total <= 0:
            raise ValueError("实验变体不合法")
        return get_db().exp_create(name, description, traffic_pct, dumps(variants))

    def assign(self, name: str, user_id) -> dict:
        """返回 {"exp": name, "variant": key, "params": {...}}；未命中流量/未登录 → 默认 A。"""
        exp = get_db().exp_get_by_name(name)
        if not exp or exp["status"] != "running" or not user_id:
            variants = loads(exp["variants"], []) if exp else []
            base = variants[0] if variants else {"key": "A", "params": {}}
            return {"exp": name, "variant": base.get("key", "A"),
                    "params": base.get("params", {})}
        existing = get_db().exp_get_assignment(exp["id"], user_id)
        if existing:
            # existing 为变体 key 字符串（如 "A"）
            variant = self._variant_by_key(loads(exp["variants"], []), existing)
            return {"exp": name, "variant": variant.get("key", "A"),
                    "params": variant.get("params", {})}
        if _bucket(name, int(user_id)) >= exp["traffic_pct"]:
            variant = loads(exp["variants"], [])[0]
            params = variant.get("params", {})
        else:
            variant = self._pick_by_weight(loads(exp["variants"], []), name, int(user_id))
            params = variant.get("params", {})
            get_db().exp_assign(exp["id"], user_id, variant.get("key", "A"))
        return {"exp": name, "variant": variant.get("key", "A"), "params": params}

    def _variant_by_key(self, variants: list, key: str) -> dict:
        for v in variants:
            if v.get("key") == key:
                return v
        return variants[0] if variants else {}

    def _pick_by_weight(self, variants: list, name: str, user_id: int) -> dict:
        total = sum(v.get("weight", 0) for v in variants) or 1
        point = _bucket(name, user_id, salt="weight") % total
        acc = 0
        for v in variants:
            acc += v.get("weight", 0)
            if point < acc:
                return v
        return variants[-1]

    def metrics(self, name: str, hours: int = 72) -> dict:
        """按变体聚合曝光/点击/CTR（效果回流）。"""
        db = get_db()
        exp = db.exp_get_by_name(name)
        if not exp:
            return {}
        since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = db.events_query(event_type="impression", since=since, limit=5000)
        clicks = db.events_query(event_type="click", since=since, limit=5000)
        per: dict = {}
        for r in rows:
            props = loads(r["props"], {}) or {}
            v = props.get("variant") or "A"
            per.setdefault(v, {"exposures": 0, "clicks": 0})
            per[v]["exposures"] += 1
        for r in clicks:
            props = loads(r["props"], {}) or {}
            v = props.get("variant") or "A"
            per.setdefault(v, {"exposures": 0, "clicks": 0})
            per[v]["clicks"] += 1
        for v, d in per.items():
            d["ctr"] = round(d["clicks"] / d["exposures"], 4) if d["exposures"] else 0.0
        return {"exp": name, "window_hours": hours, "variants": per}

    def promote(self, name: str, winner: str) -> None:
        """赢家晋升：实验结束 + 按变体参数落策略。"""
        db = get_db()
        exp = db.exp_get_by_name(name)
        if not exp:
            raise ValueError("实验不存在")
        variants = loads(exp["variants"], [])
        winner_variant = self._variant_by_key(variants, winner)
        for key, val in (winner_variant.get("params") or {}).items():
            from .strategies import strategies

            strategies.set(key, val, by="ab_promote:" + name)
        db.exp_finish(exp["id"], "finished", winner)


ab_service = ExperimentService()
