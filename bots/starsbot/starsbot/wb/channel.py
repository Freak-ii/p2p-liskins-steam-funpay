"""Канал продаж Wildberries: пул кодов активации → покупатель вводит код и @username → покупка на Fragment.

Почему пул, а не выдача по заказу, как на Ozon: отдельную площадку WB Digital закрыли 01.12.2025, а категория
«Цифровые товары» на основной витрине пока открыта не всем продавцам и без публичного API выдачи ключей.
Поэтому схема не зависит от API WB:
  1. в панели («Wildberries» → «Выпустить коды») бот создаёт пачку одноразовых кодов номинала, например 10 × 100 ⭐;
  2. вы скачиваете CSV и загружаете коды в карточку товара в кабинете WB (ключи активации) — WB выдаёт их
     покупателям сам, после оплаты;
  3. покупатель вводит код и @username в боте активации WB → бот заводит заказ (channel='wb', «WB-<код>»)
     и покупает звёзды на Fragment тем же путём, что для FunPay и Ozon.
Код без активации — это деньги, которые WB уже взял или ещё возьмёт: бот следит, чтобы на кошельке хватало TON
на все коды в пуле, и предупреждает, когда кодов номинала остаётся мало.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from typing import Any

from starsbot.core.models import OrderStatus as S
from starsbot.core.stars import normalize_username
from starsbot.ozon.channel import RedeemResult
from starsbot.wb import texts as T

log = logging.getLogger("wb")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wb_codes (
    code TEXT PRIMARY KEY, display TEXT, stars INTEGER, nm_id TEXT, batch TEXT, state TEXT DEFAULT 'pool',
    order_id TEXT, tg_chat_id INTEGER, created_at REAL, exported_at REAL, redeemed_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_wb_codes_state ON wb_codes(state);
CREATE INDEX IF NOT EXISTS idx_wb_codes_order ON wb_codes(order_id);
CREATE INDEX IF NOT EXISTS idx_wb_codes_chat ON wb_codes(tg_chat_id);
"""
# Состояния: pool — выпущен, ждёт покупателя; redeemed — активирован (есть заказ); void — аннулирован продавцом.


