"""Риск-скоринг заказа (антифрод): решает, выдавать автоматически, на ручную проверку или блокировать."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from mannbot.crm.db import Database


@dataclass
class RiskResult:
    score: int
    reasons: list[str] = field(default_factory=list)
    decision: str = "auto"          # auto | review | block


class RiskEngine:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db

    def assess(self, order, *, steam_id: str | None = None, escrow_days: int = 0) -> RiskResult:
        c = self.cfg
        if not c.enabled:
            return RiskResult(0)
        score, reasons = 0, []

        def add(points: int, why: str) -> None:
            nonlocal score
            score += points
            reasons.append(f"+{points}: {why}")

        buyer_id, username = order["buyer_id"], order["buyer_username"]
        cust = self.db.customer(buyer_id) if buyer_id else None
        if username in (c.blacklist_funpay_users or []) or (cust and cust["blacklisted"]):
            add(100, "покупатель в чёрном списке")
        if steam_id and steam_id in [str(x) for x in (c.blacklist_steam_ids or [])]:
            add(100, "Steam-аккаунт в чёрном списке")

        keys = int(order["keys_qty"] or 1)
        if keys >= int(c.large_order_keys):
            add(25, f"крупный заказ ({keys} ключей)")
        # первый заказ + крупная сумма — классический сценарий с украденной картой/возвратом
        if cust is not None and (cust["completed_count"] or 0) == 0 and keys >= max(5, int(c.large_order_keys) // 2):
            add(15, "первая покупка и большой объём")

        day_ago = time.time() - 86400
        recent = self.db.scalar("SELECT COUNT(*) FROM orders WHERE buyer_id=? AND created_at>?", (buyer_id, day_ago))
        if recent > int(c.max_orders_per_buyer_per_day):
            add(30, f"{recent} заказов за сутки")
        if cust and (cust["refunded_count"] or 0) >= 2:
            add(20, f"у покупателя {cust['refunded_count']} возврата(ов)")

        if steam_id:
            owners = [r for r in self.db.customers_with_steam_id(steam_id) if r["funpay_id"] != buyer_id]
            if owners:
                add(35, "этот Steam-аккаунт уже использовали другие покупатели FunPay: "
                        + ", ".join(r["username"] for r in owners[:3]))
            if cust:
                ids = json.loads(cust["steam_ids"] or "[]")
                if ids and steam_id not in ids and len(ids) >= 3:
                    add(15, "покупатель часто меняет Steam-аккаунты")
        if escrow_days > 0:
            add(10, f"задержка обмена {escrow_days} дн.")

        decision = "auto"
        if score >= int(c.block_score):
            decision = "block"
        elif score >= int(c.manual_review_score):
            decision = "review"
        return RiskResult(score=score, reasons=reasons, decision=decision)
