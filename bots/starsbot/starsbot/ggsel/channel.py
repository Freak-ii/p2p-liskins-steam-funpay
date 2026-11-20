"""Канал продаж GGSEL: продажа → @username из формы заказа (или из чата заказа) → покупка на Fragment.

Схема:
  1. каждые poll_interval секунд читаем последние продажи (seller-last-sales); продажа нашего товара
     (ID товара GGSEL есть в ggsel.products) → читаем заказ (purchase/info): сумму, количество и поле,
     которое покупатель заполнил при оформлении, — username в Telegram;
  2. заказ заводится в общей CRM (channel='ggsel', id «GG-<номер счёта>», чат «ggsel:<номер счёта>»)
     и идёт по общему пути движка: проверка username на Fragment → риск → предохранители → TON-транзакция;
  3. всё общение — в чате заказа GGSEL (debates): приветствие, просьба username, если поля нет или оно с
     ошибкой, ответы на вопросы и «!статус» — тем же разбором, что и на FunPay (engine.on_buyer_message);
  4. после зачисления — сообщение «готово» и просьба подтвердить получение; заказ «завершён», когда GGSEL
     закрыл счёт (invoice_state=4); возврат на GGSEL (5) до выдачи — заказ «возврат», после — CRITICAL продавцу.

Почему это не конфликтует с FunPay: это тот же процесс и тот же движок StarsBot. Кошелёк TON, сессия Fragment,
очередь покупок и блокировка покупки общие, поэтому две площадки не могут одновременно потратить одни и те же TON
или сбить seqno кошелька. Остатки товаров на GGSEL считаются по тому же свободному балансу, что и «Наличие»
лотов FunPay, а обещанные, но ещё не оплаченные звёзды всех площадок резервируются (db.stars_committed).
Заказы GGSEL не трогают чаты, лоты и координацию FunPay: свой префикс заказа и свои чаты «ggsel:…».
"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Any

from starsbot.core import messages as M
from starsbot.core.models import ChatMessage, OrderStatus as S
from starsbot.core.stars import normalize_username, username_from_text
from starsbot.ggsel import texts as T
from starsbot.ggsel.client import CANCELLED, COMPLETED, PAID, REFUNDED, GgselAuthError, GgselError, GgselOrder

log = logging.getLogger("ggsel")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ggsel_sales (
    invoice_id INTEGER PRIMARY KEY, product_id INTEGER, state INTEGER DEFAULT 0, ours INTEGER DEFAULT 1,
    order_id TEXT, handled TEXT DEFAULT '', last_msg_id INTEGER DEFAULT 0, checked_at REAL DEFAULT 0,
    first_seen REAL, updated_at REAL, error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ggsel_sales_order ON ggsel_sales(order_id);
"""

USERNAME_OPTION_RE = re.compile(r"user\s*name|юзер|ник|логин|telegram|телеграм|\bтг\b|tg|получател|аккаунт", re.I)
FRAGMENT_MIN_STARS = 50


def order_id_of(invoice: int) -> str:
    return f"GG-{int(invoice)}"


def chat_of(invoice: int) -> str:
    return f"ggsel:{int(invoice)}"


def invoice_of(o) -> int:
    try:
        return int(str(o["order_id"]).split("-", 1)[1])
    except (IndexError, ValueError, TypeError):
        return 0


def username_from_options(options: list[dict]) -> tuple[str | None, str]:
    """(username, что ввёл покупатель) из полей заказа: поле с «username/ник/Telegram» в названии, иначе
    единственное поле."""
    named = [o for o in options if USERNAME_OPTION_RE.search(o.get("name") or "")]
    pool = named or (options if len(options) == 1 else [])
    for o in pool:
        raw = str(o.get("value") or "").strip()
        if not raw:
            continue
        return (username_from_text(raw) or normalize_username(raw)), raw
    return None, ""