class WbChannel:
    name = "wb"
    title = "Wildberries"
    code_based = True

    def __init__(self, cfg, db, engine, notifier):
        self.cfg = cfg
        self.db = db
        self.engine = engine
        self.notifier = notifier
        self.client = None
        self.bot = None
        self.web = None
        self.db.conn.executescript(SCHEMA)
        self.last_ok: float | None = time.time()
        self.last_error = ""

    @property
    def c(self):
        return self.cfg.wb

    def products(self) -> list[dict]:
        return [p for p in (self.c.products or []) if p.get("stars")]

    def product_for(self, stars: int) -> dict | None:
        return next((p for p in self.products() if int(p["stars"]) == int(stars)), None)

    def price_for(self, stars: int) -> float:
        p = self.product_for(stars) or {}
        manual = float(p.get("price") or 0) or None
        if self.engine.econ is not None:
            tp = self.engine.econ.target_price(int(stars), "wb", manual)
            if tp["price"]:
                return float(tp["price"])
        return float(manual or 0)

    # ------------------------------------------------------------------ пул кодов
    def issue(self, stars: int, count: int) -> tuple[str, list[str]]:
        """Выпустить count кодов номинала stars. Возвращает (номер пачки, коды)."""
        count = max(1, min(int(count), 500))
        p = self.product_for(stars) or {}
        batch = time.strftime("%Y%m%d-%H%M%S") + f"-{int(stars)}"
        now = time.time()
        codes = []
        for _ in range(count):
            code = T.new_code(self.c.code_prefix)
            self.db.exec("INSERT INTO wb_codes(code,display,stars,nm_id,batch,state,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?,?,?)", (T.normalize_code(code), code, int(stars), str(p.get("nm_id") or ""),
                                                     batch, "pool", now, now))
            codes.append(code)
        self.db.add_event("-", "wb_codes_issued", batch=batch, stars=int(stars), count=count)
        return batch, codes

    def export_csv(self, batch: str = "") -> str:
        rows = self.db.q("SELECT * FROM wb_codes WHERE state='pool'" + (" AND batch=?" if batch else "") +
                         " ORDER BY stars, created_at", (batch,) if batch else ())
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["code", "stars", "nm_id", "batch"])
        for r in rows:
            w.writerow([r["display"], r["stars"], r["nm_id"] or "", r["batch"]])
        self.db.exec("UPDATE wb_codes SET exported_at=COALESCE(exported_at, ?) WHERE state='pool'" +
                     (" AND batch=?" if batch else ""), (time.time(), batch) if batch else (time.time(),))
        return buf.getvalue()

    def void(self, *, batch: str = "", code: str = "") -> int:
        """Аннулировать невыданные коды (например, сняли карточку с WB или коды утекли)."""
        if code:
            cur = self.db.exec("UPDATE wb_codes SET state='void', updated_at=? WHERE code=? AND state='pool'",
                               (time.time(), T.normalize_code(code)))
        elif batch:
            cur = self.db.exec("UPDATE wb_codes SET state='void', updated_at=? WHERE batch=? AND state='pool'",
                               (time.time(), batch))
        else:
            return 0
        self.db.add_event("-", "wb_codes_void", batch=batch, code=code[:6], count=cur.rowcount)
        return int(cur.rowcount or 0)

    def pool(self) -> dict[int, int]:
        return {int(r["stars"]): int(r["n"]) for r in self.db.q("SELECT stars, COUNT(*) n FROM wb_codes WHERE "
                                                                 "state='pool' GROUP BY stars")}

    def pool_stars(self) -> int:
        return int(self.db.scalar("SELECT SUM(stars) FROM wb_codes WHERE state='pool'", (), 0) or 0)

    # ------------------------------------------------------------------ активация (интерфейс бота активации)
    def code_row(self, code: str):
        return self.db.one("SELECT * FROM wb_codes WHERE code=?", (T.normalize_code(code),))

    def check_code(self, code: str) -> RedeemResult:
        c = self.code_row(code) if code else None
        if c is None:
            return RedeemResult(False, T.BOT["bad_code"], need="code")
        if c["state"] == "void":
            return RedeemResult(False, T.BOT["cancelled_code"], c["order_id"], need="code")
        if c["state"] == "pool":
            return RedeemResult(True, T.BOT["need_username"].format(stars=c["stars"]), None, need="username",
                                stars=int(c["stars"]))
        o = self.db.get_order(c["order_id"]) if c["order_id"] else None
        if o is None:
            return RedeemResult(False, T.BOT["bad_code"], need="code")
        if o["tg_username"] and S(o["status"]) != S.AWAITING_USERNAME:
            return RedeemResult(True, self.status_line(o), o["order_id"], stars=int(o["stars"]))
        return RedeemResult(True, T.BOT["need_username"].format(stars=o["stars"]), o["order_id"], need="username",
                            stars=int(o["stars"]))

    def _create_order(self, c) -> str:
        oid = f"WB-{c['code'][-8:]}"
        stars = int(c["stars"])
        price = round(self.price_for(stars), 2)
        fee = float(self.c.commission_pct or 0)
        self.db.create_order(order_id=oid, buyer_username="Wildberries", buyer_id=0, chat_id=f"wb:{oid}",
                             description=f"{stars} Telegram Stars (Wildberries)", quantity=1, stars_per_unit=stars,
                             stars=stars, price=price, currency="₽", fp_status="paid", lot_offer_id=str(c["nm_id"] or ""),
                             lang="ru", channel="wb", payout=round(price * (1 - fee / 100), 2),
                             status=S.AWAITING_USERNAME.value)
        now = time.time()
        self.db.exec("UPDATE wb_codes SET state='redeemed', order_id=?, redeemed_at=?, updated_at=? WHERE code=?",
                     (oid, now, now, c["code"]))
        self.db.add_event(oid, "created", channel="wb", code=c["display"], batch=c["batch"], price=price, stars=stars)
        return oid

    async def redeem(self, code: str, username: str, *, tg_chat_id: int | None = None,
                     source: str = "telegram") -> RedeemResult:
        chk = self.check_code(code)
        if not chk.ok or chk.need != "username":
            if chk.ok and tg_chat_id:
                self._bind(code, tg_chat_id)
            return chk
        u = normalize_username(username)
        if not u:
            return RedeemResult(False, T.BOT["bad_format"], chk.order_id, need="username", stars=chk.stars)
        c = self.code_row(code)
        if tg_chat_id:
            self._bind(code, tg_chat_id)
        oid = c["order_id"] or self._create_order(c)
        await self.notifier.notify("INFO", "wb_redeem", f"🛒 Wildberries: активирован код на {c['stars']} ⭐ → @{u}",
                                   order_id=oid)
        now = time.time()
        self.db.update_order(oid, last_buyer_msg_at=now)
        self.db.log_message(f"wb:{oid}", oid, "in", f"[{source}] код {c['display']} → @{u}")
        if self.engine.paused:
            self.engine._set(oid, event="username_received", tg_username=u, username_source="wb", username_at=now)
            return RedeemResult(True, "Код принят ✅ Звёзды будут отправлены в ближайшее время.", oid, stars=chk.stars)
        before = int(self.db.scalar("SELECT MAX(id) FROM messages", (), 0) or 0)
        await self.engine.accept_username(self.db.get_order(oid), u, source="wb")
        o = self.db.get_order(oid)
        st = S(o["status"])
        fresh = [r["text"] for r in self.db.q("SELECT text FROM messages WHERE order_id=? AND direction='out' AND "
                                              "id>? ORDER BY id", (oid, before))]
        text = "" if (tg_chat_id and fresh and self.bot is not None) else "\n\n".join(fresh) or self.status_line(o)
        return RedeemResult(st not in (S.AWAITING_USERNAME, S.MANUAL, S.BLOCKED), text, oid,
                            need="username" if st == S.AWAITING_USERNAME else "", stars=int(o["stars"]))

    def _bind(self, code: str, tg_chat_id: int) -> None:
        self.db.exec("UPDATE wb_codes SET tg_chat_id=?, updated_at=? WHERE code=?",
                     (int(tg_chat_id), time.time(), T.normalize_code(code)))

    def status_line(self, o) -> str:
        from starsbot.core import messages as M
        if not o["tg_username"]:
            return T.BOT["need_username"].format(stars=o["stars"])
        return T.BOT["already"].format(stars=o["stars"], username=o["tg_username"],
                                       status_text=M.status_text("ru", o["status"]))

    def orders_for_chat(self, tg_chat_id: int) -> list:
        return self.db.q("SELECT c.display AS code, c.stars AS stars, COALESCE(o.status,'awaiting_username') AS status, "
                         "o.tg_username AS tg_username, o.order_id AS order_id FROM wb_codes c LEFT JOIN orders o "
                         "ON o.order_id=c.order_id WHERE c.tg_chat_id=? AND c.state<>'void' ORDER BY c.updated_at "
                         "DESC LIMIT 10", (int(tg_chat_id),))

    # ------------------------------------------------------------------ сообщения (хук engine.say)
    async def say(self, o, key: str, variables: dict[str, Any]) -> None:
        tpl = (self.c.get("messages") or {}).get(key) or T.TEMPLATES.get(key)
        if not tpl:
            return
        try:
            text = tpl.format(**variables)
        except (KeyError, ValueError, IndexError):
            text = T.TEMPLATES.get(key, "").format(**variables)
        await self.say_text(o, text)

    async def say_text(self, o, text: str) -> None:
        oid = o["order_id"]
        self.db.log_message(f"wb:{oid}", oid, "out", text)
        c = self.db.one("SELECT * FROM wb_codes WHERE order_id=?", (oid,))
        if c is not None and c["tg_chat_id"] and self.bot is not None:
            await self.bot.send(int(c["tg_chat_id"]), text)

    # ------------------------------------------------------------------ хуки движка
    async def on_delivered(self, o) -> None:
        self.engine._set(o["order_id"], S.COMPLETED, "completed", completed_at=time.time())

    async def admin_refund(self, oid: str) -> str:
        self.engine._set(oid, S.REFUNDED, "admin_refund", fp_status="refunded", fail_reason="admin_refund")
        self.db.exec("UPDATE wb_codes SET state='void', updated_at=? WHERE order_id=?", (time.time(), oid))
        o = self.db.get_order(oid)
        if o is not None:
            await self.say(o, "refunded", {})
        return f"{oid}: код аннулирован в боте. Деньги покупателю возвращает Wildberries — оформите возврат в кабинете WB."

    async def stock_sync(self, balance: float, per_star: float | None, committed: int) -> None:
        """Хватит ли TON на все коды в пуле и не кончаются ли коды номиналов."""
        self.last_ok = time.time()
        self.engine.last_ok["wb"] = self.last_ok
        if not per_star:
            return
        need = self.pool_stars()
        free = max(0.0, balance - float(self.cfg.ton.fee_reserve_ton) - committed * per_star)
        if need and free < need * per_star * 1.03:
            await self.notifier.notify("WARNING", "wb_pool_unfunded", f"Wildberries: в пуле {need} ⭐ невыданных "
                                       f"кодов, а свободного TON хватает на ~{int(free / per_star)} ⭐. Пополните "
                                       "кошелёк или снимите часть кодов с продажи в кабинете WB.",
                                       throttle_key="wb_pool_unfunded")
        pool = self.pool()
        low = [int(p["stars"]) for p in self.products() if p.get("nm_id") and pool.get(int(p["stars"]), 0) <
               int(self.c.pool_warn_low or 0)]
        if low:
            await self.notifier.notify("INFO", "wb_pool_low", "Wildberries: заканчиваются коды " +
                                       ", ".join(f"{s} ⭐ (осталось {pool.get(s, 0)})" for s in low) +
                                       ". Выпустите новые на странице «Wildberries» и загрузите в кабинет WB.",
                                       throttle_key="wb_pool_low")

    def health_limits(self) -> dict[str, float]:
        return {}

    def loops(self, loop) -> list:
        return [self.bot.run()] if self.bot is not None else []

    async def close(self) -> None:
        if self.bot is not None:
            try:
                await self.bot.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ панель
    def summary(self) -> dict:
        pool = self.pool()
        prices = []
        for p in self.c.products or []:
            if not p.get("stars"):
                continue
            tp = self.engine.econ.target_price(int(p["stars"]), "wb", float(p.get("price") or 0) or None) \
                if self.engine.econ is not None else {"price": None, "floor": None, "raised": False}
            prices.append({**p, "target": tp["price"], "floor": tp["floor"], "raised": tp["raised"],
                           "pool": pool.get(int(p["stars"]), 0)})
        return {"enabled": True, "prices": prices, "pool_stars": self.pool_stars(),
                "redeemed": self.db.scalar("SELECT COUNT(*) FROM wb_codes WHERE state='redeemed'", (), 0),
                "batches": self.db.q("SELECT batch, stars, COUNT(*) n, SUM(state='pool') left_n, "
                                     "SUM(state='redeemed') used, MIN(created_at) created_at, MAX(exported_at) exported "
                                     "FROM wb_codes GROUP BY batch ORDER BY created_at DESC LIMIT 30"),
                "bot": bool(self.bot), "bot_username": getattr(self.bot, "username", "") if self.bot else ""}


