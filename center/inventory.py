"""Склад центра: что лежит у ботов прямо сейчас и сколько это стоит.

LisBot — купленные и ещё не проданные предметы: себестоимость, оценка по рынку (последняя оценка цены
из valuations: сколько бот получит после комиссии Steam, в $), бумажная прибыль или убыток, сколько дней
предмет лежит, флаги «долго лежит», «в минусе», «лимит перевыставлений».
MannBot — ключи на складе, себестоимость, скорость продаж за 14 дней и на сколько дней хватит ключей.
Ключей на меньше чем inventory.mann_days_warn дней — уведомление (не чаще раза в сутки).

Базы ботов читаются только на чтение (как в аналитике).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import web

from center.analytics import DAY, LIS_HELD, MANN_DONE, _kv, _r, _rows
from center.config import BOTS_DIR

log = logging.getLogger("center.inventory")

STATUS_TEXT = {"bought": "куплен", "locked_on_lis": "блокировка LIS", "trade_sent": "трейд от LIS", "protected": "Trade Protection",
               "ready": "готов к продаже", "listing": "выставляется", "listed": "на ТП"}
AGE_BUCKETS = ((0, 7, "до 7 дн"), (7, 14, "7–14 дн"), (14, 30, "14–30 дн"), (30, None, "30+ дн"))
VALUATION_TTL = 3 * DAY          # оценка цены старше — не доверяем, берём прогноз при покупке


class Inventory:
    def __init__(self, center, clock=time.time):
        self.center = center
        self.clock = clock
        self._task: asyncio.Task | None = None

    @property
    def cfg(self) -> dict:
        return self.center.cfg.data.get("inventory") or {}

    def _lis_sell_cfg(self) -> dict:
        try:
            base = json.loads((BOTS_DIR / "lisbot" / "config.example.json").read_text(encoding="utf-8")).get("sell") or {}
        except (OSError, ValueError):
            base = {}
        return {**base, **(self.center.analytics.lis_config().get("sell") or {})}

    # ------------------------------------------------------------------ LisBot
    def lisbot(self) -> dict:
        a = self.center.analytics
        now = self.clock()
        sell = self._lis_sell_cfg()
        old_days = float(self.cfg.get("lis_old_days") or sell.get("maxHoldDaysAlert") or 21)
        max_relists = int(sell.get("maxRelists") or 5)
        vals = {r["name"]: r for r in _rows(a.lis_db, "SELECT name, ts, ok, net_usd, trend FROM valuations")}
        rows = _rows(a.lis_db, f"SELECT id, name, status, mode, buy_price_usd, expected_net_usd, created_at, received_at, "
                     f"list_receive_cents, relists FROM items WHERE dry_run=0 AND status IN ({','.join('?' * len(LIS_HELD))}) "
                     f"ORDER BY created_at", LIS_HELD)
        items = []
        for r in rows:
            v = vals.get(r["name"])
            fresh = v is not None and v["ok"] and v["net_usd"] and now - v["ts"] / 1000 <= VALUATION_TTL
            value = float(v["net_usd"]) if fresh else r["expected_net_usd"]
            days = (now - r["created_at"] / 1000) / DAY
            pnl = value - r["buy_price_usd"] if value is not None else None
            flags = []
            if days >= old_days:
                flags.append("долго лежит")
            if pnl is not None and pnl < 0:
                flags.append("в минусе")
            if (r["relists"] or 0) >= max_relists:
                flags.append("лимит перевыставлений")
            items.append({"id": r["id"], "name": r["name"], "status": r["status"],
                          "status_text": STATUS_TEXT.get(r["status"], r["status"]), "mode": r["mode"],
                          "cost": _r(r["buy_price_usd"]), "value": _r(value), "pnl": _r(pnl),
                          "pnl_pct": _r(pnl / r["buy_price_usd"] * 100, 1) if pnl is not None and r["buy_price_usd"] else None,
                          "value_source": "рынок" if fresh else "прогноз при покупке" if value is not None else "нет оценки",
                          "trend": _r(v["trend"] * 100, 1) if fresh and v["trend"] is not None else None,
                          "list_usd": _r(r["list_receive_cents"] / 100) if r["list_receive_cents"] else None,
                          "days": _r(days, 1), "relists": r["relists"] or 0, "flags": flags})
        valued = [i for i in items if i["value"] is not None]
        buckets = []
        for lo, hi, label in AGE_BUCKETS:
            sel = [i for i in items if i["days"] >= lo and (hi is None or i["days"] < hi)]
            buckets.append({"label": label, "n": len(sel), "cost": _r(sum(i["cost"] for i in sel))})
        items.sort(key=lambda i: (not i["flags"], i["pnl"] if i["pnl"] is not None else 0))
        cost = sum(i["cost"] for i in items)
        value = sum(i["value"] for i in valued)
        pnl = sum(i["pnl"] for i in valued)
        return {"currency": "USD", "n": len(items), "cost": _r(cost), "value": _r(value), "pnl": _r(pnl),
                "pnl_pct": _r(pnl / sum(i["cost"] for i in valued) * 100, 1) if valued else None,
                "unvalued": len(items) - len(valued), "flagged": sum(1 for i in items if i["flags"]),
                "losing": sum(1 for i in items if "в минусе" in i["flags"]),
                "old": sum(1 for i in items if "долго лежит" in i["flags"]), "old_days": old_days,
                "buckets": buckets, "items": items}

    # ------------------------------------------------------------------ MannBot
    def mannbot(self) -> dict:
        a = self.center.analytics
        now = self.clock()
        window = float(self.cfg.get("mann_rate_days") or 14)
        stock = _kv(a.mann_db, "last_stock")
        stock = stock if isinstance(stock, (int, float)) else None
        cost_key, _fee = a.mann_costs()
        sold = _rows(a.mann_db, f"SELECT COALESCE(SUM(keys_qty),0) k, MIN(delivered_at) first FROM orders "
                     f"WHERE delivered_at>=? AND status IN ({','.join('?' * len(MANN_DONE))})", (now - window * DAY, *MANN_DONE))
        keys = int(sold[0]["k"]) if sold else 0
        # бот моложе окна: скорость считаем по фактическому сроку; продаж меньше чем за сутки — прогноза нет
        first = sold[0]["first"] if sold and sold[0]["first"] else None
        span = min(window, (now - first) / DAY) if first else window
        rate = keys / span if keys and span >= 1 else 0.0
        days_left = stock / rate if stock is not None and rate > 0 else None
        warn_days = float(self.cfg.get("mann_days_warn") or 3)
        level = ("empty" if stock is not None and stock <= 0 else
                 "low" if days_left is not None and days_left < warn_days else "ok" if stock is not None else "unknown")
        return {"currency": "RUB", "stock": stock, "cost_per_key": cost_key,
                "value": _r(cost_key * stock) if cost_key and stock is not None else None,
                "sold_keys": keys, "rate_days": _r(span, 1), "per_day": _r(rate, 2), "days_left": _r(days_left, 1),
                "warn_days": warn_days, "level": level}

    async def overview(self) -> dict:
        lis, mann = self.lisbot(), self.mannbot()
        rate = (await self.center.analytics.usd_rub())["rate"]
        conv = self.center.analytics._conv
        total = {}
        for to in ("USD", "RUB"):
            parts = [(lis["cost"], "USD"), (mann["value"], "RUB")]
            total[to] = {"cost": _r(sum(conv(v, c, to, rate) for v, c in parts if v is not None)),
                         "value": _r(conv(lis["value"], "USD", to, rate) + (conv(mann["value"], "RUB", to, rate) or 0)),
                         "pnl": _r(conv(lis["pnl"], "USD", to, rate))}
        return {"lisbot": lis, "mannbot": mann, "total": total, "rate": rate}

    def brief(self) -> dict:
        """Коротко для координатора и ежедневного отчёта."""
        lis, mann = self.lisbot(), self.mannbot()
        return {"lisbot": {k: lis[k] for k in ("n", "cost", "value", "pnl", "losing", "old", "old_days")}
                | {"flagged_items": [{k: i[k] for k in ("name", "days", "cost", "value", "pnl", "flags")}
                                     for i in lis["items"] if i["flags"]][:8]},
                "mannbot": {k: mann[k] for k in ("stock", "per_day", "days_left", "level")}}

    def report_lines(self) -> list[str]:
        b = self.brief()
        lis, mann = b["lisbot"], b["mannbot"]
        out = []
        if lis["n"]:
            line = f"Склад LisBot: {lis['n']} шт на ${lis['cost']:.2f}"
            if lis["pnl"] is not None:
                line += f", по рынку {'+' if lis['pnl'] >= 0 else '−'}${abs(lis['pnl']):.2f}"
            if lis["losing"] or lis["old"]:
                line += f" · в минусе {lis['losing']}, лежат дольше {lis['old_days']:.0f} дн: {lis['old']}"
            out.append(line)
        if mann["stock"] is not None:
            line = f"Склад MannBot: {mann['stock']} ключей"
            if mann["days_left"] is not None:
                line += f", хватит примерно на {mann['days_left']:.1f} дн ({mann['per_day']:.1f} в день)"
            if mann["level"] in ("low", "empty"):
                line = "⚠ " + line
            out.append(line)
        return out

    # ------------------------------------------------------------------ сторож склада
    def check(self) -> str | None:
        """Ключей MannBot мало — событие в журнал центра (оттуда уведомление), не чаще раза в сутки."""
        m = self.mannbot()
        if m["level"] != "low":
            return None
        mon = self.center.monitor
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if mon.kv_get("inventory_low_date") == today:
            return None
        mon.kv_set("inventory_low_date", today)
        msg = (f"Ключи MannBot заканчиваются: на складе {m['stock']}, продаётся {m['per_day']:.1f} в день, "
               f"хватит примерно на {m['days_left']:.1f} дн. Пополните склад.")
        self.center.events.add("mannbot", "warn", "inventory.low_stock", msg)
        return msg

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.check)
            except Exception:  # noqa: BLE001
                log.exception("проверка склада")
            await asyncio.sleep(float(self.cfg.get("check_seconds") or 900))

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="inventory")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def routes(center) -> list:
    inv: Inventory = center.inventory

    async def overview(request: web.Request):
        return web.json_response(await inv.overview())

    return [web.get("/api/inventory", overview)]