class GgselChannel:
    name = "ggsel"
    title = "GGSEL"
    buyer_chat = True          # у покупателя есть чат заказа: движок шлёт напоминания и ждёт username как на FunPay

    def __init__(self, cfg, db, engine, client, notifier):
        self.cfg = cfg
        self.db = db
        self.engine = engine
        self.client = client
        self.notifier = notifier
        self.db.conn.executescript(SCHEMA)
        self.last_ok: float | None = None
        self.last_error = ""
        self._last_price_push = 0.0

    @property
    def c(self):
        return self.cfg.ggsel

    # ------------------------------------------------------------------ товары
    def products(self) -> list[dict]:
        return [p for p in (self.c.products or []) if p.get("stars") and p.get("offer_id")]

    def product_for(self, product_id: int) -> dict | None:
        for p in self.products():
            if str(p["offer_id"]).strip() == str(product_id):
                return p
        return None

    # ------------------------------------------------------------------ опрос GGSEL
    async def poll(self) -> None:
        if not self.products():
            self.last_ok = time.time()
            return
        try:
            sales = await self.client.last_sales(int(self.c.get("sales_top") or 50))
        except GgselAuthError as e:
            self.last_error = str(e)
            await self.notifier.notify("CRITICAL", "ggsel_auth", f"GGSEL: {e}", throttle_key="ggsel_auth")
            return
        first_run = not self.db.kv_get("ggsel_initialized", False)
        for s in sorted(sales, key=lambda x: x.invoice_id):
            if self.db.one("SELECT 1 FROM ggsel_sales WHERE invoice_id=?", (s.invoice_id,)):
                continue
            await self.on_sale(s.invoice_id, s.product_id, first_run=first_run)
        if first_run:
            self.db.kv_set("ggsel_initialized", True)
        await self.refresh()
        await self.read_chats()
        self.last_ok = time.time()
        self.last_error = ""
        self.engine.last_ok["ggsel_poll"] = self.last_ok

    async def on_sale(self, invoice: int, product_id: int, *, first_run: bool = False) -> None:
        now = time.time()
        conf = self.product_for(product_id)
        if conf is None:
            self.db.exec("INSERT OR IGNORE INTO ggsel_sales(invoice_id,product_id,ours,first_seen,updated_at) "
                         "VALUES(?,?,0,?,?)", (invoice, product_id, now, now))
            return                                   # другой товар продавца — не наш
        try:
            go = await self.client.order(invoice)
        except GgselError as e:
            log.warning("GGSEL заказ %s: %s", invoice, e)
            return                                   # повторим на следующем опросе
        if first_run:
            # продажи, которые были до включения бота, не выдаём автоматически: их мог выдать продавец вручную
            self.db.exec("INSERT OR IGNORE INTO ggsel_sales(invoice_id,product_id,state,ours,handled,first_seen,"
                         "updated_at) VALUES(?,?,?,1,'skipped_first_run',?,?)",
                         (invoice, product_id, go.state, now, now))
            if go.state == PAID:
                await self.notifier.notify("WARNING", "ggsel_old_sale", f"GGSEL: заказ #{invoice} оплачен до "
                                           "включения бота и не выдан автоматически. Проверьте его в кабинете GGSEL "
                                           f"или нажмите «Взять в работу» на странице GGSEL.",
                                           throttle_key=f"ggsel_old:{invoice}")
            return
        self.db.exec("INSERT OR IGNORE INTO ggsel_sales(invoice_id,product_id,state,ours,first_seen,updated_at,"
                     "checked_at) VALUES(?,?,?,1,?,?,?)", (invoice, product_id, go.state, now, now, now))
        if go.state == PAID:
            await self.create_order(go, conf)

    async def take(self, invoice: int) -> str:
        """Взять в работу продажу, пропущенную при первом запуске (кнопка в панели)."""
        row = self.db.one("SELECT * FROM ggsel_sales WHERE invoice_id=?", (invoice,))
        if row is None or not row["ours"]:
            return "продажа не найдена среди товаров со звёздами"
        if row["order_id"]:
            return f"заказ уже в работе: {row['order_id']}"
        go = await self.client.order(invoice)
        if go.state != PAID:
            return f"счёт в статусе {go.state} — выдавать нечего"
        self.db.exec("UPDATE ggsel_sales SET handled='', state=?, updated_at=? WHERE invoice_id=?",
                     (go.state, time.time(), invoice))
        await self.create_order(go, self.product_for(row["product_id"]) or {"stars": 0})
        return f"#{invoice} взят в работу"

    async def create_order(self, go: GgselOrder, conf: dict) -> None:
        oid = order_id_of(go.invoice_id)
        if self.db.get_order(oid) is not None:
            return
        per_unit = int(conf.get("stars") or 0)
        stars = per_unit * max(1, go.quantity)
        fee = float(self.c.commission_pct or 0) / 100
        currency = "₽" if go.currency.upper() in ("RUB", "RUR", "₽", "") else go.currency
        self.db.create_order(order_id=oid, buyer_username="Покупатель GGSEL", buyer_id=0,
                             chat_id=chat_of(go.invoice_id), description=f"{stars} Telegram Stars (GGSEL)",
                             quantity=go.quantity, stars_per_unit=per_unit, stars=stars, price=round(go.amount, 2),
                             currency=currency, fp_status="paid", lot_offer_id=str(conf.get("offer_id") or ""),
                             lang="ru", channel="ggsel", payout=round(go.amount * (1 - fee), 2),
                             status=S.AWAITING_USERNAME.value)
        self.db.exec("UPDATE ggsel_sales SET order_id=?, updated_at=? WHERE invoice_id=?",
                     (oid, time.time(), go.invoice_id))
        username, raw = username_from_options(go.options)
        self.db.add_event(oid, "created", channel="ggsel", invoice=go.invoice_id, price=go.amount, stars=stars,
                          quantity=go.quantity, option=raw[:64])
        await self.notifier.notify("INFO", "ggsel_new_order", f"🛒 GGSEL #{go.invoice_id}: {stars} ⭐ за "
                                   f"{go.amount:.2f} {currency}" + (f" → @{username}" if username else
                                                                    " (username спросим в чате)"), order_id=oid)
        o = self.db.get_order(oid)
        if stars < FRAGMENT_MIN_STARS:
            self.engine._set(oid, S.MANUAL, "ggsel_too_small", fail_reason="too_small")
            await self.notifier.notify("WARNING", "ggsel_too_small", f"GGSEL #{go.invoice_id}: {stars} ⭐ — меньше "
                                       "минимума Fragment (50). Выдайте вручную или оформите возврат.", order_id=oid)
            await self.say(o, "call_seller", self.engine._order_vars(o))
            return
        if self.engine.paused:
            if username:
                self.engine._set(oid, event="username_received", tg_username=username, username_source="ggsel",
                                 username_at=time.time())
            await self.notifier.notify("INFO", "paused_order", f"(бот на паузе) GGSEL #{go.invoice_id} ждёт выдачи",
                                       order_id=oid)
            return
        if username:
            await self.engine.accept_username(o, username, source="ggsel_form")
            return
        await self.engine.say(o, "greeting_need_username")
        self.db.add_event(oid, "greeting_sent")
        if raw:                                       # покупатель что-то ввёл, но это не username
            await self.engine.bad_username(self.db.get_order(oid), "bad_format")

    # ------------------------------------------------------------------ статусы счетов
    async def refresh(self) -> None:
        """Перечитывает счета незавершённых заказов: подтверждение покупателем, возврат, отмена."""
        rs = self.c.get("refresh_seconds")
        every = float(120 if rs is None else rs)
        now = time.time()
        rows = self.db.q("SELECT s.invoice_id, s.checked_at, o.order_id, o.status FROM ggsel_sales s JOIN orders o "
                         "ON o.order_id=s.order_id WHERE s.ours=1 AND o.status NOT IN (?,?,?)",
                         (S.COMPLETED.value, S.REFUNDED.value, S.BLOCKED.value))
        for r in rows:
            if now - float(r["checked_at"] or 0) < every:
                continue
            try:
                go = await self.client.order(int(r["invoice_id"]))
            except GgselError as e:
                log.warning("GGSEL счёт %s: %s", r["invoice_id"], e)
                continue
            self.db.exec("UPDATE ggsel_sales SET state=?, checked_at=?, updated_at=? WHERE invoice_id=?",
                         (go.state, now, now, r["invoice_id"]))
            await self.on_state(self.db.get_order(r["order_id"]), go.state)

    async def on_state(self, o, state: int) -> None:
        if o is None:
            return
        oid, st = o["order_id"], S(o["status"])
        if state == COMPLETED and st == S.DELIVERED:
            o = self.engine._set(oid, S.COMPLETED, "completed", completed_at=time.time(), fp_status="closed")
            await self.say(o, "completed_thanks", self.engine._order_vars(o))
        elif state == COMPLETED and st not in (S.DELIVERED, S.COMPLETED):
            self.db.update_order(oid, fp_status="closed")
            await self.notifier.notify("WARNING", "ggsel_closed_early", f"GGSEL #{invoice_of(o)}: счёт закрыт, а "
                                       f"звёзды ещё не выданы (статус {st.value}). Бот продолжает выдачу.",
                                       order_id=oid, throttle_key=f"ggsel_early:{oid}")
        elif state in (REFUNDED, CANCELLED):
            if st in (S.DELIVERED, S.COMPLETED) or st.money_in_flight:
                self.db.update_order(oid, fp_status="refunded")
                await self.notifier.notify("CRITICAL", "ggsel_refund_paid", f"GGSEL вернул деньги по #{invoice_of(o)}"
                                           f", а звёзды на @{o['tg_username']} уже оплачены на Fragment. Откройте "
                                           "спор в кабинете GGSEL (п. 7.1 правил: возврат — если товар не доставлен).",
                                           order_id=oid)
            else:
                o = self.engine._set(oid, S.REFUNDED, "ggsel_refunded", fp_status="refunded",
                                     fail_reason="ggsel_refunded")
                await self.say(o, "refunded", self.engine._order_vars(o))

    # ------------------------------------------------------------------ чат заказа
    async def read_chats(self) -> None:
        invoices: set[int] = set()
        try:
            invoices |= set(await self.client.chats_with_new())
        except GgselError as e:
            log.warning("GGSEL чаты: %s", e)
        # заказы, где ждём ответа покупателя, читаем и без отметки «новое» (отметку может снять открытый кабинет)
        for r in self.db.q("SELECT s.invoice_id FROM ggsel_sales s JOIN orders o ON o.order_id=s.order_id WHERE "
                           "o.status=? ORDER BY o.created_at DESC LIMIT 20", (S.AWAITING_USERNAME.value,)):
            invoices.add(int(r["invoice_id"]))
        for inv in sorted(invoices):
            row = self.db.one("SELECT * FROM ggsel_sales WHERE invoice_id=? AND ours=1 AND order_id IS NOT NULL",
                              (inv,))
            if row is None:
                continue
            try:
                msgs = await self.client.messages(inv)
            except GgselError as e:
                log.warning("GGSEL чат %s: %s", inv, e)
                continue
            last = int(row["last_msg_id"] or 0)
            mine = {r["text"] for r in self.db.q("SELECT text FROM messages WHERE chat_id=? AND direction='out'",
                                                 (chat_of(inv),))}
            for m in msgs:
                if m.id <= last:
                    continue
                last = m.id
                self.db.exec("UPDATE ggsel_sales SET last_msg_id=? WHERE invoice_id=?", (last, inv))
                if not m.from_buyer or m.text.strip() in mine or not m.text.strip():
                    continue
                await self.on_buyer_text(row["order_id"], inv, m.id, m.text)

    async def on_buyer_text(self, oid: str, invoice: int, msg_id: int, text: str) -> None:
        o = self.db.get_order(oid)
        if o is None:
            return
        st = S(o["status"])
        if st.is_active and st != S.DELIVERED:
            await self.engine.on_buyer_message(chat_of(invoice), ChatMessage(
                message_id=msg_id, chat_id=chat_of(invoice), author_id=0, author_name="Покупатель GGSEL", text=text))
            return
        # заказ уже выдан/закрыт: ответить по делу и показать продавцу (п. 6.4 — ответ не позже 12 ч)
        self.db.log_message(chat_of(invoice), oid, "in", text)
        self.db.update_order(oid, last_buyer_msg_at=time.time())
        if M.detect_command(text) == "seller":
            await self.say(o, "call_seller", self.engine._order_vars(o))
        elif M.detect_faq(text):
            await self.engine.answer_faq(o, text)
        elif st in (S.DELIVERED, S.COMPLETED):
            await self.say(o, "after_delivery", self.engine._order_vars(o))
        await self.notifier.notify("WARNING" if M.detect_faq(text) in ("faq_not_arrived", "faq_refund") else "INFO",
                                   "ggsel_message", f"💬 GGSEL #{invoice} ({st.value}): {text[:300]}", order_id=oid)

    # ------------------------------------------------------------------ сообщения покупателю (хук engine.say)
    async def say(self, o, key: str, variables: dict[str, Any]) -> None:
        v = {**variables, "invoice": invoice_of(o), "order_id": invoice_of(o), "buyer": "", "returning": ""}
        if key == "bad_username" and v.get("reason"):
            v["reason"] = str(v["reason"])[:1].upper() + str(v["reason"])[1:]
        tpl = (self.c.get("messages") or {}).get(key) or T.TEMPLATES.get(key)
        try:
            text = tpl.format(**v) if tpl else M.render("ru", key, **v)
        except (KeyError, ValueError, IndexError):
            text = T.TEMPLATES.get(key, "").format(**v) if key in T.TEMPLATES else ""
        if not text:
            return
        await self.say_text(o, text)

    async def say_text(self, o, text: str) -> None:
        oid = o["order_id"]
        text = T.sanitize(text.replace(f"#{oid}", f"#{invoice_of(o)}"))
        if not text:
            return
        try:
            await self.client.send(invoice_of(o), text)
            self.db.log_message(o["chat_id"], oid, "out", text)
        except GgselError as e:
            await self.notifier.notify("WARNING", "ggsel_send_failed", f"GGSEL #{invoice_of(o)}: сообщение не ушло "
                                       f"({e})", order_id=oid, throttle_key=f"ggsel_send:{oid}")

    # ------------------------------------------------------------------ хуки движка
    async def on_delivered(self, o) -> None:
        """Звёзды оплачены. Счёт GGSEL закроется подтверждением покупателя (или автоматически)."""
        row = self.db.one("SELECT state FROM ggsel_sales WHERE order_id=?", (o["order_id"],))
        if row is not None and int(row["state"] or 0) == COMPLETED:
            self.engine._set(o["order_id"], S.COMPLETED, "completed", completed_at=time.time(), fp_status="closed")

    async def admin_refund(self, oid: str) -> str:
        """Через API возврат не оформить (п. 7.4: только в кабинете): заказ ждёт возврата, бот его не выдаёт."""
        self.engine._set(oid, S.MANUAL, "refund_requested", fail_reason="refund_pending")
        o = self.db.get_order(oid)
        return (f"#{invoice_of(o)}: выдача остановлена. Оформите возврат в кабинете GGSEL (Продажи → заказ → Возврат) "
                "— когда GGSEL вернёт деньги, бот сам закроет заказ и напишет покупателю.")

    def target_price(self, p: dict) -> float | None:
        """Цена товара на GGSEL: вручную (price) или «себестоимость + маржа» по текущему курсу TON."""
        mode = str(self.c.get("price_mode") or "manual")
        if mode != "margin":
            return float(p["price"]) if p.get("price") else None
        econ = self.engine.econ
        if econ is None:
            return None
        stars = int(p["stars"])
        f = econ.flow(stars, 0.0, channel="ggsel")
        if f.cost_rub is None:
            return None
        fee, wd = econ.fee_pcts("ggsel")
        keep = (1 - fee / 100) * (1 - wd / 100)
        m = max(float(self.c.get("margin_pct") or 0), float(self.c.get("min_margin_pct") or 0)) / 100
        if keep - m <= 0:
            return None
        price = f.cost_rub / (keep - m)
        step = 0.01 if stars < FRAGMENT_MIN_STARS else 1.0
        return round(math.ceil(price / step) * step, 2)

    def margin_at(self, p: dict, price: float) -> float | None:
        econ = self.engine.econ
        if econ is None or not price:
            return None
        return econ.flow(int(p["stars"]), float(price), channel="ggsel").margin_pct

    async def stock_sync(self, balance: float, per_star: float | None, committed: int) -> None:
        """Остаток каждого товара = сколько раз хватит свободного TON; цена — по марже (если включено)."""
        if self.engine.paused or not per_star:
            return
        free_ton = max(0.0, balance - float(self.cfg.ton.fee_reserve_ton) - committed * per_star)
        if not self.engine.fragment_ok and self.cfg.fragment.hide_lots_when_down:
            free_ton = 0.0
        push_prices = time.time() - self._last_price_push >= float(self.c.get("price_push_minutes") or 10) * 60
        min_m = float(self.c.get("min_margin_pct") or 0)
        for p in self.products():
            stars = int(p["stars"])
            fields: dict[str, Any] = {}
            price = self.target_price(p)
            margin = self.margin_at(p, price) if price else None
            low = margin is not None and margin < min_m
            if low:
                await self.notifier.notify("WARNING", "ggsel_low_margin", f"GGSEL {stars} ⭐: цена {price} ₽ даёт "
                                           f"маржу {margin}% (минимум {min_m}%). Товар скрыт, поднимите цену или "
                                           "включите цену по марже.", throttle_key=f"ggsel_margin:{p['offer_id']}")
            if self.c.stock_sync:
                cap = int(self.c.max_stock or 0) if stars >= FRAGMENT_MIN_STARS else 100000
                amount = max(0, min(int(free_ton / (stars * per_star * 1.03)), cap))
                if low and self.c.get("hide_below_min_margin", True):
                    amount = 0
                if self.db.kv_get(f"ggsel_stock:{p['offer_id']}") != amount:
                    fields["quantity"] = amount
                    fields["is_unlimited_quantity"] = False
            if price and push_prices and str(self.c.get("price_mode") or "manual") == "margin" and \
                    abs(float(self.db.kv_get(f"ggsel_price:{p['offer_id']}") or 0) - price) >= max(1.0, price * 0.01):
                fields["price"] = price
            if not fields:
                continue
            try:
                await self.client.update_offer(int(p["offer_id"]), **fields)
            except (GgselError, ValueError) as e:
                await self.notifier.notify("WARNING", "ggsel_offer_failed", f"GGSEL товар {p['offer_id']}: {e}",
                                           throttle_key=f"ggsel_offer:{p['offer_id']}")
                continue
            if "quantity" in fields:
                self.db.kv_set(f"ggsel_stock:{p['offer_id']}", fields["quantity"])
            if "price" in fields:
                self.db.kv_set(f"ggsel_price:{p['offer_id']}", fields["price"])
            self.db.add_event("-", "ggsel_offer", offer_id=p["offer_id"], stars=stars, **fields)
        if push_prices:
            self._last_price_push = time.time()

    async def timers(self) -> None:
        """Сроки GGSEL (п. 6.4, 7.5): выдача ≤ 12 ч, иначе администрация вправе вернуть деньги сама."""
        now = time.time()
        warn = float(self.c.get("sla_warn_minutes") or 30) * 60
        crit = float(self.c.get("sla_critical_hours") or 6) * 3600
        for o in self.db.q("SELECT * FROM orders WHERE channel='ggsel' AND status NOT IN (?,?,?,?)",
                           (S.DELIVERED.value, S.COMPLETED.value, S.REFUNDED.value, S.BLOCKED.value)):
            age = now - float(o["paid_at"] or now)
            inv = invoice_of(o)
            if age >= crit:
                await self.notifier.notify("CRITICAL", "ggsel_sla", f"GGSEL #{inv} не выдан {age / 3600:.1f} ч "
                                           f"({o['status']}). Через 12 ч после оплаты GGSEL может вернуть деньги сам и "
                                           "наказать продавца — выдайте вручную или оформите возврат.",
                                           order_id=o["order_id"], throttle_key=f"ggsel_sla_c:{o['order_id']}")
            elif age >= warn:
                await self.notifier.notify("WARNING", "ggsel_sla", f"GGSEL #{inv} не выдан {age / 60:.0f} мин "
                                           f"({o['status']})", order_id=o["order_id"],
                                           throttle_key=f"ggsel_sla_w:{o['order_id']}")
        hours = float(self.c.get("confirm_reminder_hours") or 0)
        if hours and not self.engine.paused:
            for o in self.db.q("SELECT * FROM orders WHERE channel='ggsel' AND status=? AND delivered_at<? AND "
                               "COALESCE(confirm_reminded,0)=0", (S.DELIVERED.value, now - hours * 3600)):
                self.db.update_order(o["order_id"], confirm_reminded=1)
                await self.say(o, "confirm_reminder", self.engine._order_vars(o))
        self.engine.last_ok["ggsel_timers"] = now

    def health_limits(self) -> dict[str, float]:
        return {"ggsel_poll": 3 * float(self.c.poll_interval or 20) + 120}

    def loops(self, loop) -> list:
        return [loop("ggsel", float(self.c.poll_interval or 20), self.poll), loop("ggsel_timers", 60, self.timers)]

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ панель
    def summary(self) -> dict:
        q = self.db.scalar
        active = (S.COMPLETED.value, S.REFUNDED.value, S.BLOCKED.value, S.DELIVERED.value)
        return {
            "enabled": True, "last_ok": self.last_ok, "error": self.last_error,
            "products": self.products(),
            "stocks": {str(p["offer_id"]): self.db.kv_get(f"ggsel_stock:{p['offer_id']}") for p in self.products()},
            "prices": {str(p["offer_id"]): self.db.kv_get(f"ggsel_price:{p['offer_id']}") for p in self.products()},
            "targets": {str(p["offer_id"]): self.target_price(p) for p in self.products()},
            "active": q("SELECT COUNT(*) FROM orders WHERE channel='ggsel' AND status NOT IN (?,?,?,?)", active),
            "awaiting_confirm": q("SELECT COUNT(*) FROM orders WHERE channel='ggsel' AND status=?",
                                  (S.DELIVERED.value,)),
            "skipped": self.db.q("SELECT * FROM ggsel_sales WHERE handled='skipped_first_run' AND state=? AND "
                                 "order_id IS NULL", (PAID,)),
        }


def build_channel(cfg, db, engine, notifier, *, demo: bool = False) -> GgselChannel | None:
    """Создаёт канал GGSEL по настройкам (None — GGSEL выключен)."""
    gc = cfg.get("ggsel") or {}
    if not gc.get("enabled"):
        return None
    if demo:
        from starsbot.ggsel.fake import FakeGgsel
        client = FakeGgsel()
    else:
        from starsbot.ggsel.client import GgselClient
        client = GgselClient(gc.seller_id, gc.api_key, base=gc.api_base, dry_run=cfg.general.dry_run)
    return GgselChannel(cfg, db, engine, client, notifier)
