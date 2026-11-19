"""Выдача звёзд подарками Telegram: заказы FunPay «N звёзд, Подарком».

Путь заказа: оплата на FunPay → бот просит @username (или берёт из заказа) → находит аккаунт через
Telegram-аккаунт продавца → подбирает подарки под номинал → отправляет их со звёздного баланса аккаунта →
пишет покупателю, как превратить подарки в звёзды, и просит подтвердить заказ.

Звёзды на баланс аккаунта продавца бот может докупать сам через Fragment (самому себе, по @username аккаунта)
тем же безопасным путём, что и обычные заказы, — см. GiftService.maybe_topup.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from starsbot.core.models import OrderStatus as S
from starsbot.core.stars import stars_from_text
from starsbot.gifts.planner import DEFAULT_CATALOG, Gift, GiftPlan, PlanError, plan_gifts
from starsbot.gifts.telegram import GiftError, TgUser

log = logging.getLogger("gifts")

BAD_RECIPIENT = {"not_found", "cannot_receive", "bad_format"}
GIFT_LOT_DENOMS = (13, 21, 43, 85, 50, 100, 150, 250, 350, 500)


class InternalChannel:
    """«Канал» для внутренних заказов бота (пополнение звёзд аккаунта через Fragment): покупателя нет, писать некому."""

    name = "internal"

    def __init__(self, service: "GiftService"):
        self.service = service

    async def say(self, o, key: str, variables: dict) -> None:
        return None

    async def say_text(self, o, text: str) -> None:
        return None

    async def on_delivered(self, o) -> None:
        db = self.service.engine.db
        db.update_order(o["order_id"], status=S.COMPLETED.value, completed_at=time.time())
        db.kv_set("topup_done_at", time.time())
        await self.service.engine.notifier.notify("INFO", "stars_topup_done", f"⭐ Аккаунт пополнен на {o['stars']} ⭐ "
                                                  "через Fragment — звёзды приходят за пару минут, подарки "
                                                  "продолжатся автоматически")
        self.service.balance_ts = 0

    async def stock_sync(self, balance: float, per_star, committed: int) -> None:
        return None

    def loops(self, make_loop) -> list:
        return []

    def health_limits(self) -> dict:
        return {}

    async def admin_refund(self, oid: str) -> str:
        return "это внутреннее пополнение звёзд — возврата нет, отметьте вручную при необходимости"

    async def close(self) -> None:
        return None


class GiftService:
    def __init__(self, cfg, engine, tg):
        self.cfg = cfg
        self.engine = engine
        self.db = engine.db
        self.tg = tg
        self.balance: int | None = (self.db.kv_get("tg_stars") or {}).get("balance")
        self.balance_ts = 0.0
        self.account: dict = self.db.kv_get("tg_account") or {}
        self.status: dict = {"ok": False, "error": "", "ts": 0}
        self._order_re = re.compile(self.gcfg.get("order_regex") or r"(?i)(подарк|gift)")
        self._lock = asyncio.Lock()
        self._users: dict[str, TgUser] = {}

    @property
    def gcfg(self):
        return self.cfg.get("gifts") or {}

    @property
    def enabled(self) -> bool:
        return bool(self.gcfg.get("enabled"))

    # ------------------------------------------------------------------ заказы
    @property
    def lots(self) -> list[dict]:
        return [l for l in (self.gcfg.get("lots") or []) if l.get("stars")]

    def lot_for(self, description: str) -> tuple[dict, int] | None:
        stars = stars_from_text(description)
        if not stars:
            return None
        for lot in self.lots:
            if int(lot["stars"]) == stars:
                return lot, stars
        return None

    def is_gift_order(self, description: str) -> bool:
        d = description or ""
        if not self.enabled or not self._order_re.search(d):
            return False
        if re.search(r"(?i)с заходом|вход на аккаунт", d):
            return False
        found = self.lot_for(d)
        if not found:
            return False
        configured = [l for l in self.lots if l.get("offer_id")]
        return not configured or bool(found[0].get("offer_id"))

    # ------------------------------------------------------------------ каталог и план
    async def catalog(self) -> list[Gift]:
        try:
            return await self.tg.catalog()
        except GiftError as e:
            log.warning("каталог подарков: %s", e)
            return []

    def plan_for(self, stars: int, catalog: list[Gift] | None = None) -> GiftPlan:
        g = self.gcfg
        return plan_gifts(catalog or DEFAULT_CATALOG, int(stars), mode=str(g.get("mode") or "convert"),
                          allowed_ids=g.get("allowed_gift_ids") or None,
                          max_gifts=int(g.get("max_gifts_per_order") or 40),
                          per_gift_penalty=float(g.get("per_gift_penalty", 3) or 0))

    def cost_for(self, stars: int) -> int:
        cat = self.tg._catalog[1] if hasattr(self.tg, "_catalog") and isinstance(self.tg._catalog, tuple) else None
        try:
            return self.plan_for(stars, cat or None).cost
        except PlanError:
            return int(stars * 1.2)

    # ------------------------------------------------------------------ получатель
    async def resolve(self, username: str) -> TgUser:
        u = await self.tg.resolve(username)
        if u.bot:
            raise GiftError(f"@{username} — это бот", "cannot_receive")
        me = self.account.get("user_id")
        if me and int(me) == int(u.user_id):
            raise GiftError("это аккаунт продавца", "cannot_receive")
        self._users[u.ref] = u
        return u

    async def accept_username(self, o, username: str) -> None:
        """Аналог Engine.accept_username для подарков: проверка аккаунта в Telegram вместо Fragment."""
        e = self.engine
        oid = o["order_id"]
        try:
            user = await self.resolve(username)
        except GiftError as err:
            if err.code in BAD_RECIPIENT:
                await e.bad_username(o, err.code, str(err))
                return
            e._set(oid, event="username_check_deferred", fail_reason=err.code if err.code != "unknown" else "network")
            if err.code in ("auth", "config"):
                await e.notifier.notify("CRITICAL", "tg_auth", f"Подарки: Telegram-аккаунт не подключён ({err}). "
                                        "Откройте «Подарки» и войдите по номеру.", throttle_key="tg_auth")
            if not e._greeted(oid):
                await e.say(o, "greeting_with_username")
                e.db.add_event(oid, "greeting_with_username")
            return
        cat = await self.catalog()
        try:
            plan = self.plan_for(int(o["stars"]), cat or None)
        except PlanError as err:
            e._set(oid, S.MANUAL, "gift_plan_failed", fail_reason="gift_plan")
            await e.say(o, "delay")
            await e.notifier.notify("CRITICAL", "gift_plan", f"#{oid}: {err}", order_id=oid)
            return
        o = e._set(oid, event="username_ok", tg_username=user.username, tg_recipient=user.ref, tg_name=user.name,
                   fail_reason="", gift_plan=json.dumps(plan.to_json()), gift_cost=plan.cost)
        if e._greeted(oid):
            await e.say(o, "username_ok", gifts=plan.summary())
        else:
            await e.say(o, "greeting_with_username")
            e.db.add_event(oid, "greeting_with_username")
        await e._after_username_ok(o)

    # ------------------------------------------------------------------ отправка
    def _safety_problem(self, o, cost: int) -> str:
        c = self.cfg
        if c.general.dry_run or c.general.demo:
            return ""
        pilot = int(self.gcfg.get("pilot_orders") or 0)
        done = int(self.db.scalar("SELECT COUNT(DISTINCT order_id) FROM gift_sends WHERE state='sent'", (), 0))
        if pilot and done < pilot:
            return f"пилот подарков: заказ {done + 1} из {pilot} — проверьте получателя и набор ({cost} ⭐)"
        mx = int(self.gcfg.get("max_order_stars") or 0)
        if mx and cost > mx:
            return f"набор стоит {cost} ⭐ — больше лимита {mx} ⭐ на заказ"
        return ""

    async def deliver(self, o) -> None:
        """Отправка подарков по заказу в статусе READY. Вызывается из очереди выдачи движка."""
        e = self.engine
        oid = o["order_id"]
        async with self._lock:
            o = e.db.get_order(oid)
            if not o or S(o["status"]) != S.READY:
                return
            if not o["tg_recipient"] or ":" not in str(o["tg_recipient"]):
                e._set(oid, S.AWAITING_USERNAME, "no_recipient")
                await self.accept_username(e.db.get_order(oid), o["tg_username"])
                return
            cat = await self.catalog()
            if not cat:
                e._set(oid, fail_reason="tg_catalog")
                e.enqueue(oid, 60)
                await e.notifier.notify("WARNING", "tg_catalog", "Подарки: каталог Telegram недоступен, повторю",
                                        throttle_key="tg_catalog")
                return
            sent = e.db.q("SELECT * FROM gift_sends WHERE order_id=? ORDER BY seq", (oid,))
            if any(r["state"] in ("sending", "unknown") for r in sent):
                e._set(oid, S.MANUAL, "gift_unknown", fail_reason="gift_unknown")
                await e.notifier.notify("CRITICAL", "gift_unknown", f"#{oid}: исход отправки подарка неизвестен — "
                                        "проверьте историю звёзд аккаунта и отметьте вручную.", order_id=oid)
                return
            done = [r for r in sent if r["state"] == "sent"]
            try:
                plan = self.plan_for(int(o["stars"]), cat)
            except PlanError as err:
                e._set(oid, S.MANUAL, "gift_plan_failed", fail_reason="gift_plan")
                await e.notifier.notify("CRITICAL", "gift_plan", f"#{oid}: {err}", order_id=oid)
                return
            if done:   # часть подарков уже ушла (перезапуск посреди заказа): досылаем остаток по сумме
                left = int(o["stars"]) - sum(int(r["convert_stars"] if plan.mode == "convert" else r["stars"])
                                             for r in done)
                plan = self.plan_for(left, cat) if left > 0 else GiftPlan([], plan.mode)
            cost = plan.cost
            balance = await self.refresh_balance()
            if balance is not None and balance < cost:
                await self.out_of_stars(o, balance, cost)
                return
            problem = self._safety_problem(o, cost)
            if problem and not o["approved"]:
                e._set(oid, S.PENDING_APPROVAL, "gift_guard", fail_reason="gift_guard")
                await e.say(o, "awaiting_approval")
                await e.notifier.notify("CRITICAL", "gift_guard", f"#{oid}: {problem}. /approve {oid} — отправить.",
                                        order_id=oid, buttons=[("✔ Отправить", f"approve {oid}"),
                                                               ("✋ В ручную", f"reject {oid}")])
                return
            user_id, access_hash = (int(x) for x in str(o["tg_recipient"]).split(":", 1))
            user = TgUser(user_id, access_hash, o["tg_username"] or "", o["tg_name"] or "")
            per_star_rub = e.cost_per_star_rub()
            per_star_ton = e.cost_per_star_ton()
            e._set(oid, S.PURCHASING, "gifts_started", purchase_started_at=time.time(), gift_cost=cost + sum(
                int(r["stars"]) for r in done), purchase_attempts=int(o["purchase_attempts"] or 0) + 1)
            text = ""
            tmpl = str(self.gcfg.get("gift_message") or "")
            if tmpl:
                try:
                    text = tmpl.format(order_id=oid, buyer=o["buyer_username"] or "")
                except (KeyError, ValueError, IndexError):
                    text = ""
            seq0 = len(sent)
            for i, g in enumerate(plan.gifts):
                now = time.time()
                cur = e.db.exec("INSERT INTO gift_sends(order_id,seq,gift_id,stars,convert_stars,recipient_id,state,"
                                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                (oid, seq0 + i, g.id, g.stars, g.convert_stars, user_id, "sending", now, now))
                sid = int(cur.lastrowid)
                try:
                    await self.tg.send_gift(user, g.id, text, bool(self.gcfg.get("hide_sender_name")))
                except GiftError as err:
                    if err.code in ("unknown", "network"):
                        ok = False
                        try:
                            ok = await self.tg.find_sent(user_id, g.stars, now)
                        except Exception:  # noqa: BLE001
                            ok = False
                        if not ok:
                            e.db.exec("UPDATE gift_sends SET state='unknown', error=?, updated_at=? WHERE id=?",
                                      (str(err)[:300], time.time(), sid))
                            e._set(oid, S.MANUAL, "gift_unknown", fail_reason="gift_unknown")
                            await e.notifier.notify("CRITICAL", "gift_unknown", f"#{oid}: связь оборвалась во время "
                                                    f"отправки подарка ({err}). Проверьте историю звёзд аккаунта: "
                                                    "если подарок ушёл — «Выдан вручную», если нет — «Одобрить».",
                                                    order_id=oid)
                            return
                    else:
                        e.db.exec("UPDATE gift_sends SET state='failed', error=?, updated_at=? WHERE id=?",
                                  (str(err)[:300], time.time(), sid))
                        if err.code == "no_stars":
                            e._set(oid, S.READY, "gift_no_stars")
                            await self.out_of_stars(e.db.get_order(oid), await self.refresh_balance() or 0, cost)
                            return
                        if err.code in BAD_RECIPIENT and i == 0 and not done:
                            e._set(oid, S.AWAITING_USERNAME, "gift_rejected")
                            await e.bad_username(e.db.get_order(oid), err.code, str(err))
                            return
                        if err.code == "flood":
                            e._set(oid, S.READY, "gift_flood", fail_reason="flood")
                            e.enqueue(oid, 120)
                            return
                        e._set(oid, S.MANUAL, "gift_failed", fail_reason=err.code)
                        await e.say(e.db.get_order(oid), "delay")
                        await e.notifier.notify("CRITICAL", "gift_failed", f"#{oid}: подарок не отправлен: {err}",
                                                order_id=oid)
                        return
                e.db.exec("UPDATE gift_sends SET state='sent', updated_at=? WHERE id=?", (time.time(), sid))
                if self.balance is not None:
                    self.balance = max(0, self.balance - g.stars)
                await asyncio.sleep(float(self.gcfg.get("pause_between_gifts") or 0))
            rows = e.db.q("SELECT * FROM gift_sends WHERE order_id=? AND state='sent'", (oid,))
            total_cost = sum(int(r["stars"]) for r in rows)
            value = sum(int(r["convert_stars"]) for r in rows)
            e._set(oid, gift_cost=total_cost, cost_rub=round(total_cost * per_star_rub, 2) if per_star_rub else None,
                   ton_amount=round(total_cost * per_star_ton, 6) if per_star_ton else None)
            await self.on_delivered(e.db.get_order(oid), rows, value)

    @staticmethod
    def summary_of(rows) -> str:
        counts: dict[int, int] = {}
        for r in rows:
            counts[int(r["stars"])] = counts.get(int(r["stars"]), 0) + 1
        return " + ".join(f"{n}×🎁{s}⭐" for s, n in sorted(counts.items(), reverse=True))

    async def on_delivered(self, o, rows, value: int) -> None:
        e = self.engine
        oid = o["order_id"]
        o = e._set(oid, S.DELIVERED, "delivered", delivered_at=time.time(), fail_reason="",
                   tx_hash="gifts:" + ",".join(str(r["id"]) for r in rows))
        await e.say(o, "delivered", gifts=self.summary_of(rows), value=value)
        await e.notifier.notify("INFO", "delivered", f"🎁 Заказ #{oid}: {self.summary_of(rows)} → @{o['tg_username']} "
                                f"({value} ⭐ при конвертации)", order_id=oid)
        self.balance_ts = 0
        if o["fp_status"] == "closed":
            await e.on_buyer_confirmed(e.db.get_order(oid))

    async def out_of_stars(self, o, balance: int, cost: int) -> None:
        e = self.engine
        oid = o["order_id"]
        e._set(oid, S.MANUAL, "out_of_stars", fail_reason="out_of_stars")
        await e.say(o, "delay")
        await e.notifier.notify("CRITICAL", "out_of_stars", f"Подарки: на аккаунте {balance} ⭐, для заказа #{oid} "
                                f"нужно {cost} ⭐. " + ("Бот пополнит звёзды через Fragment." if self.topup_on else
                                                         "Пополните звёзды аккаунта и нажмите «Одобрить»."),
                                order_id=oid, buttons=[("✔ Пополнил, отправить", f"approve {oid}")])
        await self.maybe_topup(need=cost - balance)

    # ------------------------------------------------------------------ баланс, наличие, пополнение
    async def refresh_balance(self, max_age: float = 30) -> int | None:
        if self.balance is not None and time.time() - self.balance_ts < max_age:
            return self.balance
        try:
            self.balance = await self.tg.stars_balance()
            self.balance_ts = time.time()
            self.db.kv_set("tg_stars", {"balance": self.balance, "ts": self.balance_ts})
            self.db.exec("INSERT INTO stars_history(ts,stars) VALUES(?,?)", (self.balance_ts, self.balance))
            self.status.update(ok=True, error="", ts=time.time())
        except GiftError as err:
            self.status.update(ok=False, error=str(err)[:300], ts=time.time())
            return None
        return self.balance

    def capacity(self, balance: int | None) -> int:
        """Сколько звёзд аккаунта свободно под новые заказы (минус обещанные активным заказам)."""
        return max(0, int(balance or 0) - self.db.gift_stars_committed())

    @property
    def topup_on(self) -> bool:
        t = self.gcfg.get("topup") or {}
        return bool(t.get("enabled")) and self.enabled

    async def watch(self) -> None:
        """Цикл раз в 2 минуты: аккаунт, баланс звёзд, наличие лотов «Подарком», автопополнение."""
        if not self.enabled:
            return
        try:
            me = await self.tg.whoami()
            self.account = {"user_id": me.user_id, "username": me.username, "name": me.name, "ts": time.time()}
            self.db.kv_set("tg_account", self.account)
        except GiftError as err:
            self.status.update(ok=False, error=str(err)[:300], ts=time.time())
            await self.engine.notifier.notify("CRITICAL" if err.code in ("auth", "config") else "WARNING", "tg_auth",
                                              f"Подарки: {err}", throttle_key="tg_auth")
            await self.stock_sync(0)
            return
        balance = await self.refresh_balance(max_age=0)
        if balance is None:
            return
        await self.catalog()
        self.engine.last_ok["tg_gifts"] = time.time()
        low = int(self.gcfg.get("low_stars_alert") or 0)
        if low and balance < low:
            await self.engine.notifier.notify("WARNING", "low_stars", f"На аккаунте для подарков {balance} ⭐ "
                                              f"(порог {low}).", throttle_key="low_stars")
        await self.maybe_topup()
        await self.stock_sync(balance)
        for o in self.db.orders_by_status(S.MANUAL):        # звёзды появились — заказы снова в работу
            if o["fail_reason"] == "out_of_stars" and o["delivery"] == "gift" and \
                    self.capacity(balance) + int(o["gift_cost"] or 0) >= int(o["gift_cost"] or o["stars"]):
                self.engine._set(o["order_id"], S.READY, "stars_restored", fail_reason="")
                self.engine.enqueue(o["order_id"])

    def topup_pending(self) -> Any:
        return self.db.one("SELECT * FROM orders WHERE channel='internal' AND status NOT IN ('completed','refunded',"
                           "'blocked') ORDER BY created_at DESC LIMIT 1")

    async def maybe_topup(self, need: int = 0) -> str:
        """Докупить звёзды на аккаунт продавца через Fragment, когда их меньше порога."""
        if not self.topup_on:
            return "автопополнение выключено"
        t = self.gcfg.get("topup") or {}
        balance = self.balance if self.balance is not None else 0
        free = self.capacity(balance)
        if need <= 0 and free >= int(t.get("min_stars") or 0):
            return "звёзд достаточно"
        if self.topup_pending() is not None:
            return "пополнение уже идёт"
        # звёзды от Fragment приходят не мгновенно: не покупаем снова, пока не прошла пауза после прошлого пополнения
        cooldown = float(t.get("cooldown_minutes", 20) or 0) * 60
        if time.time() - float(self.db.kv_get("topup_done_at", 0) or 0) < cooldown:
            return "пауза после прошлого пополнения — ждём, когда звёзды дойдут"
        day_cap = int(t.get("max_per_day") or 0)
        bought = int(self.db.scalar("SELECT SUM(stars) FROM orders WHERE channel='internal' AND created_at>=? AND "
                                    "status NOT IN ('refunded','blocked')", (time.time() - 86400,), 0) or 0)
        username = str(t.get("username") or self.account.get("username") or "").lstrip("@")
        if not username:
            await self.engine.notifier.notify("WARNING", "topup_no_username", "Автопополнение звёзд: у аккаунта нет "
                                              "@username — укажите его в «Подарки → Пополнение».",
                                              throttle_key="topup_no_username")
            return "нет username"
        amount = max(int(t.get("amount") or 1000), need + 50, 50)
        if day_cap and bought + amount > day_cap:
            await self.engine.notifier.notify("WARNING", "topup_day_cap", f"Автопополнение: за сутки уже куплено "
                                              f"{bought} ⭐, лимит {day_cap} ⭐ — жду или пополните вручную.",
                                              throttle_key="topup_day_cap")
            return "дневной лимит пополнений"
        return await self.create_topup(amount, username)

    async def create_topup(self, amount: int, username: str) -> str:
        e = self.engine
        import secrets
        oid = f"TOPUP-{time.strftime('%m%d%H%M')}-{secrets.token_hex(2).upper()}"
        e.db.create_order(order_id=oid, buyer_username="(пополнение аккаунта)", buyer_id=0, chat_id=None,
                          description=f"Пополнение звёзд аккаунта @{username}", quantity=1, stars_per_unit=amount,
                          stars=amount, price=0, currency="₽", fp_status="internal", channel="internal",
                          delivery="fragment", approved=1, lang="ru")
        e.db.add_event(oid, "created", stars=amount, topup=True)
        await e.notifier.notify("INFO", "stars_topup", f"⭐ Автопополнение: покупаю {amount} ⭐ на @{username} через "
                                "Fragment", order_id=oid)
        await e.accept_username(e.db.get_order(oid), username, source="autopilot")
        return f"создано пополнение {oid} на {amount} ⭐"

    async def stock_sync(self, balance: int | None) -> None:
        g = self.gcfg
        if not g.get("stock_sync", True) or self.engine.paused:
            return
        free = self.capacity(balance)
        extra = 0
        if self.topup_on and self.engine.fragment_ok:
            # звёзды можно докупить через Fragment: считаем и то, на что хватит TON кошелька
            w = self.engine.wallet_info or {}
            extra = max(0, int(w.get("capacity") or 0))
        for lot in self.lots:
            if not lot.get("offer_id"):
                continue
            stars = int(lot["stars"])
            cost = self.cost_for(stars)
            amount = max(0, min(int((free + extra) / cost) if cost else 0, int(g.get("max_display_amount") or 200)))
            key = f"gift_lot_amount:{lot['offer_id']}"
            if self.db.kv_get(key) == amount:
                continue
            active = amount > 0 or not g.get("deactivate_when_empty", True)
            try:
                await self.engine.fp.update_lot(int(lot["offer_id"]), int(self.cfg.funpay.node_id),
                                                amount=max(amount, 1 if active else 0), active=active)
                self.db.kv_set(key, amount)
                self.db.add_event("-", "gift_lot_stock", offer_id=lot["offer_id"], stars=stars, amount=amount,
                                  active=active)
            except Exception as err:  # noqa: BLE001
                await self.engine.notifier.notify("WARNING", "lot_update_failed", f"Лот подарков {lot['offer_id']}: "
                                                  f"{err}", throttle_key=f"lot:{lot['offer_id']}")

    async def recover(self) -> None:
        """После перезапуска: подарки «в полёте» — только продавцу, остальное — снова в очередь."""
        for o in self.db.q("SELECT * FROM orders WHERE delivery='gift' AND status=?", (S.PURCHASING.value,)):
            sending = self.db.q("SELECT * FROM gift_sends WHERE order_id=? AND state='sending'", (o["order_id"],))
            if sending:
                self.db.exec("UPDATE gift_sends SET state='unknown' WHERE order_id=? AND state='sending'",
                             (o["order_id"],))
                self.engine._set(o["order_id"], S.MANUAL, "gift_unknown", fail_reason="gift_unknown")
                await self.engine.notifier.notify("CRITICAL", "gift_unknown", f"#{o['order_id']}: бот перезапустился "
                                                  "во время отправки подарка — проверьте историю звёзд аккаунта.",
                                                  order_id=o["order_id"])
            else:
                self.engine._set(o["order_id"], S.READY, "recovered_unsent")

    async def admin_mark_delivered(self, o) -> None:
        rows = self.db.q("SELECT * FROM gift_sends WHERE order_id=?", (o["order_id"],))
        self.db.exec("UPDATE gift_sends SET state='sent' WHERE order_id=? AND state='unknown'", (o["order_id"],))
        return None if rows else None

    async def close(self) -> None:
        await self.tg.close()
