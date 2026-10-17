"""Движок бота: жизненный цикл заказа FunPay → обмен Steam → закрытие сделки."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from mannbot.core import messages as M
from mannbot.core.models import ChatMessage, FunPayOrder, OrderStatus as S, TradeOfferState as T
from mannbot.core.risk import RiskEngine
from mannbot.core.rtba import RTBAEngine
from mannbot.crm.db import Database
from mannbot.funpay.client import FunPayAuthError, FunPayError
from mannbot.funpay.parsers import classify_system
from mannbot.notify.notifier import Notifier
from mannbot.steam.client import SteamAuthError, SteamError
from mannbot.steam.tradelink import TradeLinkError, find_trade_url, looks_like_wrong_link, parse_trade_url

log = logging.getLogger("engine")

# Ошибки, в которых виноваты данные покупателя → просим реальные данные
BUYER_DATA_ERRORS = {"invalid_token", "cannot_trade", "trade_banned", "inventory_full", "inventory_private",
                     "bad_format", "no_token", "bad_token", "self", "blacklisted"}


class Engine:
    def __init__(self, cfg, db: Database, funpay, steam, notifier: Notifier, analytics=None):
        self.cfg = cfg
        self.db = db
        self.fp = funpay
        self.steam = steam
        self.notifier = notifier
        self.analytics = analytics
        self.risk = RiskEngine(cfg.risk, db)
        self.rtba = RTBAEngine(cfg.rtba)
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._chat_last_sent: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._order_re = re.compile(cfg.funpay.order_match_regex)
        self.started_at = time.time()
        self.last_ok: dict[str, float] = {}
        self.stock_cache: dict[str, Any] = {"total": None, "ts": 0}

    # ================================================================== helpers
    @property
    def paused(self) -> bool:
        return bool(self.db.kv_get("paused", False))

    def set_paused(self, value: bool) -> None:
        self.db.kv_set("paused", bool(value))

    def _bot_vars(self) -> dict:
        level = f" (уровень Steam {self.steam.level})" if getattr(self.steam, "level", None) else ""
        return {"bot_name": self.steam.persona_name or "MannBot", "bot_level": level,
                "ttl": self.cfg.steam.offer_ttl_hours}

    def _order_vars(self, o) -> dict:
        return {"order_id": o["order_id"], "buyer": o["buyer_username"], "keys": o["keys_qty"],
                **self._bot_vars()}

    async def say(self, o, key: str, **extra: Any) -> None:
        """Отправить шаблонное сообщение в чат заказа."""
        await self.say_text(o, M.render(o["lang"] or self.cfg.general.language, key,
                                        **{**self._order_vars(o), **extra}))

    async def say_text(self, o, text: str) -> None:
        chat_id = o["chat_id"]
        if not chat_id:
            await self.notifier.notify("WARNING", "no_chat", f"У заказа #{o['order_id']} не найден чат",
                                       order_id=o["order_id"])
            return
        gap = float(self.cfg.deal.message_rate_limit_seconds) - (time.time() - self._chat_last_sent.get(chat_id, 0))
        if gap > 0:
            await asyncio.sleep(gap)
        try:
            await self.fp.send_message(chat_id, text)
            self._chat_last_sent[chat_id] = time.time()
            self.db.log_message(chat_id, o["order_id"], "out", text)
        except FunPayError as e:
            await self.notifier.notify("WARNING", "send_message_failed",
                                       f"Не удалось написать в чат заказа #{o['order_id']}: {e}",
                                       order_id=o["order_id"])

    def _set(self, order_id: str, status: S | None = None, event: str | None = None, **fields: Any):
        if status is not None:
            fields["status"] = status
        if fields:
            self.db.update_order(order_id, **fields)
        if event:
            self.db.add_event(order_id, event, status=status.value if status else None,
                              **{k: v for k, v in fields.items() if k != "status"})
        return self.db.get_order(order_id)

    def enqueue(self, order_id: str) -> None:
        if order_id not in self._queued:
            self._queued.add(order_id)
            self.queue.put_nowait(order_id)

    def _lot_for(self, description: str) -> dict:
        lots = self.cfg.funpay.lots or [{}]
        for lot in lots:
            rx = lot.get("match")
            if rx and re.search(rx, description or "", re.I):
                return lot
        return lots[0]

    # ================================================================== заказы
    async def sync_orders(self) -> None:
        sales = await self.fp.get_sales()
        first_run = not self.db.kv_get("orders_initialized", False)
        for so in sales:
            row = self.db.get_order(so.order_id)
            if row is None:
                if so.status != "paid" or not self._order_re.search(so.description or ""):
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
        lot = self._lot_for(so.description)
        qty = page.quantity or so.quantity or 1
        keys = qty * int(lot.get("keys_per_unit", 1) or 1)
        self.db.create_order(order_id=so.order_id, buyer_username=so.buyer_username,
                             buyer_id=so.buyer_id or page.buyer_id or 0, chat_id=page.chat_id,
                             description=so.description, quantity=qty, keys_qty=keys, price=so.price,
                             currency=so.currency, fp_status="paid", lot_offer_id=str(lot.get("offer_id", "")),
                             lang=self.cfg.general.language)
        self.db.touch_customer(so.buyer_id or page.buyer_id or 0, so.buyer_username, new_order_total=so.price,
                               keys=keys)
        self.db.add_event(so.order_id, "created", price=so.price, keys=keys, imported=imported)
        o = self.db.get_order(so.order_id)
        if imported:
            # заказы, оплаченные до первого запуска бота, — только продавцу: их могли уже выдать вручную
            self._set(so.order_id, S.MANUAL, "imported_on_first_run")
            await self.notifier.notify("WARNING", "imported_order",
                                       f"Заказ #{so.order_id} оплачен до запуска бота — проверьте вручную "
                                       f"(/take {so.order_id} — передать боту).", order_id=so.order_id)
            return
        await self.notifier.notify("INFO", "new_order", f"🛒 Новый заказ #{so.order_id}: {keys} ключ(ей), "
                                   f"{so.price:g} {so.currency}, покупатель {so.buyer_username}",
                                   order_id=so.order_id)
        if self.cfg.risk.enabled:
            risk = self.risk.assess(o)
            if risk.decision == "block":
                self._set(so.order_id, S.BLOCKED, "risk_block", risk_score=risk.score,
                          risk_reasons=json.dumps(risk.reasons, ensure_ascii=False))
                await self.say(o, "call_seller")
                await self.notifier.notify("CRITICAL", "risk_block", f"Заказ #{so.order_id} заблокирован "
                                           f"риск-системой ({risk.score}): " + "; ".join(risk.reasons),
                                           order_id=so.order_id)
                return
        available = await self.available_keys()  # уже учитывает ключи этого заказа как обещанные
        if available is not None and available < 0:
            await self.out_of_stock(o, available)
            return
        await self.say(o, "greeting")
        self._set(so.order_id, S.AWAITING_LINK, "greeting_sent")

    async def out_of_stock(self, o, available: int | None) -> None:
        self._set(o["order_id"], S.MANUAL, "out_of_stock", fail_reason="out_of_stock")
        await self.say(o, "out_of_stock")
        await self.notifier.notify("CRITICAL", "out_of_stock",
                                   f"Нет ключей для заказа #{o['order_id']}: нужно {o['keys_qty']}, "
                                   f"доступно {available}. Пополните инвентарь или сделайте возврат.",
                                   order_id=o["order_id"])
        if self.cfg.deal.auto_refund_out_of_stock:
            await self.admin_refund(o["order_id"], reason="нет в наличии (авто)")

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
            # Покупатель подтвердил ДО получения ключей — продолжаем выдачу, это наша обязанность.
            await self.notifier.notify("WARNING", "confirmed_before_delivery",
                                       f"Заказ #{oid} подтверждён до выдачи ключей — бот продолжит выдачу.",
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

    async def on_refund(self, o) -> None:
        oid = o["order_id"]
        delivered = S(o["status"]) in (S.DELIVERED, S.COMPLETED, S.ESCROW)
        if o["offer_id"] and self.cfg.deal.cancel_offer_on_refund and S(o["status"]) == S.OFFER_SENT:
            ok = await self._safe_steam(self.steam.cancel_offer(o["offer_id"]))
            self.db.update_offer_state(o["offer_id"], int(T.CANCELED), canceled_by_bot=True)
            self.db.add_event(oid, "offer_canceled_refund", ok=bool(ok))
        self.db.release_assets(oid)
        self._set(oid, S.REFUNDED, "refunded", fail_reason=o["fail_reason"] or "refund")
        self.db.customer_incr(o["buyer_id"], "refunded_count")
        await self.say(self.db.get_order(oid), "refunded")
        level = "CRITICAL" if delivered else "WARNING"
        await self.notifier.notify(level, "refund", f"Возврат по заказу #{oid}" +
                                   (" — НО КЛЮЧИ УЖЕ ВЫДАНЫ! Проверьте арбитраж." if delivered else ""),
                                   order_id=oid)

    # ================================================================== чат
    async def process_chat(self, chat_id: str, msgs: list[ChatMessage]) -> None:
        for msg in sorted(msgs, key=lambda m: m.message_id):
            self.db.set_last_seen_message(chat_id, msg.message_id)
            if msg.author_id == self.fp.user_id:
                continue
            if msg.is_system:
                await self.on_system_message(chat_id, msg)
                continue
            await self.on_buyer_message(chat_id, msg)

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
        self.db.log_message(chat_id, None, "in", msg.text)
        o = self.db.active_order_for_chat(chat_id)
        if o is None and find_trade_url(msg.text):
            # покупатель прислал ссылку раньше, чем мы увидели оплату — синхронизируем заказы
            await self.sync_orders()
            o = self.db.active_order_for_chat(chat_id)
        if o is None:
            delivered = self.db.one("SELECT * FROM orders WHERE chat_id=? ORDER BY created_at DESC LIMIT 1",
                                    (chat_id,))
            if M.is_off_platform_request(msg.text) and delivered:
                await self.say(delivered, "off_platform")
            # п. 2.1.4 — не игнорируем: пересылаем продавцу
            await self.notifier.notify("INFO", "chat_message",
                                       f"💬 {msg.author_name}: {msg.text[:500]}", throttle_key=f"chat:{chat_id}")
            return
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

        url = find_trade_url(msg.text)
        cmd = M.detect_command(msg.text)
        status = S(o["status"])
        if M.is_off_platform_request(msg.text) and not url:
            await self.say(o, "off_platform")
            return
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
        if cmd == "resend":
            await self.request_resend(o)
            return
        if cmd == "send_anyway" and o["trade_url"] and o["escrow_days"] and status == S.AWAITING_LINK:
            self.db.add_event(oid, "buyer_accepts_escrow")
            await self._after_link_ok(o, accept_escrow=True)
            return
        if url and status in (S.AWAITING_LINK, S.NEW):
            await self.handle_trade_link(o, url)
            return
        if url and status in (S.OFFER_SENT, S.PENDING_APPROVAL, S.READY_TO_SEND):
            await self.say(o, "status", status_text=M.status_text(o["lang"], status.value))
            return
        if status == S.AWAITING_LINK:
            wrong = looks_like_wrong_link(msg.text)
            if wrong:
                await self.bad_data(o, wrong)
            else:
                await self.say(o, "unknown")
                if "?" in msg.text:
                    await self.notifier.notify("INFO", "buyer_question", f"Вопрос в заказе #{oid}: {msg.text[:300]}",
                                               order_id=oid)
            return
        await self.notifier.notify("INFO", "buyer_message", f"💬 #{oid} {msg.author_name}: {msg.text[:300]}",
                                   order_id=oid)

    # ================================================================== ссылка на обмен
    async def bad_data(self, o, code: str, detail: str = "") -> None:
        oid = o["order_id"]
        attempts = int(o["link_attempts"] or 0) + 1
        max_att = int(self.cfg.deal.max_link_attempts)
        why = M.reason(o["lang"], code, detail)
        o = self._set(oid, S.AWAITING_LINK, "bad_data", link_attempts=attempts, fail_reason=code)
        if attempts >= max_att:
            await self.say(o, "bad_data_final", reason=why)
            self._set(oid, S.MANUAL, "bad_data_limit")
            await self.notifier.notify("WARNING", "bad_data_limit", f"Заказ #{oid}: покупатель {attempts} раз "
                                       f"прислал неверные данные ({why}). Нужна ручная обработка.", order_id=oid)
        else:
            await self.say(o, "bad_data", reason=why, attempt=attempts, max_attempts=max_att)

    async def handle_trade_link(self, o, url: str) -> None:
        oid = o["order_id"]
        try:
            link = parse_trade_url(url)
        except TradeLinkError as e:
            await self.bad_data(o, e.code, str(e))
            return
        if link.steam_id64 in [str(x) for x in (self.cfg.risk.blacklist_steam_ids or [])]:
            await self.bad_data(o, "blacklisted")
            return
        try:
            check = await self.steam.check_trade_link(link)
        except SteamAuthError as e:
            await self.steam_auth_problem(e)
            self._set(oid, event="link_check_deferred", trade_url=link.canonical)
            return
        except SteamError as e:
            await self.notifier.notify("WARNING", "steam_check_failed", f"Проверка ссылки #{oid}: {e}", order_id=oid)
            await self.say(o, "link_received", partner=link.steam_id64)
            self._set(oid, event="link_check_failed", trade_url=link.canonical, steam_id64=link.steam_id64)
            return
        if not check.ok:
            if check.reason_code == "network":
                await self.notifier.notify("WARNING", "steam_check_network", f"#{oid}: {check.reason_text}",
                                           order_id=oid)
                return
            await self.bad_data(o, check.reason_code, check.reason_text)
            return
        o = self._set(oid, event="link_ok", trade_url=link.canonical, steam_id64=check.steam_id64,
                      partner_name=check.partner_name or "", escrow_days=check.their_escrow_days,
                      link_received_at=time.time())
        self.db.customer_add_steam_id(o["buyer_id"], check.steam_id64)
        await self.say(o, "link_received", partner=check.partner_name or check.steam_id64)
        await self._after_link_ok(o)
        # Другие оплаченные заказы того же покупателя в этом чате — та же ссылка, без повторных вопросов
        for other in self.db.q("SELECT * FROM orders WHERE chat_id=? AND status=? AND order_id<>? "
                               "AND (trade_url IS NULL OR trade_url='')", (o["chat_id"], S.AWAITING_LINK.value, oid)):
            other = self._set(other["order_id"], event="link_reused", trade_url=link.canonical,
                              steam_id64=check.steam_id64, partner_name=check.partner_name or "",
                              escrow_days=check.their_escrow_days, link_received_at=time.time())
            await self._after_link_ok(other)

    async def _after_link_ok(self, o, accept_escrow: bool = False) -> None:
        oid = o["order_id"]
        days = int(o["escrow_days"] or 0)
        if days > 0 and not accept_escrow:
            policy = self.cfg.deal.trade_hold_policy
            key = {"require_fix": "trade_hold_fix", "warn_and_send": "trade_hold_send"}.get(policy, "trade_hold_manual")
            await self.say(o, "trade_hold", days=days, policy_text=M.render(o["lang"], key))
            if policy == "require_fix":
                return
            if policy == "manual":
                self._set(oid, S.PENDING_APPROVAL, "escrow_manual")
                await self.notifier.notify("WARNING", "trade_hold", f"Заказ #{oid}: у покупателя задержка {days} дн. "
                                           f"— /approve {oid} или /reject {oid}", order_id=oid)
                return
        if self.cfg.risk.enabled and not o["approved"]:
            risk = self.risk.assess(o, steam_id=o["steam_id64"], escrow_days=days)
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
                                           f"\n/approve {oid} — выдать, /reject {oid} — отклонить", order_id=oid)
                return
        self._set(oid, S.READY_TO_SEND, "ready")
        self.enqueue(oid)

    # ================================================================== выдача
    async def available_keys(self, force: bool = False) -> int | None:
        """Свободные ключи = обмениваемые в инвентаре − зарезервированные − обещанные активным заказам."""
        try:
            if force or time.time() - self.stock_cache["ts"] > 60 or self.stock_cache["total"] is None:
                keys = await self.steam.get_tradable_keys()
                self.stock_cache = {"total": len(keys), "ids": keys, "ts": time.time()}
        except SteamError as e:
            log.warning("инвентарь недоступен: %s", e)
            if self.stock_cache["total"] is None:
                return None
        reserved = self.db.reserved_asset_ids()
        free = len([k for k in self.stock_cache.get("ids", []) if k not in reserved])
        return free - self.db.keys_committed()

    async def deliver_worker(self) -> None:
        while True:
            oid = await self.queue.get()
            self._queued.discard(oid)
            try:
                if not self.paused:
                    await self.deliver(oid)
                else:
                    await asyncio.sleep(5)
                    self.enqueue(oid)
            except SteamAuthError as e:
                await self.steam_auth_problem(e)
                await asyncio.sleep(30)
                self.enqueue(oid)
            except Exception as e:  # noqa: BLE001
                log.exception("ошибка выдачи %s", oid)
                await self.notifier.notify("CRITICAL", "deliver_crash", f"Сбой выдачи #{oid}: {e!r}", order_id=oid)
                self._set(oid, S.MANUAL, "deliver_crash", fail_reason=str(e)[:200])

    async def deliver(self, oid: str) -> None:
        o = self.db.get_order(oid)
        if not o or S(o["status"]) != S.READY_TO_SEND:
            return
        # Идемпотентность: если по заказу уже есть живой трейд — второй не отправляем.
        if o["offer_id"]:
            prev = self.db.get_offer(o["offer_id"])
            if prev and prev["state"] in (int(T.ACTIVE), int(T.NEEDS_CONFIRMATION)):
                self._set(oid, S.OFFER_SENT, "dup_prevented")
                return
        link = parse_trade_url(o["trade_url"])
        need = int(o["keys_qty"])
        max_per = int(self.cfg.steam.max_keys_per_offer)
        if need > max_per:
            self._set(oid, S.MANUAL, "too_many_keys", fail_reason="too_many_keys")
            await self.notifier.notify("WARNING", "too_many_keys", f"#{oid}: {need} ключей > max_keys_per_offer",
                                       order_id=oid)
            return
        # Steam мог создать трейд, а ответ потеряться по таймауту — ищем свой трейд по номеру заказа.
        try:
            sent_active, _ = await self.steam.get_active_offers()
        except SteamError:
            sent_active = []
        for off in sent_active:
            if f"#{oid}" in str(off.get("message", "")):
                found = str(off["tradeofferid"])
                assets = [str(i.get("assetid")) for i in off.get("items_to_give", [])]
                self.db.record_offer(found, oid, int(off.get("trade_offer_state", T.ACTIVE)), assets)
                self.db.reserve_assets(oid, assets)
                o = self._set(oid, S.OFFER_SENT, "offer_adopted", offer_id=found, offer_sent_at=time.time())
                await self.say(o, "offer_sent", offer_id=found)
                return
        await self.available_keys(force=True)
        reserved = self.db.reserved_asset_ids()
        free = [k for k in self.stock_cache.get("ids", []) if k not in reserved]
        if len(free) < need:
            await self.out_of_stock(o, len(free))
            return
        assets = free[:need]
        self.db.reserve_assets(oid, assets)
        message = self.cfg.steam.offer_message.format(order_id=oid, qty=need)
        try:
            sent = await self.steam.send_offer(link, assets, message)
        except SteamError as e:
            self.db.release_assets(oid)
            self.db.add_event(oid, "send_failed", code=e.code, error=str(e))
            if e.code in BUYER_DATA_ERRORS:
                await self.bad_data(o, e.code, str(e))
            elif e.code in ("rate_limit", "network", "timeout", "server", "too_many_offers"):
                fails = int(self.db.kv_get(f"send_fail:{oid}", 0)) + 1
                self.db.kv_set(f"send_fail:{oid}", fails)
                if fails >= 5:
                    self._set(oid, S.MANUAL, "send_retry_limit", fail_reason=e.code)
                    await self.notifier.notify("CRITICAL", "send_retry_limit", f"#{oid}: Steam не принимает трейд "
                                               f"({e}). Требуется вмешательство.", order_id=oid)
                else:
                    asyncio.get_running_loop().call_later(30 * fails, self.enqueue, oid)
            else:
                self._set(oid, S.MANUAL, "send_failed", fail_reason=e.code)
                await self.notifier.notify("CRITICAL", "send_failed", f"#{oid}: трейд не отправлен: {e}", order_id=oid)
            return
        self.db.record_offer(sent.offer_id, oid, int(T.NEEDS_CONFIRMATION if sent.needs_confirmation else T.ACTIVE),
                             assets)
        self._set(oid, event="offer_created", offer_id=sent.offer_id)
        if sent.needs_confirmation:
            ok = await self.steam.confirm_offer(sent.offer_id)
            if not ok:
                self._set(oid, S.MANUAL, "confirm_failed", fail_reason="confirm_failed")
                await self.notifier.notify("CRITICAL", "confirm_failed",
                                           f"#{oid}: трейд {sent.offer_id} создан, но НЕ подтверждён в Steam Guard. "
                                           "Проверьте identity_secret / подтвердите вручную.", order_id=oid)
                return
            self.db.update_offer_state(sent.offer_id, int(T.ACTIVE))
        o = self._set(oid, S.OFFER_SENT, "offer_sent", offer_sent_at=time.time())
        self.db.kv_set(f"send_fail:{oid}", 0)
        await self.say(o, "offer_sent", offer_id=sent.offer_id)
        await self._verify_offer(o, sent.offer_id)

    async def _verify_offer(self, o, offer_id: str) -> None:
        """Сверяем созданный трейд: правильный получатель и мы ничего не запрашиваем."""
        if offer_id.startswith("dry"):
            return
        try:
            offer = await self.steam.get_offer(offer_id)
        except SteamError:
            return
        their = str(int(offer.get("accountid_other", 0)) + 76561197960265728)
        if their != o["steam_id64"] or offer.get("items_to_receive"):
            await self._safe_steam(self.steam.cancel_offer(offer_id))
            self.db.update_offer_state(offer_id, int(T.CANCELED), canceled_by_bot=True)
            self._set(o["order_id"], S.MANUAL, "offer_mismatch", fail_reason="offer_mismatch")
            await self.notifier.notify("CRITICAL", "offer_mismatch", f"Трейд {offer_id} по #{o['order_id']} не совпал "
                                       "с ожидаемым (получатель/предметы) и отменён!", order_id=o["order_id"])

    async def _safe_steam(self, coro):
        try:
            return await coro
        except SteamError as e:
            log.warning("steam: %s", e)
            return None

    async def request_resend(self, o) -> None:
        oid = o["order_id"]
        status = S(o["status"])
        if status not in (S.AWAITING_LINK, S.MANUAL) or not o["trade_url"]:
            await self.say(o, "status", status_text=M.status_text(o["lang"], status.value))
            return
        if int(o["resends"] or 0) >= int(self.cfg.deal.max_resends):
            await self.say(o, "resend_limit")
            await self.notifier.notify("WARNING", "resend_limit", f"#{oid}: исчерпан лимит повторов", order_id=oid)
            return
        self._set(oid, S.READY_TO_SEND, "resend", resends=int(o["resends"] or 0) + 1)
        self.enqueue(oid)

    # ================================================================== отслеживание трейдов
    async def watch_offers(self) -> None:
        for o in self.db.orders_by_status(S.OFFER_SENT, S.ESCROW):
            oid, offer_id = o["order_id"], o["offer_id"]
            if not offer_id:
                continue
            try:
                state = await self.steam.get_offer_state(offer_id)
            except SteamAuthError as e:
                await self.steam_auth_problem(e)
                return
            except SteamError as e:
                log.debug("offer %s: %s", offer_id, e)
                continue
            prev = self.db.get_offer(offer_id)
            if prev and prev["state"] == int(state) and state == T.ACTIVE:
                await self._check_offer_ttl(o)
                continue
            self.db.update_offer_state(offer_id, int(state))
            await self.on_offer_state(o, state)
        self.last_ok["steam_offers"] = time.time()

    async def _check_offer_ttl(self, o) -> None:
        sent = o["offer_sent_at"] or 0
        if time.time() - sent > float(self.cfg.steam.offer_ttl_hours) * 3600:
            await self._safe_steam(self.steam.cancel_offer(o["offer_id"]))
            self.db.update_offer_state(o["offer_id"], int(T.CANCELED), canceled_by_bot=True)
            await self.on_offer_state(o, T.EXPIRED)

    async def on_offer_state(self, o, state: T) -> None:
        oid = o["order_id"]
        if state == T.ACCEPTED:
            o = self._set(oid, S.DELIVERED, "delivered", delivered_at=time.time())
            self.db.release_assets(oid)
            self.stock_cache["ts"] = 0
            await self.say(o, "success")
            await self.notifier.notify("INFO", "delivered", f"🔑 Заказ #{oid} выдан: {o['keys_qty']} ключ(ей) → "
                                       f"{o['partner_name'] or o['steam_id64']}", order_id=oid)
            if o["fp_status"] == "closed":
                await self.on_buyer_confirmed(self.db.get_order(oid))
        elif state == T.IN_ESCROW:
            if S(o["status"]) != S.ESCROW:
                o = self._set(oid, S.ESCROW, "escrow")
                await self.say(o, "escrow")
        elif state in (T.DECLINED, T.EXPIRED, T.COUNTERED):
            self.db.release_assets(oid)
            what = {T.DECLINED: "отклонён", T.EXPIRED: "отменён по истечении срока",
                    T.COUNTERED: "изменён вами (контрпредложения не принимаются)"}[state]
            if o["lang"] == "en":
                what = {T.DECLINED: "declined", T.EXPIRED: "expired",
                        T.COUNTERED: "countered (counter-offers are not accepted)"}[state]
            o = self._set(oid, S.AWAITING_LINK, f"offer_{state.name.lower()}")
            await self.say(o, "declined", what=what)
            await self.notifier.notify("INFO", "offer_declined", f"#{oid}: трейд {state.name}", order_id=oid)
        elif state == T.CANCELED:
            off = self.db.get_offer(o["offer_id"])
            self.db.release_assets(oid)
            o = self._set(oid, S.AWAITING_LINK, "offer_canceled")
            if not (off and off["canceled_by_bot"]):
                # Мы не отменяли — классический признак API-скама на стороне покупателя.
                await self.say(o, "api_scam_warning")
                await self.notifier.notify("WARNING", "api_scam_suspect", f"#{oid}: трейд отменён не ботом — "
                                           "возможен API-скам у покупателя. Покупатель предупреждён.", order_id=oid)
        elif state in (T.INVALID_ITEMS, T.INVALID):
            self.db.release_assets(oid)
            self._set(oid, S.READY_TO_SEND, "offer_invalid_items")
            await self.notifier.notify("CRITICAL", "invalid_items", f"#{oid}: ключи в трейде стали недоступны — "
                                       "проверьте инвентарь и безопасность аккаунта! Повторная отправка.", order_id=oid)
            self.enqueue(oid)
        elif state == T.NEEDS_CONFIRMATION:
            if not await self.steam.confirm_offer(o["offer_id"], attempts=2):
                await self.notifier.notify("WARNING", "needs_confirmation", f"#{oid}: трейд ждёт подтверждения",
                                           order_id=oid)
        elif state == T.CANCELED_BY_SECOND_FACTOR:
            self.db.release_assets(oid)
            self._set(oid, S.MANUAL, "canceled_2fa")
            await self.notifier.notify("CRITICAL", "canceled_2fa", f"#{oid}: трейд отменён подтверждением "
                                       "Steam Guard — кто-то отклонил его в мобильном приложении?", order_id=oid)

    # ================================================================== безопасность аккаунта
    async def security_watch(self) -> None:
        try:
            sent, received = await self.steam.get_active_offers()
        except SteamAuthError as e:
            await self.steam_auth_problem(e)
            return
        except SteamError:
            return
        sec = self.cfg.security
        known = self.db.known_offer_ids()
        for off in sent:
            oid = str(off.get("tradeofferid"))
            if oid not in known and not self.db.kv_get(f"foreign_seen:{oid}"):
                self.db.kv_set(f"foreign_seen:{oid}", True)
                canceled = False
                if sec.cancel_foreign_offers:
                    canceled = bool(await self._safe_steam(self.steam.cancel_offer(oid)))
                await self.notifier.notify("CRITICAL", "foreign_offer", f"🚨 На аккаунте Steam исходящий трейд {oid}, "
                                           f"созданный НЕ ботом{' — отменён' if canceled else ''}. Если это не вы — "
                                           "смените пароль, отзовите API-ключ и завершите все сессии!",
                                           throttle_key=f"foreign:{oid}")
        for off in received:
            if off.get("items_to_give") and sec.decline_incoming_requests:
                await self._safe_steam(self.steam.decline_offer(str(off.get("tradeofferid"))))
                self.db.metric("declined_incoming", 1)
        self.last_ok["security"] = time.time()

    async def steam_auth_problem(self, e: Exception) -> None:
        await self.notifier.notify("CRITICAL", "steam_auth", f"Steam-сессия недействительна: {e}. Пробую перелогин.",
                                   throttle_key="steam_auth")
        try:
            await self.steam.ensure_session()
            await self.notifier.notify("INFO", "steam_auth_ok", "Steam: сессия восстановлена")
        except Exception as e2:  # noqa: BLE001
            await self.notifier.notify("CRITICAL", "steam_login_failed", f"Steam: перелогин не удался: {e2}. "
                                       "Выдача остановлена до исправления.", throttle_key="steam_login_failed")

    # ================================================================== таймеры / SLA
    async def timers(self) -> None:
        now = time.time()
        d = self.cfg.deal
        for o in self.db.orders_by_status(S.AWAITING_LINK):
            base = max(o["paid_at"] or 0, o["last_buyer_msg_at"] or 0)
            idle = (now - base) / 60
            if not o["reminded"] and not o["trade_url"] and idle >= float(d.reminder_after_minutes):
                self._set(o["order_id"], event="reminder", reminded=1)
                await self.say(o, "reminder")
            if not o["admin_alerted"] and idle >= float(d.admin_alert_after_minutes):
                self._set(o["order_id"], event="sla_alert", admin_alerted=1)
                await self.notifier.notify("WARNING", "sla_link", f"#{o['order_id']}: покупатель {o['buyer_username']} "
                                           f"не присылает данные {int(idle)} мин.", order_id=o["order_id"])
        for o in self.db.orders_by_status(S.PENDING_APPROVAL, S.MANUAL):
            if (now - (o["updated_at"] or now)) > 900 and not o["admin_alerted"]:
                self._set(o["order_id"], admin_alerted=1)
                await self.notifier.notify("WARNING", "waiting_admin", f"#{o['order_id']} ждёт решения продавца уже "
                                           f"{int((now - o['updated_at']) / 60)} мин. ({o['status']})",
                                           order_id=o["order_id"])
        for o in self.db.active_orders():
            total_h = (now - (o["paid_at"] or now)) / 3600
            if S(o["status"]) not in (S.DELIVERED,) and total_h > float(self.cfg.sla.order_total_hours):
                await self.notifier.notify("WARNING", "sla_total", f"#{o['order_id']} не закрыт {total_h:.0f} ч. "
                                           f"(статус {o['status']})", order_id=o["order_id"],
                                           throttle_key=f"sla_total:{o['order_id']}")

    # ================================================================== склад и лоты
    async def stock_sync(self) -> None:
        avail = await self.available_keys(force=True)
        if avail is None:
            return
        self.db.exec("INSERT INTO stock_history(ts,available,reserved) VALUES(?,?,?)",
                     (time.time(), avail, len(self.db.reserved_asset_ids())))
        await self._inventory_drop_check()
        last = self.db.kv_get("last_stock")
        if avail <= 0 and last != avail:
            await self.notifier.notify("CRITICAL" if avail < 0 else "WARNING", "stock_empty",
                                       f"Склад пуст: свободно {avail} ключей. Лот будет скрыт.", throttle_key="stock")
        self.db.kv_set("last_stock", avail)
        if not self.cfg.funpay.stock_sync or self.paused:
            return
        for lot in self.cfg.funpay.lots:
            if not lot.get("offer_id"):
                continue
            per = int(lot.get("keys_per_unit", 1) or 1)
            amount = max(0, min(avail // per, int(self.cfg.funpay.max_display_amount)))
            key = f"lot_amount:{lot['offer_id']}"
            if self.db.kv_get(key) == amount:
                continue
            active = amount > 0 or not self.cfg.funpay.deactivate_when_empty
            try:
                await self.fp.update_lot(int(lot["offer_id"]), int(lot["node_id"]), amount=max(amount, 1 if active else 0),
                                         active=active)
                self.db.kv_set(key, amount)
                self.db.add_event("-", "lot_stock", offer_id=lot["offer_id"], amount=amount, active=active)
            except FunPayError as e:
                await self.notifier.notify("WARNING", "lot_update_failed", f"Лот {lot['offer_id']}: {e}",
                                           throttle_key=f"lot:{lot['offer_id']}")

    async def _inventory_drop_check(self) -> None:
        """Ключи пропали из инвентаря не через наши трейды → возможна компрометация аккаунта."""
        total = self.stock_cache.get("total")
        prev = self.db.kv_get("inv_check")
        now = time.time()
        self.db.kv_set("inv_check", {"total": total, "ts": now})
        if total is None or not prev or not self.cfg.security.inventory_drop_alert:
            return
        delivered = int(self.db.scalar("SELECT SUM(keys_qty) FROM orders WHERE delivered_at>?", (prev["ts"],), 0))
        missing = prev["total"] - total - delivered
        if missing > 0:
            await self.notifier.notify("CRITICAL", "inventory_drop", f"🚨 Из инвентаря пропало {missing} ключ(ей) без "
                                       "трейдов бота! Проверьте историю обменов и безопасность аккаунта Steam.",
                                       throttle_key="inventory_drop")

    async def raise_lots(self) -> None:
        if not self.cfg.funpay.auto_raise or self.paused:
            return
        last = float(self.db.kv_get("last_raise", 0))
        if time.time() - last < float(self.cfg.funpay.raise_interval_minutes) * 60:
            return
        done = set()
        for lot in self.cfg.funpay.lots:
            key = (lot.get("game_id"), lot.get("node_id"))
            if not all(key) or key in done:
                continue
            done.add(key)
            try:
                ok, msg = await self.fp.raise_lots(int(key[0]), int(key[1]))
                self.db.add_event("-", "raise", ok=ok, msg=msg)
            except FunPayError as e:
                log.warning("поднятие лотов: %s", e)
        self.db.kv_set("last_raise", time.time())

    async def rtba_step(self) -> None:
        cfg = self.cfg.rtba
        if not cfg.enabled or self.paused:
            return
        own_ids = {str(l.get("offer_id")) for l in self.cfg.funpay.lots}
        for lot in self.cfg.funpay.lots:
            if not lot.get("offer_id") or not lot.get("node_id"):
                continue
            try:
                lots = await self.fp.get_public_lots(int(lot["node_id"]), cfg.lot_filter_regex)
                fields = await self.fp.get_lot_fields(int(lot["offer_id"]), int(lot["node_id"]))
            except FunPayError as e:
                log.warning("RTBA: %s", e)
                return
            current = float(fields.get("price") or 0) or None
            self.rtba.calibrate(lots, own_ids, current)
            stock = int(self.db.kv_get("last_stock", 0) or 0)
            d = self.rtba.decide(lots, own_offer_ids=own_ids, own_username=self.fp.username,
                                 current_lot_price=current, stock=stock)
            self.db.exec("INSERT INTO competitor_snapshots(ts,data) VALUES(?,?)",
                         (time.time(), json.dumps([l.__dict__ for l in lots[:40]], ensure_ascii=False)))
            applied = False
            if d.apply and d.target_lot_price is not None:
                try:
                    fields["price"] = f"{d.target_lot_price:.2f}"
                    await self.fp.save_lot(fields)
                    self.rtba.mark_applied()
                    applied = True
                except FunPayError as e:
                    await self.notifier.notify("WARNING", "rtba_save_failed", f"RTBA: цена не сохранена: {e}",
                                               throttle_key="rtba_save")
            self.db.exec("INSERT INTO price_history(ts,offer_id,old_price,new_price,best_competitor,competitors,"
                         "position,reason,applied) VALUES(?,?,?,?,?,?,?,?,?)",
                         (time.time(), str(lot["offer_id"]), current, d.target_lot_price, d.best_competitor,
                          d.competitors, d.position, d.reason + ("; " + "; ".join(d.notes) if d.notes else ""),
                          int(applied)))
            if any("пола" in n for n in d.notes):
                await self.notifier.notify("WARNING", "rtba_floor", f"RTBA: конкуренты ниже пола цены "
                                           f"(лидер {d.best_competitor}). Мы стоим на минимуме {d.target_lot_price}.",
                                           throttle_key="rtba_floor")

    # ================================================================== действия админа
    async def admin_approve(self, oid: str) -> str:
        o = self.db.get_order(oid)
        if not o:
            return "заказ не найден"
        if S(o["status"]) not in (S.PENDING_APPROVAL, S.MANUAL, S.BLOCKED):
            return f"статус {o['status']} — подтверждать нечего"
        if not o["trade_url"]:
            self._set(oid, S.AWAITING_LINK, "admin_take", approved=1, link_attempts=0, admin_alerted=0)
            await self.say(o, "greeting")
            return "заказ передан боту: ждём ссылку от покупателя"
        self._set(oid, S.READY_TO_SEND, "admin_approved", approved=1)
        self.enqueue(oid)
        return f"#{oid} одобрен, трейд отправляется"

    async def admin_reject(self, oid: str) -> str:
        o = self.db.get_order(oid)
        if not o:
            return "заказ не найден"
        self._set(oid, S.MANUAL, "admin_rejected")
        return f"#{oid} переведён в ручную обработку (оформите возврат на FunPay при необходимости)"

    async def admin_resend(self, oid: str) -> str:
        o = self.db.get_order(oid)
        if not o or not o["trade_url"]:
            return "нет заказа или ссылки на обмен"
        if o["offer_id"] and S(o["status"]) == S.OFFER_SENT:
            await self._safe_steam(self.steam.cancel_offer(o["offer_id"]))
            self.db.update_offer_state(o["offer_id"], int(T.CANCELED), canceled_by_bot=True)
            self.db.release_assets(oid)
        self._set(oid, S.READY_TO_SEND, "admin_resend")
        self.enqueue(oid)
        return f"#{oid}: трейд будет отправлен заново"

    async def admin_refund(self, oid: str, reason: str = "") -> str:
        o = self.db.get_order(oid)
        if not o:
            return "заказ не найден"
        if S(o["status"]) in (S.DELIVERED, S.COMPLETED, S.ESCROW):
            return "ключи уже выданы — возврат через бота запрещён"
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
        await self.say_text(o, text)
        self.db.add_event(oid, "admin_message", text=text)
        return "отправлено"

    def admin_blacklist(self, buyer_id: int, value: bool = True, note: str = "") -> str:
        self.db.set_blacklist(buyer_id, value, note)
        return "готово"

    async def admin_mark_delivered(self, oid: str) -> str:
        o = self.db.get_order(oid)
        if not o:
            return "заказ не найден"
        o = self._set(oid, S.DELIVERED, "admin_mark_delivered", delivered_at=time.time())
        self.db.release_assets(oid)
        await self.say(o, "success")
        if o["fp_status"] == "closed":
            await self.on_buyer_confirmed(o)
        return f"#{oid} отмечен выданным, покупателю отправлено сообщение"

    async def handle_command(self, cmd: str, args: list[str]) -> str:
        a0 = args[0].lstrip("#").upper() if args else ""
        if cmd in ("status", "start"):
            return self.status_text()
        if cmd == "pause":
            self.set_paused(True)
            return "⏸ Бот на паузе: заказы фиксируются, но трейды/сообщения/лоты не трогаются"
        if cmd == "resume":
            self.set_paused(False)
            for o in self.db.orders_by_status(S.READY_TO_SEND):
                self.enqueue(o["order_id"])
            return "▶️ Работа возобновлена"
        if cmd in ("approve", "take"):
            return await self.admin_approve(a0)
        if cmd == "reject":
            return await self.admin_reject(a0)
        if cmd == "resend":
            return await self.admin_resend(a0)
        if cmd == "refund":
            return await self.admin_refund(a0, "telegram")
        if cmd == "delivered":
            return await self.admin_mark_delivered(a0)
        if cmd == "msg" and len(args) > 1:
            return await self.admin_message(a0, " ".join(args[1:]))
        if cmd == "orders":
            rows = self.db.active_orders()[:20]
            return "\n".join(f"#{r['order_id']} {r['status']} {r['keys_qty']}🔑 {r['buyer_username']}" for r in rows) \
                or "активных заказов нет"
        if cmd == "stock":
            return f"Свободно ключей: {await self.available_keys(force=True)}"
        if cmd == "report" and self.analytics:
            return self.analytics.text_report()
        return ("Команды: /status /orders /stock /report /pause /resume /approve ID /reject ID /resend ID "
                "/refund ID /delivered ID /take ID /msg ID текст")

    def status_text(self) -> str:
        counts = {r["status"]: r["n"] for r in self.db.q("SELECT status, COUNT(*) n FROM orders GROUP BY status")}
        up = (time.time() - self.started_at) / 3600
        lines = [f"{'⏸ ПАУЗА' if self.paused else '▶️ работает'} · аптайм {up:.1f} ч"
                 f"{' · DRY-RUN' if self.cfg.general.dry_run else ''}",
                 f"Свободно ключей: {self.db.kv_get('last_stock', '?')}",
                 "Заказы: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
        for k, ts in self.last_ok.items():
            lines.append(f"{k}: {int(time.time() - ts)} c назад")
        return "\n".join(lines)

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
            except SteamAuthError as e:
                await self.steam_auth_problem(e)
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
        for bm in upd["bookmarks"]:
            seen = self.db.last_seen_message(bm.chat_id)
            if not init:
                self.db.set_last_seen_message(bm.chat_id, bm.last_message_id)
                continue
            if bm.last_message_id > seen:
                msgs = await self.fp.get_chat_history(bm.chat_id, seen)
                await self.process_chat(bm.chat_id, msgs)
        if not init and upd["bookmarks"]:
            self.db.kv_set("chats_initialized", True)
        self.last_ok["funpay_poll"] = time.time()

    async def start(self) -> None:
        await self.fp.login()
        await self.steam.login()
        for o in self.db.orders_by_status(S.READY_TO_SEND):
            self.enqueue(o["order_id"])
        await self.notifier.notify("INFO", "started", f"🤖 Бот запущен. FunPay: {self.fp.username}, Steam: "
                                   f"{self.steam.persona_name}{' · DRY-RUN' if self.cfg.general.dry_run else ''}")
        self.notifier.command_handler = self.handle_command
        loops = [
            self._loop("funpay", float(self.cfg.funpay.poll_interval), self.funpay_poll),
            self._loop("offers", 20, self.watch_offers),
            self._loop("security", 60, self.security_watch),
            self._loop("timers", 30, self.timers),
            self._loop("stock", 120, self.stock_sync),
            self._loop("raise", 300, self.raise_lots),
            self._loop("rtba", float(self.cfg.rtba.interval_seconds), self.rtba_step),
            self.deliver_worker(),
            self.notifier.poll_commands(),
        ]
        if self.analytics:
            loops.append(self._loop("analytics", 600, self.analytics.tick))
        self._tasks = [asyncio.create_task(c) for c in loops]
        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
