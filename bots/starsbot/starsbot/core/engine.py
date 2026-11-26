"""Движок бота: заказ FunPay → проверка @username на Fragment → оплата звёзд TON-транзакцией → закрытие сделки.

Главное правило денег: покупка звёзд необратима, поэтому
  • одновременно в сети может быть только одна наша транзакция (seqno кошелька идут по порядку);
  • перед отправкой попытка записывается в базу (seqno + valid_until), и после любого сбоя её исход
    сверяется с блокчейном: seqno вырос → транзакция исполнена; срок вышел, а seqno прежний → не
    исполнится никогда, можно повторить; всё остальное — только вручную;
  • в чате просим подтвердить заказ только после того, как транзакция подтверждена сетью (п. 2.1.2).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import socket
import re
import statistics
import time
from pathlib import Path
from typing import Any

from starsbot import evidence as EV
from starsbot.core import messages as M
from starsbot.core.models import ChatMessage, FunPayOrder, OrderStatus as S
from starsbot.core.risk import RiskEngine
from starsbot.core.rtba import RTBAEngine
from starsbot.core import strategy as strat
from starsbot.core.stars import looks_like_username_attempt, stars_from_text, username_from_params, \
    username_from_text
from starsbot.crm.db import Database
from starsbot.fragment.client import FragmentAuthError, FragmentError
from starsbot.funpay.client import FunPayAuthError, FunPayError
from starsbot.funpay.parsers import classify_system
from starsbot.notify.notifier import Notifier
from starsbot.premium.service import is_premium, months_text
from starsbot.ton.wallet import TonError, from_nano, to_nano

log = logging.getLogger("engine")


def is_gift(o) -> bool:
    """Заказ выдаётся подарками Telegram (а не звёздами через Fragment)."""
    return o is not None and "delivery" in o.keys() and o["delivery"] == "gift"

BAD_RECIPIENT = {"not_found", "cannot_receive", "bad_format", "blacklisted"}
RETRYABLE = {"network", "rate_limit", "layout", "fragment", "api"}


class Engine:
    def __init__(self, cfg, db: Database, funpay, fragment, wallet, notifier: Notifier, rates, coord,
                 analytics=None):
        self.cfg = cfg
        self.db = db
        self.fp = funpay
        self.fragment = fragment
        self.wallet = wallet
        self.notifier = notifier
        self.rates = rates
        self.coord = coord
        self.analytics = analytics
        self.econ = None                       # starsbot.economics.Economics (подключает Supervisor)
        self.channels: dict[str, Any] = {}     # другие площадки продаж, например «ozon» (starsbot.ozon.channel)
        self.gifts = None                      # starsbot.gifts.service.GiftService — выдача подарками Telegram
        self.nft = None                        # starsbot.nft_supply.NftSupplyManager — NFT-подарки для TG Gift
        self.premium = None                    # starsbot.premium.service.PremiumService — продажа Telegram Premium
        self.treasury = None                   # starsbot.treasury.service.Treasury — балансы и пополнение кошелька
        from starsbot.orbit import StarsOrbit
        self.orbit = StarsOrbit(self)          # Орбита центра: какие позиции сняты с продажи (starsbot/orbit.py)
        self.risk = RiskEngine(cfg.risk, db)
        self.rtba = RTBAEngine(cfg.rtba)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._chat_last_sent: dict[str, float] = {}
        self._chat_last_text: dict[str, tuple[str, float]] = {}   # чат → (последний автоответ, когда) — против повторов
        self._tasks: list[asyncio.Task] = []
        self._purchase_lock = asyncio.Lock()
        self._order_re = re.compile(cfg.funpay.order_match_regex)
        self._exclude_re = re.compile(cfg.funpay.order_exclude_regex) if cfg.funpay.order_exclude_regex else None
        self.started_at = time.time()
        self.last_ok: dict[str, float] = {}
        self.fragment_ok = False
        self.fragment_status: dict[str, Any] = db.kv_get("fragment_status") or {
            "session": "unknown", "kyc": "unknown", "checked_at": 0, "error": ""}
        self.wallet_info: dict[str, Any] = {"balance": None, "ts": 0}
        self.lease_id = f"{socket.gethostname()}:{os.getpid()}:{id(self):x}"

    # ================================================================== helpers
    @property
    def paused(self) -> bool:
        return bool(self.db.kv_get("paused", False))

    def set_paused(self, value: bool) -> None:
        self.db.kv_set("paused", bool(value))
        self.db.emit({"type": "state", "paused": bool(value)})

    @property
    def lots(self) -> list[dict]:
        return [l for l in (self.cfg.funpay.lots or []) if l.get("stars")]

    def orbit_closed(self, *keys: str) -> bool:
        """Позиция снята с орбиты в Центре (Пульт Орбиты): её лоты и товары держим выключенными."""
        return self.orbit is not None and self.orbit.blocked(*keys)

    def _denominations(self) -> list[int]:
        active = sorted({int(l["stars"]) for l in self.lots if l.get("offer_id")})
        return active or sorted({int(l["stars"]) for l in self.lots})

    def _order_vars(self, o) -> dict:
        name = o["tg_name"] or ""
        en = (o["lang"] or self.cfg.general.language) == "en"
        returning, last_hint = "", ""
        prev = self.db.one("SELECT tg_username FROM orders WHERE buyer_id=? AND order_id<>? AND delivered_at IS NOT NULL "
                           "ORDER BY delivered_at DESC LIMIT 1", (o["buyer_id"], o["order_id"])) if o["buyer_id"] else None
        if prev is not None:     # постоянный покупатель: тёплое приветствие и подсказка прошлого получателя
            returning = "Welcome back 🙌 " if en else "Рады видеть вас снова 🙌 "
            if prev["tg_username"]:
                last_hint = (f"Last time the stars went to @{prev['tg_username']} — if it's the same account, just "
                             "send it again.\n" if en else
                             f"Прошлый заказ вы получали на @{prev['tg_username']} — если аккаунт тот же, просто "
                             "пришлите его ещё раз.\n")
        gifts, value = "", o["stars"]
        if is_gift(o) and o["gift_plan"]:
            try:
                plan = json.loads(o["gift_plan"])
                from starsbot.gifts.service import GiftService
                gifts = GiftService.summary_of(plan)
                value = sum(int(g.get("convert") or 0) for g in plan)
            except (ValueError, TypeError, KeyError):
                pass
        months = int(o["premium_months"] or 0) if "premium_months" in o.keys() else 0
        if is_premium(o) and last_hint:
            last_hint = last_hint.replace("the stars went to", "the order went to")
        if is_premium(o) and months:
            what = f"Telegram Premium for {months_text(months, 'en')}" if en else \
                f"Telegram Premium на {months_text(months)}"
        else:
            what = f"{o['stars']} ⭐ Telegram Stars"
        return {"order_id": o["order_id"], "buyer": o["buyer_username"], "stars": o["stars"], "what": what,
                "gifts": gifts, "value": value, "months": months,
                "months_text": months_text(months, "en" if en else "ru") if months else "",
                "gift": self.gift_title(o), "qty": o["quantity"] or 1,
                "username": o["tg_username"] or "", "name_part": f" ({name})" if name else "",
                "denominations": ", ".join(str(d) for d in self._denominations()),
                "returning": returning, "last_hint": last_hint}

    def gift_title(self, o) -> str:
        """Название подарка заказа (магазин подарков TG Gift); у StarsBot пусто."""
        return ""

    def channel_of(self, o):
        """Канал продаж заказа, если это не FunPay (у FunPay-заказов channel = 'funpay' или пусто)."""
        name = o["channel"] if o is not None and "channel" in o.keys() else None
        return self.channels.get(name) if name and name != "funpay" else None

    def waits_code(self, o) -> bool:
        """Заказ площадки с кодами активации (Ozon, WB), код которого покупатель ещё не активировал: писать ему
        некуда, поэтому это не просрочка. У площадок с чатом (Avito) покупателю можно напомнить, как на FunPay."""
        ch = self.channel_of(o)
        return ch is not None and not getattr(ch, "buyer_chat", False) and getattr(ch, "code_based", True) \
            and not o["tg_username"]

    def message_variants(self, o) -> list[str]:
        """Суффиксы вариантов шаблона по типу заказа: для подарков — «…_gift», для Premium — «…_premium»."""
        if is_gift(o):
            return ["_gift"]
        if is_premium(o):
            return ["_premium"]
        return []

    async def say(self, o, key: str, **extra: Any) -> None:
        """Отправить шаблонное сообщение в чат заказа (для заказов подарками — вариант «…_gift», если он есть)."""
        for suffix in self.message_variants(o):
            if f"{key}{suffix}" in M.TEMPLATES.get(o["lang"] or "ru", M.TEMPLATES["ru"]):
                key = f"{key}{suffix}"
                break
        ch = self.channel_of(o)
        if ch is not None:
            await ch.say(o, key, {**self._order_vars(o), **extra})
            return
        await self.say_text(o, M.render(o["lang"] or self.cfg.general.language, key,
                                        **{**self._order_vars(o), **extra}))

    async def say_text(self, o, text: str, dedupe: bool = True) -> None:
        ch = self.channel_of(o)
        if ch is not None:
            await ch.say_text(o, text)
            return
        await self.send_chat(o["chat_id"], text, o["order_id"], dedupe=dedupe)

    REPEAT_WINDOW = 15 * 60          # тот же автоответ в тот же чат не чаще раза в 15 минут

    async def send_chat(self, chat_id: str | None, text: str, order_id: str | None = None,
                        dedupe: bool = True) -> None:
        if not chat_id:
            await self.notifier.notify("WARNING", "no_chat", f"У заказа #{order_id} не найден чат", order_id=order_id)
            return
        prev = self._chat_last_text.get(str(chat_id))
        if dedupe and prev and prev[0] == text and time.time() - prev[1] < self.REPEAT_WINDOW:
            log.info("чат %s: тот же автоответ уже отправлен %.0f с назад — не повторяем", chat_id, time.time() - prev[1])
            return
        gap = float(self.cfg.deal.message_rate_limit_seconds) - (time.time() - self._chat_last_sent.get(chat_id, 0))
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            await self.fp.send_message(chat_id, text)
            self._chat_last_sent[chat_id] = time.time()
            self._chat_last_text[str(chat_id)] = (text, time.time())
            self.db.log_message(chat_id, order_id, "out", text)
        except FunPayError as e:
            await self.notifier.notify("WARNING", "send_message_failed",
                                       f"Не удалось написать в чат {'заказа #' + order_id if order_id else chat_id}: {e}",
                                       order_id=order_id)

    def _set(self, order_id: str, status: S | None = None, event: str | None = None, **fields: Any):
        if status is not None:
            fields["status"] = status
        if fields:
            self.db.update_order(order_id, **fields)
        if event:
            self.db.add_event(order_id, event, status=status.value if status else None,
                              **{k: v for k, v in fields.items() if k != "status"})
        o = self.db.get_order(order_id)
        if o is not None and status is not None and status.is_final:
            self.coord.release_chat(o["chat_id"], order_id)
        return o

    def enqueue(self, order_id: str, delay: float = 0) -> None:
        if delay > 0:
            asyncio.get_running_loop().call_later(delay, self.enqueue, order_id)
            return
        if order_id not in self._queued:
            self._queued.add(order_id)
            self.queue.put_nowait(order_id)

    def lot_for(self, description: str) -> tuple[dict, int] | None:
        """Лот по описанию заказа («50 звёзд, По username») → (лот, звёзд в единице лота)."""
        stars = stars_from_text(description)
        if not stars:
            return None
        for lot in self.lots:
            if int(lot["stars"]) == stars:
                return lot, stars
        return None

    def is_our_order(self, description: str) -> bool:
        d = description or ""
        if not self._order_re.search(d) or (self._exclude_re and self._exclude_re.search(d)):
            return False
        found = self.lot_for(d)
        if not found:
            return False
        lot, _ = found
        configured = [l for l in self.lots if l.get("offer_id")]
        return not configured or bool(lot.get("offer_id"))

    def is_gift_order(self, description: str) -> bool:
        return self.gifts is not None and self.gifts.is_gift_order(description)

    def is_premium_order(self, description: str) -> bool:
        return self.premium is not None and self.premium.is_order(description)

    def accepts_sale(self, so: FunPayOrder) -> bool:
        """Брать ли новый оплаченный заказ из списка продаж. Заказы чужого раздела (например, «Подарки» —
        их ведёт бот TG Gift на том же аккаунте) не трогаем, даже если в описании есть подходящее число звёзд."""
        rx = self.cfg.funpay.get("foreign_subcategory_regex")
        if rx and so.subcategory and re.search(rx, so.subcategory):
            return False
        return self.is_our_order(so.description) or self.is_gift_order(so.description) \
            or self.is_premium_order(so.description)

    def _greeted(self, oid: str) -> bool:
        return bool(self.db.scalar("SELECT COUNT(*) FROM order_events WHERE order_id=? AND kind IN "
                                   "('greeting_sent','greeting_with_username')", (oid,)))

    # ================================================================== заказы
    async def sync_orders(self) -> None:
        sales = await self.fp.get_sales()
        first_run = not self.db.kv_get("orders_initialized", False)
        for so in sales:
            row = self.db.get_order(so.order_id)
            if row is None:
                if so.status != "paid" or not self.accepts_sale(so):
                    continue
                if not self.coord.claim_order(so.order_id):
                    log.info("заказ %s уже обрабатывает другой бот (%s)", so.order_id,
                             self.coord.order_owner(so.order_id))
                    continue
                await self.on_new_order(so, imported=first_run)
            elif row["fp_status"] != so.status:
                await self.on_fp_status_change(row, so.status)
        if first_run:
            self.db.kv_set("orders_initialized", True)
        self.last_ok["funpay_orders"] = time.time()

    async def on_new_order(self, so: FunPayOrder, imported: bool = False) -> None:
        try:
            page = await self.fp.get_order(so.order_id)
        except FunPayError as e:
            log.warning("страница заказа %s недоступна: %s", so.order_id, e)
            return
        params_text = " ".join(page.params.values())
        premium = self.is_premium_order(so.description) or self.is_premium_order(params_text)
        gift = not premium and (self.is_gift_order(so.description) or self.is_gift_order(params_text))
        if premium:
            found = self.premium.lot_for(so.description) or self.premium.lot_for(params_text)
        elif gift:
            found = self.gifts.lot_for(so.description) or self.gifts.lot_for(" ".join(page.params.values()))
        else:
            found = self.lot_for(so.description) or self.lot_for(" ".join(page.params.values()))
        if not found:
            return
        lot, per_unit = found
        qty = page.quantity or so.quantity or 1
        months = per_unit if premium else 0
        stars = 0 if premium else qty * per_unit
        buyer_id = so.buyer_id or page.buyer_id or 0
        tg = username_from_params(page.params)
        self.db.create_order(order_id=so.order_id, buyer_username=so.buyer_username, buyer_id=buyer_id,
                             chat_id=page.chat_id or so.chat_id, description=so.description, quantity=qty,
                             stars_per_unit=0 if premium else per_unit, stars=stars, price=so.price,
                             currency=so.currency, fp_status="paid", lot_offer_id=str(lot.get("offer_id", "")),
                             lang=self.cfg.general.language,
                             delivery="premium" if premium else "gift" if gift else "fragment",
                             premium_months=months or None)
        self.db.touch_customer(buyer_id, so.buyer_username, new_order_total=so.price, stars=stars)
        self.db.add_event(so.order_id, "created", price=so.price, stars=stars, tg_from_order=tg or "",
                          imported=imported, params=dict(page.params or {}), **({"months": months} if premium else {}))
        self.coord.claim_chat(page.chat_id or so.chat_id, so.order_id)
        o = self.db.get_order(so.order_id)
        if imported:
            # заказы, оплаченные до первого запуска бота, — только продавцу: их могли уже выдать вручную
            self._set(so.order_id, S.MANUAL, "imported_on_first_run", tg_username=tg or None)
            await self.notifier.notify("WARNING", "imported_order",
                                       f"Заказ #{so.order_id} оплачен до запуска бота — проверьте вручную "
                                       f"(/take {so.order_id} — передать боту).", order_id=so.order_id)
            return
        what = f"Telegram Premium {months_text(months)}" if premium else \
            f"{stars} ⭐{' подарками' if gift else ''}"
        await self.notifier.notify("INFO", "new_order", f"🛒 Новый заказ #{so.order_id}: {what}, "
                                   f"{so.price:g} {so.currency}, покупатель {so.buyer_username}"
                                   + (f", получатель @{tg}" if tg else ""), order_id=so.order_id)
        if premium and qty > 1:
            # один подарок Premium на один аккаунт за раз: несколько штук в одном заказе — решает продавец
            self._set(so.order_id, S.MANUAL, "premium_multi", tg_username=tg or None, fail_reason="premium_qty")
            await self.say(o, "call_seller")
            await self.notifier.notify("WARNING", "premium_multi", f"#{so.order_id}: {qty} × Premium "
                                       f"{months_text(months)} в одном заказе — выдайте вручную или уточните у "
                                       "покупателя получателей.", order_id=so.order_id)
            return
        if self.cfg.risk.enabled:
            risk = self.risk.assess(o, tg_username=tg)
            if risk.decision == "block":
                self._set(so.order_id, S.BLOCKED, "risk_block", risk_score=risk.score,
                          risk_reasons=json.dumps(risk.reasons, ensure_ascii=False))
                await self.say(o, "call_seller")
                await self.notifier.notify("CRITICAL", "risk_block", f"Заказ #{so.order_id} заблокирован "
                                           f"риск-системой ({risk.score}): " + "; ".join(risk.reasons),
                                           order_id=so.order_id)
                return
        if self.paused:
            self._set(so.order_id, S.AWAITING_USERNAME, "created_while_paused", tg_username=tg or None)
            return
        if tg:
            self._set(so.order_id, S.AWAITING_USERNAME, username_source="order")
            await self.accept_username(self.db.get_order(so.order_id), tg, source="order")
        else:
            await self.say(o, "greeting_need_username")
            self._set(so.order_id, S.AWAITING_USERNAME, "greeting_sent")

    async def on_fp_status_change(self, o, fp_status: str) -> None:
        oid = o["order_id"]
        self.db.update_order(oid, fp_status=fp_status)
        self.db.add_event(oid, "funpay_status", fp_status=fp_status)
        if fp_status == "closed":
            await self.on_buyer_confirmed(self.db.get_order(oid))
        elif fp_status == "refunded":
            await self.on_refund(self.db.get_order(oid))

    async def on_buyer_confirmed(self, o) -> None:
        oid = o["order_id"]
        status = S(o["status"])
        if status == S.DELIVERED:
            self._set(oid, S.COMPLETED, "completed", completed_at=time.time())
            self.db.customer_incr(o["buyer_id"], "completed_count")
            await self.notifier.notify("INFO", "order_completed", f"✅ Заказ #{oid} подтверждён покупателем",
                                       order_id=oid)
            asyncio.create_task(self._fetch_review_later(oid))
        elif status.is_active:
            # Покупатель подтвердил ДО получения звёзд — выдачу продолжаем, это наша обязанность.
            self.db.add_event(oid, "confirmed_before_delivery")
            await self.notifier.notify("WARNING", "confirmed_before_delivery",
                                       f"Заказ #{oid} подтверждён до выдачи звёзд — бот продолжит выдачу.",
                                       order_id=oid)

    async def _fetch_review_later(self, oid: str, delay: float = 600) -> None:
        await asyncio.sleep(delay)
        try:
            page = await self.fp.get_order(oid)
        except FunPayError:
            return
        if page.review_stars:
            self.db.update_order(oid, review_stars=page.review_stars)
            if page.review_stars <= 3:
                await self.notifier.notify("WARNING", "bad_review", f"Отзыв {page.review_stars}★ по заказу #{oid}: "
                                           f"{page.review_text[:300]}", order_id=oid)
            elif self.cfg.deal.get("review_reply", True) and not page.review_answered:
                await self.reply_review(oid)

    async def reply_review(self, oid: str) -> None:
        """Ответ под хорошим отзывом: один раз на заказ, на плохие отвечает человек."""
        o = self.db.get_order(oid)
        if not o or self.db.kv_get(f"review_replied:{oid}"):
            return
        text = M.render(o["lang"] or self.cfg.general.language,
                        "review_reply_premium" if is_premium(o) else "review_reply", **self._order_vars(o))
        try:
            await self.fp.reply_review(oid, text)
        except FunPayError as e:
            log.warning("ответ на отзыв %s: %s", oid, e)
            return
        self.db.kv_set(f"review_replied:{oid}", time.time())
        self.db.add_event(oid, "review_replied", text=text)

    async def on_refund(self, o) -> None:
        oid = o["order_id"]
        status = S(o["status"])
        paid = status in (S.DELIVERED, S.COMPLETED) or status.money_in_flight
        self._set(oid, S.REFUNDED, "refunded", fail_reason=o["fail_reason"] or "refund")
        self.db.customer_incr(o["buyer_id"], "refunded_count")
        await self.say(self.db.get_order(oid), "refunded")
        level = "CRITICAL" if paid else "WARNING"
        await self.notifier.notify(level, "refund", f"Возврат по заказу #{oid}" +
                                   (" — НО ЗВЁЗДЫ УЖЕ ОПЛАЧЕНЫ на Fragment! Проверьте арбитраж." if paid else ""),
                                   order_id=oid)

    # ================================================================== username
    async def accept_username(self, o, username: str, *, source: str) -> None:
        """Проверяет username на Fragment и ставит заказ в очередь покупки."""
        oid = o["order_id"]
        o = self._set(oid, event="username_received", tg_username=username, username_source=source,
                      username_at=time.time())
        self.db.customer_add_tg(o["buyer_id"], username)
        if username.lower() in {str(x).lstrip("@").lower() for x in (self.cfg.risk.blacklist_usernames or [])}:
            await self.bad_username(o, "blacklisted")
            return
        if is_gift(o):
            if self.gifts is None:
                self._set(oid, S.MANUAL, "gifts_disabled", fail_reason="gifts_disabled")
                await self.notifier.notify("WARNING", "gifts_disabled", f"#{oid}: заказ подарками, но режим подарков "
                                           "выключен", order_id=oid)
                return
            await self.gifts.accept_username(o, username)
            return
        try:
            if is_premium(o):
                rec = await self.fragment.search_premium_recipient(username, int(o["premium_months"] or 0))
            else:
                rec = await self.fragment.search_recipient(username, o["stars"])
        except FragmentAuthError as e:
            await self.fragment_auth_problem(e)
            self._set(oid, event="username_check_deferred", fail_reason="fragment_auth")
            if not self._greeted(oid):
                await self.say(o, "greeting_with_username")
                self.db.add_event(oid, "greeting_with_username")
            return
        except FragmentError as e:
            if e.code in BAD_RECIPIENT:
                await self.bad_username(o, e.code, str(e))
            else:
                self._set(oid, event="username_check_deferred", fail_reason=e.code)
                await self.notifier.notify("WARNING", "fragment_check_failed", f"#{oid}: проверка @{username} не "
                                           f"удалась ({e}), повторю автоматически", order_id=oid)
                if not self._greeted(oid):
                    await self.say(o, "greeting_with_username")
                    self.db.add_event(oid, "greeting_with_username")
            return
        o = self._set(oid, event="username_ok", tg_username=rec.username, tg_recipient=rec.recipient,
                      tg_name=rec.name, fail_reason="")
        if self.needs_username_confirm(o, source):
            await self.ask_username_confirm(o)
            return
        if self._greeted(oid):
            await self.say(o, "username_ok")
        else:
            await self.say(o, "greeting_with_username")
            self.db.add_event(oid, "greeting_with_username")
        await self._after_username_ok(o)

    async def _after_username_ok(self, o) -> None:
        oid = o["order_id"]
        if self.cfg.risk.enabled and not o["approved"]:
            risk = self.risk.assess(o, tg_username=o["tg_username"])
            o = self._set(oid, risk_score=risk.score, risk_reasons=json.dumps(risk.reasons, ensure_ascii=False))
            if risk.decision == "block":
                self._set(oid, S.BLOCKED, "risk_block")
                await self.say(o, "call_seller")
                await self.notifier.notify("CRITICAL", "risk_block", f"Заказ #{oid} заблокирован ({risk.score}): "
                                           + "; ".join(risk.reasons), order_id=oid)
                return
            if risk.decision == "review":
                self._set(oid, S.PENDING_APPROVAL, "risk_review")
                await self.say(o, "awaiting_approval")
                await self.notifier.notify("WARNING", "risk_review", f"Заказ #{oid} требует проверки (риск "
                                           f"{risk.score}): " + "; ".join(risk.reasons) +
                                           f"\n/approve {oid} — выдать, /reject {oid} — отклонить", order_id=oid,
                                           buttons=[("✔ Выдать", f"approve {oid}"), ("✋ В ручную", f"reject {oid}")])
                return
        self._set(oid, S.READY, "ready")
        self.enqueue(oid)

    # ------------------------------------------------------------------ подтверждение получателя
    def needs_username_confirm(self, o, source: str = "") -> bool:
        """Спрашивать ли «Да» на получателя до выдачи (доказательство для арбитража FunPay)."""
        d = self.cfg.deal
        mode = str(d.get("confirm_username") or "off").lower()
        if mode == "off" or self.channel_of(o) is not None or is_gift(o) or o["approved"] or \
                str(source).startswith("admin"):
            return False
        oid, user = o["order_id"], str(o["tg_username"] or "").lower()
        if user and self.db.kv_get(f"ucfm_ok:{oid}") == user:
            return False                         # этот получатель по заказу уже подтверждён
        if mode == "always":
            return True
        if o["buyer_id"]:
            if self.db.scalar("SELECT COUNT(*) FROM orders WHERE buyer_id=? AND order_id<>? AND lower(tg_username)=? "
                              "AND (fp_status='closed' OR status='completed')", (o["buyer_id"], oid, user), 0):
                return False                     # этот аккаунт покупатель уже получал и подтверждал заказ
            new = not self.db.scalar("SELECT COUNT(*) FROM orders WHERE buyer_id=? AND order_id<>? AND "
                                     "delivered_at IS NOT NULL", (o["buyer_id"], oid), 0)
        else:
            new = True
        big = is_premium(o) or int(o["stars"] or 0) >= int(d.get("confirm_username_min_stars") or 1000)
        return new or big

    def _confirm_vars(self, o) -> dict:
        d = self.cfg.deal
        mins = int(float(d.get("confirm_username_timeout_minutes") or 15))
        en = (o["lang"] or "ru") == "en"
        if str(d.get("confirm_username_timeout_action") or "deliver") == "deliver":
            line = (f"\nIf there is no reply, I'll send to @{o['tg_username']} in {mins} min." if en else
                    f"\nБез ответа отправлю на @{o['tg_username']} через {mins} мин.")
        else:
            line = ""
        return {"timeout_line": line}

    async def ask_username_confirm(self, o) -> None:
        oid = o["order_id"]
        self.db.kv_set(f"ucfm:{oid}", time.time())
        greeted = self._greeted(oid)
        o = self._set(oid, S.AWAITING_USERNAME, "username_confirm_asked", reminded=1)
        if greeted:
            await self.say(o, "confirm_username", **self._confirm_vars(o))
        else:
            await self.say(o, "greeting_confirm_username", **self._confirm_vars(o))
            self.db.add_event(oid, "greeting_with_username")

    def confirm_pending(self, o) -> bool:
        return bool(o is not None and o["tg_recipient"] and self.db.kv_get(f"ucfm:{o['order_id']}", 0))

    async def username_confirmed(self, o, kind: str = "username_confirmed", **data: Any) -> None:
        oid = o["order_id"]
        self.db.kv_set(f"ucfm:{oid}", 0)
        self.db.kv_set(f"ucfm_ok:{oid}", str(o["tg_username"] or "").lower())
        self.db.add_event(oid, kind, username=o["tg_username"], **data)
        if kind == "username_confirmed":
            await self.say(o, "username_confirm_thanks")
        await self._after_username_ok(self.db.get_order(oid))

    async def on_confirm_reply(self, o, text: str, username: str | None) -> None:
        oid = o["order_id"]
        if username and username.lower() != str(o["tg_username"] or "").lower():
            self.db.kv_set(f"ucfm:{oid}", 0)
            self._set(oid, event="username_changed", old=o["tg_username"], new=username)
            await self.accept_username(self.db.get_order(oid), username, source="chat_change")
            return
        if M.is_no(text):
            self.db.kv_set(f"ucfm:{oid}", 0)
            self._set(oid, event="username_rejected", tg_username=None, tg_recipient=None, tg_name=None)
            await self.say(o, "username_confirm_no")
            return
        if username or M.is_yes(text):
            await self.username_confirmed(o, text=str(text or "")[:200])
            return
        if M.detect_faq(text):
            await self.answer_faq(o, text)
            return
        await self.say(o, "username_confirm_hint")

    async def bad_username(self, o, code: str, detail: str = "") -> None:
        oid = o["order_id"]
        attempts = int(o["username_attempts"] or 0) + 1
        max_att = int(self.cfg.deal.max_username_attempts)
        why = M.reason(o["lang"], code, detail)
        o = self._set(oid, S.AWAITING_USERNAME, "bad_username", username_attempts=attempts, fail_reason=code,
                      tg_recipient=None)
        if attempts >= max_att:
            await self.say(o, "bad_username_final", reason=why)
            self._set(oid, S.MANUAL, "bad_username_limit")
            await self.notifier.notify("WARNING", "bad_username_limit", f"Заказ #{oid}: покупатель {attempts} раз "
                                       f"прислал неверный username ({why}). Нужна ручная обработка.", order_id=oid)
            if self.cfg.deal.auto_refund_bad_recipient:
                await self.admin_refund(oid, "не удалось получить username (авто)")
        else:
            await self.say(o, "bad_username", reason=why, attempt=attempts, max_attempts=max_att)

    # ================================================================== чат
    async def process_chat(self, chat_id: str, msgs: list[ChatMessage]) -> None:
        for msg in sorted(msgs, key=lambda m: m.message_id):
            self.db.set_last_seen_message(chat_id, msg.message_id)
            if msg.author_id == self.fp.user_id:
                continue
            if (not msg.is_system or (msg.author_id and not classify_system(msg.text))) and \
                    await self.maybe_dispute(chat_id, msg):
                continue
            if msg.is_system:
                await self.on_system_message(chat_id, msg)
                continue
            await self.on_buyer_message(chat_id, msg)

    # ------------------------------------------------------------------ споры и доказательства
    def evidence_cfg(self) -> dict:
        return self.cfg.get("evidence") or {}

    def evidence_folder(self) -> Path:
        f = Path(str(self.evidence_cfg().get("folder") or "data/evidence"))
        if f.is_absolute():
            return f
        ap = getattr(self, "autopilot", None)
        if ap is not None:
            return Path(ap.home) / f
        return (Path(self.db.path).parent.parent if self.db.path != ":memory:" else Path(".")) / f

    def _ev_kw(self) -> dict:
        return {"wallet_address": str(getattr(self.wallet, "friendly_address", "") or "").replace("—", ""),
                "seller": str(getattr(self.fp, "username", "") or "")}

    def evidence(self, o) -> dict:
        return EV.collect(self.db, o, **self._ev_kw())

    def save_evidence(self, o) -> str:
        if not self.evidence_cfg().get("enabled", True):
            return ""
        try:
            path = EV.save_snapshot(self.db, o, self.evidence_folder(), **self._ev_kw())
        except Exception as e:  # noqa: BLE001 — доказательства не должны ломать выдачу
            log.warning("снимок доказательств #%s: %s", o["order_id"], e)
            return ""
        self.db.add_event(o["order_id"], "evidence_saved", path=str(path))
        return str(path)

    def support_pack(self, orders) -> str:
        orders = [o for o in orders if o is not None]
        if not orders:
            return ""
        path = EV.make_pack(self.db, orders, self.evidence_folder(), **self._ev_kw())
        for o in orders:
            self.db.add_event(o["order_id"], "support_pack", path=str(path))
        return str(path)

    def dispute_order(self, chat_id: str, msg: ChatMessage):
        """Заказ, о котором пишет арбитраж/покупатель: по номеру в тексте, иначе последний заказ этого чата."""
        ids = re.findall(r"#([A-Z0-9]{8})\b", msg.text or "")
        for i in ids:
            o = self.db.get_order(i)
            if o is not None:
                return o
        if ids:
            return None                          # речь о чужом заказе (другой бот аккаунта или ручная продажа)
        o = self.db.active_order_for_chat(chat_id)
        if o is None and self.coord.chat_is_foreign(chat_id):
            return None
        return o or self.db.last_order_for_chat(chat_id)

    async def maybe_dispute(self, chat_id: str, msg: ChatMessage) -> bool:
        """Сообщение арбитража/поддержки FunPay или требование доказательств — не отвечаем ему как покупателю."""
        o = self.dispute_order(chat_id, msg)
        if o is None:
            return False
        staff = bool(o["buyer_id"] and msg.author_id and msg.author_id != o["buyer_id"])
        delivered = bool(o["delivered_at"]) and S(o["status"]) in (S.DELIVERED, S.COMPLETED)
        if not staff and not (delivered and EV.STAFF_RE.search(msg.text or "")):
            return False
        await self.on_dispute_message(o, msg, staff=staff, delivered=delivered)
        return True

    async def on_dispute_message(self, o, msg: ChatMessage, *, staff: bool, delivered: bool) -> None:
        oid = o["order_id"]
        self.db.log_message(o["chat_id"], oid, "staff" if staff else "in", msg.text)
        self.db.add_event(oid, "arbitration", author=msg.author_name, author_id=msg.author_id, staff=staff,
                          text=str(msg.text or "")[:500])
        if not o["confirm_reminded"]:
            self.db.update_order(oid, confirm_reminded=1)    # во время спора «подтвердите заказ» не пишем
        pack = ""
        try:
            pack = self.support_pack([self.db.get_order(oid)])
        except Exception as e:  # noqa: BLE001
            log.warning("пакет доказательств #%s: %s", oid, e)
        posted = False
        if delivered and self.evidence_cfg().get("auto_reply_arbitration", True) and not self.paused:
            posted = await self.post_evidence(oid)
        who = "Арбитраж/поддержка FunPay" if staff else f"Покупатель {o['buyer_username']}"
        await self.notifier.notify(
            "CRITICAL", "arbitration",
            f"⚖️ #{oid}: {who} требует доказательства или возврат"
            + (" — заказ ещё НЕ выдан, ответьте сами." if not delivered else ".")
            + (" Доказательства уже отправлены в чат заказа." if posted else "")
            + (f"\nПакет для поддержки: {pack}" if pack else "")
            + "\nВозврат не делайте без проверки: звёзды, оплаченные на Fragment, вернуть нельзя."
            + f"\n«{str(msg.text or '')[:300]}»", order_id=oid,
            buttons=[("📄 Доказательства в чат", f"evidence {oid}")])

    async def post_evidence(self, oid: str, force: bool = False) -> bool:
        """Отправить доказательства в чат заказа (не чаще раза в 30 мин, если не force)."""
        o = self.db.get_order(oid)
        if o is None or not o["delivered_at"]:
            return False
        key = f"evidence_posted:{oid}"
        if not force and time.time() - float(self.db.kv_get(key, 0) or 0) < 1800:
            return False
        self.db.kv_set(key, time.time())
        await self.say_text(o, EV.arbitration_message(self.evidence(o)), dedupe=False)
        self.db.add_event(oid, "evidence_posted")
        return True

    async def on_system_message(self, chat_id: str, msg: ChatMessage) -> None:
        kind = classify_system(msg.text)
        if not kind:
            return
        what, oid = kind
        if what == "paid" and not self.db.order_exists(oid):
            await self.sync_orders()
        o = self.db.get_order(oid)
        if not o:
            return
        if what == "confirmed" and o["fp_status"] != "closed":
            self.db.update_order(oid, fp_status="closed")
            await self.on_buyer_confirmed(self.db.get_order(oid))
        elif what == "refund" and o["status"] != S.REFUNDED.value:
            self.db.update_order(oid, fp_status="refunded")
            await self.on_refund(self.db.get_order(oid))
        elif what == "review":
            asyncio.create_task(self._fetch_review_later(oid, delay=5))

    async def on_buyer_message(self, chat_id: str, msg: ChatMessage) -> None:
        o = self.db.active_order_for_chat(chat_id)
        if o is None and username_from_text(msg.text, allow_bare=False):
            # username пришёл раньше, чем мы увидели оплату — синхронизируем заказы
            await self.sync_orders()
            o = self.db.active_order_for_chat(chat_id)
        if o is None:
            await self.on_chat_without_order(chat_id, msg)
            return
        self.db.log_message(chat_id, o["order_id"], "in", msg.text)
        oid = o["order_id"]
        if o["buyer_id"] and msg.author_id and msg.author_id != o["buyer_id"]:
            return
        self.db.update_order(oid, last_buyer_msg_at=time.time())
        if o["lang"] == self.cfg.general.language:
            lang = M.detect_lang(msg.text, o["lang"])
            if lang != o["lang"]:
                self.db.update_order(oid, lang=lang)
                o = self.db.get_order(oid)
        if self.paused:
            await self.notifier.notify("INFO", "paused_message", f"(бот на паузе) #{oid} {msg.author_name}: "
                                       f"{msg.text[:300]}", order_id=oid)
            return
        status = S(o["status"])
        cmd = M.detect_command(msg.text)
        if cmd == "status":
            await self.say(o, "status", status_text=M.status_text(o["lang"], status.value))
            return
        if cmd == "help":
            await self.say(o, "help")
            return
        if cmd == "seller":
            await self.say(o, "call_seller")
            await self.notifier.notify("WARNING", "buyer_calls_seller", f"Покупатель {o['buyer_username']} зовёт "
                                       f"продавца в заказе #{oid}: {msg.text[:300]}", order_id=oid)
            return
        if M.is_off_platform_request(msg.text):
            await self.say(o, "off_platform")
            return
        username = username_from_text(msg.text)
        if status == S.AWAITING_USERNAME and self.confirm_pending(o):
            await self.on_confirm_reply(o, msg.text, username)
            return
        if status == S.AWAITING_USERNAME:
            if username:
                await self.accept_username(o, username, source="chat")
            elif looks_like_username_attempt(msg.text):
                await self.bad_username(o, "bad_format")
            elif M.detect_faq(msg.text):
                await self.answer_faq(o, msg.text)
            else:
                await self.say(o, "unknown")
                if M.is_question(msg.text):
                    await self.notifier.notify("INFO", "buyer_question", f"Вопрос в заказе #{oid}: {msg.text[:300]}",
                                               order_id=oid)
            return
        if status in (S.READY, S.PENDING_APPROVAL) and username and \
                username.lower() != (o["tg_username"] or "").lower():
            async with self._purchase_lock:          # покупка не начнётся, пока меняем получателя
                o = self.db.get_order(oid)
                if S(o["status"]) in (S.READY, S.PENDING_APPROVAL):
                    self._set(oid, S.AWAITING_USERNAME, "username_changed", old=o["tg_username"], new=username)
                    await self.accept_username(self.db.get_order(oid), username, source="chat_change")
                    return
            o = self.db.get_order(oid)
            status = S(o["status"])
        if username and status in (S.PURCHASING, S.SENT) and username.lower() != (o["tg_username"] or "").lower():
            await self.say(o, "change_username_late")
            await self.notifier.notify("WARNING", "late_username_change", f"#{oid}: покупатель прислал другой "
                                       f"username @{username} уже после оплаты на @{o['tg_username']}", order_id=oid)
            return
        if M.detect_faq(msg.text):
            await self.answer_faq(o, msg.text)
            return
        if M.is_question(msg.text) or username:
            await self.say(o, "status", status_text=M.status_text(o["lang"], status.value))
        await self.notifier.notify("INFO", "buyer_message", f"💬 #{oid} {msg.author_name}: {msg.text[:300]}",
                                   order_id=oid)

    async def answer_faq(self, o, text: str) -> None:
        key = M.detect_faq(text)
        hint = ""
        if key in ("faq_not_arrived", "faq_refund") and o["delivered_at"] and \
                S(o["status"]) in (S.DELIVERED, S.COMPLETED):
            # «не пришло» после выдачи — частый развод: отвечаем доказательством и готовим файл для поддержки
            ref = o["fragment_ref"] if "fragment_ref" in o.keys() else ""
            when = time.strftime("%d.%m %H:%M", time.localtime(float(o["delivered_at"])))
            await self.say(o, "faq_not_arrived_done", delivered_at=when,
                           ref_part=f" (платёж Fragment {ref})" if ref else "")
            support = ""
            ap = getattr(self, "autopilot", None)
            if ap is not None:
                try:
                    support = " Файл для поддержки: " + Path(ap.make_support_file([o])).name
                    pack = self.support_pack([o])
                    support += f"; пакет доказательств: {Path(pack).name}" if pack else ""
                except Exception as e:  # noqa: BLE001
                    log.warning("файл для поддержки #%s: %s", o["order_id"], e)
            await self.notifier.notify("WARNING", "buyer_claims_not_received",
                                       f"#{o['order_id']}: покупатель {o['buyer_username']} пишет, что не получил / "
                                       f"просит возврат, хотя звёзды отправлены {when} на @{o['tg_username']}"
                                       f"{' ' + ref if ref else ''}. Возможен развод — возврат не делайте без "
                                       f"проверки.{support}\n«{text[:200]}»", order_id=o["order_id"])
            return
        if key in ("faq_time", "faq_not_arrived"):
            hint = M.render(o["lang"], "status", **self._order_vars(o),
                            status_text=M.status_text(o["lang"], o["status"]))
        await self.say(o, key, status_hint=hint)
        if key in ("faq_not_arrived", "faq_refund"):      # п. 2.1.4: такие вопросы продавец видит сразу
            what = "звёзды не пришли" if key == "faq_not_arrived" else "просит возврат/отмену"
            await self.notifier.notify("WARNING", f"buyer_{key[4:]}", f"#{o['order_id']} ({o['status']}): покупатель "
                                       f"{o['buyer_username']} — {what}: {text[:300]}", order_id=o["order_id"])

    async def on_chat_without_order(self, chat_id: str, msg: ChatMessage) -> None:
        """Сообщение в чате, где у нас нет активного заказа (пресейл или вопрос после выдачи)."""
        if self.coord.chat_is_foreign(chat_id):
            return                               # чат ведёт другой бот аккаунта (например, MannBot)
        last = self.db.last_order_for_chat(chat_id)
        if last is not None:
            self.db.log_message(chat_id, last["order_id"], "in", msg.text)
            st = S(last["status"])
            since = time.time() - (last["delivered_at"] or 0)
            faq = M.detect_faq(msg.text)
            replied = True
            if M.is_off_platform_request(msg.text):
                await self.say(last, "off_platform")
            elif st in (S.DELIVERED, S.COMPLETED) and since < 7 * 86400 and \
                    (faq or (M.is_question(msg.text) and (st == S.DELIVERED or since < 3 * 86400))):
                await self.reply_after_delivery(last, msg.text, faq)
            elif st == S.DELIVERED or (st == S.COMPLETED and since < 86400 and not self.presale_topic(msg.text)
                                       and not self.presale_hello(msg.text)):
                pass                             # «спасибо», «ок» после выдачи — не отвечаем «звёзды уже отправлены»
            else:
                replied = False
            await self.notifier.notify("INFO", "chat_message", f"💬 {msg.author_name} (заказ #{last['order_id']}): "
                                       f"{msg.text[:500]}", throttle_key=f"chat:{chat_id}")
            if replied or not st.is_final:
                return
            # старый заказ закрыт, а покупатель снова пишет «привет / есть звёзды?» — отвечаем как новому
        text_in = msg.text or ""
        about_stars = self.presale_topic(text_in)
        about_premium = self.premium is not None and self.premium.enabled and \
            bool(re.search(r"(?i)преми?ум|premium", text_in))
        if about_premium and not about_stars:
            if last is None:
                self.db.log_message(chat_id, None, "in", msg.text)
                await self.notifier.notify("INFO", "presale_question", f"💬 Вопрос о Premium от {msg.author_name}: "
                                           f"{msg.text[:500]}", throttle_key=f"chat:{chat_id}")
            if not self.cfg.funpay.presale_replies or self.paused or self.presale_done(chat_id, last):
                return
            self.db.mark_presale(chat_id)
            terms = ", ".join(str(l["months"]) for l in self.premium.lots if l.get("offer_id")) or "3, 6, 12"
            lang = M.detect_lang(msg.text, self.cfg.general.language)
            await self.send_chat(chat_id, M.render(lang, "presale_premium",
                                                   premium_terms=terms + (" мес." if lang == "ru" else " mo.")))
            return
        hello = not about_stars and self.presale_hello(text_in)
        # новый человек пишет что угодно без заказа — один раз отвечаем шаблоном (кроме вопросов о чужих товарах)
        anything = last is None and not about_stars and not hello and self.presale_any(text_in)
        if not about_stars and not hello and not anything:
            return                               # не про звёзды и не «вы тут?» — не наш покупатель, молчим
        if last is None:
            self.db.log_message(chat_id, None, "in", msg.text)
            await self.notifier.notify("INFO", "presale_question", f"💬 Сообщение без заказа от {msg.author_name}: "
                                       f"{msg.text[:500]}", throttle_key=f"chat:{chat_id}")
        if not self.cfg.funpay.presale_replies or self.paused or self.presale_done(chat_id, last):
            return                               # шаблон в этом чате уже был — дальше отвечает продавец
        if not self.coord.claim_presale(chat_id):
            return                               # на этот вопрос уже отвечает другой бот аккаунта
        self.db.mark_presale(chat_id)
        lang = M.detect_lang(msg.text, self.cfg.general.language)
        await self.send_chat(chat_id, self.presale_text(lang, about_stars, msg.text))

    async def reply_after_delivery(self, o, text: str, faq: str | None) -> None:
        """Вопрос после выдачи: отвечаем один раз на заказ, дальше молчим и зовём продавца, если это жалоба."""
        oid = o["order_id"]
        complaint = faq in ("faq_not_arrived", "faq_refund")
        if complaint and not o["confirm_reminded"]:
            self.db.update_order(oid, confirm_reminded=1)   # недовольному покупателю не напоминаем «подтвердите»
        key = f"after_delivery_reply:{oid}"
        if self.db.kv_get(key, 0):
            if complaint:
                await self.notifier.notify("WARNING", f"buyer_{faq[4:]}", f"#{oid}: покупатель {o['buyer_username']} "
                                           f"снова пишет после выдачи (автоответ уже был, ответьте сами): "
                                           f"{text[:300]}", order_id=oid)
            return
        self.db.kv_set(key, time.time())
        if faq:
            await self.answer_faq(o, text)
        else:
            await self.say(o, "after_delivery")

    def presale_done(self, chat_id: str, last=None) -> bool:
        """Шаблон без заказа отправляем один раз на чат; вернувшемуся покупателю — снова, но после нового заказа."""
        ts = float(self.db.scalar("SELECT presale_at FROM chat_state WHERE chat_id=?", (chat_id,), 0) or 0)
        if not ts:
            return False
        return last is None or ts > float(last["created_at"] or 0)

    # пресейл: о чём вопрос и что ответить (бот TG Gift переопределяет под подарки)
    def presale_any(self, text: str) -> bool:
        """Сообщение не о звёздах и без приветствия: отвечаем шаблоном, если это не вопрос о чужом товаре
        (Steam — MannBot, подарки — TG Gift)."""
        t = text or ""
        if M.OTHER_GOODS_RE.search(t):
            return False
        try:
            from starsbot.giftshop.engine import GIFT_TOPIC_RE
        except Exception:  # noqa: BLE001
            return True
        return not GIFT_TOPIC_RE.search(t)

    def presale_topic(self, text: str) -> bool:
        return bool(M.STARS_TOPIC_RE.search(text or ""))

    def presale_hello(self, text: str) -> bool:
        t = text or ""
        return bool(M.HELLO_RE.search(t)) and not M.OTHER_GOODS_RE.search(t) and len(t) <= 200

    def presale_text(self, lang: str, about_topic: bool, text: str) -> str:
        out = M.render(lang, "presale" if about_topic else "presale_hello",
                       denominations=", ".join(str(d) for d in self._denominations()))
        faq = M.detect_faq(text)
        if faq and faq != "faq_time":
            out += "\n\n" + M.render(lang, faq, status_hint="")
        return out

    # ================================================================== покупка звёзд
    def cost_per_star_ton(self) -> float | None:
        """Цена звезды в TON: 0.015 $ по живому курсу GRAM, иначе медиана последних покупок, котировка, настройки."""
        live = self.econ.star_ton_live() if self.econ is not None and hasattr(self.econ, "star_ton_live") else None
        if live:
            return live
        rows = self.db.q("SELECT ton_amount, stars FROM orders WHERE ton_amount>0 AND stars>0 AND sent_at IS NOT NULL "
                         "ORDER BY sent_at DESC LIMIT 7")
        vals = [r["ton_amount"] / r["stars"] for r in rows]
        if vals:
            return statistics.median(vals)
        quote = self.db.kv_get("last_quote")
        if quote and quote.get("stars"):
            return quote["ton"] / quote["stars"]
        return float(self.cfg.rtba.fallback_ton_per_star or 0) or None

    def cost_per_star_rub(self) -> float | None:
        if self.econ is not None:
            return self.econ.cost_per_star_rub()
        ton = self.cost_per_star_ton()
        return self.rates.rub(ton) if ton else None

    def ton_to_rub(self, ton: float) -> float | None:
        """TON → ₽ по эффективному курсу (ваши пополнения или биржа + спред)."""
        if self.econ is not None:
            rate, _ = self.econ.effective_rate()
            return round(ton * rate, 2) if rate else None
        return self.rates.rub(ton)

    async def deliver_worker(self) -> None:
        while True:
            oid = await self.queue.get()
            self._queued.discard(oid)
            try:
                await self.purchase(oid)
                o = self.db.get_order(oid)
                if o is not None and S(o["status"]) == S.SENT:
                    await asyncio.sleep(float(self.cfg.deal.purchase_pause_seconds or 0))
            except Exception as e:  # noqa: BLE001
                log.exception("ошибка покупки %s", oid)
                o = self.db.get_order(oid)
                if o and S(o["status"]).money_in_flight:
                    # исход неизвестен — оставляем сверку наблюдателю за транзакциями, повтор запрещён
                    await self.notifier.notify("CRITICAL", "purchase_crash", f"Сбой во время оплаты #{oid}: {e!r}. "
                                               "Бот сверит транзакцию с блокчейном.", order_id=oid)
                else:
                    self._set(oid, S.MANUAL, "purchase_crash", fail_reason=str(e)[:200])
                    await self.notifier.notify("CRITICAL", "purchase_crash", f"Сбой покупки #{oid}: {e!r}",
                                               order_id=oid)

    def _in_flight(self) -> list:
        """TON-транзакции «в полёте» (подарки идут со звёздного баланса Telegram и сюда не входят)."""
        return [o for o in self.db.orders_by_status(S.PURCHASING, S.SENT) if not is_gift(o)]

    async def purchase(self, oid: str) -> None:
        o = self.db.get_order(oid)
        if not o or S(o["status"]) != S.READY:
            return
        if self.paused:
            self.enqueue(oid, 10)
            return
        if is_gift(o):
            if self.gifts is not None:
                await self.gifts.deliver(o)
            return
        if self.engine_nft_wait(o):
            return
        if self._in_flight():
            self.enqueue(oid, 5)                 # одна транзакция за раз: seqno кошелька идут по порядку
            return
        stale = time.time() - float(self.fragment_status.get("checked_at") or 0) > \
            float(self.cfg.fragment.session_check_minutes) * 60
        if not self.fragment_ok or stale:
            if not await self.ensure_fragment(force=stale and self.fragment_ok):
                self.enqueue(oid, 60)
                return
        async with self._purchase_lock:
            o = self.db.get_order(oid)
            if not o or S(o["status"]) != S.READY:
                return
            if int(o["purchase_attempts"] or 0) >= int(self.cfg.deal.purchase_retry_limit):
                self._set(oid, S.MANUAL, "purchase_retry_limit", fail_reason="retry_limit")
                await self.say(o, "delay")
                await self.notifier.notify("CRITICAL", "purchase_retry_limit", f"#{oid}: покупка не прошла "
                                           f"{o['purchase_attempts']} раз. Требуется вмешательство.", order_id=oid)
                return
            if self.nft is not None and self.nft.owns(o):
                await self.nft.step(o)
                return
            await self._purchase_locked(o)

    def engine_nft_wait(self, o) -> bool:
        """Задание NFT при выключенном модуле не исполняем (оно останется в очереди у человека)."""
        if str(o["order_id"]).startswith("NFT-") and self.nft is None:
            self._set(o["order_id"], S.MANUAL, "nft_disabled", fail_reason="nft_disabled")
            return True
        return False

    async def _purchase_locked(self, o) -> None:
        oid, stars = o["order_id"], int(o["stars"])
        if not o["tg_recipient"]:
            self._set(oid, S.AWAITING_USERNAME, "no_recipient")
            await self.accept_username(self.db.get_order(oid), o["tg_username"], source=o["username_source"] or "chat")
            return
        # 1. котировка Fragment. recipient от Fragment привязан к сессии и живёт недолго — как сайт, ищем
        # получателя заново прямо перед заявкой (старый recipient даёт «Access denied»)
        premium = is_premium(o)
        months = int(o["premium_months"] or 0) if premium else 0
        try:
            if premium:
                rec = await self.fragment.search_premium_recipient(o["tg_username"], months)
            else:
                rec = await self.fragment.search_recipient(o["tg_username"], stars)
            if rec.recipient != o["tg_recipient"]:
                self.db.update_order(oid, tg_recipient=rec.recipient)
            if premium:
                req_id, amount = await self.fragment.init_premium(rec.recipient, months)
            else:
                req_id, amount = await self.fragment.init_buy(rec.recipient, stars)
        except FragmentAuthError as e:
            await self.fragment_auth_problem(e)
            self.enqueue(oid, 60)
            return
        except FragmentError as e:
            await self._purchase_error(o, e.code, str(e))
            return
        if premium:
            self.premium.record_quote(months, amount)
        else:
            self.db.kv_set("last_quote", {"ton": amount, "stars": stars, "ts": time.time()})
        self._fragment_state(kyc="verified")
        await self.rates.refresh()
        cost_rub = self.ton_to_rub(amount)
        # 2. предохранители цены
        per_star = amount / stars if stars else 0
        max_ps = float(self.cfg.fragment.max_ton_per_star or 0)
        share = float(self.cfg.fragment.max_cost_share_pct or 0)
        problem = ""
        if amount <= 0:
            problem = "Fragment не сообщил сумму покупки"
        elif max_ps and per_star > max_ps and not premium:
            problem = f"цена {per_star:.5f} TON за звезду выше лимита {max_ps}"
        elif share and cost_rub is not None and o["price"] and cost_rub > float(o["price"]) * share / 100:
            problem = (f"себестоимость {cost_rub:.2f} ₽ больше {share:g}% суммы заказа {o['price']:g} ₽ "
                       "(вырос курс TON или цена на Fragment?)")
        if not problem:
            problem = self._loss_problem(o, stars, amount)
        if not problem:
            problem = self._safety_problem(amount)
        if not problem and premium:
            problem = self.premium.safety_problem(o, amount)
        if problem and not o["approved"]:
            self._set(oid, S.PENDING_APPROVAL, "price_guard", fail_reason="price_guard", ton_amount=amount,
                      cost_rub=cost_rub)
            await self.say(o, "awaiting_approval")
            await self.notifier.notify("CRITICAL", "price_guard", f"#{oid}: покупка остановлена — {problem}. "
                                       f"/approve {oid} — купить всё равно.", order_id=oid,
                                       buttons=[("✔ Купить", f"approve {oid}"), ("✋ В ручную", f"reject {oid}")])
            return
        # 3. транзакция от Fragment и её проверка
        try:
            tx = await (self.fragment.get_premium_link(req_id, amount) if premium else
                        self.fragment.get_buy_link(req_id, amount))
        except FragmentAuthError as e:
            await self.fragment_auth_problem(e)
            self.enqueue(oid, 60)
            return
        except (FragmentError, TonError) as e:
            await self._purchase_error(o, getattr(e, "code", "fragment"), str(e))
            return
        await self.send_fragment_tx(o, req_id, tx, amount, stars=stars)

    async def send_fragment_tx(self, o, req_id: str, tx, amount: float, *, stars: int,
                               max_total_ton: float | None = None) -> bool:
        """Проверка транзакции Fragment, запись попытки, подпись и отправка в сеть (одна транзакция за раз).
        Возвращает True, если транзакция ушла (или её исход сверит наблюдатель за транзакциями)."""
        oid = o["order_id"]
        total = sum(m.amount for m in tx.messages)
        own = self.wallet.raw_address.lower()
        limit = to_nano(max_total_ton) if max_total_ton else to_nano(amount) * 1.03 + to_nano(0.05)
        if total > limit or any(m.address.lower() == own for m in tx.messages):
            self._set(oid, S.MANUAL, "tx_mismatch", fail_reason="tx_mismatch")
            await self.notifier.notify("CRITICAL", "tx_mismatch", f"#{oid}: транзакция Fragment не совпала с "
                                       f"котировкой ({from_nano(total):.4f} TON против {amount:.4f}). Оплата не "
                                       "выполнена, проверьте вручную.", order_id=oid)
            return False
        dest = tx.messages[0].address
        ref = (re.search(r"Ref#\w+", tx.comment or "") or [None])[0] or ""
        pid = self.db.add_purchase(order_id=oid, req_id=req_id, stars=stars, recipient=o["tg_recipient"], dest=dest,
                                   amount_nano=total, comment=tx.comment, state="prepared")
        o = self._set(oid, S.PURCHASING, "purchase_started", req_id=req_id, ton_amount=from_nano(total),
                      ton_rate=self.rates.value, cost_rub=self.ton_to_rub(from_nano(total)), fragment_ref=ref,
                      purchase_started_at=time.time(), purchase_attempts=int(o["purchase_attempts"] or 0) + 1)

        async def on_signed(seqno: int, valid_until: int, msg_hash: str) -> None:
            self.db.update_purchase(pid, seqno=seqno, valid_until=valid_until, msg_hash=msg_hash, state="signed")
            self.db.update_order(oid, tx_seqno=seqno, tx_valid_until=valid_until, tx_msg_hash=msg_hash)

        # 4. подпись и отправка в сеть
        try:
            sent = await self.wallet.transfer(tx.messages, tx.valid_until, on_signed=on_signed)
        except TonError as e:
            p = self.db.last_purchase(oid)
            if p and p["state"] == "signed" and e.code == "network":
                # подписали, но не знаем, дошло ли до сети — решит сверка по seqno и valid_until
                self.db.update_purchase(pid, state="unknown", error=str(e))
                self._set(oid, S.SENT, "send_unknown", sent_at=time.time(), fail_reason="send_unknown")
                return True
            self.db.update_purchase(pid, state="failed", error=str(e))
            self._set(oid, S.READY, "purchase_not_sent", fail_reason=e.code)
            if e.code == "insufficient_funds":
                await self.out_of_funds(self.db.get_order(oid), str(e))
                return False
            await self._purchase_error(self.db.get_order(oid), e.code, str(e))
            return False
        self.db.update_purchase(pid, state="sent", msg_hash=sent.msg_hash)
        self._set(oid, S.SENT, "tx_sent", sent_at=time.time(), tx_msg_hash=sent.msg_hash)
        self.wallet_info["ts"] = 0
        await self.fragment.confirm(tx, sent.boc)
        return True

    def live_purchases(self) -> int:
        """Сколько реальных (не dry-run) оплат уже ушло в сеть."""
        return int(self.db.scalar("SELECT COUNT(*) FROM purchases WHERE state IN ('sent','confirmed') "
                                  "AND msg_hash NOT LIKE 'dry%'", (), 0))

    def spent_ton_24h(self) -> float:
        return float(self.db.scalar("SELECT SUM(amount_nano) FROM purchases WHERE state IN ('sent','confirmed',"
                                    "'signed','unknown') AND created_at>=?", (time.time() - 86400,), 0) or 0) / 1e9

    def _loss_problem(self, o, stars: int, amount: float) -> str:
        """Не покупать в минус: прибыль после комиссий FunPay/вывода и по реальному курсу TON."""
        minp = float((self.cfg.get("safety") or {}).get("min_profit_pct") or 0)
        if is_premium(o) and self.premium is not None:
            minp = max(minp, float(self.premium.c.get("min_margin_pct") or 0))
        if not minp or self.econ is None or self.cfg.general.demo or not o["price"]:
            return ""
        price = float(o["price"])
        if self.channel_name(o) == "funpay" and str(self.cfg.rtba.get("preset") or "custom") != "custom":
            # сегмент с маржой ниже порога (мелкие лоты «в ноль») не должен ждать одобрения: допускаем
            # маржу сегмента минус 1% на колебание курса TON между выставлением цены и покупкой
            plan = self.lot_strategy(int(o["stars_per_unit"] or stars) if "stars_per_unit" in o.keys() else stars)
            minp = min(minp, float(plan.get("margin_pct") or 0) - float(plan.get("night_bonus") or 0) - 1.0)
        fl = self.econ.flow(stars, price, ton_stars=amount, channel=self.channel_name(o))
        if fl.profit is None:
            return "нет курса TON — не могу проверить, что заказ не в минус"
        if fl.profit < price * minp / 100:
            return (f"заказ без прибыли: выручка после комиссий {fl.net_rub:.2f} ₽, себестоимость {fl.cost_rub:.2f} ₽ "
                    f"(прибыль {fl.profit:+.2f} ₽, нужно от {minp:g}%). Поднимите цену лота")
        return ""

    def channel_name(self, o) -> str:
        return (o["channel"] if "channel" in o.keys() else None) or "funpay"

    async def price_guard(self) -> None:
        """Цена лота не ниже безубытка + минимальная маржа (растёт TON или Fragment — растёт и цена)."""
        s = self.cfg.get("safety") or {}
        if not s.get("price_guard", True) or self.econ is None or self.paused:
            return
        if not self.cost_per_star_ton() or not self.econ.effective_rate()[0]:
            return
        node = int(self.cfg.funpay.node_id)
        for lot in self.lots:
            if not lot.get("offer_id"):
                continue
            stars = int(lot["stars"])
            # маржа сегмента с ночной надбавкой — та же, что у пола RTBA, иначе они перетягивали бы цену
            margin = float(self.lot_strategy(stars).get("margin_pct", self.cfg.rtba.min_margin_pct) or 0)
            fl = self.econ.flow(stars, 100.0)
            if fl.breakeven_price is None:
                continue
            # до копеек, как пол RTBA: округление до рубля поднимало цену выше пола RTBA, и они перетягивали её
            floor = math.ceil(round(fl.breakeven_price * (1 + margin / 100), 6) * 100) / 100
            try:
                fields = await self.fp.get_lot_fields(int(lot["offer_id"]), node)
                price = float(str(fields.get("price") or 0).replace(",", "."))
                if price and price < floor - 0.005:
                    await self.fp.update_lot(int(lot["offer_id"]), node, price=floor)
                    self.db.add_event("-", "price_guard", offer_id=lot["offer_id"], stars=stars, old=price, new=floor)
                    await self.notifier.notify("WARNING", "price_guard_raise",
                                               f"Лот {stars} ⭐: цена {price:g} ₽ была ниже безубытка с маржой "
                                               f"{margin:g}% — поднял до {floor:g} ₽ (себестоимость "
                                               f"{fl.cost_rub:.2f} ₽)", throttle_key=f"pg:{lot['offer_id']}")
            except (FunPayError, ValueError) as e:
                log.warning("защита цены, лот %s: %s", lot.get("offer_id"), e)

    def _safety_problem(self, amount: float) -> str:
        """Предохранители боевого режима: пилотные покупки, лимит на заказ и на сутки."""
        c = self.cfg
        if c.general.dry_run or c.general.demo:
            return ""
        s = c.get("safety") or {}
        pilot = int(s.get("pilot_orders") or 0)
        done = self.live_purchases()
        if pilot and done < pilot:
            return (f"пилотный режим: боевая покупка {done + 1} из {pilot} — проверьте получателя и сумму "
                    f"({amount:.4f} TON)")
        max_order = float(s.get("max_order_ton") or 0)
        if max_order and amount > max_order:
            return f"заказ стоит {amount:.4f} TON — больше лимита {max_order:g} TON на заказ"
        daily = float(s.get("daily_ton_limit") or 0)
        spent = self.spent_ton_24h()
        if daily and spent + amount > daily:
            return f"за сутки уже потрачено {spent:.3f} TON, с этим заказом будет больше лимита {daily:g} TON"
        return ""

    async def _purchase_error(self, o, code: str, text: str) -> None:
        oid = o["order_id"]
        self.db.add_event(oid, "purchase_error", code=code, error=text[:300])
        if code in BAD_RECIPIENT:
            await self.bad_username(o, code, text)
            return
        if code in ("insufficient_funds",):
            await self.out_of_funds(o, text)
            return
        fails = int(self.db.kv_get(f"purchase_fail:{oid}", 0)) + 1
        self.db.kv_set(f"purchase_fail:{oid}", fails)
        if code in RETRYABLE and fails < 6:
            self._set(oid, S.READY, fail_reason=code)
            self.enqueue(oid, 20 * fails)
            if fails == 3:
                await self.notifier.notify("WARNING", "purchase_retry", f"#{oid}: Fragment отвечает ошибкой ({text}), "
                                           "повторяю", order_id=oid)
            return
        self._set(oid, S.MANUAL, "purchase_failed", fail_reason=code)
        await self.say(o, "delay")
        await self.notifier.notify("CRITICAL", "purchase_failed", f"#{oid}: {'Premium' if is_premium(o) else 'звёзды'} "
                                   f"не куплен{'' if is_premium(o) else 'ы'}: {text}", order_id=oid)

    async def out_of_funds(self, o, text: str) -> None:
        oid = o["order_id"]
        self._set(oid, S.MANUAL, "out_of_funds", fail_reason="out_of_funds")
        await self.say(o, "delay")
        await self.notifier.notify("CRITICAL", "out_of_funds", f"Не хватает TON для заказа #{oid} ({o['stars']} ⭐): "
                                   f"{text}. Пополните кошелёк и нажмите «Одобрить», или оформите возврат.",
                                   order_id=oid, buttons=[("✔ Пополнил, купить", f"approve {oid}"),
                                                          ("💰 Баланс", "balance")])
        if self.cfg.deal.auto_refund_no_funds:
            await self.admin_refund(oid, "нет средств на кошельке (авто)")

    # ================================================================== сверка транзакций с блокчейном
    async def watch_purchases(self) -> None:
        pending = self._in_flight()
        if not pending:
            self.last_ok["ton_watch"] = time.time()
            return
        try:
            seqno_now = await self.wallet.current_seqno()
        except TonError as e:
            log.warning("TON: состояние кошелька недоступно: %s", e)
            return
        now = time.time()
        for o in pending:
            oid = o["order_id"]
            p = self.db.last_purchase(oid)
            if p is None or p["seqno"] is None:
                # до подписи не дошли — в сеть ничего не уходило, повтор безопасен
                self._set(oid, S.READY, "purchase_reset")
                self.enqueue(oid)
                continue
            if str(p["msg_hash"] or "").startswith("dry"):
                await self.on_delivered(o, {"hash": "dry-run", "fee": 0})
                continue
            if seqno_now > int(p["seqno"]):
                tx = await self.wallet.find_outgoing(p["dest"], int(p["amount_nano"]), float(p["created_at"]),
                                                     o["fragment_ref"] or "")
                if tx:
                    self.db.update_purchase(p["id"], state="confirmed", tx_hash=tx["hash"])
                    await self.on_delivered(self.db.get_order(oid), tx)
                elif now - float(p["updated_at"] or now) > 600 and not o["admin_alerted"]:
                    self._set(oid, S.MANUAL, "seqno_used_elsewhere", admin_alerted=1, fail_reason="tx_not_found")
                    await self.notifier.notify("CRITICAL", "tx_not_found", f"#{oid}: seqno кошелька вырос, но нашей "
                                               "транзакции в истории нет. Проверьте кошелёк (не отправлялось ли что-то "
                                               "вручную?) и историю на Fragment.", order_id=oid)
                continue
            if now > int(p["valid_until"] or 0) + 60:
                # транзакция больше никогда не исполнится — можно безопасно покупать заново
                self.db.update_purchase(p["id"], state="expired")
                self._set(oid, S.READY, "tx_expired", fail_reason="tx_expired")
                await self.notifier.notify("WARNING", "tx_expired", f"#{oid}: транзакция не попала в блокчейн до "
                                           "истечения срока, покупка будет повторена", order_id=oid)
                self.enqueue(oid)
                continue
            sent_at = float(o["sent_at"] or now)
            if now - sent_at > 180 and not self.db.scalar("SELECT COUNT(*) FROM order_events WHERE order_id=? AND "
                                                         "kind='slow_network'", (oid,)):
                self.db.add_event(oid, "slow_network")
                await self.say(o, "slow_network")
            if now - sent_at > float(self.cfg.deal.confirm_wait_minutes) * 60:
                await self.notifier.notify("WARNING", "tx_slow", f"#{oid}: транзакция ждёт подтверждения "
                                           f"{int((now - sent_at) / 60)} мин.", order_id=oid,
                                           throttle_key=f"tx_slow:{oid}")
        self.last_ok["ton_watch"] = time.time()

    async def on_delivered(self, o, tx: dict) -> None:
        oid = o["order_id"]
        if self.nft is not None and self.nft.owns(o):       # этап покупки/отправки NFT-подарка для TG Gift
            await self.nft.on_tx_confirmed(o, tx)
            return
        fee = tx.get("fee")
        o = self._set(oid, S.DELIVERED, "delivered", delivered_at=time.time(), tx_hash=tx.get("hash", ""),
                      tx_fee_ton=from_nano(fee) if fee else None, fail_reason="")
        self.db.kv_set(f"purchase_fail:{oid}", 0)
        self.wallet_info["ts"] = 0
        await self.say(o, "delivered")
        self.save_evidence(self.db.get_order(oid))
        what = f"Premium {months_text(int(o['premium_months'] or 0))}" if is_premium(o) else f"{o['stars']} звёзд"
        await self.notifier.notify("INFO", "delivered", f"⭐ Заказ #{oid}: {what} → @{o['tg_username']} "
                                   f"({o['ton_amount'] or 0:.4f} TON)", order_id=oid)
        ch = self.channel_of(o)
        if ch is not None:
            await ch.on_delivered(self.db.get_order(oid))
        elif o["fp_status"] == "closed":
            await self.on_buyer_confirmed(self.db.get_order(oid))

    # ================================================================== Fragment и кошелёк
    def _fragment_state(self, **kw: Any) -> None:
        self.fragment_status.update(kw)
        self.db.kv_set("fragment_status", self.fragment_status)
        self.db.emit({"type": "fragment", **self.fragment_status})

    async def ensure_fragment(self, force: bool = False) -> bool:
        """Проверка сессии Fragment (и кошелька) — при старте и перед покупкой, не чаще раза в минуту."""
        last = float(self.db.kv_get("fragment_login_try", 0))
        if not force and time.time() - last < 60:
            return self.fragment_ok
        self.db.kv_set("fragment_login_try", time.time())
        try:
            await self.wallet.refresh()
            await self.fragment.login()
            was_down = not self.fragment_ok and self.fragment_status.get("session") == "expired"
            self.fragment_ok = True
            self._fragment_state(session="ok", checked_at=time.time(), error="")
            self.last_ok["fragment"] = time.time()
            if was_down:
                await self.notifier.notify("INFO", "fragment_ok", "Fragment: сессия снова в порядке, покупки идут")
            return True
        except FragmentAuthError as e:
            await self.fragment_auth_problem(e)
        except (FragmentError, TonError) as e:
            level = "CRITICAL" if getattr(e, "code", "") in ("key_mismatch", "inactive", "config") else "WARNING"
            self._fragment_state(checked_at=time.time(), error=str(e)[:300])
            await self.notifier.notify(level, "fragment_unavailable", f"Fragment/TON недоступен: {e}",
                                       throttle_key="fragment_unavailable")
        self.fragment_ok = False
        return False

    async def fragment_auth_problem(self, e: Exception) -> None:
        self.fragment_ok = False
        if getattr(e, "code", "") == "kyc":
            self._fragment_state(kyc="required", checked_at=time.time(), error=str(e)[:300])
            text = (f"Fragment требует верификацию (KYC): {e}. Пройдите её на fragment.com тем же аккаунтом и "
                    "кошельком — покупки остановлены, заказы ждут.")
        else:
            self._fragment_state(session="expired", checked_at=time.time(), error=str(e)[:300])
            text = f"Fragment: {e}. Покупки остановлены — обновите cookie Fragment в настройках."
        await self.notifier.notify("CRITICAL", "fragment_auth", text, throttle_key="fragment_auth")
        if self.cfg.fragment.hide_lots_when_down:
            await self.stock_sync(0.0, self.cost_per_star_ton() or 1.0, 0)

    async def wallet_watch(self) -> None:
        try:
            balance = await self.wallet.balance_ton()
        except TonError as e:
            await self.notifier.notify("CRITICAL" if e.code in ("key_mismatch", "inactive") else "WARNING",
                                       "wallet", f"Кошелёк TON: {e}", throttle_key="wallet")
            return
        await self.rates.refresh()
        per_star = self.cost_per_star_ton()
        committed = self.db.stars_committed()
        reserve = float(self.cfg.ton.fee_reserve_ton)
        prem_ton = self.premium.committed_ton() if self.premium is not None else 0.0
        capacity = int(max(0.0, balance - reserve - prem_ton) / per_star) - committed if per_star else None
        self.wallet_info = {"balance": balance, "ts": time.time(), "per_star": per_star, "capacity": capacity,
                            "committed": committed, "address": self.wallet.friendly_address}
        self.db.kv_set("wallet", self.wallet_info)
        self.db.exec("INSERT INTO balance_history(ts,ton,ton_rub,capacity_stars) VALUES(?,?,?,?)",
                     (time.time(), balance, self.rates.value, capacity))
        self.db.emit({"type": "wallet", **self.wallet_info})
        if self.db.kv_get("wallet_start_balance") is None:
            # точка отсчёта для сверки «баланс по учёту против баланса в сети»
            self.db.kv_set("wallet_start_balance", {"ton": balance, "ts": time.time()})
        if self.econ is not None and not self._in_flight():
            rec = self.econ.reconcile()
            if rec.get("alarm"):
                await self.notifier.notify("CRITICAL", "balance_mismatch", f"Баланс кошелька {rec['actual']:.3f} TON "
                                           f"расходится с учётом ({rec['expected']:.3f} TON) на {rec['diff']:+.3f}. "
                                           "Внесите пополнения/выводы в «Деньги» или проверьте, не было ли чужих "
                                           "списаний.", throttle_key="balance_mismatch")
        low = float(self.cfg.ton.low_balance_ton or 0)
        if low and balance < low:
            await self.notifier.notify("WARNING", "low_balance", f"На кошельке {balance:.3f} TON (порог {low}). "
                                       "Пополните, иначе крупные номиналы скроются.", throttle_key="low_balance")
        self.last_ok["wallet"] = time.time()
        await self.stock_sync(balance, per_star, committed)
        # заказы, остановленные из-за нехватки денег, — снова в работу, когда деньги появились
        for o in self.db.orders_by_status(S.MANUAL):
            if o["fail_reason"] != "out_of_funds":
                continue
            if is_premium(o) and self.premium is not None:
                need = (self.premium.ton_for(int(o["premium_months"] or 0)) or 0) + self.premium.gas_ton()
                if need and balance - reserve - prem_ton - (per_star or 0) * committed >= need * 1.02:
                    self._set(o["order_id"], S.READY, "funds_restored", fail_reason="")
                    self.enqueue(o["order_id"])
                continue
            if per_star and balance - reserve - prem_ton >= per_star * (committed + int(o["stars"])) * 1.02:
                self._set(o["order_id"], S.READY, "funds_restored", fail_reason="")
                self.enqueue(o["order_id"])

    async def stock_sync(self, balance: float, per_star: float | None, committed: int) -> None:
        if self.premium is not None and self.premium.enabled:
            await self.premium.stock_sync(balance, per_star, committed)
            balance = balance - self.premium.committed_ton()   # TON под заказы Premium звёздам не обещаем
        for ch in self.channels.values():          # кошелёк общий: остатки на других площадках — по тому же балансу
            await ch.stock_sync(balance, per_star, committed)
        if not self.cfg.funpay.stock_sync or self.paused or not per_star:
            return
        free_ton = max(0.0, balance - float(self.cfg.ton.fee_reserve_ton) - committed * per_star)
        if not self.fragment_ok and self.cfg.fragment.hide_lots_when_down:
            free_ton = 0.0                       # без сессии Fragment продавать нечем — скрываем лоты
        for lot in self.lots:
            if not lot.get("offer_id"):
                continue
            stars = int(lot["stars"])
            amount = max(0, min(int(free_ton / (stars * per_star * 1.03)), int(self.cfg.funpay.max_display_amount)))
            off = self.orbit_closed("stars")
            if off:
                amount = 0
            key = f"lot_amount:{lot['offer_id']}"
            if self.db.kv_get(key) == amount:
                continue
            active = not off and (amount > 0 or not self.cfg.funpay.deactivate_when_empty)
            try:
                await self.fp.update_lot(int(lot["offer_id"]), int(self.cfg.funpay.node_id),
                                         amount=max(amount, 1 if active else 0), active=active)
                self.db.kv_set(key, amount)
                self.db.add_event("-", "lot_stock", offer_id=lot["offer_id"], stars=stars, amount=amount, active=active)
            except FunPayError as e:
                await self.notifier.notify("WARNING", "lot_update_failed", f"Лот {lot['offer_id']}: {e}",
                                           throttle_key=f"lot:{lot['offer_id']}")

    # ================================================================== таймеры / SLA
    async def confirm_reminders(self, now: float | None = None) -> int:
        """Одно напоминание подтвердить заказ через deal.confirm_reminder_minutes после выдачи, если покупатель
        ещё не подтвердил. Старые выдачи (больше 6 ч) не трогаем — им напомнит автопилот, если он включён."""
        mins = float(self.cfg.deal.get("confirm_reminder_minutes") or 0)
        if mins <= 0 or self.paused:
            return 0
        now = now or time.time()
        n = 0
        for o in self.db.q("SELECT * FROM orders WHERE status=? AND fp_status='paid' AND COALESCE(confirm_reminded,0)=0 "
                           "AND delivered_at<? AND delivered_at>? AND COALESCE(channel,'funpay')='funpay'",
                           (S.DELIVERED.value, now - mins * 60, now - 6 * 3600)):
            if not o["chat_id"]:
                continue
            self.db.update_order(o["order_id"], confirm_reminded=1)     # сначала отметка: второго не будет
            self.db.add_event(o["order_id"], "confirm_reminder", after_minutes=mins)
            await self.say(o, "confirm_reminder")
            n += 1
        return n

    async def timers(self) -> None:
        now = time.time()
        d = self.cfg.deal
        for o in self.db.orders_by_status(S.AWAITING_USERNAME):
            if self.paused:
                break
            if self.waits_code(o):
                continue                 # код Ozon ещё не активирован: напомнить покупателю некуда, это не просрочка
            if self.confirm_pending(o):
                await self._confirm_timeout(o, now)
                continue
            if o["tg_username"] and not o["tg_recipient"] and o["fail_reason"] in ("fragment_auth", "network",
                                                                                   "rate_limit", "layout", "fragment",
                                                                                   "auth", "flood", "config"):
                if now - float(o["updated_at"] or 0) > 60:
                    await self.accept_username(o, o["tg_username"], source=o["username_source"] or "order")
                continue
            base = max(o["paid_at"] or 0, o["last_buyer_msg_at"] or 0)
            idle = (now - base) / 60
            if not o["reminded"] and not o["tg_username"] and idle >= float(d.reminder_after_minutes):
                self._set(o["order_id"], event="reminder", reminded=1)
                await self.say(o, "reminder")
            if not o["admin_alerted"] and idle >= float(d.admin_alert_after_minutes):
                self._set(o["order_id"], event="sla_alert", admin_alerted=1)
                await self.notifier.notify("WARNING", "sla_username", f"#{o['order_id']}: покупатель "
                                           f"{o['buyer_username']} не присылает username {int(idle)} мин.",
                                           order_id=o["order_id"])
        await self.confirm_reminders(now)
        for o in self.db.orders_by_status(S.PENDING_APPROVAL, S.MANUAL):
            if (now - (o["updated_at"] or now)) > 900 and not o["admin_alerted"]:
                self._set(o["order_id"], admin_alerted=1)
                await self.notifier.notify("WARNING", "waiting_admin", f"#{o['order_id']} ждёт решения продавца уже "
                                           f"{int((now - o['updated_at']) / 60)} мин. ({o['status']})",
                                           order_id=o["order_id"])
        for o in self.db.orders_by_status(S.READY):
            if o["order_id"] not in self._queued and not self._in_flight():
                self.enqueue(o["order_id"])
        for o in self.db.active_orders():
            total_m = (now - (o["paid_at"] or now)) / 60
            if self.waits_code(o) and S(o["status"]) == S.AWAITING_USERNAME:
                continue
            if S(o["status"]) != S.DELIVERED and total_m > float(self.cfg.sla.order_total_minutes):
                await self.notifier.notify("WARNING", "sla_total", f"#{o['order_id']} не выдан {total_m:.0f} мин. "
                                           f"(статус {o['status']})", order_id=o["order_id"],
                                           throttle_key=f"sla_total:{o['order_id']}")

    async def _confirm_timeout(self, o, now: float) -> None:
        d = self.cfg.deal
        oid = o["order_id"]
        asked = float(self.db.kv_get(f"ucfm:{oid}", 0) or 0)
        mins = float(d.get("confirm_username_timeout_minutes") or 15)
        if now - max(asked, float(o["last_buyer_msg_at"] or 0)) < mins * 60:
            return
        if str(d.get("confirm_username_timeout_action") or "deliver") == "deliver":
            await self.username_confirmed(o, "username_confirm_timeout", minutes=mins)
            return
        self.db.kv_set(f"ucfm:{oid}", 0)
        self._set(oid, S.MANUAL, "username_confirm_timeout", fail_reason="no_confirm")
        await self.notifier.notify("WARNING", "username_no_confirm", f"#{oid}: покупатель {o['buyer_username']} "
                                   f"не подтвердил получателя @{o['tg_username']} за {mins:g} мин. — выдайте "
                                   f"вручную (/approve {oid}) или уточните в чате.", order_id=oid)

    async def raise_lots(self) -> None:
        if not self.cfg.funpay.auto_raise or self.paused or not int(self.cfg.funpay.game_id or 0):
            return
        last = float(self.db.kv_get("last_raise", 0))
        if time.time() - last < float(self.cfg.funpay.raise_interval_minutes) * 60:
            return
        try:
            ok, msg = await self.fp.raise_lots(int(self.cfg.funpay.game_id), int(self.cfg.funpay.node_id))
            self.db.add_event("-", "raise", ok=ok, msg=msg)
        except FunPayError as e:
            log.warning("поднятие лотов: %s", e)
        self.db.kv_set("last_raise", time.time())

    def turnover_usage(self) -> tuple[float | None, float, float]:
        """Доля лимита карты, выбранная выручкой FunPay за сутки/месяц (кэш на минуту)."""
        c = getattr(self, "_usage_cache", None)
        if c and time.time() - c[0] < 60:
            return c[1]
        day, month = strat.turnover(self.db)
        res = (strat.usage(self.cfg.rtba, day, month), day, month)
        self._usage_cache = (time.time(), res)
        return res

    def lot_strategy(self, stars: int) -> dict:
        used = self.turnover_usage()[0] if str(self.cfg.rtba.get("preset") or "custom") == "auto" else None
        return strat.resolve(self.cfg.rtba, int(stars), used=used)

    @staticmethod
    def _plan_tag(plan: dict | None) -> str:
        if not plan or plan.get("effective") == "custom" and not plan.get("night"):
            return ""
        parts = [plan["title"]]
        seg = plan.get("segment")
        if seg:
            parts.append(f"{seg.get('from')}–{seg.get('to')}⭐ маржа {plan.get('margin_pct', 0):g}%")
        if plan.get("night"):
            parts.append(f"ночь +{plan['night_bonus']:g}%")
        return "[" + " · ".join(parts) + "] "

    async def rtba_step(self) -> None:
        cfg = self.cfg.rtba
        if self.paused:
            return
        gift_lots = self.gifts.lots if self.gifts is not None and self.gifts.enabled else []
        entries = [(l, "username") for l in self.lots] + [(l, "gift") for l in gift_lots]
        own_ids = {str(l.get("offer_id")) for l, _ in entries if l.get("offer_id")}
        if not cfg.enabled:      # автоцена выключена — ведём только лоты, у которых выбрана позиция на странице
            entries = [(l, k) for l, k in entries if int(l.get("position") or 0) > 0]
        if not entries:
            return
        node = int(self.cfg.funpay.node_id)
        try:
            lots = await self.fp.get_public_lots(node)
        except FunPayError as e:
            log.warning("RTBA: %s", e)
            return
        await self.rates.refresh()
        cost = self.cost_per_star_rub()
        for lot, kind in entries:
            if not lot.get("offer_id"):
                continue
            offer_id, stars = str(lot["offer_id"]), int(lot["stars"])
            overrides: dict = {}
            if kind == "gift":
                overrides["method_filter"] = r"подарк|gift"
            pos = int(lot.get("position") or 0)
            if pos > 0:
                overrides.update(strategy="topN", target_position=pos)
            plan = None
            if kind == "username":       # пресет и сегмент номинала (подарки — по своей позиции, как раньше)
                plan = self.lot_strategy(stars)
                overrides.update(plan["overrides"])
            try:
                fields = await self.fp.get_lot_fields(int(offer_id), node)
            except FunPayError as e:
                log.warning("RTBA лот %s: %s", offer_id, e)
                continue
            current = float(fields.get("price") or 0) or None
            self.rtba.calibrate(lots, offer_id, current)
            lot_cost = cost
            if cost and kind == "gift":                  # подарки дороже номинала: 13 ⭐ = мишка за 15 ⭐
                lot_cost = cost * self.gifts.cost_for(stars) / stars
            elif cost and self.econ is not None:         # газ сети платится с каждого заказа — раскладываем на звёзды
                gas_rub = self.ton_to_rub(float(self.cfg.economics.gas_per_purchase_ton or 0)) or 0.0
                lot_cost = cost + gas_rub / stars
            d = self.rtba.decide(lots, offer_id=offer_id, stars=stars, own_offer_ids=own_ids,
                                 own_username=self.fp.username, current_lot_price=current, cost_per_star_rub=lot_cost,
                                 fee_pct=self.econ.fees_pct() if self.econ is not None else 0.0, overrides=overrides)
            from starsbot.core.rtba import lot_matches
            same = [l for l in lots if lot_matches(l, stars, overrides.get("method_filter", cfg.method_filter))]
            self.db.exec("INSERT INTO competitor_snapshots(ts,stars,data) VALUES(?,?,?)",
                         (time.time(), stars, json.dumps([{k: v for k, v in l.__dict__.items() if k != "fields"}
                                                          for l in same[:40]], ensure_ascii=False)))
            applied = False
            if d.apply and d.target_lot_price is not None:
                try:
                    fields["price"] = f"{d.target_lot_price:.2f}"
                    await self.fp.save_lot(fields)
                    self.rtba.mark_applied(offer_id)
                    applied = True
                except FunPayError as e:
                    await self.notifier.notify("WARNING", "rtba_save_failed", f"RTBA: цена лота {stars} ⭐ не "
                                               f"сохранена: {e}", throttle_key=f"rtba_save:{offer_id}")
            self.db.exec("INSERT INTO price_history(ts,offer_id,stars,old_price,new_price,best_competitor,competitors,"
                         "position,floor,reason,applied) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (time.time(), offer_id, stars, current, d.target_lot_price, d.best_competitor, d.competitors,
                          d.position, d.floor, self._plan_tag(plan) + d.reason + ("; " + "; ".join(d.notes) if d.notes else ""),
                          int(applied)))
            if any("пола" in n for n in d.notes):
                below = d.best_competitor is not None and d.target_buyer_price is not None and \
                    d.best_competitor < d.target_buyer_price
                why = "конкуренты ниже нашей себестоимости" if below else "лидер у нашего минимума, подрезать нельзя"
                await self.notifier.notify("WARNING", "rtba_floor", f"RTBA {stars} ⭐: {why} (лидер "
                                           f"{d.best_competitor}). Стоим на минимуме {d.target_lot_price}.",
                                           throttle_key=f"rtba_floor:{offer_id}")
        self.db.emit({"type": "prices"})

    # ================================================================== действия продавца
    async def admin_approve(self, oid: str) -> str:
        o = self.db.get_order(oid)
        if not o:
            return "заказ не найден"
        st = S(o["status"])
        if st.money_in_flight:
            return "оплата уже в процессе — дождитесь сверки с блокчейном"
        if st not in (S.PENDING_APPROVAL, S.MANUAL, S.BLOCKED, S.AWAITING_USERNAME):
            return f"статус {o['status']} — одобрять нечего"
        if st == S.MANUAL and o["fail_reason"] in ("tx_not_found", "tx_mismatch"):
            return "по заказу была транзакция с неясным исходом — проверьте кошелёк и отметьте «Выдан вручную»"
        if is_gift(o) and o["fail_reason"] == "gift_unknown":
            # продавец проверил историю звёзд: подарок не ушёл — можно отправить заново
            self.db.exec("UPDATE gift_sends SET state='failed', error='проверено продавцом: не отправлен' "
                         "WHERE order_id=? AND state IN ('unknown','sending')", (oid,))
        if not o["tg_username"]:
            self._set(oid, S.AWAITING_USERNAME, "admin_take", approved=1, username_attempts=0, admin_alerted=0)
            if self.waits_code(o):
                return "заказ передан боту: ждём, когда покупатель активирует код"
            await self.say(o, "greeting_need_username")
            self.db.add_event(oid, "greeting_sent")
            return "заказ передан боту: ждём username от покупателя"
        self._set(oid, S.READY, "admin_approved", approved=1, admin_alerted=0, fail_reason="")
        if not o["tg_recipient"]:
            await self.accept_username(self.db.get_order(oid), o["tg_username"], source="admin")
            return f"#{oid} одобрен: проверяю @{o['tg_username']} и покупаю"
        self.enqueue(oid)
        return f"#{oid} одобрен, звёзды покупаются"

    async def admin_set_username(self, oid: str, username: str) -> str:
        from starsbot.core.stars import normalize_username
        o = self.db.get_order(oid)
        u = normalize_username(username)
        if not o or not u:
            return "нет заказа или неверный username"
        if S(o["status"]).money_in_flight or S(o["status"]) in (S.DELIVERED, S.COMPLETED):
            return "звёзды уже оплачены — менять получателя нельзя"
        self._set(oid, S.AWAITING_USERNAME, "admin_set_username", approved=1, username_attempts=0)
        await self.accept_username(self.db.get_order(oid), u, source="admin")
        return f"#{oid}: получатель @{u}"

    async def admin_reject(self, oid: str) -> str:
        o = self.db.get_order(oid)
        if not o:
            return "заказ не найден"
        if S(o["status"]).money_in_flight:
            return "оплата уже в процессе — дождитесь сверки с блокчейном"
        self._set(oid, S.MANUAL, "admin_rejected")
        return f"#{oid} переведён в ручную обработку (оформите возврат на FunPay при необходимости)"

    async def admin_refund(self, oid: str, reason: str = "") -> str:
        o = self.db.get_order(oid)
        if not o:
            return "заказ не найден"
        st = S(o["status"])
        if st in (S.DELIVERED, S.COMPLETED) or st.money_in_flight:
            return "звёзды уже оплачены — возврат через бота запрещён"
        ch = self.channel_of(o)
        if ch is not None:
            return await ch.admin_refund(oid)
        try:
            await self.fp.refund(oid)
        except FunPayError as e:
            return f"возврат не удался: {e}"
        self.db.add_event(oid, "admin_refund", reason=reason)
        self.db.update_order(oid, fp_status="refunded")
        await self.on_refund(self.db.get_order(oid))
        return f"#{oid}: возврат оформлен"

    async def admin_message(self, oid: str, text: str) -> str:
        o = self.db.get_order(oid)
        if not o:
            return "заказ не найден"
        if M.contains_forbidden_link(text):
            return "в сообщении ссылка на сторонний ресурс — FunPay это запрещает (п. 1.12)"
        await self.say_text(o, text, dedupe=False)
        self.db.add_event(oid, "admin_message", text=text)
        return "отправлено"

    def admin_blacklist(self, buyer_id: int, value: bool = True, note: str = "") -> str:
        self.db.set_blacklist(buyer_id, value, note)
        return "готово"

    async def admin_mark_delivered(self, oid: str) -> str:
        o = self.db.get_order(oid)
        if not o:
            return "заказ не найден"
        if is_gift(o):
            self.db.exec("UPDATE gift_sends SET state='sent' WHERE order_id=? AND state IN ('unknown','sending')",
                         (oid,))
        o = self._set(oid, S.DELIVERED, "admin_mark_delivered", delivered_at=time.time())
        if o["tg_username"]:
            await self.say(o, "delivered")
        if o["fp_status"] == "closed":
            await self.on_buyer_confirmed(o)
        return f"#{oid} отмечен выданным, покупателю отправлено сообщение"

    async def handle_command(self, cmd: str, args: list[str]) -> str:
        a0 = args[0].lstrip("#").upper() if args else ""
        if cmd in ("status", "start"):
            return self.status_text()
        if cmd == "pause":
            self.set_paused(True)
            return "⏸ Бот на паузе: заказы фиксируются, но звёзды не покупаются и сообщения не отправляются"
        if cmd == "resume":
            self.set_paused(False)
            for o in self.db.orders_by_status(S.READY):
                self.enqueue(o["order_id"])
            return "▶️ Работа возобновлена"
        if cmd in ("approve", "take"):
            return await self.admin_approve(a0)
        if cmd == "reject":
            return await self.admin_reject(a0)
        if cmd == "refund":
            return await self.admin_refund(a0, "telegram")
        if cmd == "delivered":
            return await self.admin_mark_delivered(a0)
        if cmd == "user" and len(args) > 1:
            return await self.admin_set_username(a0, args[1])
        if cmd == "msg" and len(args) > 1:
            return await self.admin_message(a0, " ".join(args[1:]))
        if cmd == "orders":
            rows = self.db.active_orders()[:20]
            return "\n".join(f"#{r['order_id']} {r['status']} "
                             f"{('Premium ' + str(r['premium_months']) + ' мес.') if is_premium(r) else str(r['stars']) + '⭐'}"
                             f" @{r['tg_username'] or '?'} "
                             f"{r['buyer_username']}" for r in rows) or "активных заказов нет"
        if cmd in ("balance", "stock"):
            await self.wallet_watch()
            w = self.wallet_info
            return (f"Кошелёк: {w.get('balance') or 0:.3f} TON · хватит примерно на {w.get('capacity') or '—'} ⭐ "
                    f"· цена звезды {w.get('per_star') or 0:.5f} TON")
        ap = getattr(self, "autopilot", None)
        if cmd == "withdraw_ok" and ap is not None and args:
            return await ap.execute_withdraw(int(args[0]))
        if cmd == "withdraw_no" and ap is not None and args:
            return ap.cancel_withdraw(int(args[0]))
        tr = getattr(self, "treasury", None)
        if tr is not None and cmd.startswith("treasury"):
            n = int(args[0]) if args and args[0].isdigit() else 0
            if cmd == "treasury_ok":
                return await tr.approve(n)
            if cmd == "treasury_no":
                return tr.reject(n)
            if cmd == "treasury_done":
                return tr.advice_done(n)
            if cmd == "treasury_skip":
                return tr.advice_skip(n)
            if cmd == "treasury":
                await tr.refresh()
                return tr.summary_text()
        if cmd == "evidence" and a0:
            o = self.db.get_order(a0)
            if o is None:
                return "заказ не найден"
            pack = self.support_pack([o])
            sent = await self.post_evidence(a0, force=True) if o["delivered_at"] else False
            return (f"#{a0}: доказательства {'отправлены в чат' if sent else 'не отправлены (заказ не выдан)'}; "
                    f"пакет {pack}")
        if cmd == "support" and ap is not None:
            due = ap.unconfirmed(float(ap.acfg.get("support_after_hours") or 72))
            return ap.make_support_file(due) if due else "неподтверждённых заказов старше порога нет"
        lm = getattr(self, "lot_manager", None)
        if cmd == "lots_on":
            self.reset_lot_amounts()
            await self.refresh_stock()
            return "лоты включены по балансу кошелька"
        if cmd == "lots_off" and lm is not None:
            return await lm.close_all(reason="команда")
        if cmd == "lots_create" and lm is not None:
            return "\n".join(await lm.create_missing())
        if cmd == "stars" and self.gifts is not None:
            bal = await self.gifts.refresh_balance(max_age=0)
            return f"Аккаунт для подарков: {bal if bal is not None else '?'} ⭐ (свободно {self.gifts.capacity(bal)})"
        if cmd == "topup" and self.gifts is not None:
            if len(args) > 0 and args[0].isdigit():
                return await self.gifts.create_topup(int(args[0]), self.gifts.account.get("username") or "")
            return await self.gifts.maybe_topup(need=1)
        if cmd == "premium" and self.premium is not None:
            sm = self.premium.summary()
            lines = [f"Premium: {'включён' if sm['enabled'] else 'выключен'} · за 30 дн. продано {sm['sold_30d']} на "
                     f"{sm['revenue_30d']:g} ₽ · в работе {sm['active']} · резерв {sm['committed_ton']:.3f} TON"]
            for r in self.premium.rows():
                lines.append(f"{r['title']}: лот {r['offer_id'] or '—'}, себестоимость "
                             f"{(str(round(r['cost_rub'])) + ' ₽') if r['cost_rub'] else '—'}, пол "
                             f"{r['floor'] or '—'} ₽, цена {r['price'] or '—'} ₽, место {r['rank'] or '—'}, "
                             f"наличие {r['amount'] if r['amount'] is not None else '—'}")
            return "\n".join(lines)
        if cmd in ("rtba_preset", "card_limit"):
            upd: dict = {}
            if cmd == "rtba_preset":
                if not args or args[0] not in strat.PRESET_TITLES:
                    return "пресеты: " + ", ".join(strat.PRESET_TITLES)
                upd["preset"] = args[0]
            else:
                try:
                    upd["card_limit"] = {"daily_rub": float(args[0]) if args else 0.0,
                                         "monthly_rub": float(args[1]) if len(args) > 1 else
                                         float((self.cfg.rtba.get("card_limit") or {}).get("monthly_rub") or 0)}
                except ValueError:
                    return "формат: card_limit <в сутки ₽> [в месяц ₽]"
            for k, v in upd.items():
                if isinstance(v, dict):
                    self.cfg.rtba.setdefault(k, {}).update(v)
                else:
                    self.cfg.rtba[k] = v
            self._usage_cache = None
            if lm is not None and lm.save_config is not None:
                lm.save_config({"rtba": upd}, rebuild=False)
            used, day, month = self.turnover_usage()
            plan = self.lot_strategy(500)
            return (f"RTBA: пресет {strat.PRESET_TITLES.get(self.cfg.rtba.get('preset'), '?')} → сейчас {plan['title']}; "
                    f"выручка сегодня {day:.0f} ₽, месяц {month:.0f} ₽"
                    + (f", лимит выбран на {used * 100:.0f}%" if used is not None else ", лимит карты не задан"))
        if cmd == "report" and self.analytics:
            return self.analytics.text_report()
        return ("Команды: /status /orders /balance /report /pause /resume /approve ID /reject ID /refund ID "
                "/delivered ID /user ID username /msg ID текст /stars /topup [N] /support /lots_create /lots_off /premium "
                "/rtba_preset custom|turnover|balanced|margin|auto /card_limit ₽_в_сутки [₽_в_месяц]")

    def status_text(self) -> str:
        counts = {r["status"]: r["n"] for r in self.db.q("SELECT status, COUNT(*) n FROM orders GROUP BY status")}
        up = (time.time() - self.started_at) / 3600
        w = self.wallet_info
        lines = [f"{'⏸ ПАУЗА' if self.paused else '▶️ работает'} · аптайм {up:.1f} ч"
                 f"{' · DRY-RUN' if self.cfg.general.dry_run else ''}",
                 f"Fragment: {'подключён' if self.fragment_ok else 'НЕТ СВЯЗИ'} · кошелёк "
                 f"{w.get('balance') if w.get('balance') is not None else '?'} TON",
                 "Заказы: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
        for k, ts in self.last_ok.items():
            lines.append(f"{k}: {int(time.time() - ts)} c назад")
        return "\n".join(lines)

    # ================================================================== здоровье и резервные копии
    def health(self) -> dict:
        """Свежесть рабочих циклов: если цикл давно не отрабатывал, бот «завис» на этом участке."""
        now = time.time()
        limits = {"funpay_poll": 3 * float(self.cfg.funpay.poll_interval) + 60, "ton_watch": 180, "wallet": 600}
        for ch in self.channels.values():
            limits.update(ch.health_limits())
        if self.gifts is not None and self.gifts.enabled:
            limits["tg_gifts"] = 600
        checks = []
        for name, limit in limits.items():
            ts = self.last_ok.get(name)
            age = now - ts if ts else now - self.started_at
            checks.append({"name": name, "age": round(age), "limit": round(limit), "ok": age <= limit})
        fr = self.fragment_status
        checks.append({"name": "fragment_session", "ok": fr.get("session") != "expired" and fr.get("kyc") != "required",
                       "detail": f"{fr.get('session')}/{fr.get('kyc')}"})
        return {"ok": all(c["ok"] for c in checks), "uptime": round(now - self.started_at), "paused": self.paused,
                "checks": checks, "fragment_age_days": self.fragment_cookie_age_days()}

    def fragment_cookie_age_days(self) -> float | None:
        ts = float(self.cfg.fragment.get("cookies_set_at") or 0)
        return round((time.time() - ts) / 86400, 1) if ts else None

    async def fragment_age_reminder(self) -> None:
        """Cookies Fragment живут 2–4 недели: напомнить заранее, а не узнать по скрытым лотам."""
        warn = float(self.cfg.fragment.get("session_warn_days") or 0)
        age = self.fragment_cookie_age_days()
        if not warn or age is None or age < warn or self.fragment_status.get("session") == "expired":
            return
        if time.time() - float(self.db.kv_get("fragment_age_warned", 0)) < 86400:
            return                                   # не чаще раза в сутки
        self.db.kv_set("fragment_age_warned", time.time())
        await self.notifier.notify("WARNING", "fragment_age",
                                   f"Cookies Fragment вставлены {int(age)} дн. назад. Сессия скоро истечёт: "
                                   "обновите их в мастере настройки заранее, чтобы лоты не скрылись.",
                                   throttle_key="fragment_age")

    async def watchdog(self) -> None:
        if time.time() - self.started_at < 300:
            return                                   # дать циклам разогнаться после запуска
        stale = [c for c in self.health()["checks"] if not c["ok"] and "age" in c]
        if stale:
            await self.notifier.notify("CRITICAL", "watchdog", "Бот не обновлял: " + ", ".join(
                f"{c['name']} ({c['age'] // 60} мин)" for c in stale) + ". Проверьте интернет и журнал.",
                throttle_key="watchdog")
        await self.fragment_age_reminder()

    async def backup_db(self) -> None:
        folder = Path(self.db.path).parent / "backups" if self.db.path != ":memory:" else None
        if folder is None:
            return
        keep = int((self.cfg.get("general") or {}).get("backup_keep_days") or 14)
        dest = await asyncio.to_thread(self.db.backup, folder, keep)
        if dest is not None:
            self.last_ok["backup"] = time.time()

    # ================================================================== циклы
    async def _loop(self, name: str, interval: float, fn) -> None:
        errors = 0
        while True:
            try:
                await fn()
                errors = 0
            except asyncio.CancelledError:
                raise
            except FunPayAuthError as e:
                await self.notifier.notify("CRITICAL", "funpay_auth", f"FunPay: {e}. Обновите golden_key!",
                                           throttle_key="funpay_auth")
                await asyncio.sleep(120)
                try:
                    await self.fp.login()
                except Exception:  # noqa: BLE001
                    pass
            except FragmentAuthError as e:
                await self.fragment_auth_problem(e)
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.exception("цикл %s", name)
                if errors in (3, 10, 50):
                    await self.notifier.notify("CRITICAL" if errors >= 10 else "WARNING", f"loop_{name}",
                                               f"Цикл «{name}» падает {errors} раз подряд: {e!r}",
                                               throttle_key=f"loop_{name}")
                await asyncio.sleep(min(300, 5 * errors))
            await asyncio.sleep(interval)

    async def funpay_poll(self) -> None:
        upd = await self.fp.poll_updates()
        last_sync = float(self.db.kv_get("last_orders_sync", 0))
        if upd["orders_changed"] or time.time() - last_sync > 60:
            await self.sync_orders()
            self.db.kv_set("last_orders_sync", time.time())
        init = self.db.kv_get("chats_initialized", False)
        bookmarks = upd["bookmarks"]
        now = time.time()
        if bookmarks:
            self._runner_bm_at = now
        elif now - getattr(self, "_runner_bm_at", 0) > 60 and now - getattr(self, "_page_bm_at", 0) > 20:
            # runner давно не присылал список диалогов — берём его со страницы /chat/
            self._page_bm_at = now
            try:
                bookmarks = await self.fp.get_bookmarks_page()
            except FunPayError as e:
                log.warning("список диалогов /chat/: %s", e)
        fetched = 0
        for bm in bookmarks:
            seen = self.db.last_seen_message(bm.chat_id)
            if not init:
                self.db.set_last_seen_message(bm.chat_id, bm.last_message_id)
                continue
            if bm.last_message_id > seen:
                if fetched >= 6:         # остальные чаты — в следующем опросе, чтобы опрос не затягивался
                    break
                fetched += 1
                try:
                    msgs = await self.fp.get_chat_history(bm.chat_id, seen)
                except FunPayError as e:
                    log.warning("чат %s: %s", bm.chat_id, e)
                    continue
                if seen == 0:
                    # чат читаем впервые: запоминаем, докуда дочитали, даже если отвечать не на что
                    self.db.set_last_seen_message(bm.chat_id, max([bm.last_message_id] +
                                                                  [m.message_id for m in msgs]))
                    msgs = self._unanswered_tail(msgs)
                await self.process_chat(bm.chat_id, msgs)
        if not init:                     # первый опрос: старую переписку не обрабатываем, дальше — всё новое
            self.db.kv_set("chats_initialized", True)
        else:
            await self.poll_order_chats()
        self.last_ok["funpay_poll"] = time.time()

    def _unanswered_tail(self, msgs: list[ChatMessage]) -> list[ChatMessage]:
        """Чат, который бот ещё не читал: берём только то, что пришло после нашего последнего ответа
        (а если мы там ещё не писали — последние 3 сообщения), чтобы не отвечать на старую переписку."""
        msgs = sorted(msgs, key=lambda m: m.message_id)
        own = [i for i, m in enumerate(msgs) if m.author_id == self.fp.user_id]
        return msgs[own[-1] + 1:] if own else msgs[-3:]

    async def poll_order_chats(self) -> None:
        """Чаты заказов, где ждём ответа покупателя (username), читаем напрямую — не полагаясь на список диалогов."""
        now = time.time()
        if now - getattr(self, "_order_chats_at", 0) < 12:
            return
        self._order_chats_at = now
        for o in self.db.orders_by_status(S.AWAITING_USERNAME)[:10]:
            chat = o["chat_id"]
            if not chat:
                continue
            seen = self.db.last_seen_message(chat)
            try:
                msgs = await self.fp.get_chat_history(chat, seen)
            except FunPayError as e:
                log.warning("чат заказа #%s: %s", o["order_id"], e)
                continue
            if not msgs:
                continue
            if seen == 0:
                # чат ещё не читали: переписку до оплаты этого заказа не разбираем
                self.db.set_last_seen_message(chat, max(m.message_id for m in msgs))
                start = 0
                for m in msgs:
                    k = classify_system(m.text) if m.is_system else None
                    if k and k[0] == "paid" and k[1] == o["order_id"]:
                        start = m.message_id
                if start:
                    msgs = [m for m in msgs if m.message_id > start]
                else:                    # отметки об оплате нет в истории — читаем после нашего последнего ответа
                    msgs = self._unanswered_tail(msgs)
            await self.process_chat(chat, msgs)

    async def recover(self) -> None:
        """После перезапуска: заказы с транзакцией «в полёте» сверяются с блокчейном, READY — снова в очередь."""
        if self.gifts is not None:
            await self.gifts.recover()
        for o in self.db.orders_by_status(S.PURCHASING):
            if is_gift(o):
                continue
            p = self.db.last_purchase(o["order_id"])
            if p is None or p["seqno"] is None:
                self._set(o["order_id"], S.READY, "recovered_unsent")
            else:
                self._set(o["order_id"], S.SENT, "recovered_in_flight", sent_at=o["sent_at"] or time.time())
        for o in self.db.orders_by_status(S.READY):
            self.enqueue(o["order_id"])

    # ================================================================== один экземпляр на базу
    LEASE_TTL = 75.0

    def take_lease(self) -> dict | None:
        """Аренда базы: два запущенных бота на одной базе и одном аккаунте FunPay мешают друг другу
        (дерутся за лоты, могут дважды выдать заказ). Возвращает чужую живую аренду или None, если взяли свою."""
        if self.db.path == ":memory:":
            return None
        cur = self.db.kv_get("engine_lease") or {}
        if cur.get("id") and cur["id"] != self.lease_id and time.time() - float(cur.get("ts") or 0) < self.LEASE_TTL:
            return cur
        self.db.kv_set("engine_lease", {"id": self.lease_id, "pid": os.getpid(), "ts": time.time()})
        return None

    async def remote_inbox(self) -> None:
        """Команды из файла data/remote_cmd.txt (по строке, как в Telegram: «approve ABC», «lots_off»).
        Файл забирается и удаляется, ответы дописываются в data/remote_cmd.out.txt."""
        if self.db.path == ":memory:":
            return
        folder = Path(self.db.path).parent
        inbox, out = folder / "remote_cmd.txt", folder / "remote_cmd.out.txt"
        if not inbox.exists():
            return
        work = folder / "remote_cmd.work.txt"
        inbox.replace(work)
        lines = [l.strip().lstrip("/") for l in work.read_text(encoding="utf-8-sig").splitlines()]
        work.unlink()
        for line in lines:
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            try:
                res = await self.handle_command(parts[0].lower(), parts[1:])
            except Exception as e:  # noqa: BLE001
                res = f"ошибка: {e!r}"
            log.warning("удалённая команда «%s»: %s", line, str(res)[:300])
            with out.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} > {line}\n{res}\n\n")

    async def lease_keeper(self) -> None:
        while True:
            await asyncio.sleep(20)
            other = self.take_lease()
            if other:
                await self.notifier.notify("CRITICAL", "second_instance", "Запущен второй StarsBot на той же базе "
                                           f"(pid {other.get('pid')}) — этот экземпляр останавливается.")
                asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.stop()))
                return

    def release_lease(self) -> None:
        try:
            if (self.db.kv_get("engine_lease") or {}).get("id") == self.lease_id:
                self.db.kv_set("engine_lease", None)
        except Exception:  # noqa: BLE001
            pass

    LOT_AMOUNT_KEYS = (("funpay", "lot_amount:"), ("gifts", "gift_lot_amount:"), ("premium", "premium_lot_amount:"),
                       ("giftshop", "shop_lot_amount:"))

    def reset_lot_amounts(self) -> None:
        """Забыть записанное «наличие» лотов: следующая синхронизация выставит его на FunPay заново."""
        for section, prefix in self.LOT_AMOUNT_KEYS:
            for l in ((self.cfg.get(section) or {}).get("lots") or []):
                if l.get("offer_id"):
                    self.db.kv_set(prefix + str(l["offer_id"]), None)

    async def refresh_stock(self) -> None:
        """Пересчитать наличие лотов по балансу (у StarsBot — кошелёк TON)."""
        await self.wallet_watch()

    def no_lots_yet(self) -> bool:
        return not any(l.get("offer_id") for l in (self.cfg.funpay.lots or []))

    def start_banner(self) -> str:
        return (f"🤖 Бот запущен. FunPay: {self.fp.username}, кошелёк: {self.wallet.friendly_address}, Fragment: "
                f"{'ok' if self.fragment_ok else 'нет связи'}{' · DRY-RUN' if self.cfg.general.dry_run else ''}")

    async def start(self) -> None:
        other = self.take_lease()
        for _ in range(int(self.LEASE_TTL // 5) + 2):    # перезапуск: прежний процесс ещё закрывается
            if not other:
                break
            await asyncio.sleep(5)
            other = self.take_lease()
        if other:
            raise RuntimeError(f"уже работает другой экземпляр StarsBot на этой базе (pid {other.get('pid')}). "
                               "Закройте его (диспетчер задач → python.exe) и запустите бота снова.")
        await self.fp.login()
        await self.recover()
        await self.ensure_fragment(force=True)
        if not self.cfg.general.dry_run:
            # остатки, записанные в тестовом режиме, на FunPay не отправлялись: первая синхронизация
            # в боевом режиме должна выставить наличие и включить лоты заново
            self.reset_lot_amounts()
        lm = getattr(self, "lot_manager", None)
        if lm is not None:
            try:
                msg = await lm.refresh_texts()
                if "обновлены" in msg:
                    log.info("лоты: %s", msg)
            except Exception as e:  # noqa: BLE001
                log.warning("обновление описаний лотов: %s", e)
        no_lots = self.no_lots_yet()
        if lm is not None and (self.cfg.funpay.get("auto_create_lots") or no_lots):
            try:
                report = await lm.create_missing()
                if any("выставлен" in r for r in report):
                    await self.notifier.notify("INFO", "lots_created", "Выставлены лоты: " + "; ".join(report))
            except Exception as e:  # noqa: BLE001
                log.warning("автовыставление лотов: %s", e)
        await self.notifier.notify("INFO", "started", self.start_banner())
        self.notifier.command_handler = self.handle_command
        loops = [
            self._loop("funpay", float(self.cfg.funpay.poll_interval), self.funpay_poll),
            self._loop("ton_watch", 8, self.watch_purchases),
            self._loop("wallet", 90, self.wallet_watch),
            self._loop("timers", 30, self.timers),
            self._loop("watchdog", 120, self.watchdog),
            self._loop("backup", 3600, self.backup_db),
            self._loop("raise", 300, self.raise_lots),
            self._loop("rtba", float(self.cfg.rtba.interval_seconds), self.rtba_step),
            self._loop("price_guard", 60 * float((self.cfg.get("safety") or {}).get("price_guard_minutes") or 10),
                       self.price_guard),
            self.deliver_worker(),
            self.notifier.poll_commands(),
            self.lease_keeper(),
            self._loop("remote_inbox", 15, self.remote_inbox),
            self._loop("orbit", 5, self.orbit.tick),
        ]
        if self.analytics:
            loops.append(self._loop("analytics", 600, self.analytics.tick))
        if self.gifts is not None and self.gifts.enabled:
            loops.append(self._loop("gifts", 120, self.gifts.watch))
        if getattr(self, "autopilot", None) is not None:
            loops.append(self._loop("autopilot", 300, self.autopilot.tick))
        if self.premium is not None:
            loops += self.premium.loops(self._loop)
        tr = getattr(self, "treasury", None)
        if tr is not None and tr.enabled:
            loops.append(self._loop("treasury", float(tr.t.get("interval_seconds") or 120), tr.tick))
        if getattr(self, "supply", None) is not None and self.supply.enabled:
            loops.append(self._loop("supply", 120, self.supply.tick))
        if self.gifts is not None and getattr(self.gifts, "moved", False):
            loops.append(self._loop("gift_handover", 300, self.gifts.handover_tick))
        if self.nft is not None and self.coord.enabled:
            loops.append(self._loop("nft", 60, self.nft.tick))
        assort = getattr(self.gifts, "assortment", None) if self.gifts is not None else None
        if assort is not None:
            loops.append(self._loop("assortment", 600, assort.tick))
        for ch in self.channels.values():
            loops += ch.loops(self._loop)
        self._tasks = [asyncio.create_task(c) for c in loops]
        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        me = asyncio.current_task()
        for t in self._tasks:
            if t is not me:
                t.cancel()
        await asyncio.gather(*[t for t in self._tasks if t is not me], return_exceptions=True)
        self.release_lease()