def build_channel(cfg, db, engine, notifier, *, demo: bool = False) -> WbChannel | None:
    wb = cfg.get("wb") or {}
    if not wb.get("enabled"):
        return None
    ch = WbChannel(cfg, db, engine, notifier)
    tb = wb.activation_bot
    if tb.enabled and tb.bot_token and not demo:
        from starsbot.ozon.tgbot import ActivationBot
        ch.bot = ActivationBot(tb.bot_token, ch, prefix=wb.code_prefix, texts=T)
    return ch


async def run_wb_demo(engine, wallet) -> dict:
    """Демо WB: выпуск пачки кодов, активация одного кода в боте активации."""
    from starsbot.ozon.tgbot import ActivationBot
    ch = engine.channels["wb"]
    if ch.bot is None:
        ch.bot = ActivationBot("", ch, prefix=ch.c.code_prefix, texts=T)
    engine.fragment.users.update({"maria_tg": "Мария"})
    batch, codes = ch.issue(100, 5)
    ch.issue(250, 3)
    await ch.bot.handle(8001, codes[0])
    await ch.bot.handle(8001, "@maria_tg")
    for _ in range(3):
        while not engine.queue.empty():
            oid = engine.queue.get_nowait()
            engine._queued.discard(oid)
            await engine.purchase(oid)
            wallet.tick()
            await engine.watch_purchases()
        wallet.tick()
        await engine.watch_purchases()
    return {"batch": batch, "codes": codes}
