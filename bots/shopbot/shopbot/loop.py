"""Круг денег магазина: каждый приём сразу получает маршрут в общий круг, а не лежит активом.

Круг (казначейство StarsBot, PR #26): хаб TON → рабочий кошелёк StarsBot → Fragment → звёзды/Premium/подарки
покупателям; USDT → Bybit → TON → хаб. Магазин сам деньги не двигает: он записывает приход, маршрут и следующий
шаг, а панель «Круг» и уведомления админу показывают, что ещё не вернулось в оборот.

  способ        куда попадает                         что дальше (step)                       статус
  wallet TON    хаб TON (общий кошелёк)               ничего — уже в круге                    in_loop
  wallet USDT   хаб TON (жетон USDT)                  обмен USDT→TON (Bybit/STON.fi) при нужде  in_loop
  crypto        баланс @CryptoBot приложения          вывести на хаб, когда ≥ sweep_cryptobot  to_sweep
  stars (XTR)   баланс звёзд бота                     звёзды → подарки (sendGift) сразу, или  hold → ready
                                                      вывод через Fragment в TON после 21 дня
  gift          аккаунт приёма подарков               запас для заказов TG Gift / в звёзды    to_convert
  skins         Steam-аккаунт магазина                продать на Market.CSGO → USDT → хаб     to_convert
  card          банк (эквайринг)                      P2P USDT → Bybit → хаб (как сейчас)     to_convert
  balance       внутренний баланс клиента             деньги уже учтены при пополнении        internal
"""
from __future__ import annotations

import time

DAY = 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS inflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, order_id TEXT, method TEXT, asset TEXT, amount REAL,
    rub REAL, market_rub REAL, route TEXT, step TEXT, state TEXT, ready_at REAL, done_at REAL, note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS inflows_state ON inflows(state);
"""
STATE_TITLES = {"in_loop": "в круге", "to_sweep": "вывести на хаб", "hold": "заморожено", "ready": "можно выводить",
                "to_convert": "обменять", "internal": "внутренний баланс", "done": "возвращено в круг"}


class MoneyLoop:
    def __init__(self, shop):
        self.shop = shop
        self.cfg = shop.cfg
        self.db = shop.db
        with self.db.lock:
            self.db.con.executescript(SCHEMA)

    def route(self, method: str, asset: str) -> tuple[str, str, str, float]:
        """(куда попало, следующий шаг, статус, когда можно двигать)."""
        now = time.time()
        if method == "wallet":
            if asset == "TON":
                return "хаб TON", "уже в круге: хаб пополняет кошелёк StarsBot", "in_loop", now
            return "хаб TON (USDT)", "уже в круге; при нехватке TON — обмен USDT→TON", "in_loop", now
        if method == "crypto":
            return "@CryptoBot", f"вывести на хаб TON, когда наберётся {self.cfg.get('loop.sweep_cryptobot_usdt')} USDT", \
                "to_sweep", now
        if method == "stars":
            days = float(self.cfg.get("loop.xtr_hold_days") or 21)
            return "звёзды бота (XTR)", f"сразу: звёзды на подарки заказов (sendGift); через {days:g} дн. — вывод " \
                                      "через Fragment в TON на хаб", "hold", now + days * DAY
        if method == "gift":
            return "аккаунт приёма подарков", "в запас TG Gift под заказы или конвертировать в звёзды", "to_convert", now
        if method == "skins":
            return "Steam магазина", "продать на Market.CSGO (LisBot) → USDT → хаб", "to_convert", now
        if method == "card":
            prov = str(self.cfg.get("payments.card.provider") or "yookassa")
            if prov != "yookassa":
                return f"баланс шлюза {prov}", "вывод шлюза в USDT (TRC20/TON) → Bybit → хаб TON", "to_sweep", now
            return "банк (эквайринг)", "P2P USDT → Bybit → хаб TON", "to_convert", now
        return "баланс клиента", "уже учтено", "internal", now

    def record(self, o, *, method: str | None = None, asset: str | None = None, amount: float | None = None,
               rub: float | None = None) -> int:
        method = method or o["pay_method"]
        asset = (asset or o["pay_currency"] or "RUB").upper()
        amount = float(o["pay_amount"] if amount is None else amount)
        rub = float(o["total_rub"] if rub is None else rub)
        market = rub
        if method == "wallet" and asset in ("USDT", "TON"):
            market = round(amount * self.shop.rates.rub_per(asset), 2)
        if self.db.scalar("SELECT id FROM inflows WHERE order_id=? AND method=?", (o["id"], method)):
            return 0
        where, step, state, ready = self.route(method, asset)
        return self.db.exec("INSERT INTO inflows(ts,order_id,method,asset,amount,rub,market_rub,route,step,state,"
                            "ready_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (time.time(), o["id"], method, asset, amount, rub, market, where, step, state,
                             ready)).lastrowid

    def tick(self) -> None:
        self.db.exec("UPDATE inflows SET state='ready' WHERE state='hold' AND ready_at<=?", (time.time(),))

    def mark_done(self, iid: int, note: str = "") -> str:
        r = self.db.one("SELECT * FROM inflows WHERE id=?", (iid,))
        if r is None or r["state"] in ("done", "in_loop", "internal"):
            return "нечего отмечать"
        self.db.exec("UPDATE inflows SET state='done', done_at=?, note=? WHERE id=?", (time.time(), note[:200], iid))
        return "отмечено: вернулось в круг"

    def rows(self, limit: int = 100) -> list:
        return self.db.q("SELECT * FROM inflows ORDER BY id DESC LIMIT ?", (limit,))

    def summary(self) -> dict:
        self.tick()
        by_state = {r["state"]: {"n": r["n"], "rub": round(r["rub"] or 0, 2)}
                    for r in self.db.q("SELECT state, COUNT(*) n, SUM(rub) rub FROM inflows GROUP BY state")}
        by_method = {r["method"]: {"n": r["n"], "rub": round(r["rub"] or 0, 2), "asset": r["asset"],
                                   "amount": round(r["amount"] or 0, 4)}
                     for r in self.db.q("SELECT method, asset, COUNT(*) n, SUM(rub) rub, SUM(amount) amount "
                                        "FROM inflows GROUP BY method, asset")}
        idle = sum(v["rub"] for k, v in by_state.items() if k in ("to_sweep", "ready", "to_convert"))
        frozen = by_state.get("hold", {}).get("rub", 0)
        loop = sum(v["rub"] for k, v in by_state.items() if k in ("in_loop", "done"))
        return {"by_state": by_state, "by_method": by_method, "idle_rub": round(idle, 2),
                "frozen_rub": round(frozen, 2), "in_loop_rub": round(loop, 2)}

    def todo_text(self) -> str:
        s = self.summary()
        if not s["idle_rub"]:
            return ""
        parts = []
        for st in ("to_sweep", "ready", "to_convert"):
            v = s["by_state"].get(st)
            if v:
                parts.append(f"{STATE_TITLES[st]}: {v['n']} шт. на {v['rub']:.0f} ₽")
        return "♻️ Вне круга: " + "; ".join(parts)
