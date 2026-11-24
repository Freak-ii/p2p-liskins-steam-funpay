"""Продажа Telegram Premium: FunPay / GGSEL / магазин → подарок Premium на Fragment → оплата тем же кошельком TON.

Premium живёт внутри движка StarsBot, а не отдельным процессом: кошелёк TON один, и транзакции должны идти
строго по очереди (seqno). Заказ Premium — это обычный заказ движка с delivery='premium', stars=0 и
premium_months=N. Покупка идёт через ту же очередь, сверку с блокчейном и предохранители, что и звёзды,
только методы Fragment другие (searchPremiumGiftRecipient → initGiftPremiumRequest → getGiftPremiumLink).

Здесь: распознавание заказов и сроков, оценка себестоимости по котировкам Fragment, резерв TON под активные
заказы, наличие и цена лотов FunPay (по рынку с полом по марже или «себестоимость + маржа»), заказы магазина
(supply) с идемпотентным ref.
"""
from __future__ import annotations

import logging
import math
import re
import statistics
import time
from typing import Any

from starsbot.core.models import OrderStatus as S

log = logging.getLogger("premium")

MONTHS = (3, 6, 12)
_MONTHS_RE = re.compile(r"(\d{1,2})\s*(?:-?\s*)(?:мес|month|mo\b|м\b)", re.I)
_YEAR_RE = re.compile(r"(?:\b1\s*)?(?:год|year|12\s*мес)", re.I)
_HALF_RE = re.compile(r"полгода|пол\s+года|half\s*a?\s*year", re.I)


def is_premium(o) -> bool:
    return o is not None and "delivery" in o.keys() and o["delivery"] == "premium"


def months_from_text(text: str) -> int | None:
    """«Telegram Premium, 3 месяца» → 3; «на 1 год» → 12; «полгода» → 6."""
    t = text or ""
    m = _MONTHS_RE.search(t)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None
    if _HALF_RE.search(t):
        return 6
    if _YEAR_RE.search(t):
        return 12
    return None


def months_text(months: int, lang: str = "ru") -> str:
    m = int(months or 0)
    if lang == "en":
        return f"{m} month" + ("" if m == 1 else "s")
    if m % 10 == 1 and m % 100 != 11:
        word = "месяц"
    elif 2 <= m % 10 <= 4 and not 12 <= m % 100 <= 14:
        word = "месяца"
    else:
        word = "месяцев"
    return f"{m} {word}"


class SupplyChannel:
    """Заказы магазина (Telegram-бот магазина берёт Premium у этого бота): покупателю пишет сам магазин."""

    def __init__(self, service: "PremiumService", name: str):
        self.service = service
        self.name = name
        self.buyer_chat = False

    async def say(self, o, key: str, variables: dict) -> None:
        return None

    async def say_text(self, o, text: str) -> None:
        return None

    async def on_delivered(self, o) -> None:
        self.service.engine._set(o["order_id"], S.COMPLETED, "completed", completed_at=time.time(), fp_status="closed")

    async def stock_sync(self, balance: float, per_star, committed: int) -> None:
        return None

    def loops(self, make_loop) -> list:
        return []

    def health_limits(self) -> dict:
        return {}

    async def admin_refund(self, oid: str) -> str:
        self.service.engine._set(oid, S.MANUAL, "refund_requested", fail_reason="refund_pending")
        return "заказ магазина остановлен: деньги покупателю возвращает магазин"

    async def close(self) -> None:
        return None


