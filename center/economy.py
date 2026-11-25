"""Экономика всей системы: журнал каждой операции, сводка в целом и по направлениям, круги денег с итогом
каждого шага и сверка кошельков.

Модуль только читает базы ботов (SQLite в режиме ro) по путям от home центра, поэтому одинаково работает на ПК
и на сервере (CENTER_HOME). Схемы читаются терпимо: нет таблицы или колонки — направление просто пустое.

Операция журнала (одна строка = одно движение денег или товара):
    {ts, bot, direction, position, channel, circle, kind, amount, currency, rub, ref, status, note}
    kind: sale | purchase | fee | gas | topup | withdraw | transfer | fx
    status: done (деньги у нас) | pending (FunPay ещё держит) | estimate (будущая комиссия вывода, оценка)
    rub: знак как у денег системы (+ пришло, − ушло), пересчёт в ₽ по фактической цене закупки (FIFO).

Себестоимость TON считается FIFO по пополнениям кошелька (cashflow 'topup': сколько ₽ отдали и сколько TON
пришло). Разница с рыночным курсом в момент покупки звёзд — потеря на шаге «₽ → TON» (обмен/P2P).

Круг: {id, title, legs:[{from, to, kind, position?, channel?, pct, rub}], pct, open, ...}. id и узлы общие с
center/loops.py (тред «Пульт позиций»): fp_rub, ggsel_rub, card_rub, usdt, ton, lis_usd, steam, market_rub, xtr.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import sqlite3
import statistics
import time
from collections import defaultdict, deque
from pathlib import Path

import yaml

log = logging.getLogger("center.economy")

DAY = 86400

DIRECTIONS = {
    "stars": "Звёзды Telegram",
    "premium": "Telegram Premium",
    "gifts": "Подарки Telegram",
    "shop": "Магазин DPB Fastchange",
    "mann": "Ключи Mann Co.",
    "lis": "Скины Lis-Skins",
    "treasury": "Казна (переводы)",
}
BOTS = {"starsbot": "StarsBot", "mannbot": "MannBot", "lisbot": "LisBot", "shopbot": "ShopBot"}
NODES = {
    "fp_rub": "FunPay ₽", "ggsel_rub": "GGSEL ₽", "card_rub": "Карта ₽", "usdt": "USDT", "ton": "TON-кошелёк",
    "lis_usd": "Lis-Skins $", "steam": "Steam", "market_rub": "Market.CSGO ₽", "xtr": "Звёзды",
    "shop_rub": "Касса магазина",
}
KINDS = {"sale": "Продажа", "purchase": "Закупка", "fee": "Комиссия", "gas": "Газ сети", "topup": "Пополнение",
         "withdraw": "Вывод", "transfer": "Перевод", "fx": "Обмен"}

STARS_SENT = ("sent", "delivered", "completed", "refunded", "manual")   # транзакция могла уйти
STARS_PENDING = ("delivered", "sent", "purchasing", "manual")
CHANNEL_NODE = {"funpay": "fp_rub", "ggsel": "ggsel_rub"}


# ---------------------------------------------------------------------------- чтение баз
def _ro(path: Path) -> sqlite3.Connection | None:
    if not path or not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    return con


def _rows(path: Path, sql: str, args: tuple = ()) -> list[dict]:
    con = _ro(path)
    if con is None:
        return []
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except sqlite3.Error as e:            # нет таблицы/колонки у бота на старом релизе
        log.debug("%s: %s", path.name, e)
        return []
    finally:
        con.close()


def _cols(path: Path, table: str) -> set[str]:
    return {r["name"] for r in _rows(path, f'PRAGMA table_info("{table}")')}


def _sel(path: Path, table: str, want: list[str], tail: str = "") -> list[dict]:
    """SELECT только тех колонок, что есть в базе; отсутствующие приходят как None."""
    have = _cols(path, table)
    if not have:
        return []
    cols = [c if c in have else f"NULL AS {c}" for c in want]
    return _rows(path, f'SELECT {", ".join(cols)} FROM "{table}" {tail}')


def _yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _r(x, n: int = 2):
    return round(x, n) if isinstance(x, (int, float)) else x


def _pct(part: float, base: float) -> float | None:
    return round(part / base * 100, 2) if base else None


# ---------------------------------------------------------------------------- FIFO TON
class TonLots:
    """Партии TON на кошельке StarsBot: FIFO-списание даёт настоящую цену в ₽ каждой покупки на Fragment."""

    def __init__(self, lots: list[tuple[float, float, float]], fallback_rate: float):
        self.q = deque([[ts, ton, rub / ton] for ts, rub, ton in sorted(lots) if ton > 0 and rub > 0])
        self.fallback = fallback_rate
        self.unlotted_ton = 0.0          # списано сверх известных пополнений (было на кошельке до учёта)

    def take(self, ton: float, market_rate: float) -> float:
        rub, left = 0.0, ton
        while left > 1e-12 and self.q:
            lot = self.q[0]
            use = min(left, lot[1])
            rub += use * lot[2]
            lot[1] -= use
            left -= use
            if lot[1] <= 1e-12:
                self.q.popleft()
        if left > 1e-12:
            self.unlotted_ton += left
            rub += left * (self.fallback or market_rate)
        return rub

    @property
    def left_ton(self) -> float:
        return sum(l[1] for l in self.q)

    @property
    def left_rub(self) -> float:
        return sum(l[1] * l[2] for l in self.q)


# ---------------------------------------------------------------------------- главный класс
class Economy:
    def __init__(self, home: Path, cfg: dict | None = None):
        self.home = Path(home)
        self.cfg = cfg or {}

    # пути от home центра (на сервере home = CENTER_HOME), не от папки кода
    @property
    def stars_db(self) -> Path:
        return self.home / "starsbot" / "data" / "starsbot.sqlite3"

    @property
    def mann_db(self) -> Path:
        return self.home / "mannbot" / "data" / "mannbot.sqlite3"

    @property
    def lis_db(self) -> Path:
        return self.home / "lisbot" / "data" / "bot.sqlite"

    @property
    def shop_db(self) -> Path:
        return self.home / "shopbot" / "data" / "shopbot.sqlite3"

    def bot_config(self, bot: str) -> dict:
        d = self.home / bot
        if bot == "lisbot":
            try:
                return json.loads((d / "config.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        return _merge(_yaml(d / "config.yaml"), _yaml(d / "config.patch.yaml"))

    def stars_economics(self) -> dict:
        e = self.bot_config("starsbot").get("economics") or {}
        own = self.cfg.get("starsbot") or {}
        return {"withdraw_fee_pct": _f(own.get("withdraw_fee_pct", e.get("withdraw_fee_pct", 3.0))),
                "withdraw_fee_fixed_rub": _f(e.get("withdraw_fee_fixed_rub")),
                "funpay_fee_pct": _f(e.get("funpay_fee_pct")),
                "cost_rate_rub": _f(e.get("cost_rate_rub"))}

    # ------------------------------------------------------------------ StarsBot
    def _stars(self, out: list[dict], extra: dict) -> None:
        db = self.stars_db
        if not db.exists():
            return
        econ = self.stars_economics()
        wfee = econ["withdraw_fee_pct"] / 100
        cash = _sel(db, "cashflow", ["id", "ts", "kind", "rub", "ton", "note"], "ORDER BY ts")
        orders = _sel(db, "orders", ["order_id", "description", "stars", "price", "currency", "status", "fp_status",
                                     "tg_username", "ton_amount", "ton_rate", "cost_rub", "tx_hash", "tx_fee_ton",
                                     "created_at", "sent_at", "delivered_at", "completed_at", "channel", "delivery",
                                     "premium_months", "gift_cost", "payout"], "ORDER BY COALESCE(sent_at, created_at)")
        rates = [_f(o["ton_rate"]) for o in orders if _f(o["ton_rate"]) > 0]
        market = statistics.median(rates) if rates else 0.0
        lots = TonLots([(_f(c["ts"]), _f(c["rub"]), _f(c["ton"])) for c in cash if c["kind"] == "topup"],
                       econ["cost_rate_rub"] or market)
        for c in cash:
            kind = "topup" if c["kind"] == "topup" else ("withdraw" if "withdraw" in str(c["kind"]) else "transfer")
            rub, ton = _f(c["rub"]), _f(c["ton"])
            if kind == "topup":
                circle = "fp_rub>card_rub>usdt>ton[stars@funpay]>fp_rub"
                out.append(self._op(c["ts"], "starsbot", "stars", "topup", ton, "TON", -rub, f"cash#{c['id']}", "done",
                                    f"Покупка TON ({c['note'] or 'обмен'}): {rub:,.0f} ₽ → {ton:.4f} TON = "
                                    f"{rub / ton if ton else 0:.2f} ₽/TON", circle=circle, position="stars"))
            else:
                out.append(self._op(c["ts"], "starsbot", "treasury", kind, ton or rub, "TON" if ton else "RUB",
                                    -rub if rub else 0, f"cash#{c['id']}", "done", str(c["note"] or c["kind"])))
        spent_ton = gas_ton = 0.0
        for o in orders:
            st = o["status"] or ""
            ch = o["channel"] or "funpay"
            direction, position = ("premium", "premium") if o["premium_months"] else \
                (("gifts", "stars_gift") if str(o["delivery"] or "").startswith("gift") else ("stars", "stars"))
            circle = f"fp_rub>card_rub>usdt>ton[{position}@{ch}]>{CHANNEL_NODE.get(ch, 'fp_rub')}"
            ton, gas = _f(o["ton_amount"]), _f(o["tx_fee_ton"])
            ts = _f(o["sent_at"] or o["created_at"])
            label = o["description"] or f"{o['stars']}⭐"
            sent = bool(o["tx_hash"]) or st in ("delivered", "completed")
            if sent and ton > 0:
                rate = _f(o["ton_rate"]) or market
                real = lots.take(ton + gas, rate)
                gas_rub = real * gas / (ton + gas) if ton + gas else 0
                buy_rub = real - gas_rub
                spent_ton += ton
                gas_ton += gas
                out.append(self._op(ts, "starsbot", direction, "purchase", -ton, "TON", -buy_rub, o["order_id"], "done",
                                    f"Fragment: {label} → @{o['tg_username'] or '?'}; по рынку {ton * rate:,.2f} ₽, "
                                    f"по закупке {buy_rub:,.2f} ₽", circle=circle, position=position, channel=ch,
                                    market_rub=-ton * rate))
                if gas:
                    out.append(self._op(ts, "starsbot", direction, "gas", -gas, "TON", -gas_rub, o["order_id"], "done",
                                        "комиссия сети TON", circle=circle, position=position, channel=ch))
            price = _f(o["payout"] or o["price"])
            if price and st not in ("refunded", "blocked", "new", "awaiting_username", "pending_approval"):
                done = st == "completed"
                sts = _f(o["created_at"] or o["delivered_at"])     # день сделки, а не день подтверждения
                out.append(self._op(sts, "starsbot", direction, "sale", price, "RUB", price, o["order_id"],
                                    "done" if done else "pending",
                                    f"{ch}: {label}" + (f", подтверждён {dt.datetime.fromtimestamp(_f(o['completed_at'])):%d.%m %H:%M}"
                                                        if done and o["completed_at"] else
                                                        "" if done else " — ждём подтверждения покупателя"),
                                    circle=circle, position=position, channel=ch))
                if wfee or econ["withdraw_fee_fixed_rub"]:
                    out.append(self._op(sts, "starsbot", direction, "fee", -price * wfee, "RUB", -price * wfee,
                                        o["order_id"], "estimate",
                                        f"вывод с {ch} на карту {econ['withdraw_fee_pct']:g}% (оценка, пока не выведено)",
                                        circle=circle, position=position, channel=ch))
            elif st == "refunded" and sent:
                out.append(self._op(_f(o["completed_at"] or ts), "starsbot", direction, "sale", 0, "RUB", 0,
                                    o["order_id"], "done", "возврат покупателю: звёзды ушли, денег нет",
                                    circle=circle, position=position, channel=ch))
        for w in _sel(db, "withdrawals", ["id", "ts", "amount_rub", "method", "state", "detail"], "ORDER BY ts"):
            out.append(self._op(w["ts"], "starsbot", "treasury", "withdraw", -_f(w["amount_rub"]), "RUB", 0,
                                f"wd#{w['id']}", "done" if w["state"] in ("done", "ok", "sent") else "pending",
                                f"вывод {w['method'] or ''} ({w['state']})"))
        for m in _sel(db, "treasury_moves", ["id", "ts", "kind", "amount", "unit", "src", "dst", "state", "rub",
                                             "detail"], "ORDER BY ts"):
            out.append(self._op(m["ts"], "starsbot", "treasury", "transfer", _f(m["amount"]), m["unit"] or "",
                                0, f"tm#{m['id']}", "done" if m["state"] == "done" else "pending",
                                f"{m['kind']}: {m['src']} → {m['dst']} ({m['state']})"))
        # сверка кошелька: старт + пополнения − покупки − газ − остаток
        bal = _rows(db, "SELECT ts, ton FROM balance_history ORDER BY ts")
        topup_ton = sum(_f(c["ton"]) for c in cash if c["kind"] == "topup")
        topup_rub = sum(_f(c["rub"]) for c in cash if c["kind"] == "topup")
        if bal:
            start, end = _f(bal[0]["ton"]), _f(bal[-1]["ton"])
            diff = start + topup_ton - spent_ton - gas_ton - end
            twin = next((c for c in cash if c["kind"] == "topup" and abs(_f(c["ton"]) - diff) < 0.1), None)
            extra["wallets"].append({
                "id": "starsbot_ton", "title": "TON-кошелёк StarsBot", "asset": "TON", "start": _r(start, 4),
                "in": _r(topup_ton, 4), "out": _r(spent_ton + gas_ton, 4), "end": _r(end, 4), "diff": _r(diff, 4),
                "end_rub": _r(end * (lots.q[0][2] if lots.q else market)), "at": bal[-1]["ts"],
                "note": ("сходится" if abs(diff) < 0.05 else
                         f"лишние {diff:.4f} TON ≈ пополнение «{twin['note']}» на {_f(twin['rub']):,.0f} ₽: оно, похоже, "
                         "уже было на кошельке при первом замере (записано задним числом), сверка по TON сходится"
                         if twin else f"расхождение {diff:+.4f} TON: на кошельке при первом замере было {start:.4f} TON, "
                         "часть пополнений могла быть уже в нём или ушла вне бота")})
            extra["positions"].append({"bot": "starsbot", "position": "ton", "title": "TON на кошельке StarsBot",
                                       "qty": _r(end, 4), "unit": "TON", "value_rub": _r(end * (market or 0)),
                                       "circle": "fp_rub>card_rub>usdt>ton[stars@funpay]>fp_rub"})
        extra["rates"]["ton_market_rub"] = _r(market)
        extra["rates"]["ton_paid_rub"] = _r(topup_rub / topup_ton) if topup_ton else None
        extra["rates"]["unlotted_ton"] = _r(lots.unlotted_ton, 4)
        extra["rates"]["topups"] = [{"ts": c["ts"], "note": c["note"] or "", "rub": _r(_f(c["rub"])),
                                     "ton": _r(_f(c["ton"]), 4),
                                     "rate": _r(_f(c["rub"]) / _f(c["ton"])) if _f(c["ton"]) else None,
                                     "over_market_pct": _pct(_f(c["rub"]) / _f(c["ton"]) - market, market)
                                     if _f(c["ton"]) and market else None}
                                    for c in cash if c["kind"] == "topup"]
        pend = [o for o in orders if o["status"] in STARS_PENDING]
        if pend:
            extra["positions"].append({"bot": "starsbot", "position": "fp_hold", "title": "FunPay держит до подтверждения",
                                       "qty": len(pend), "unit": "заказ",
                                       "value_rub": _r(sum(_f(o["price"]) for o in pend)),
                                       "circle": "fp_rub>card_rub>usdt>ton[stars@funpay]>fp_rub"})

    # ------------------------------------------------------------------ MannBot
    def _mann(self, out: list[dict], extra: dict) -> None:
        db = self.mann_db
        if not db.exists():
            return
        cfg = self.bot_config("mannbot")
        cost_key = _f((cfg.get("rtba") or {}).get("cost_per_key"))
        fee_pct = _f((cfg.get("rtba") or {}).get("funpay_fee_pct"))
        wfee = self.stars_economics()["withdraw_fee_pct"] / 100
        circle = "fp_rub>card_rub>usdt>lis_usd>steam[mann_keys@funpay]>fp_rub"
        for o in _sel(db, "orders", ["order_id", "description", "keys_qty", "quantity", "price", "status",
                                     "created_at", "delivered_at", "completed_at"], "ORDER BY created_at"):
            if o["status"] not in ("delivered", "completed"):
                continue
            ts = _f(o["completed_at"] or o["delivered_at"])
            price = _f(o["price"]) * (1 - fee_pct / 100)
            keys = int(o["keys_qty"] or o["quantity"] or 0)
            done = o["status"] == "completed"
            out.append(self._op(ts, "mannbot", "mann", "sale", price, "RUB", price, o["order_id"],
                                "done" if done else "pending", f"FunPay: {o['description'] or keys} ключ(ей)",
                                circle=circle, position="mann_keys", channel="funpay"))
            if cost_key:
                out.append(self._op(ts, "mannbot", "mann", "purchase", -keys, "KEY", -keys * cost_key, o["order_id"],
                                    "done", f"себестоимость {keys} ключ(ей) × {cost_key:g} ₽ (rtba.cost_per_key)",
                                    circle=circle, position="mann_keys", channel="funpay"))
            if wfee:
                out.append(self._op(ts, "mannbot", "mann", "fee", -price * wfee, "RUB", -price * wfee, o["order_id"],
                                    "estimate", "вывод с FunPay на карту (оценка)", circle=circle,
                                    position="mann_keys", channel="funpay"))

    # ------------------------------------------------------------------ LisBot
    def _lis(self, out: list[dict], extra: dict, usd_rub: float) -> None:
        db = self.lis_db
        if not db.exists():
            return
        held_usd, held_n = 0.0, 0
        for it in _sel(db, "items", ["id", "name", "mode", "status", "dry_run", "buy_price_usd", "created_at",
                                     "sold_at", "sold_usd", "profit_usd", "expected_net_usd"], "ORDER BY created_at"):
            if it["dry_run"]:
                continue
            arb = (it["mode"] or "") == "arb"
            pos, circle = ("skins_arb", "fp_rub>card_rub>usdt>lis_usd>market_rub[skins_arb@market]>card_rub") if arb \
                else ("skins_steam", "fp_rub>card_rub>usdt>lis_usd>steam[skins_steam@steam]>lis_usd")
            buy = _f(it["buy_price_usd"])
            out.append(self._op(_f(it["created_at"]) / 1000, "lisbot", "lis", "purchase", -buy, "USD", -buy * usd_rub,
                                f"item#{it['id']}", "done", f"Lis-Skins: {it['name']}", circle=circle, position=pos,
                                channel="market" if arb else "steam"))
            if it["status"] == "sold" and it["sold_at"]:
                s = _f(it["sold_usd"])
                out.append(self._op(_f(it["sold_at"]) / 1000, "lisbot", "lis", "sale", s, "USD", s * usd_rub,
                                    f"item#{it['id']}", "done", f"продано: {it['name']}", circle=circle,
                                    position=pos, channel="market" if arb else "steam"))
            elif it["status"] not in ("failed", "cancelled", "refunded"):
                held_usd += buy
                held_n += 1
        snap = _rows(db, "SELECT ts, balance_usd FROM balance_snapshots ORDER BY ts DESC LIMIT 1")
        if snap:
            b = _f(snap[0]["balance_usd"])
            extra["positions"].append({"bot": "lisbot", "position": "lis_usd", "title": "Баланс Lis-Skins",
                                       "qty": _r(b), "unit": "USD", "value_rub": _r(b * usd_rub),
                                       "circle": "fp_rub>card_rub>usdt>lis_usd>market_rub[skins_arb@market]>card_rub",
                                       "idle": True})
        if held_n:
            extra["positions"].append({"bot": "lisbot", "position": "skins", "title": "Скины в работе",
                                       "qty": held_n, "unit": "шт.", "value_rub": _r(held_usd * usd_rub)})

    # ------------------------------------------------------------------ ShopBot
    def _shop(self, out: list[dict], extra: dict) -> None:
        db = self.shop_db
        if not db.exists():
            return
        for o in _sel(db, "orders", ["id", "kind", "product", "item", "amount", "status", "price_rub", "cost_rub",
                                     "fee_rub", "total_rub", "margin_rub", "pay_method", "payout_amount",
                                     "payout_asset", "created_at", "paid_at", "delivered_at", "loyalty_rub",
                                     "bonus_rub", "ref_reward_rub"], "ORDER BY created_at"):
            pos = str(o["product"] or "shop")
            circle = f"shop_rub>ton[{pos}@shop]>shop_rub"
            ts = _f(o["delivered_at"] or o["paid_at"] or o["created_at"])
            label = f"{o['product']} {o['item'] or o['amount'] or ''}".strip()
            if o["kind"] == "buy" and o["status"] == "delivered":
                total = _f(o["total_rub"])
                out.append(self._op(ts, "shopbot", "shop", "sale", total, "RUB", total, o["id"], "done",
                                    f"магазин: {label} ({o['pay_method'] or 'оплата'})", circle=circle, position=pos,
                                    channel="shop"))
                if _f(o["cost_rub"]):
                    out.append(self._op(ts, "shopbot", "shop", "purchase", -_f(o["cost_rub"]), "RUB",
                                        -_f(o["cost_rub"]), o["id"], "done", f"закупка: {label}", circle=circle,
                                        position=pos, channel="shop"))
                fees = _f(o["fee_rub"]) + _f(o["ref_reward_rub"]) + _f(o["bonus_rub"])
                if fees:
                    out.append(self._op(ts, "shopbot", "shop", "fee", -fees, "RUB", -fees, o["id"], "done",
                                        "комиссия оплаты, реферальные и бонусы", circle=circle, position=pos,
                                        channel="shop"))
            elif o["kind"] == "sell" and o["status"] == "paid_out":
                amt = _f(o["payout_amount"])
                out.append(self._op(ts, "shopbot", "shop", "purchase", -amt, o["payout_asset"] or "RUB",
                                    -_f(o["total_rub"]), o["id"], "done", f"выкуп у клиента: {label}",
                                    circle=circle, position=pos, channel="shop"))

    # ------------------------------------------------------------------ общее
    @staticmethod
    def _op(ts, bot, direction, kind, amount, currency, rub, ref, status, note, *, circle="", position="",
            channel="", market_rub=None) -> dict:
        d = {"ts": _r(_f(ts), 3), "bot": bot, "direction": direction, "position": position, "channel": channel,
             "circle": circle, "kind": kind, "amount": _r(amount, 6), "currency": currency, "rub": _r(rub),
             "ref": str(ref or ""), "status": status, "note": note}
        if market_rub is not None:
            d["market_rub"] = _r(market_rub)
        return d

    def collect(self, usd_rub: float = 90.0) -> tuple[list[dict], dict]:
        ops: list[dict] = []
        extra = {"wallets": [], "positions": [], "rates": {"usd_rub": usd_rub}, "errors": []}
        for name, fn in (("starsbot", lambda: self._stars(ops, extra)), ("mannbot", lambda: self._mann(ops, extra)),
                         ("lisbot", lambda: self._lis(ops, extra, usd_rub)), ("shopbot", lambda: self._shop(ops, extra))):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — одна битая база не должна ронять весь отчёт
                log.exception("economy %s", name)
                extra["errors"].append(f"{BOTS[name]}: {e}")
        ops.sort(key=lambda o: o["ts"])
        return ops, extra

    def ledger(self, days: float | None = None, usd_rub: float = 90.0, **flt) -> list[dict]:
        ops, _ = self.collect(usd_rub)
        return self._filter(ops, days, **flt)

    @staticmethod
    def _filter(ops, days=None, bot=None, direction=None, kind=None, status=None, q=None) -> list[dict]:
        since = time.time() - days * DAY if days else 0
        ql = (q or "").lower()
        return [o for o in ops if o["ts"] >= since and (not bot or o["bot"] == bot)
                and (not direction or o["direction"] == direction) and (not kind or o["kind"] == kind)
                and (not status or o["status"] == status)
                and (not ql or ql in o["note"].lower() or ql in o["ref"].lower())]

    # ------------------------------------------------------------------ итоги
    @staticmethod
    def _totals(ops: list[dict]) -> dict:
        t = defaultdict(float)
        orders = set()
        for o in ops:
            r = o["rub"] or 0
            if o["kind"] == "sale":
                t["revenue" if o["status"] == "done" else "revenue_pending"] += r
                orders.add((o["bot"], o["ref"]))
            elif o["kind"] == "purchase":
                t["cost"] += -r
                if o.get("market_rub") is not None:
                    t["cost_market"] += -o["market_rub"]
            elif o["kind"] == "gas":
                t["gas"] += -r
            elif o["kind"] == "fee":
                t["fee_estimate" if o["status"] == "estimate" else "fee"] += -r
        revenue = t["revenue"] + t["revenue_pending"]
        gross = revenue - t["cost"] - t["gas"] - t["fee"]
        net = gross - t["fee_estimate"]
        return {"orders": len(orders), "revenue": _r(revenue), "revenue_done": _r(t["revenue"]),
                "revenue_pending": _r(t["revenue_pending"]), "cost": _r(t["cost"]), "gas": _r(t["gas"]),
                "fees": _r(t["fee"]), "fee_estimate": _r(t["fee_estimate"]),
                "fx_loss": _r(t["cost"] - t["cost_market"]) if t["cost_market"] else 0,
                "gross": _r(gross), "gross_pct": _pct(gross, revenue), "net": _r(net), "net_pct": _pct(net, revenue)}

    def _circles(self, ops: list[dict], extra: dict) -> list[dict]:
        by = defaultdict(list)
        for o in ops:
            if o["circle"]:
                by[o["circle"]].append(o)
        out = []
        for cid, rows in by.items():
            t = self._totals(rows)
            parts = cid.replace("[", ">[").split(">")
            nodes = [p for p in parts if not p.startswith("[")]
            pos = next((p.strip("[]") for p in parts if p.startswith("[")), "")
            position, _, channel = pos.partition("@")
            legs = self._legs(cid, rows, t, extra, nodes, position, channel)
            out.append({"id": cid, "title": self._circle_title(position, channel), "nodes": nodes,
                        "node_titles": [NODES.get(n, n) for n in nodes], "position": position, "channel": channel,
                        "direction": rows[0]["direction"], "legs": legs, "open": True, **t,
                        "pct": t["net_pct"], "pnl_rub": t["net"], "first_ts": rows[0]["ts"], "last_ts": rows[-1]["ts"]})
        out.sort(key=lambda c: -(c["revenue"] or 0))
        return out

    @staticmethod
    def _circle_title(position: str, channel: str) -> str:
        names = {"stars": "Звёзды", "premium": "Premium", "stars_gift": "Звёзды подарком", "mann_keys": "Ключи Mann",
                 "skins_arb": "Скины Lis → Market.CSGO", "skins_steam": "Скины Lis → Steam"}
        ch = {"funpay": "FunPay", "ggsel": "GGSEL", "shop": "магазин", "market": "Market.CSGO", "steam": "Steam"}
        return f"{names.get(position, position)} через {ch.get(channel, channel)}"

    @staticmethod
    def _legs(cid, rows, t, extra, nodes, position, channel) -> list[dict]:
        """Шаги круга с потерей/прибылью каждого в ₽ и в % от выручки круга."""
        rev = t["revenue"] or 0
        legs = []
        if "ton" in nodes and position in ("stars", "premium", "stars_gift"):
            paid = extra["rates"].get("ton_paid_rub")
            mkt = extra["rates"].get("ton_market_rub")
            fx = t["fx_loss"] or 0
            legs.append({"from": "card_rub", "to": "ton", "kind": "transfer", "title": "₽ → USDT → TON (P2P и биржа)",
                         "rub": _r(-fx), "pct": _pct(-fx, rev),
                         "note": f"платим {paid} ₽ за TON при рынке {mkt} ₽" if paid and mkt else ""})
            legs.append({"from": "ton", "to": "xtr", "kind": "purchase", "title": "TON → звёзды на Fragment",
                         "rub": _r(-(t["cost"] - fx) - t["gas"]), "pct": _pct(-(t["cost"] - fx) - t["gas"], rev),
                         "note": f"по рыночному курсу TON, газ {t['gas']:.2f} ₽"})
            legs.append({"from": "xtr", "to": CHANNEL_NODE.get(channel, "fp_rub"), "kind": "sale",
                         "position": position, "channel": channel, "title": "звёзды → покупателю, ₽ на FunPay",
                         "rub": _r(rev), "pct": 100.0 if rev else None,
                         "note": f"ждёт подтверждения {t['revenue_pending']:.2f} ₽" if t["revenue_pending"] else ""})
        else:
            legs.append({"from": nodes[0], "to": nodes[-2] if len(nodes) > 1 else nodes[0], "kind": "purchase",
                         "title": "закупка товара", "rub": _r(-t["cost"] - t["gas"] - t["fees"]),
                         "pct": _pct(-t["cost"] - t["gas"] - t["fees"], rev)})
            legs.append({"from": nodes[-2] if len(nodes) > 1 else nodes[0], "to": nodes[-1], "kind": "sale",
                         "position": position, "channel": channel, "title": "продажа", "rub": _r(rev),
                         "pct": 100.0 if rev else None})
        if t["fee_estimate"]:
            legs.append({"from": nodes[-1], "to": "card_rub", "kind": "transfer", "title": "вывод на карту (оценка)",
                         "rub": _r(-t["fee_estimate"]), "pct": _pct(-t["fee_estimate"], rev)})
        return legs

    def _daily(self, ops: list[dict]) -> list[dict]:
        days = defaultdict(list)
        for o in ops:
            days[dt.date.fromtimestamp(o["ts"]).isoformat()].append(o)
        return [{"day": d, **self._totals(v)} for d, v in sorted(days.items())]

    def report(self, days: float | None = None, usd_rub: float = 90.0, ops_limit: int = 300) -> dict:
        all_ops, extra = self.collect(usd_rub)
        ops = self._filter(all_ops, days)
        by_dir = defaultdict(list)
        for o in ops:
            by_dir[o["direction"]].append(o)
        directions = [{"id": d, "title": DIRECTIONS.get(d, d), **self._totals(v),
                       "bots": sorted({o["bot"] for o in v})} for d, v in by_dir.items()]
        directions.sort(key=lambda d: -(d["revenue"] or 0))
        capital = sum(_f(p.get("value_rub")) for p in extra["positions"])
        total = self._totals(ops)
        warnings = list(extra["errors"])
        r = extra["rates"]
        if r.get("ton_paid_rub") and r.get("ton_market_rub") and r["ton_paid_rub"] > r["ton_market_rub"] * 1.02:
            warnings.append(f"TON покупается на {(r['ton_paid_rub'] / r['ton_market_rub'] - 1) * 100:.1f}% дороже рынка "
                            f"({r['ton_paid_rub']} против {r['ton_market_rub']} ₽): это главный расход круга звёзд")
        for c in self._circles(ops, extra):
            if c["net_pct"] is not None and c["net_pct"] < 0:
                warnings.append(f"Круг «{c['title']}» в минусе: {c['net']:+,.0f} ₽ ({c['net_pct']:+.1f}%) после вывода")
        idle = [p for p in extra["positions"] if p.get("idle") and _f(p.get("value_rub")) > 0]
        for p in idle:
            warnings.append(f"{p['title']}: {p['qty']} {p['unit']} лежат без оборота")
        return {"generated_at": time.time(), "days": days, "totals": total, "directions": directions,
                "circles": self._circles(ops, extra), "positions": extra["positions"], "wallets": extra["wallets"],
                "rates": r, "capital_rub": _r(capital), "daily": self._daily(ops), "warnings": warnings,
                "ops_total": len(ops), "ops": ops[-ops_limit:][::-1],
                "titles": {"directions": DIRECTIONS, "bots": BOTS, "nodes": NODES, "kinds": KINDS}}

    def position_margin(self, position: str, channel: str = "funpay", days: float | None = 30) -> float | None:
        """Фактическая маржа позиции в % от выручки после всех расходов, включая оценку вывода (для center/loops.py)."""
        ops, _ = self.collect()
        rows = [o for o in self._filter(ops, days) if o["position"] == position and (not channel or o["channel"] == channel)]
        return self._totals(rows)["net_pct"] if rows else None

    # ------------------------------------------------------------------ экспорт
    def csv(self, ops: list[dict]) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Время", "Бот", "Направление", "Операция", "Сумма", "Валюта", "₽", "Статус", "Ссылка", "Круг",
                    "Описание"])
        for o in ops:
            w.writerow([dt.datetime.fromtimestamp(o["ts"]).strftime("%Y-%m-%d %H:%M:%S"), BOTS.get(o["bot"], o["bot"]),
                        DIRECTIONS.get(o["direction"], o["direction"]), KINDS.get(o["kind"], o["kind"]),
                        str(o["amount"]).replace(".", ","), o["currency"], str(o["rub"]).replace(".", ","),
                        o["status"], o["ref"], o["circle"], o["note"]])
        return ("﻿" + buf.getvalue()).encode("utf-8")


# ---------------------------------------------------------------------------- маршруты
def routes(center) -> list:
    from aiohttp import web  # noqa: PLC0415

    static = Path(__file__).resolve().parent / "static"

    def eco() -> Economy:
        return Economy(center.cfg.home, center.cfg.data.get("economy") or {})

    async def rate() -> float:
        try:
            return float((await center.analytics.usd_rub())["rate"])
        except Exception:  # noqa: BLE001
            return 90.0

    def days_q(request) -> float | None:
        v = request.query.get("days", "")
        try:
            return float(v) if v not in ("", "all", "0") else None
        except ValueError:
            return None

    async def page(request):
        return web.FileResponse(static / "economy.html", headers={"Cache-Control": "no-store"})

    async def summary(request):
        rep = eco().report(days_q(request), await rate(), ops_limit=int(request.query.get("ops", 300) or 300))
        return web.json_response(rep)

    async def ledger(request):
        q = request.query
        ops = eco().ledger(days_q(request), await rate(), bot=q.get("bot"), direction=q.get("direction"),
                           kind=q.get("kind"), status=q.get("status"), q=q.get("q"))
        return web.json_response({"ops": ops[::-1], "total": len(ops)})

    async def export(request):
        e = eco()
        ops = e.ledger(days_q(request), await rate())
        name = f"economy-{dt.date.today().isoformat()}.csv"
        return web.Response(body=e.csv(ops), content_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return [web.get("/economy", page), web.get("/api/economy/summary", summary),
            web.get("/api/economy/ledger", ledger), web.get("/api/economy/export", export)]
