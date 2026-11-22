"""Канал продаж Авито: оплата через «Купить» (безопасная сделка) → @username в чате → покупка на Fragment.

Схема:
  1. каждые poll_interval секунд читаем чаты своих объявлений со звёздами (/messenger/v2/.../chats);
  2. системное сообщение Авито об оплате (avito.paid_patterns) → заказ в общей CRM (channel='avito',
     id «AV-<чат>-<n>», чат «avito:<id чата>»), бот здороваться и просит @username — как на FunPay;
  3. сообщения покупателя в чате с активным заказом идут в общий движок (engine.on_buyer_message): username,
     команды, FAQ, смена получателя — те же правила, что на FunPay;
  4. до оплаты бот отвечает на вопросы (как купить, цена) и запоминает username, если его прислали заранее;
  5. после выдачи заказ «выдан» до подтверждения покупателем (avito.done_patterns) или автоматически через
     auto_complete_hours — Авито переводит деньги продавцу после подтверждения;
  6. цены объявлений бот держит сам: ваша цена, но не ниже пола по марже (min_margin_pct), см. Economics.

Чаты чужих объявлений (не из avito.products) бот не трогает — на том же аккаунте можно продавать что угодно ещё.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from starsbot.avito import texts as T
from starsbot.avito.client import AvitoAuthError, AvitoChat, AvitoError, AvitoMessage
from starsbot.core import messages as M
from starsbot.core.models import ChatMessage
from starsbot.core.models import OrderStatus as S
from starsbot.core.stars import username_from_text

log = logging.getLogger("avito")

SCHEMA = """
CREATE TABLE IF NOT EXISTS avito_chats (
    chat_id TEXT PRIMARY KEY, item_id INTEGER, item_title TEXT, buyer_id INTEGER, buyer_name TEXT,
    last_seen REAL DEFAULT 0, last_msg_id TEXT DEFAULT '', pending_username TEXT DEFAULT '',
    presale_at REAL DEFAULT 0, seller_at REAL DEFAULT 0, first_seen REAL, updated_at REAL
);
"""
FIRST_SEEN_LOOKBACK = 600          # при первом знакомстве с чатом отвечаем только на сообщения за 10 минут


def _match(patterns, text: str) -> bool:
    for p in patterns or []:
        try:
            if re.search(str(p), text or "", re.I):
                return True
        except re.error:
            if str(p).lower() in (text or "").lower():
                return True
    return False


class AvitoChannel:
    name = "avito"
    title = "Авито"
    code_based = False               # покупатель пишет в чат: напоминания и SLA — как на FunPay

    def __init__(self, cfg, db, engine, client, notifier):
        self.cfg = cfg
        self.db = db
        self.engine = engine
        self.client = client
        self.notifier = notifier
        self.db.conn.executescript(SCHEMA)
        self.started = time.time()
        self.last_ok: float | None = None
        self.last_error = ""
        self.unavailable: set[int] = set()        # номиналы, на которые сейчас не хватает TON

    @property
    def c(self):
        return self.cfg.avito

    # ------------------------------------------------------------------ товары
    def products(self) -> list[dict]:
        return [p for p in (self.c.products or []) if p.get("stars") and int(p.get("item_id") or 0)]

    def product_for(self, item_id: int) -> dict | None:
        for p in self.products():
            if int(p["item_id"]) == int(item_id or 0):
                return p
        return None

    def denominations(self) -> str:
        return ", ".join(str(int(p["stars"])) for p in sorted(self.products(), key=lambda p: int(p["stars"])))

    def price_for(self, p: dict) -> float:
        """Цена объявления: последняя выставленная ботом, иначе расчётная, иначе ручная из настроек."""
        last = self.db.kv_get(f"avito_price:{int(p['item_id'])}")
        if last:
            return float(last)
        econ = self.engine.econ
        if econ is not None:
            tp = econ.target_price(int(p["stars"]), "avito", float(p.get("price") or 0) or None)
            if tp["price"]:
                return float(tp["price"])
        return float(p.get("price") or 0)

    # ------------------------------------------------------------------ опрос
    def _chat_row(self, chat_id: str):
        return self.db.one("SELECT * FROM avito_chats WHERE chat_id=?", (chat_id,))

    @staticmethod
    def key(chat_id: str) -> str:
        return f"avito:{chat_id}"

    async def poll(self) -> None:
        if not self.products():
            self.last_ok = time.time()
            return
        try:
            chats = await self.client.list_chats(limit=50)
        except AvitoAuthError as e:
            self.last_error = str(e)
            await self.notifier.notify("CRITICAL", "avito_auth", f"Авито: {e}. Проверьте client_id/client_secret и "
                                       "что тариф даёт доступ к API мессенджера.", throttle_key="avito_auth")
            return
        for ch in chats:
            row = self._chat_row(ch.chat_id)
            if row is None and self.product_for(ch.item_id) is None:
                continue                                  # чат чужого объявления
            if row is not None and ch.last_created and ch.last_created <= float(row["last_seen"] or 0) and \
                    ch.last_message_id == row["last_msg_id"]:
                continue
            try:
                await self.on_chat(ch, row)
            except AvitoError as e:
                log.warning("Авито, чат %s: %s", ch.chat_id, e)
        self.auto_complete()
        self.last_ok = time.time()
        self.last_error = ""
        self.engine.last_ok["avito_poll"] = self.last_ok

    async def on_chat(self, ch: AvitoChat, row) -> None:
        now = time.time()
        if row is None:
            self.db.exec("INSERT OR IGNORE INTO avito_chats(chat_id,item_id,item_title,buyer_id,buyer_name,last_seen,"
                         "first_seen,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         (ch.chat_id, ch.item_id, ch.item_title, ch.buyer_id, ch.buyer_name,
                          min(now, self.started) - FIRST_SEEN_LOOKBACK, now, now))
            row = self._chat_row(ch.chat_id)
        last_seen = float(row["last_seen"] or 0)
        msgs = await self.client.messages(ch.chat_id)
        fresh = [m for m in msgs if m.created > last_seen or (m.created == last_seen and m.message_id !=
                                                              row["last_msg_id"] and m.created > 0)]
        incoming = False
        for m in fresh:
            if m.is_system:
                incoming = True
                await self.on_system(ch, m)
            elif m.direction == "out":
                if not self._ours(m):
                    # продавец ответил сам — не перебиваем его автоответами до оплаты
                    self.db.exec("UPDATE avito_chats SET seller_at=? WHERE chat_id=?", (m.created or now, ch.chat_id))
            elif m.text.strip():
                incoming = True
                await self.on_buyer_text(ch, m)
            last_seen = max(last_seen, m.created)
        last_id = fresh[-1].message_id if fresh else row["last_msg_id"]
        self.db.exec("UPDATE avito_chats SET last_seen=?, last_msg_id=?, item_id=COALESCE(NULLIF(?,0),item_id), "
                     "buyer_id=COALESCE(NULLIF(?,0),buyer_id), buyer_name=?, updated_at=? WHERE chat_id=?",
                     (last_seen, last_id, ch.item_id, ch.buyer_id, ch.buyer_name, now, ch.chat_id))
        if incoming:
            try:
                await self.client.mark_read(ch.chat_id)
            except AvitoError:
                pass

    def _ours(self, m: AvitoMessage) -> bool:
        """Сообщение отправил бот (есть в журнале исходящих) — а не продавец вручную из приложения."""
        t = (m.text or "").strip()[:200]
        return bool(t) and bool(self.db.one("SELECT 1 FROM messages WHERE direction='out' AND chat_id LIKE 'avito:%' "
                                            "AND substr(text,1,200)=? LIMIT 1", (t,)))

    # ------------------------------------------------------------------ системные сообщения Авито
    async def on_system(self, ch: AvitoChat, m: AvitoMessage) -> None:
        key = self.key(ch.chat_id)
        text = m.text or ""
        self.db.log_message(key, None, "in", f"[Авито] {text}")
        if _match(self.c.cancel_patterns, text):
            await self.on_cancelled(ch, text)
        elif _match(self.c.done_patterns, text):
            o = self.db.last_order_for_chat(key)
            if o is not None and S(o["status"]) == S.DELIVERED:
                self.engine._set(o["order_id"], S.COMPLETED, "avito_confirmed", completed_at=time.time(),
                                 fp_status="closed")
        elif _match(self.c.paid_patterns, text):
            await self.on_paid(ch, m)

    async def on_paid(self, ch: AvitoChat, m: AvitoMessage) -> None:
        key = self.key(ch.chat_id)
        if self.db.one("SELECT 1 FROM order_events WHERE kind='avito_paid' AND data LIKE ? LIMIT 1",
                       (f'%"msg": "{m.message_id}"%',)):
            return                                        # это сообщение уже превратили в заказ
        if self.db.active_order_for_chat(key) is not None:
            return                                        # Авито повторил уведомление об оплате того же заказа
        p = self.product_for(ch.item_id)
        if p is None:
            await self.notifier.notify("WARNING", "avito_unknown_item", f"Авито: оплата в чате {ch.chat_id} по "
                                       f"объявлению {ch.item_id} «{ch.item_title}», которого нет в настройках бота. "
                                       "Выдайте вручную или добавьте объявление на странице «Авито».")
            return
        n = int(self.db.scalar("SELECT COUNT(*) FROM orders WHERE chat_id=?", (key,), 0) or 0) + 1
        oid = f"AV-{ch.chat_id[-10:]}-{n}"
        stars = int(p["stars"])
        price = round(self.price_for(p), 2)
        fee = float(self.c.commission_pct or 0)
        self.db.create_order(order_id=oid, buyer_username=ch.buyer_name, buyer_id=ch.buyer_id, chat_id=key,
                             description=ch.item_title or f"{stars} Telegram Stars", quantity=1, stars_per_unit=stars,
                             stars=stars, price=price, currency="₽", fp_status="paid", lot_offer_id=str(p["item_id"]),
                             lang="ru", channel="avito", payout=round(price * (1 - fee / 100), 2),
                             status=S.AWAITING_USERNAME.value)
        self.db.add_event(oid, "created", channel="avito", chat=ch.chat_id, item=ch.item_id, price=price, stars=stars)
        self.db.add_event(oid, "avito_paid", msg=m.message_id)
        await self.notifier.notify("INFO", "avito_new_order", f"🛒 Авито: {ch.buyer_name} оплатил {stars} ⭐ "
                                   f"({price:g} ₽)", order_id=oid)
        o = self.db.get_order(oid)
        row = self._chat_row(ch.chat_id)
        pending = (row["pending_username"] if row else "") or ""
        if pending:
            self.db.exec("UPDATE avito_chats SET pending_username='' WHERE chat_id=?", (ch.chat_id,))
            await self.engine.accept_username(o, pending, source="avito_presale")
        else:
            await self.engine.say(o, "greeting_need_username")
            self.db.add_event(oid, "greeting_sent")

    async def on_cancelled(self, ch: AvitoChat, text: str) -> None:
        key = self.key(ch.chat_id)
        o = self.db.active_order_for_chat(key) or self.db.last_order_for_chat(key)
        if o is None or S(o["status"]) == S.REFUNDED:
            return
        st = S(o["status"])
        if st in (S.DELIVERED, S.COMPLETED) or st.money_in_flight:
            self.db.add_event(o["order_id"], "avito_cancelled_after_payment", text=text[:200])
            await self.notifier.notify("CRITICAL", "avito_cancel_paid", f"Авито: заказ {o['order_id']} отменён "
                                       f"(«{text[:120]}»), а звёзды уже оплачены на Fragment. Оспорьте отмену в "
                                       "поддержке Авито: в чате есть подтверждение выдачи.", order_id=o["order_id"])
            return
        self.engine._set(o["order_id"], S.REFUNDED, "avito_cancelled", fp_status="refunded",
                         fail_reason="avito_cancelled")
        await self.notifier.notify("INFO", "avito_cancelled", f"Авито: заказ {o['order_id']} отменён до выдачи",
                                   order_id=o["order_id"])

    # ------------------------------------------------------------------ сообщения покупателя
    async def on_buyer_text(self, ch: AvitoChat, m: AvitoMessage) -> None:
        key = self.key(ch.chat_id)
        o = self.db.active_order_for_chat(key)
        if o is not None:
            await self.engine.on_buyer_message(key, ChatMessage(message_id=0, chat_id=key,
                                                                author_id=int(o["buyer_id"] or 0),
                                                                author_name=ch.buyer_name, text=m.text))
            return
        self.db.log_message(key, None, "in", m.text)
        last = self.db.last_order_for_chat(key)
        if last is not None and S(last["status"]) in (S.DELIVERED, S.COMPLETED) and \
                time.time() - float(last["delivered_at"] or 0) < 3 * 86400:
            if M.detect_faq(m.text):
                await self.engine.answer_faq(last, m.text)
            else:
                await self.engine.say(last, "after_delivery")
            await self.notifier.notify("INFO", "buyer_message", f"💬 Авито {ch.buyer_name} (после выдачи "
                                       f"{last['order_id']}): {m.text[:300]}", order_id=last["order_id"])
            return
        if self.engine.paused:
            await self.notifier.notify("INFO", "paused_message", f"(бот на паузе) Авито {ch.buyer_name}: "
                                       f"{m.text[:300]}")
            return
        row = self._chat_row(ch.chat_id)
        if M.is_off_platform_request(m.text):
            await self.send_chat(ch.chat_id, self.text("off_platform", {}))
            return
        u = username_from_text(m.text, allow_bare=False)
        if u:
            self.db.exec("UPDATE avito_chats SET pending_username=? WHERE chat_id=?", (u, ch.chat_id))
            await self.send_chat(ch.chat_id, self.text("presale_username", {"username": u}))
            return
        if not self.c.presale_replies:
            return
        now = time.time()
        if row is not None and now - float(row["seller_at"] or 0) < 3600:
            return                                        # продавец ведёт диалог сам
        p = self.product_for(ch.item_id)
        if p is not None and int(p["stars"]) in self.unavailable:
            await self.send_chat(ch.chat_id, self.text("presale_unavailable", {}))
        elif row is None or now - float(row["presale_at"] or 0) >= float(self.c.presale_cooldown_minutes or 60) * 60:
            await self.send_chat(ch.chat_id, self.text("presale", {"denominations": self.denominations()}))
        else:
            await self.notifier.notify("INFO", "buyer_question", f"💬 Авито {ch.buyer_name} («{ch.item_title}»): "
                                       f"{m.text[:300]}", throttle_key=f"avq:{ch.chat_id}")
            return
        self.db.exec("UPDATE avito_chats SET presale_at=? WHERE chat_id=?", (now, ch.chat_id))

    # ------------------------------------------------------------------ отправка
    def text(self, key: str, variables: dict[str, Any], lang: str = "ru") -> str:
        tpl = (self.c.get("messages") or {}).get(key) or T.TEMPLATES.get(key)
        if tpl:
            try:
                return tpl.format(**variables)
            except (KeyError, ValueError, IndexError):
                if key in T.TEMPLATES:
                    try:
                        return T.TEMPLATES[key].format(**variables)
                    except (KeyError, ValueError, IndexError):
                        pass
        try:
            return M.render(lang, key, **variables).replace("FunPay", "Авито")
        except KeyError:
            return ""

    async def say(self, o, key: str, variables: dict[str, Any]) -> None:
        text = self.text(key, variables, o["lang"] or "ru")
        if text:
            await self.say_text(o, text)

    async def say_text(self, o, text: str) -> None:
        await self.send_chat(str(o["chat_id"]).split(":", 1)[-1], text, o["order_id"])

    async def send_chat(self, chat_id: str, text: str, order_id: str | None = None) -> None:
        self.db.log_message(self.key(chat_id), order_id, "out", text)
        try:
            await self.client.send(chat_id, text)
        except AvitoError as e:
            await self.notifier.notify("WARNING", "avito_send_failed", f"Авито: не отправлено сообщение в чат "
                                       f"{chat_id}: {e}", order_id=order_id, throttle_key=f"avs:{chat_id}")

    # ------------------------------------------------------------------ хуки движка
    async def on_delivered(self, o) -> None:
        """Звёзды ушли. Деньги Авито переведёт после подтверждения покупателем — до этого заказ «выдан»."""
        self.db.add_event(o["order_id"], "avito_wait_confirm")

    def auto_complete(self) -> None:
        hours = float(self.c.get("auto_complete_hours") or 48)
        for o in self.db.q("SELECT order_id FROM orders WHERE channel='avito' AND status=? AND delivered_at<?",
                           (S.DELIVERED.value, time.time() - hours * 3600)):
            self.engine._set(o["order_id"], S.COMPLETED, "avito_auto_completed", completed_at=time.time())

    async def admin_refund(self, oid: str) -> str:
        self.engine._set(oid, S.REFUNDED, "admin_refund", fp_status="refunded", fail_reason="admin_refund")
        o = self.db.get_order(oid)
        if o is not None:
            await self.say(o, "refunded", self.engine._order_vars(o))
        return (f"{oid}: заказ закрыт в боте. Деньги покупателю вернёт Авито — отмените заказ в приложении Авито "
                "(Мои заказы → заказ → «Отменить»).")

    async def stock_sync(self, balance: float, per_star: float | None, committed: int) -> None:
        """Хватит ли TON на каждый номинал. Не хватает — предупреждаем: объявление надо закрыть (правило продавца)."""
        if not per_star:
            return
        free = max(0.0, balance - float(self.cfg.ton.fee_reserve_ton) - committed * per_star)
        now_out = {int(p["stars"]) for p in self.products() if free < int(p["stars"]) * per_star * 1.03}
        if not self.engine.fragment_ok and self.cfg.fragment.hide_lots_when_down:
            now_out = {int(p["stars"]) for p in self.products()}
        new = now_out - self.unavailable
        self.unavailable = now_out
        if new:
            await self.notifier.notify("WARNING", "avito_low_funds", "Авито: на кошельке не хватает TON на "
                                       + ", ".join(f"{s} ⭐" for s in sorted(new)) + ". Снимите эти объявления с "
                                       "публикации или пополните кошелёк — покупатель может оплатить в любой момент.",
                                       throttle_key="avito_low_funds")

    async def price_sync(self) -> None:
        if not self.c.price_sync or self.engine.paused or self.engine.econ is None:
            return
        for p in self.products():
            tp = self.engine.econ.target_price(int(p["stars"]), "avito", float(p.get("price") or 0) or None)
            new = tp["price"]
            if not new:
                continue
            item = int(p["item_id"])
            old = self.db.kv_get(f"avito_price:{item}")
            if old is not None and abs(float(old) - float(new)) < 1:
                continue
            try:
                await self.client.update_price(item, int(round(new)))
            except AvitoError as e:
                await self.notifier.notify("WARNING", "avito_price_failed", f"Авито: цена объявления {item} не "
                                           f"обновлена: {e}", throttle_key=f"avp:{item}")
                continue
            self.db.kv_set(f"avito_price:{item}", int(round(new)))
            self.db.add_event("-", "avito_price", item=item, stars=p["stars"], old=old, new=int(round(new)),
                              floor=tp["floor"], raised=tp["raised"])
            if tp["raised"]:
                await self.notifier.notify("WARNING", "avito_price_floor", f"Авито {p['stars']} ⭐: ваша цена "
                                           f"{tp['want']:g} ₽ ниже пола маржи {self.c.min_margin_pct:g}% — "
                                           f"поставил {int(round(new))} ₽", throttle_key=f"avf:{item}")

    def health_limits(self) -> dict[str, float]:
        return {"avito_poll": 3 * float(self.c.poll_interval or 20) + 120}

    def loops(self, loop) -> list:
        return [loop("avito", float(self.c.poll_interval or 20), self.poll),
                loop("avito_price", float(self.c.price_interval or 900), self.price_sync)]

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ панель
    def summary(self) -> dict:
        q = self.db.scalar
        prices = []
        for p in self.c.products or []:
            if not p.get("stars"):
                continue
            tp = self.engine.econ.target_price(int(p["stars"]), "avito", float(p.get("price") or 0) or None) \
                if self.engine.econ is not None else {"price": None, "floor": None, "raised": False}
            prices.append({**p, "target": tp["price"], "floor": tp["floor"], "raised": tp["raised"],
                           "live": self.db.kv_get(f"avito_price:{int(p.get('item_id') or 0)}"),
                           "out": int(p["stars"]) in self.unavailable})
        return {
            "enabled": True, "last_ok": self.last_ok, "error": self.last_error, "prices": prices,
            "chats": q("SELECT COUNT(*) FROM avito_chats", (), 0),
            "waiting": q("SELECT COUNT(*) FROM orders WHERE channel='avito' AND status=?",
                         (S.AWAITING_USERNAME.value,), 0),
            "unconfirmed": q("SELECT COUNT(*) FROM orders WHERE channel='avito' AND status=?", (S.DELIVERED.value,), 0),
            "presold": q("SELECT COUNT(*) FROM avito_chats WHERE pending_username<>''", (), 0),
        }


def build_channel(cfg, db, engine, notifier, *, demo: bool = False) -> AvitoChannel | None:
    av = cfg.get("avito") or {}
    if not av.get("enabled"):
        return None
    if demo:
        from starsbot.avito.fake import FakeAvito
        client = FakeAvito()
    else:
        from starsbot.avito.client import AvitoClient
        client = AvitoClient(av.client_id, av.client_secret, user_id=int(av.get("user_id") or 0),
                             base=av.api_base, dry_run=cfg.general.dry_run)
    return AvitoChannel(cfg, db, engine, client, notifier)
