"""Снабжение звёздами других ботов аккаунта (TG Gift) — сторона StarsBot.

TG Gift раз в пару минут пишет в общий файл координации заявку: @username своего Telegram-аккаунта, баланс
и сколько звёзд нужно. StarsBot покупает эти звёзды на Fragment на этот @username обычным путём выдачи
(внутренний заказ SUPPLY-…, channel='internal'): с проверкой получателя, одной транзакцией за раз и сверкой
с блокчейном. Пока поставка не завершилась, новая не создаётся; после неё — пауза, чтобы звёзды дошли.
"""
from __future__ import annotations

import logging
import secrets
import time

from starsbot.core.models import OrderStatus as S

log = logging.getLogger("supply")

PREFIX = "SUPPLY-"


class SupplyManager:
    def __init__(self, cfg, engine):
        self.cfg = cfg
        self.engine = engine
        self.db = engine.db
        self.last: dict = {}

    @property
    def scfg(self):
        return self.cfg.get("supply") or {}

    @property
    def enabled(self) -> bool:
        return bool(self.scfg.get("enabled", True)) and self.engine.coord.enabled and \
            self.cfg.general.get("profile", "stars") != "giftshop"

    @staticmethod
    def owns(o) -> bool:
        return o is not None and str(o["order_id"]).startswith(PREFIX)

    def _pending(self, username: str):
        return self.db.one("SELECT * FROM orders WHERE channel='internal' AND order_id LIKE ? AND lower(tg_username)=? "
                           "AND status NOT IN ('completed','refunded','blocked') ORDER BY created_at DESC LIMIT 1",
                           (PREFIX + "%", username.lower()))

    def _bought_24h(self, username: str) -> int:
        return int(self.db.scalar("SELECT SUM(stars) FROM orders WHERE channel='internal' AND order_id LIKE ? AND "
                                  "lower(tg_username)=? AND created_at>=? AND status NOT IN ('refunded','blocked')",
                                  (PREFIX + "%", username.lower(), time.time() - 86400), 0) or 0)

    def _last_done_at(self, username: str) -> float:
        return float(self.db.scalar("SELECT MAX(completed_at) FROM orders WHERE channel='internal' AND order_id LIKE ? "
                                    "AND lower(tg_username)=? AND status='completed'",
                                    (PREFIX + "%", username.lower()), 0) or 0)

    async def tick(self) -> None:
        """Раз в 2 минуты: сверить прошлые поставки и выполнить свежие заявки."""
        if not self.enabled:
            return
        self.reconcile()
        e = self.engine
        if e.paused:
            return
        for req in e.coord.supply_requests(max_age=900):
            if req.get("bot") == e.coord.bot or int(req.get("need") or 0) <= 0:
                continue
            res = await self.fulfil(req)
            self.last[req["account"]] = {"ts": time.time(), "result": res}
            log.info("снабжение %s: %s", req["account"], res)

    async def fulfil(self, req: dict) -> str:
        e = self.engine
        s = self.scfg
        username = str(req.get("username") or "").lstrip("@")
        if not username:
            return "в заявке нет @username"
        if self._pending(username) is not None:
            return "поставка уже идёт"
        cooldown = float(s.get("cooldown_minutes", 20) or 0) * 60
        if time.time() - self._last_done_at(username) < cooldown:
            return "пауза после прошлой поставки — звёзды ещё доходят"
        amount = int(req["need"])
        cap = int(s.get("max_per_topup") or 0)
        if cap:
            amount = min(amount, cap)
        day = int(s.get("max_per_day") or 0)
        bought = self._bought_24h(username)
        if day and bought + amount > day:
            amount = day - bought
            if amount < 50:
                await e.notifier.notify("WARNING", "supply_day_cap", f"Снабжение @{username}: за сутки уже {bought} ⭐ "
                                        f"(лимит {day} ⭐). Подарки встанут, когда звёзды кончатся — поднимите "
                                        "supply.max_per_day или пополните вручную.", throttle_key=f"supply_cap:{username}")
                return "дневной лимит поставок"
        amount = max(50, amount)                 # Fragment продаёт от 50 звёзд
        w = e.wallet_info or {}
        capacity = w.get("capacity")
        reserve = int(s.get("min_wallet_reserve_stars") or 0)
        if capacity is not None and int(capacity) - amount < reserve:
            await e.notifier.notify("WARNING", "supply_low_wallet", f"Снабжение @{username}: на кошельке хватит на "
                                    f"{capacity} ⭐, а нужно {amount} ⭐ и запас {reserve} ⭐ для своих заказов. "
                                    "Пополните TON.", throttle_key="supply_low_wallet")
            return "мало TON на кошельке"
        return await self.create(amount, username, req)

    async def create(self, amount: int, username: str, req: dict) -> str:
        e = self.engine
        oid = f"{PREFIX}{time.strftime('%m%d%H%M')}-{secrets.token_hex(2).upper()}"
        e.db.create_order(order_id=oid, buyer_username=f"(снабжение {req.get('bot') or 'бота'})", buyer_id=0,
                          chat_id=None, description=f"Звёзды для @{username} ({req.get('account')})", quantity=1,
                          stars_per_unit=amount, stars=amount, price=0, currency="₽", fp_status="internal",
                          channel="internal", delivery="fragment", approved=1, lang="ru")
        e.db.add_event(oid, "created", stars=amount, supply=req.get("account"))
        e.coord.supply_log_add(req["account"], username, amount, oid, note=f"заявка: {req.get('note') or ''}")
        await e.notifier.notify("INFO", "supply", f"🎁 Снабжение {req.get('bot') or 'бота'}: покупаю {amount} ⭐ на "
                                f"@{username} через Fragment", order_id=oid)
        await e.accept_username(e.db.get_order(oid), username, source="supply")
        return f"создана поставка {oid} на {amount} ⭐"

    async def on_delivered(self, o) -> None:
        e = self.engine
        e.coord.supply_log_update(o["order_id"], "done", cost_rub=o["cost_rub"], ton=o["ton_amount"])
        await e.notifier.notify("INFO", "supply_done", f"🎁 Поставка #{o['order_id']}: {o['stars']} ⭐ → "
                                f"@{o['tg_username']} оплачена ({o['ton_amount'] or 0:.4f} TON). Звёзды придут за пару "
                                "минут — TG Gift продолжит сам.", order_id=o["order_id"])

    def reconcile(self) -> None:
        """Состояние поставок в общем журнале по статусам заказов StarsBot (ручные/отменённые — видны TG Gift)."""
        rows = self.db.q("SELECT order_id, status, cost_rub, ton_amount FROM orders WHERE channel='internal' AND "
                         "order_id LIKE ? AND created_at>=?", (PREFIX + "%", time.time() - 3 * 86400))
        for r in rows:
            st = S(r["status"])
            state = {S.COMPLETED: "done", S.REFUNDED: "cancelled", S.BLOCKED: "cancelled",
                     S.MANUAL: "manual"}.get(st, "pending")
            try:
                self.engine.coord.supply_log_update(r["order_id"], state, cost_rub=r["cost_rub"], ton=r["ton_amount"])
            except Exception:  # noqa: BLE001
                pass
