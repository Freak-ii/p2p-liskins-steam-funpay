"""Канал продаж Ozon: отправление Ozon → коды активации → @username от покупателя → покупка на Fragment.

Схема (покупатель Ozon не сообщает продавцу свой @username):
  1. каждые poll_interval секунд читаем отправления цифровых товаров (/v2/posting/digital/list);
  2. на каждую единицу товара-звёзд заводим заказ в общей CRM (channel='ozon', id «OZ-<отправление>-<n>»)
     и одноразовый код активации; коды передаём в Ozon (/v1/posting/digital/codes/upload) до дедлайна —
     Ozon показывает код покупателю в заказе и присылает на почту;
  3. покупатель вводит код и @username в боте активации (Telegram или веб-страница) → engine.accept_username:
     дальше общий путь FunPay-заказов (проверка на Fragment → риск → предохранители → TON-транзакция);
  4. после подтверждения транзакции заказ сразу «завершён» (подтверждать на Ozon нечего);
  5. отмена отправления до активации — код аннулируется, заказ «возврат» (деньги покупателю вернёт Ozon);
     отмена после оплаты звёзд — CRITICAL продавцу.

Остатки товаров на Ozon считаются по балансу TON-кошелька так же, как «Наличие» лотов FunPay: кошелёк общий,
обещанные, но не активированные коды резервируют звёзды (db.stars_committed).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from starsbot.core.models import OrderStatus as S
from starsbot.core.stars import normalize_username
from starsbot.ozon import texts as T
from starsbot.ozon.client import OzonAuthError, OzonError, OzonPosting

log = logging.getLogger("ozon")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ozon_postings (
    posting_number TEXT PRIMARY KEY, order_number TEXT, status TEXT, deadline TEXT, created_at TEXT,
    codes_state TEXT DEFAULT 'pending', upload_attempts INTEGER DEFAULT 0, error TEXT DEFAULT '',
    ours INTEGER DEFAULT 1, first_seen REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS ozon_codes (
    code TEXT PRIMARY KEY, display TEXT, order_id TEXT, posting_number TEXT, sku INTEGER, offer_id TEXT,
    stars INTEGER, state TEXT DEFAULT 'new', tg_chat_id INTEGER, created_at REAL, issued_at REAL,
    redeemed_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ozon_codes_order ON ozon_codes(order_id);
CREATE INDEX IF NOT EXISTS idx_ozon_codes_posting ON ozon_codes(posting_number);
CREATE INDEX IF NOT EXISTS idx_ozon_codes_chat ON ozon_codes(tg_chat_id);
"""

# Состояния кода: new — создан, ещё не передан в Ozon; issued — Ozon принял, покупатель его видит;
# cancelled — отправление отменено; declined — передали Ozon «нет в наличии» (деньги вернёт Ozon).
LIVE_CODE = ("issued",)


@dataclass
class RedeemResult:
    ok: bool
    text: str
    order_id: str | None = None
    need: str = ""                    # "" | "code" | "username" — чего ещё ждём от покупателя
    stars: int = 0


