"""Campaign 与优先图书配置（运营与策略中心）。"""
from ..db import dumps, get_db, loads, now_iso

SLOT_HOMEPAGE = "homepage"


class CampaignService:
    def create(self, name: str, slot: str, book_ids: list, weight: float = 1.0,
               start_at: str = "", end_at: str = "", enabled: bool = True) -> int:
        return get_db().campaign_create(
            name, slot, dumps(book_ids), weight,
            start_at, end_at, 1 if enabled else 0,
        )

    def list(self) -> list:
        rows = get_db().campaign_list()
        for r in rows:
            r["book_ids"] = loads(r["book_ids"], [])
        return rows

    def delete(self, campaign_id: int) -> None:
        get_db().campaign_delete(campaign_id)

    def set_enabled(self, campaign_id: int, enabled: bool) -> None:
        get_db().campaign_update(campaign_id, 1 if enabled else 0)

    def active_for_slot(self, slot: str) -> list:
        """返回 [(book_id, campaign_name, weight)] 有序（weight 降序）。"""
        now = now_iso()
        out = []
        for c in get_db().campaigns_active(now):
            if c["slot"] != slot:
                continue
            for bid in loads(c["book_ids"], []):
                out.append((bid, c["name"], float(c["weight"])))
        out.sort(key=lambda x: x[2], reverse=True)
        return out

    def priority_books(self, slot: str) -> list:
        out = []
        for r in get_db().priority_list(slot):
            out.append(
                {"book_id": r["book_id"], "weight": r["weight"], "reason": r["reason"]}
            )
        return out

    def set_priority(self, slot: str, book_id: str, weight: float, reason: str) -> None:
        get_db().priority_upsert(slot, book_id, weight, reason)

    def delete_priority(self, slot: str, book_id: str) -> None:
        get_db().priority_delete(slot, book_id)


campaigns = CampaignService()