class PremiumService:
    def __init__(self, cfg, engine):
        self.cfg = cfg
        self.engine = engine
        self.db = engine.db
        self.updates: dict[str, list[float]] = {}
        self.markup: dict[str, float] = {}
        self.status: dict[str, Any] = {"rtba": "", "ts": 0}

    # ------------------------------------------------------------------ настройки
    @property
    def c(self):
        return self.cfg.get("premium") or {}

    @property
    def enabled(self) -> bool:
        return bool(self.c.get("enabled"))

    @property
    def node(self) -> int:
        return int(self.c.get("node_id") or 1391)

    @property
    def lots(self) -> list[dict]:
        return [l for l in (self.c.get("lots") or []) if int(l.get("months") or 0) in MONTHS]

    # ------------------------------------------------------------------ заказы
    def lot_for(self, description: str) -> tuple[dict, int] | None:
        months = months_from_text(description)
        if months not in MONTHS:
            return None
        for lot in self.lots:
            if int(lot["months"]) == months:
                return lot, months
        return None

    def is_order(self, description: str) -> bool:
        d = description or ""
        if not self.enabled or not re.search(self.c.get("order_regex") or r"(?i)premium|премиум", d):
            return False
        ex = self.c.get("order_exclude_regex")
        if ex and re.search(ex, d):
            return False
        found = self.lot_for(d)
        if not found:
            return False
        configured = [l for l in self.lots if l.get("offer_id")]
        return not configured or bool(found[0].get("offer_id"))

    def describe(self, months: int) -> str:
        return f"Telegram Premium {months_text(months)}"

    # ------------------------------------------------------------------ себестоимость
    def record_quote(self, months: int, ton: float) -> None:
        if ton > 0:
            self.db.kv_set(f"premium_quote:{int(months)}", {"ton": float(ton), "ts": time.time()})

    def ton_for(self, months: int) -> float | None:
        """TON за подарок Premium на N месяцев: медиана последних покупок, иначе котировка Fragment, иначе настройки."""
        m = int(months)
        rows = self.db.q("SELECT ton_amount FROM orders WHERE delivery='premium' AND premium_months=? AND "
                         "ton_amount>0 AND sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT 5", (m,))
        quote = self.db.kv_get(f"premium_quote:{m}")
        if quote and rows and float(quote.get("ts") or 0) > time.time() - 3 * 3600:
            return float(quote["ton"])            # свежая котировка точнее старых покупок (курс TON меняется)
        if rows:
            return statistics.median(float(r["ton_amount"]) for r in rows)
        if quote:
            return float(quote["ton"])
        fb = self.c.get("fallback_ton") or {}
        v = float(fb.get(str(m)) or fb.get(m) or 0)
        return v or None

    def gas_ton(self) -> float:
        return float(self.cfg.economics.get("gas_per_purchase_ton") or 0)

    def cost_rub(self, months: int) -> float | None:
        ton = self.ton_for(months)
        if not ton:
            return None
        return self.engine.ton_to_rub(ton + self.gas_ton())

    def keep(self, channel: str = "funpay") -> float:
        econ = self.engine.econ
        fee, wd = econ.fee_pcts(channel) if econ is not None else (0.0, 0.0)
        return (1 - fee / 100) * (1 - wd / 100)

    def floor_price(self, months: int, channel: str = "funpay", margin_pct: float | None = None) -> float | None:
        """Минимальная цена (что получает продавец): себестоимость + маржа с поправкой на комиссии."""
        cost = self.cost_rub(months)
        if cost is None:
            return None
        m = float(self.c.get("min_margin_pct") or 0) if margin_pct is None else float(margin_pct)
        k = self.keep(channel) - m / 100
        if k <= 0:
            return None
        return float(math.ceil(cost / k))

    def margin_at(self, months: int, price: float, channel: str = "funpay") -> float | None:
        cost = self.cost_rub(months)
        if cost is None or not price:
            return None
        return round((price * self.keep(channel) - cost) / price * 100, 1)

    def committed_ton(self) -> float:
        """TON, обещанные активным заказам Premium, ещё не ушедшим в сеть."""
        waiting = (S.NEW, S.AWAITING_USERNAME, S.PENDING_APPROVAL, S.READY, S.PURCHASING)
        marks = ",".join("?" * len(waiting))
        total = 0.0
        for r in self.db.q(f"SELECT premium_months m, COUNT(*) n FROM orders WHERE delivery='premium' AND status IN "
                           f"({marks}) GROUP BY premium_months", [s.value for s in waiting]):
            ton = self.ton_for(int(r["m"] or 0)) or 0.0
            total += (ton + self.gas_ton()) * int(r["n"])
        return total

    def free_ton(self, balance: float, per_star: float | None, committed_stars: int) -> float:
        free = balance - float(self.cfg.ton.fee_reserve_ton) - committed_stars * (per_star or 0) - self.committed_ton()
        if not self.engine.fragment_ok and self.cfg.fragment.hide_lots_when_down:
            free = 0.0
        return max(0.0, free)

    def capacity(self, months: int, free_ton: float) -> int:
        ton = self.ton_for(months)
        if not ton:
            return 0
        return int(free_ton / ((ton + self.gas_ton()) * 1.03))

    def live_purchases(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM purchases p JOIN orders o ON o.order_id=p.order_id WHERE "
                                  "o.delivery='premium' AND p.state IN ('sent','confirmed') AND p.msg_hash NOT LIKE "
                                  "'dry%'", (), 0))

    def safety_problem(self, o, amount: float) -> str:
        c = self.cfg
        if c.general.dry_run or c.general.demo:
            return ""
        pilot = int(self.c.get("pilot_orders") or 0)
        done = self.live_purchases()
        if pilot and done < pilot and not o["approved"]:
            return (f"пилотный режим Premium: боевая покупка {done + 1} из {pilot} — проверьте получателя "
                    f"@{o['tg_username']} и сумму ({amount:.4f} TON)")
        return ""

    # ------------------------------------------------------------------ наличие лотов
    async def stock_sync(self, balance: float, per_star: float | None, committed: int) -> None:
        if not self.enabled or not self.c.get("stock_sync", True) or self.engine.paused:
            return
        free = self.free_ton(balance, per_star, committed)
        for lot in self.lots:
            if not lot.get("offer_id"):
                continue
            months = int(lot["months"])
            if not self.ton_for(months):
                continue                         # себестоимость неизвестна — наличие не трогаем
            amount = max(0, min(self.capacity(months, free), int(self.c.get("max_display_amount") or 50)))
            key = f"premium_lot_amount:{lot['offer_id']}"
            if self.db.kv_get(key) == amount:
                continue
            active = amount > 0 or not self.c.get("deactivate_when_empty", True)
            try:
                await self.engine.fp.update_lot(int(lot["offer_id"]), self.node, amount=max(amount, 1 if active else 0),
                                                active=active)
                self.db.kv_set(key, amount)
                self.db.add_event("-", "premium_lot_stock", offer_id=lot["offer_id"], months=months, amount=amount,
                                  active=active)
            except Exception as e:  # noqa: BLE001 — FunPayError и сетевые ошибки
                await self.engine.notifier.notify("WARNING", "lot_update_failed", f"Лот Premium {lot['offer_id']}: {e}",
                                                  throttle_key=f"plot:{lot['offer_id']}")

    # ------------------------------------------------------------------ цены
    @staticmethod
    def competitor_months(lot) -> int | None:
        for v in (lot.fields or {}).values():
            m = months_from_text(str(v))
            if m:
                return m
        return months_from_text(lot.description)

    def competitors(self, lots: list, months: int, own_ids: set[str]) -> list:
        rx = re.compile(self.c.get("competitor_regex") or ".", re.I)
        ex = re.compile(self.c.get("order_exclude_regex") or r"$^", re.I)
        ignore = {str(s).lower() for s in (self.c.get("ignore_competitors") or [])}
        own_name = (self.engine.fp.username or "").lower()
        out = []
        for l in lots:
            text = " ".join([l.description or "", *[str(v) for v in (l.fields or {}).values()]])
            if self.competitor_months(l) != months or l.offer_id in own_ids:
                continue
            if (own_name and l.seller.lower() == own_name) or l.seller.lower() in ignore:
                continue
            if ex.search(text) or not rx.search(text):
                continue
            out.append(l)
        out.sort(key=lambda l: l.price)
        return out

    def decide(self, lots: list, lot: dict, current: float | None, own_ids: set[str]) -> dict:
        """Цена лота Premium: позиция среди таких же лотов «без входа», но не ниже пола по марже."""
        months, offer_id = int(lot["months"]), str(lot["offer_id"])
        for l in lots:                                   # наценка FunPay для покупателя — по своему лоту в таблице
            if l.offer_id == offer_id and current and 0.8 < l.price / current < 1.6:
                self.markup[offer_id] = round(l.price / current, 4)
        # раздел Premium (1391): FunPay добавляет покупателю 15.07%, в «Звёздах» 14.05% (lots/calc, 24.11.2026)
        pct = self.c.get("buyer_markup_pct")
        markup = self.markup.get(offer_id) or 1 + float(15.07 if pct is None else pct) / 100
        floor = self.floor_price(months)
        mode = str(self.c.get("price_mode") or "rtba")
        comps = self.competitors(lots, months, own_ids)
        best = comps[0].price if comps else None
        if mode == "manual":
            return {"target": None, "reason": "цены вручную", "floor": floor, "best": best, "n": len(comps)}
        if mode == "margin" or not comps:
            target = self.floor_price(months, margin_pct=float(self.c.get("margin_pct") or 0))
            reason = "себестоимость + маржа" if mode == "margin" else "нет конкурентов — цена по марже"
        else:
            pos = int(lot.get("position") or 0) or int(self.c.get("target_position") or 1)
            anchor = comps[min(pos, len(comps)) - 1].price
            target_buyer = anchor - float(self.c.get("undercut_abs") or 0)
            if pos > 1 and len(comps) >= pos:
                target_buyer = max(target_buyer, comps[pos - 2].price + 0.02 * markup)
            target = math.floor(target_buyer / markup * 100) / 100
            reason = f"позиция {pos}: конкурент {anchor:.2f} ₽"
        if target is None:
            return {"target": None, "reason": "себестоимость неизвестна", "floor": floor, "best": best,
                    "n": len(comps)}
        if floor and target < floor:
            target, reason = floor, reason + f"; рынок ниже пола {floor:g} ₽ — стоим на полу"
        target = round(target, 2)
        rank = 1 + sum(1 for c in comps if c.price < target * markup)
        return {"target": target, "reason": reason, "floor": floor, "best": best, "n": len(comps), "rank": rank}

    async def rtba_step(self) -> None:
        if not self.enabled or self.engine.paused:
            return
        lots = [l for l in self.lots if l.get("offer_id")]
        if not lots:
            return
        fp = self.engine.fp
        try:
            public = await fp.get_public_lots(self.node)
        except Exception as e:  # noqa: BLE001
            log.warning("Premium RTBA: %s", e)
            return
        await self.engine.rates.refresh()
        own_ids = {str(l["offer_id"]) for l in lots}
        now = time.time()
        for lot in lots:
            offer_id = str(lot["offer_id"])
            try:
                fields = await fp.get_lot_fields(int(offer_id), self.node)
            except Exception as e:  # noqa: BLE001
                log.warning("Premium лот %s: %s", offer_id, e)
                continue
            current = float(str(fields.get("price") or 0).replace(",", ".")) or None
            d = self.decide(public, lot, current, own_ids)
            applied = False
            ups = [u for u in self.updates.get(offer_id, []) if now - u < 3600]
            self.updates[offer_id] = ups
            t = d["target"]
            change = t is not None and (current is None or abs(t - current) >= float(self.c.get("min_change") or 0)
                                        or (d.get("floor") and current < d["floor"]))
            if change and len(ups) < int(self.c.get("max_updates_per_hour") or 4):
                try:
                    fields["price"] = f"{t:.2f}"
                    await fp.save_lot(fields)
                    ups.append(now)
                    applied = True
                except Exception as e:  # noqa: BLE001
                    await self.engine.notifier.notify("WARNING", "rtba_save_failed", f"Premium {lot['months']} мес.: "
                                                      f"цена не сохранена: {e}", throttle_key=f"prtba:{offer_id}")
            self.db.exec("INSERT INTO price_history(ts,offer_id,stars,old_price,new_price,best_competitor,competitors,"
                         "position,floor,reason,applied) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (now, offer_id, 0, current, t, d.get("best"), d.get("n"), d.get("rank"), d.get("floor"),
                          f"Premium {lot['months']} мес.: " + d["reason"], int(applied)))
        self.status = {"rtba": "ok", "ts": now}
        self.db.emit({"type": "prices"})

    async def refresh_quotes(self) -> None:
        """Котировка Fragment без оплаты: заявка на подарок самому себе создаётся и просто истекает."""
        u = str(self.c.get("quote_username") or "").lstrip("@").strip()
        if not self.enabled or not u or not self.engine.fragment_ok:
            return
        frag = self.engine.fragment
        for months in MONTHS:
            try:
                rec = await frag.search_premium_recipient(u, months)
                _, amount = await frag.init_premium(rec.recipient, months)
                self.record_quote(months, amount)
            except Exception as e:  # noqa: BLE001
                log.info("котировка Premium %s мес.: %s", months, e)
                return

    def loops(self, make_loop) -> list:
        if not self.enabled:
            return []
        return [make_loop("premium_rtba", float(self.c.get("interval_seconds") or 300), self.rtba_step),
                make_loop("premium_quotes", 60 * float(self.c.get("quote_minutes") or 30), self.refresh_quotes)]

    # ------------------------------------------------------------------ панель
    def rows(self) -> list[dict]:
        out = []
        free = None
        w = self.engine.wallet_info or {}
        if w.get("balance") is not None:
            free = self.free_ton(float(w["balance"]), w.get("per_star"), int(w.get("committed") or 0))
        for l in self.c.get("lots") or []:
            m = int(l.get("months") or 0)
            oid = l.get("offer_id") or 0
            last = self.db.one("SELECT new_price, position, best_competitor, floor, reason FROM price_history WHERE "
                               "offer_id=? ORDER BY ts DESC LIMIT 1", (str(oid),)) if oid else None
            sold = self.db.one("SELECT COUNT(*) n, SUM(price) rub FROM orders WHERE delivery='premium' AND "
                               "premium_months=? AND status IN ('delivered','completed')", (m,))
            price = last["new_price"] if last else None
            out.append({"months": m, "title": months_text(m), "offer_id": oid, "position": int(l.get("position") or 0),
                        "amount": self.db.kv_get(f"premium_lot_amount:{oid}") if oid else None,
                        "ton": self.ton_for(m), "cost_rub": self.cost_rub(m), "floor": self.floor_price(m),
                        "price": price, "margin": self.margin_at(m, price) if price else None,
                        "rank": last["position"] if last else None, "best": last["best_competitor"] if last else None,
                        "reason": last["reason"] if last else "", "capacity": self.capacity(m, free) if free else None,
                        "sold": int(sold["n"] or 0) if sold else 0, "sold_rub": float(sold["rub"] or 0) if sold else 0})
        return out

    def orders(self, limit: int = 50) -> list:
        return self.db.q("SELECT * FROM orders WHERE delivery='premium' ORDER BY created_at DESC LIMIT ?", (limit,))

    def summary(self) -> dict:
        r = self.db.one("SELECT COUNT(*) n, SUM(price) rub, SUM(cost_rub) cost FROM orders WHERE delivery='premium' "
                        "AND status IN ('delivered','completed') AND delivered_at>=?", (time.time() - 30 * 86400,))
        active = self.db.scalar("SELECT COUNT(*) FROM orders WHERE delivery='premium' AND status NOT IN "
                                "('completed','refunded','blocked','delivered')", (), 0)
        return {"enabled": self.enabled, "sold_30d": int(r["n"] or 0), "revenue_30d": round(float(r["rub"] or 0), 2),
                "cost_30d": round(float(r["cost"] or 0), 2), "active": int(active or 0),
                "committed_ton": round(self.committed_ton(), 4)}

    def set_lot(self, months: int, *, offer_id: int | None = None, position: int | None = None) -> list[dict]:
        lots = [dict(l) for l in (self.c.get("lots") or [])]
        for l in lots:
            if int(l.get("months") or 0) == int(months):
                if offer_id is not None:
                    l["offer_id"] = int(offer_id)
                if position is not None:
                    l["position"] = max(0, int(position))
        if self.cfg.get("premium") is not None:
            self.cfg.premium["lots"] = lots
        return lots

    async def find_own_lots(self) -> list[str]:
        """Найти свои лоты Premium в разделе FunPay и записать их id по срокам."""
        report = []
        try:
            own = await self.engine.fp.get_own_lots(self.node)
        except Exception as e:  # noqa: BLE001
            return [f"не удалось прочитать ваши лоты Premium: {e}"]
        ex = self.c.get("order_exclude_regex")
        for ol in own:
            m = months_from_text(ol.description)
            if m not in MONTHS or (ex and re.search(ex, ol.description)):
                continue
            lot = next((l for l in self.c.get("lots") or [] if int(l.get("months") or 0) == m), None)
            if lot is not None and not lot.get("offer_id"):
                self.set_lot(m, offer_id=int(ol.offer_id))
                report.append(f"{months_text(m)}: найден лот {ol.offer_id}")
        return report or ["новых лотов Premium не найдено (сроки 3/6/12 мес., без входа в аккаунт)"]

    # ------------------------------------------------------------------ магазин (supply)
    def register_supply_channels(self) -> None:
        """После перезапуска заказы магазина снова идут через свой «канал» (писать покупателю — дело магазина)."""
        for r in self.db.q("SELECT DISTINCT channel FROM orders WHERE order_id LIKE 'PR-SUP-%'"):
            if r["channel"] and r["channel"] not in self.engine.channels:
                self.engine.channels[r["channel"]] = SupplyChannel(self, r["channel"])

    @staticmethod
    def supply_order_id(ref: str) -> str:
        return "PR-SUP-" + re.sub(r"[^A-Za-z0-9_-]", "", str(ref))[:40]

    def supply_quote(self, months: int) -> dict:
        months = int(months or 0)
        if not self.enabled:
            return {"available": False, "cost_rub": None, "cost_ton": None, "reason": "Premium выключен"}
        if months not in MONTHS:
            return {"available": False, "cost_rub": None, "cost_ton": None, "reason": "Fragment дарит 3, 6 или 12 мес."}
        ton = self.ton_for(months)
        if not ton:
            return {"available": False, "cost_rub": None, "cost_ton": None, "reason": "нет котировки Fragment"}
        e = self.engine
        reason = ""
        w = e.wallet_info or {}
        if e.paused:
            reason = "бот на паузе"
        elif not e.fragment_ok:
            reason = "нет сессии Fragment"
        elif w.get("balance") is not None and \
                self.capacity(months, self.free_ton(float(w["balance"]), w.get("per_star"), int(w.get("committed") or 0))) < 1:
            reason = "не хватает TON на кошельке"
        cost = self.cost_rub(months)
        return {"available": not reason, "cost_rub": round(cost, 2) if cost else None,
                "cost_ton": round(ton + self.gas_ton(), 6), "reason": reason}

    async def supply_order(self, ref: str, username: str, months: int, channel: str = "shop",
                           price: float = 0.0) -> dict:
        from starsbot.core.stars import normalize_username
        oid = self.supply_order_id(ref)
        if not str(ref or "").strip() or oid == "PR-SUP-":
            return {"ok": False, "order_id": "", "status": "failed", "detail": "пустой ref"}
        if self.db.get_order(oid) is not None:
            return {"ok": True, "order_id": oid, **self.supply_status(ref)}
        q = self.supply_quote(months)
        if not q["available"]:
            return {"ok": False, "order_id": "", "status": "failed", "detail": q["reason"]}
        u = normalize_username(username or "")
        if not u:
            return {"ok": False, "order_id": "", "status": "failed", "detail": "неверный username"}
        e = self.engine
        if channel not in e.channels:
            e.channels[channel] = SupplyChannel(self, channel)
        self.db.create_order(order_id=oid, buyer_username=f"Магазин ({channel})", buyer_id=0, chat_id=None,
                             description=self.describe(months), quantity=1, stars_per_unit=0, stars=0,
                             price=float(price or 0), currency="₽", fp_status="paid", lot_offer_id="", lang="ru",
                             channel=channel, payout=float(price or 0), delivery="premium",
                             premium_months=int(months), status=S.AWAITING_USERNAME.value)
        self.db.add_event(oid, "created", channel=channel, ref=str(ref), months=int(months), username=u)
        await e.notifier.notify("INFO", "new_order", f"🛒 Магазин: Telegram Premium {months_text(months)} → @{u}",
                                order_id=oid)
        await e.accept_username(self.db.get_order(oid), u, source="supply")
        return {"ok": True, "order_id": oid, **self.supply_status(ref)}

    def supply_status(self, ref: str) -> dict:
        oid = self.supply_order_id(ref)
        o = self.db.get_order(oid)
        if o is None:
            return {"status": "failed", "order_id": "", "detail": "заказ не найден"}
        st = S(o["status"])
        status = {S.NEW: "queued", S.AWAITING_USERNAME: "queued", S.READY: "queued", S.PENDING_APPROVAL: "manual",
                  S.PURCHASING: "delivering", S.SENT: "delivering", S.DELIVERED: "delivered",
                  S.COMPLETED: "delivered", S.MANUAL: "manual", S.REFUNDED: "failed", S.BLOCKED: "failed"}[st]
        if st == S.AWAITING_USERNAME and o["fail_reason"] in ("not_found", "cannot_receive", "bad_format", "blacklisted"):
            status = "failed"
        detail = o["fail_reason"] or ""
        if status == "delivered":
            detail = f"Premium {months_text(int(o['premium_months'] or 0))} → @{o['tg_username']}"
        return {"status": status, "order_id": oid, "detail": detail}