def _ts(iso_str: str) -> float | None:
    try:
        return datetime.fromisoformat(str(iso_str).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class OzonChannel:
    name = "ozon"
    title = "Ozon"

    def __init__(self, cfg, db, engine, client, notifier):
        self.cfg = cfg
        self.db = db
        self.engine = engine
        self.client = client
        self.notifier = notifier
        self.bot = None                   # starsbot.ozon.tgbot.ActivationBot (подключает build_channel)
        self.web = None                   # starsbot.ozon.web.ActivationWeb
        self.db.conn.executescript(SCHEMA)
        self.last_ok: float | None = None
        self.last_error = ""

    @property
    def c(self):
        return self.cfg.ozon

    # ------------------------------------------------------------------ товары
    def products(self) -> list[dict]:
        return [p for p in (self.c.products or []) if p.get("stars") and (p.get("offer_id") or p.get("sku"))]

    def product_for(self, offer_id: str, sku: int) -> dict | None:
        for p in self.products():
            if (p.get("offer_id") and str(p["offer_id"]) == str(offer_id)) or \
                    (p.get("sku") and int(p["sku"]) == int(sku or 0)):
                return p
        return None

    # ------------------------------------------------------------------ опрос Ozon
    async def poll(self) -> None:
        if not self.products():
            self.last_ok = time.time()
            return
        lookback = float(self.c.lookback_hours or 72) * 3600
        try:
            postings = await self.client.list_postings(time.time() - lookback)
        except OzonAuthError as e:
            self.last_error = str(e)
            await self.notifier.notify("CRITICAL", "ozon_auth", f"Ozon: {e}. Проверьте Client-Id и Api-Key (ключ "
                                       "кабинета цифровых товаров).", throttle_key="ozon_auth")
            return
        for p in postings:
            await self.on_posting(p)
        await self.retry_uploads()
        self.last_ok = time.time()
        self.last_error = ""
        self.engine.last_ok["ozon_poll"] = self.last_ok

    def _posting_row(self, number: str):
        return self.db.one("SELECT * FROM ozon_postings WHERE posting_number=?", (number,))

    async def on_posting(self, p: OzonPosting) -> None:
        row = self._posting_row(p.posting_number)
        now = time.time()
        if row is None:
            ours = [(pr, self.product_for(pr.offer_id, pr.sku)) for pr in p.products]
            ours = [(pr, conf) for pr, conf in ours if conf]
            self.db.exec("INSERT OR IGNORE INTO ozon_postings(posting_number,order_number,status,deadline,created_at,"
                         "codes_state,ours,first_seen,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (p.posting_number, p.order_number, p.status, p.deadline, p.created_at,
                          "pending" if ours and p.needs_codes else "skipped", 1 if ours else 0, now, now))
            if not ours or not p.needs_codes:
                return                    # чужой товар, уже выданный ранее или отменённый до нас
            await self.create_orders(p, ours)
            await self.upload(p.posting_number)
            return
        if row["status"] != p.status:
            self.db.exec("UPDATE ozon_postings SET status=?, updated_at=? WHERE posting_number=?",
                         (p.status, now, p.posting_number))
        if p.cancelled and row["codes_state"] not in ("cancelled", "skipped"):
            await self.on_cancelled(p)
        elif not p.cancelled and not p.needs_codes and row["codes_state"] in ("pending", "failed"):
            # Ozon больше не ждёт кодов: наша загрузка дошла, но ответ потерялся (коды в Ozon грузим только мы)
            for c in self.codes_for_posting(p.posting_number):
                if c["state"] == "new":
                    self.db.exec("UPDATE ozon_codes SET state='issued', issued_at=?, updated_at=? WHERE code=?",
                                 (now, now, c["code"]))
                    self.db.add_event(c["order_id"], "ozon_code_issued", code=c["display"], recovered=True)
            self.db.exec("UPDATE ozon_postings SET codes_state='uploaded', error='', updated_at=? WHERE "
                         "posting_number=?", (now, p.posting_number))

    async def create_orders(self, p: OzonPosting, ours: list) -> None:
        n = 0
        for pr, conf in ours:
            units = pr.required_codes or pr.quantity or 1
            stars = int(conf["stars"])
            unit_price = round(pr.price if pr.price else 0.0, 2)
            unit_payout = round(pr.payout / max(1, pr.quantity), 2) if pr.payout is not None else None
            for _ in range(units):
                n += 1
                oid = f"OZ-{p.posting_number}-{n}"
                code = T.new_code(self.c.code_prefix)
                self.db.create_order(order_id=oid, buyer_username="Ozon", buyer_id=0, chat_id=f"ozon:{oid}",
                                     description=pr.name or f"{stars} Telegram Stars", quantity=1,
                                     stars_per_unit=stars, stars=stars, price=unit_price, currency="₽",
                                     fp_status="paid", lot_offer_id=str(pr.offer_id), lang="ru", channel="ozon",
                                     payout=unit_payout, status=S.AWAITING_USERNAME.value)
                self.db.exec("INSERT INTO ozon_codes(code,display,order_id,posting_number,sku,offer_id,stars,state,"
                             "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (T.normalize_code(code), code, oid, p.posting_number, pr.sku, str(pr.offer_id), stars,
                              "new", time.time(), time.time()))
                self.db.add_event(oid, "created", channel="ozon", posting=p.posting_number, price=unit_price,
                                  payout=unit_payout, stars=stars, deadline=p.deadline)
        total = sum(int(conf["stars"]) * (pr.required_codes or pr.quantity or 1) for pr, conf in ours)
        await self.notifier.notify("INFO", "ozon_new_order", f"🛒 Ozon: отправление {p.posting_number}, {total} ⭐ "
                                   f"({n} код.), ждём активацию кода покупателем", order_id=f"OZ-{p.posting_number}-1")
        cap = (self.engine.wallet_info or {}).get("capacity")
        if cap is not None and cap < 0:
            await self.notifier.notify("CRITICAL", "ozon_short", f"Ozon {p.posting_number}: на кошельке не хватает "
                                       f"TON на все выданные коды (не хватает ~{-cap} ⭐). Пополните кошелёк — "
                                       "покупатели активируют коды в любой момент.", throttle_key="ozon_short")

    def codes_for_posting(self, number: str) -> list:
        return self.db.q("SELECT * FROM ozon_codes WHERE posting_number=? ORDER BY created_at", (number,))

    async def upload(self, number: str) -> bool:
        """Передаёт в Ozon коды отправления. Повтор безопасен: Ozon принимает коды до дедлайна."""
        codes = [c for c in self.codes_for_posting(number) if c["state"] == "new"]
        if not codes:
            return True
        by_sku: dict[int, list[str]] = {}
        for c in codes:
            by_sku.setdefault(int(c["sku"]), []).append(c["display"])
        items = [{"sku": sku, "exemplar_keys": keys, "not_available_exemplar_qty": 0} for sku, keys in by_sku.items()]
        self.db.exec("UPDATE ozon_postings SET upload_attempts=upload_attempts+1, updated_at=? WHERE posting_number=?",
                     (time.time(), number))
        try:
            res = await self.client.upload_codes(number, items)
        except OzonError as e:
            self.db.exec("UPDATE ozon_postings SET error=? WHERE posting_number=?", (str(e)[:300], number))
            await self._deadline_alarm(number, str(e))
            return False
        received = {int(r.get("sku") or 0): r for r in res}
        failed = []
        for sku, keys in by_sku.items():
            r = received.get(sku) or {}
            if int(r.get("received_qty") or 0) >= len(keys) and not r.get("failed_exemplars"):
                for k in keys:
                    self.db.exec("UPDATE ozon_codes SET state='issued', issued_at=?, updated_at=? WHERE code=?",
                                 (time.time(), time.time(), T.normalize_code(k)))
            else:
                failed.append(f"SKU {sku}: принято {r.get('received_qty', 0)} из {len(keys)} "
                              f"{json.dumps(r.get('failed_exemplars') or [], ensure_ascii=False)[:200]}")
        for c in self.codes_for_posting(number):
            if c["state"] == "issued":
                self.db.add_event(c["order_id"], "ozon_code_issued", code=c["display"], dry_run=bool(
                    getattr(self.client, "dry_run", False)))
        if failed:
            self.db.exec("UPDATE ozon_postings SET codes_state='failed', error=? WHERE posting_number=?",
                         ("; ".join(failed)[:300], number))
            await self.notifier.notify("CRITICAL", "ozon_upload_failed", f"Ozon {number}: коды приняты не все — "
                                       + "; ".join(failed) + ". Проверьте отправление в кабинете Ozon.",
                                       order_id=f"OZ-{number}-1")
            return False
        self.db.exec("UPDATE ozon_postings SET codes_state='uploaded', error='', updated_at=? WHERE posting_number=?",
                     (time.time(), number))
        return True

    async def retry_uploads(self) -> None:
        for row in self.db.q("SELECT posting_number FROM ozon_postings WHERE codes_state='pending'"):
            await self.upload(row["posting_number"])

    async def _deadline_alarm(self, number: str, error: str) -> None:
        row = self._posting_row(number)
        deadline = _ts(row["deadline"]) if row else None
        left = (deadline - time.time()) / 60 if deadline else None
        level = "CRITICAL" if left is not None and left < 15 else "WARNING"
        await self.notifier.notify(level, "ozon_upload_retry", f"Ozon {number}: коды пока не переданы ({error}). "
                                   + (f"До дедлайна {left:.0f} мин. " if left is not None else "") + "Повторю.",
                                   throttle_key=f"ozon_upload:{number}")

    async def on_cancelled(self, p: OzonPosting) -> None:
        number = p.posting_number
        self.db.exec("UPDATE ozon_postings SET codes_state='cancelled', updated_at=? WHERE posting_number=?",
                     (time.time(), number))
        paid = []
        for c in self.codes_for_posting(number):
            o = self.db.get_order(c["order_id"])
            if o is None:
                continue
            st = S(o["status"])
            if st in (S.DELIVERED, S.COMPLETED) or st.money_in_flight:
                paid.append(o["order_id"])
                self.db.add_event(o["order_id"], "ozon_cancelled_after_payment", initiator=p.cancel_initiator)
                continue
            self.db.exec("UPDATE ozon_codes SET state='cancelled', updated_at=? WHERE code=?", (time.time(), c["code"]))
            if not st.is_final:
                self.engine._set(o["order_id"], S.REFUNDED, "ozon_cancelled", fp_status="refunded",
                                 fail_reason="ozon_cancelled")
                if c["tg_chat_id"] or c["redeemed_at"]:
                    await self.say(self.db.get_order(o["order_id"]), "refunded", {})
        if paid:
            await self.notifier.notify("CRITICAL", "ozon_cancel_paid", f"Ozon отменил отправление {number} "
                                       f"({p.cancel_initiator or 'инициатор неизвестен'}), а звёзды по "
                                       + ", ".join(paid) + " уже оплачены на Fragment. Оспорьте отмену в кабинете Ozon.",
                                       order_id=paid[0])
        else:
            await self.notifier.notify("INFO", "ozon_cancelled", f"Ozon: отправление {number} отменено "
                                       f"({p.cancel_initiator or '—'}), коды аннулированы", throttle_key=f"oc:{number}")

    # ------------------------------------------------------------------ активация кода
    def code_row(self, code: str):
        return self.db.one("SELECT * FROM ozon_codes WHERE code=?", (T.normalize_code(code),))

    def code_for_order(self, oid: str):
        return self.db.one("SELECT * FROM ozon_codes WHERE order_id=?", (oid,))

    def check_code(self, code: str) -> RedeemResult:
        """Проверка кода без активации (бот спрашивает username следующим сообщением)."""
        c = self.code_row(code) if code else None
        if c is None or c["state"] == "new":
            return RedeemResult(False, T.BOT["bad_code"], need="code")
        if c["state"] in ("cancelled", "declined"):
            return RedeemResult(False, T.BOT["cancelled_code"], c["order_id"], need="code")
        o = self.db.get_order(c["order_id"])
        if o is None:
            return RedeemResult(False, T.BOT["bad_code"], need="code")
        if o["tg_username"] and S(o["status"]) != S.AWAITING_USERNAME:
            return RedeemResult(True, self.status_line(o), o["order_id"], stars=int(o["stars"]))
        return RedeemResult(True, T.BOT["need_username"].format(stars=o["stars"]), o["order_id"], need="username",
                            stars=int(o["stars"]))

    async def redeem(self, code: str, username: str, *, tg_chat_id: int | None = None,
                     source: str = "telegram") -> RedeemResult:
        chk = self.check_code(code)
        if not chk.ok or chk.need != "username":
            if chk.ok and tg_chat_id:
                self._bind(code, tg_chat_id)
            return chk
        c = self.code_row(code)
        oid = c["order_id"]
        u = normalize_username(username)
        if not u:
            return RedeemResult(False, T.BOT["bad_format"], oid, need="username", stars=chk.stars)
        if tg_chat_id:
            self._bind(code, tg_chat_id)
        now = time.time()
        self.db.exec("UPDATE ozon_codes SET redeemed_at=COALESCE(redeemed_at, ?), updated_at=? WHERE code=?",
                     (now, now, c["code"]))
        self.db.update_order(oid, last_buyer_msg_at=now)
        self.db.log_message(f"ozon:{oid}", oid, "in", f"[{source}] код {c['display']} → @{u}")
        self.db.add_event(oid, "ozon_redeem", source=source, username=u)
        if self.engine.paused:
            self.engine._set(oid, event="username_received", tg_username=u, username_source="ozon",
                             username_at=now)
            await self.notifier.notify("INFO", "paused_redeem", f"(бот на паузе) {oid}: код активирован на @{u}",
                                       order_id=oid)
            return RedeemResult(True, "Код принят ✅ Звёзды будут отправлены в ближайшее время.", oid, stars=chk.stars)
        before = int(self.db.scalar("SELECT MAX(id) FROM messages", (), 0) or 0)
        await self.engine.accept_username(self.db.get_order(oid), u, source="ozon")
        o = self.db.get_order(oid)
        st = S(o["status"])
        need = "username" if st == S.AWAITING_USERNAME else ""
        fresh = [r["text"] for r in self.db.q("SELECT text FROM messages WHERE order_id=? AND direction='out' AND "
                                              "id>? ORDER BY id", (oid, before))]
        # в Telegram эти сообщения уже ушли через say_text — повторять их в ответе не нужно
        text = "" if (tg_chat_id and fresh and self.bot is not None) else "\n\n".join(fresh) or self.status_line(o)
        return RedeemResult(st not in (S.AWAITING_USERNAME, S.MANUAL, S.BLOCKED), text, oid, need=need,
                            stars=int(o["stars"]))

    def _bind(self, code: str, tg_chat_id: int) -> None:
        self.db.exec("UPDATE ozon_codes SET tg_chat_id=?, updated_at=? WHERE code=?",
                     (int(tg_chat_id), time.time(), T.normalize_code(code)))

    def status_line(self, o) -> str:
        from starsbot.core import messages as M
        if not o["tg_username"]:
            return T.BOT["need_username"].format(stars=o["stars"])
        return T.BOT["already"].format(stars=o["stars"], username=o["tg_username"],
                                       status_text=M.status_text("ru", o["status"]))

    def last_text(self, oid: str) -> str:
        r = self.db.one("SELECT text FROM messages WHERE order_id=? AND direction='out' ORDER BY id DESC LIMIT 1",
                        (oid,))
        return r["text"] if r else ""

    def orders_for_chat(self, tg_chat_id: int) -> list:
        return self.db.q("SELECT o.*, c.display code FROM ozon_codes c JOIN orders o ON o.order_id=c.order_id "
                         "WHERE c.tg_chat_id=? ORDER BY c.updated_at DESC LIMIT 10", (int(tg_chat_id),))

    # ------------------------------------------------------------------ сообщения покупателю (хук engine.say)
    async def say(self, o, key: str, variables: dict[str, Any]) -> None:
        tpl = (self.c.get("messages") or {}).get(key) or T.TEMPLATES.get(key)
        if not tpl:
            return                         # напоминаний «пришлите username» на Ozon нет: писать покупателю некуда
        try:
            text = tpl.format(**variables)
        except (KeyError, ValueError, IndexError):
            text = T.TEMPLATES.get(key, "").format(**variables)
        await self.say_text(o, text)

    async def say_text(self, o, text: str) -> None:
        oid = o["order_id"]
        self.db.log_message(f"ozon:{oid}", oid, "out", text)
        c = self.code_for_order(oid)
        if c is not None and c["tg_chat_id"] and self.bot is not None:
            await self.bot.send(int(c["tg_chat_id"]), text)

    # ------------------------------------------------------------------ хуки движка
    async def on_delivered(self, o) -> None:
        """Подтверждать на Ozon нечего: код передан, звёзды оплачены — заказ завершён."""
        self.engine._set(o["order_id"], S.COMPLETED, "completed", completed_at=time.time())

    async def admin_refund(self, oid: str) -> str:
        o = self.db.get_order(oid)
        c = self.code_for_order(oid)
        if c is not None:
            self.db.exec("UPDATE ozon_codes SET state='cancelled', updated_at=? WHERE code=?", (time.time(), c["code"]))
        self.engine._set(oid, S.REFUNDED, "admin_refund", fp_status="refunded", fail_reason="admin_refund")
        if o is not None and c is not None and c["tg_chat_id"]:
            await self.say(self.db.get_order(oid), "refunded", {})
        return (f"{oid}: код аннулирован в боте. Деньги покупателю возвращаются через Ozon — оформите отмену/возврат "
                "в кабинете Ozon (через API цифровых товаров возврат не делается).")

    async def stock_sync(self, balance: float, per_star: float | None, committed: int) -> None:
        """Остаток каждого товара на Ozon = сколько раз хватит свободного TON на этот номинал."""
        if not self.c.stock_sync or self.engine.paused or not per_star:
            return
        free_ton = max(0.0, balance - float(self.cfg.ton.fee_reserve_ton) - committed * per_star)
        if not self.engine.fragment_ok and self.cfg.fragment.hide_lots_when_down:
            free_ton = 0.0
        stocks: dict[str, int] = {}
        for p in self.products():
            if not p.get("offer_id"):
                continue
            amount = max(0, min(int(free_ton / (int(p["stars"]) * per_star * 1.03)), int(self.c.max_stock or 0)))
            if self.db.kv_get(f"ozon_stock:{p['offer_id']}") != amount:
                stocks[str(p["offer_id"])] = amount
        if not stocks:
            return
        try:
            res = await self.client.import_stocks(stocks)
        except OzonError as e:
            await self.notifier.notify("WARNING", "ozon_stock_failed", f"Ozon: остатки не обновлены: {e}",
                                       throttle_key="ozon_stock")
            return
        errors = {str(r.get("offer_id")): r.get("errors") for r in res if r.get("errors")}
        for offer, amount in stocks.items():
            if offer in errors:
                continue
            self.db.kv_set(f"ozon_stock:{offer}", amount)
        self.db.add_event("-", "ozon_stock", **stocks)
        if errors:
            await self.notifier.notify("WARNING", "ozon_stock_errors", "Ozon не принял остатки: " +
                                       json.dumps(errors, ensure_ascii=False)[:500], throttle_key="ozon_stock_err")

    def health_limits(self) -> dict[str, float]:
        return {"ozon_poll": 3 * float(self.c.poll_interval or 30) + 120}

    def loops(self, loop) -> list:
        out = [loop("ozon", float(self.c.poll_interval or 30), self.poll)]
        if self.bot is not None:
            out.append(self.bot.run())
        if self.web is not None:
            out.append(self.web.run())
        return out

    async def close(self) -> None:
        for x in (self.client, self.bot):
            try:
                if x is not None:
                    await x.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ панель
    def summary(self) -> dict:
        q = self.db.scalar
        return {
            "enabled": True, "last_ok": self.last_ok, "error": self.last_error,
            "products": self.products(),
            "stocks": {str(p["offer_id"]): self.db.kv_get(f"ozon_stock:{p['offer_id']}") for p in self.products()
                       if p.get("offer_id")},
            "codes_waiting": q("SELECT COUNT(*) FROM ozon_codes c JOIN orders o ON o.order_id=c.order_id WHERE "
                               "c.state='issued' AND o.status=?", (S.AWAITING_USERNAME.value,)),
            "stars_waiting": q("SELECT SUM(o.stars) FROM ozon_codes c JOIN orders o ON o.order_id=c.order_id WHERE "
                               "c.state='issued' AND o.status=?", (S.AWAITING_USERNAME.value,)) or 0,
            "pending_upload": q("SELECT COUNT(*) FROM ozon_postings WHERE codes_state IN ('pending','failed')"),
            "bot": bool(self.bot), "web": bool(self.web), "web_url": str(self.c.activation_web.public_url or ""),
            "bot_username": getattr(self.bot, "username", "") if self.bot else "",
        }


def build_channel(cfg, db, engine, notifier, *, demo: bool = False) -> OzonChannel | None:
    """Создаёт канал Ozon по настройкам (None — Ozon выключен)."""
    oc = cfg.get("ozon") or {}
    if not oc.get("enabled"):
        return None
    if demo:
        from starsbot.ozon.fake import FakeOzon
        client = FakeOzon()
    else:
        from starsbot.ozon.client import OzonClient
        client = OzonClient(oc.client_id, oc.api_key, base=oc.api_base, dry_run=cfg.general.dry_run)
    ch = OzonChannel(cfg, db, engine, client, notifier)
    tb = oc.activation_bot
    if tb.enabled and tb.bot_token and not demo:
        from starsbot.ozon.tgbot import ActivationBot
        ch.bot = ActivationBot(tb.bot_token, ch, prefix=oc.code_prefix)
    aw = oc.activation_web
    if aw.enabled and not demo:
        from starsbot.ozon.web import ActivationWeb
        ch.web = ActivationWeb(ch, aw.host, int(aw.port))
    return ch
