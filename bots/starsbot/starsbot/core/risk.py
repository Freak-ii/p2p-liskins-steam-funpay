"""Риск-скоринг заказа (антифрод): выдавать автоматически, на ручную проверку или блокировать.

Типичная схема обмана при продаже звёзд — оплата краденой картой с последующим возвратом денег,
причём звёзды вывести обратно уже нельзя. Поэтому подозрительными считаются крупные первые
покупки, много заказов за сутки и раздача звёзд на много разных аккаунтов.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from starsbot.crm.db import Database


@dataclass
class RiskResult:
    score: int
    reasons: list[str] = field(default_factory=list)
    decision: str = "auto"          # auto | review | block


class RiskEngine:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db

    def assess(self, order, *, tg_username: str | None = None) -> RiskResult:
        c = self.cfg
        if not c.enabled:
            return RiskResult(0)
        score, reasons = 0, []

        def add(points: int, why: str) -> None:
            nonlocal score
            score += points
            reasons.append(f"+{points}: {why}")

        buyer_id, username = order["buyer_id"], order["buyer_username"]
        if "channel" in order.keys() and order["channel"] not in (None, "", "funpay"):
            return self._assess_marketplace(order, tg_username, add, lambda: (score, reasons))
        cust = self.db.customer(buyer_id) if buyer_id else None
        if username in (c.blacklist_funpay_users or []) or (cust and cust["blacklisted"]):
            add(100, "покупатель в чёрном списке")
        if tg_username and tg_username.lower() in {str(x).lstrip("@").lower() for x in (c.blacklist_usernames or [])}:
            add(100, f"username @{tg_username} в чёрном списке")

        stars = int(order["stars"] or 0)
        if stars >= int(c.large_order_stars):
            add(25, f"крупный заказ ({stars} ⭐)")
        if cust is not None and (cust["completed_count"] or 0) == 0 and stars >= max(500, int(c.large_order_stars) // 2):
            add(15, "первая покупка и большой объём")

        day_ago = time.time() - 86400
        recent = self.db.scalar("SELECT COUNT(*) FROM orders WHERE buyer_id=? AND created_at>?", (buyer_id, day_ago))
        if recent > int(c.max_orders_per_buyer_per_day):
            add(30, f"{recent} заказов за сутки")
        if cust and (cust["refunded_count"] or 0) >= 2:
            add(20, f"у покупателя {cust['refunded_count']} возврата(ов)")

        if tg_username:
            rows = self.db.q("SELECT DISTINCT lower(tg_username) u FROM orders WHERE buyer_id=? AND created_at>? AND "
                             "tg_username IS NOT NULL AND tg_username<>''", (buyer_id, day_ago))
            names = {r["u"] for r in rows} | {tg_username.lower()}
            if len(names) > int(c.max_recipients_per_buyer_per_day):
                add(30, f"звёзды на {len(names)} разных аккаунтов за сутки")
            others = self.db.q("SELECT DISTINCT buyer_username FROM orders WHERE lower(tg_username)=? AND buyer_id<>? "
                               "AND created_at>?", (tg_username.lower(), buyer_id, time.time() - 7 * 86400))
            if len(others) >= 2:
                add(15, "на этот username за неделю покупали разные покупатели FunPay: "
                    + ", ".join(r["buyer_username"] for r in others[:3]))
            if cust:
                known = json.loads(cust["tg_usernames"] or "[]")
                if len(known) >= 5 and tg_username.lower() not in known:
                    add(10, "покупатель часто меняет получателей")

        return self._decide(score, reasons)

    def _assess_marketplace(self, order, tg_username, add, result) -> RiskResult:
        """Ozon и другие площадки не сообщают продавцу покупателя: считаем по получателю звёзд."""
        c = self.cfg
        stars = int(order["stars"] or 0)
        if stars >= int(c.large_order_stars):
            add(25, f"крупный заказ ({stars} ⭐)")
        if tg_username and tg_username.lower() in {str(x).lstrip("@").lower() for x in (c.blacklist_usernames or [])}:
            add(100, f"username @{tg_username} в чёрном списке")
        if tg_username:
            day_ago = time.time() - 86400
            n = self.db.scalar("SELECT COUNT(*) FROM orders WHERE lower(tg_username)=? AND channel=? AND created_at>? "
                               "AND order_id<>?", (tg_username.lower(), order["channel"], day_ago, order["order_id"]))
            if n >= int(c.max_orders_per_buyer_per_day):
                add(30, f"на @{tg_username} за сутки уже {n} заказов с {order['channel']}")
        score, reasons = result()
        return self._decide(score, reasons)

    def _decide(self, score: int, reasons: list[str]) -> RiskResult:
        c = self.cfg
        decision = "auto"
        if score >= int(c.block_score):
            decision = "block"
        elif score >= int(c.manual_review_score):
            decision = "review"
        return RiskResult(score=score, reasons=reasons, decision=decision)
