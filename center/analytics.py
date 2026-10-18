"""Аналитика перепродажи по всему центру: единый P&L обоих ботов в USD и RUB, дневные ряды,
детали по ботам (LisBot — предметы, точность прогноза, удержание; MannBot — воронка и время сделки)
и экспорт всех сделок в CSV/XLSX.

Базы ботов читаются только на чтение. Деньги LisBot ведутся в USD (buy_price_usd, sold_usd, profit_usd),
MannBot — в валюте FunPay (обычно ₽); себестоимость ключа и комиссия FunPay берутся из config.yaml MannBot.
Сделка относится к дню продажи: LisBot — sold_at, MannBot — delivered_at (ключи у покупателя).
"""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import logging
import sqlite3
import statistics
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import aiohttp
import yaml

log = logging.getLogger("center.analytics")

DAY = 86400
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
LIS_HELD = ("bought", "locked_on_lis", "trade_sent", "protected", "ready", "listing", "listed")
MANN_DONE = ("delivered", "completed")


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    return con


def _rows(path: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    con = _ro(path)
    if con is None:
        return []
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        log.debug("%s: %s", path.name, e)
        return []
    finally:
        con.close()


def _kv(path: Path, key: str):
    rows = _rows(path, "SELECT value FROM kv WHERE key=?", (key,))
    try:
        return json.loads(rows[0]["value"]) if rows else None
    except (TypeError, ValueError):
        return None


def _median(xs) -> float | None:
    xs = [x for x in xs if x is not None and x >= 0]
    return statistics.median(xs) if xs else None


def _r(x, n=2):
    return round(x, n) if isinstance(x, (int, float)) else x


def _day(ts: float) -> str:
    return dt.date.fromtimestamp(ts).isoformat()


class Analytics:
    def __init__(self, center):
        self.center = center
        self._rate: dict = {}            # {"rate", "source", "ts"} последнего внешнего курса
        self._rate_lock = asyncio.Lock()
        self._rate_fail_at = 0.0        # когда ЦБ последний раз не ответил: повтор не раньше чем через 10 мин

    # ------------------------------------------------------------------ источники
    @property
    def cfg(self) -> dict:
        return self.center.cfg.data.get("analytics") or {}

    @property
    def lis_db(self) -> Path:
        return self.center.workers["lisbot"].data_dir / "bot.sqlite"

    @property
    def mann_db(self) -> Path:
        return self.center.workers["mannbot"]._cfg_path.parent / "data" / "mannbot.sqlite3"

    def lis_config(self) -> dict:
        try:
            return json.loads(self.center.workers["lisbot"].config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def mann_config(self) -> dict:
        try:
            return yaml.safe_load(self.center.workers["mannbot"]._cfg_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}

    def mann_effective_config(self) -> dict:
        """Настройки, с которыми MannBot работает на самом деле (только для чтения: записывать — mann_config).
        В демо лаунчер MannBot накладывает DEMO_OVERRIDES (лоты, себестоимость ключа) поверх config.yaml."""
        raw = self.mann_config()
        if not self.center.demo:
            return raw
        try:
            from center.rtba import _merge, mannbot_modules  # noqa: PLC0415
            mannbot_modules()                                       # bots/mannbot в sys.path
            from mannbot.app.launcher import DEMO_OVERRIDES  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return raw
        return _merge(raw, DEMO_OVERRIDES)

    # ------------------------------------------------------------------ курс USD→RUB
    async def usd_rub(self) -> dict:
        """Курс в порядке приоритета: center.yaml analytics.usd_rub → курс LisBot (FxService, если кошелёк
        Steam в рублях) → ЦБ РФ (кэш 6 ч) → analytics.fallback_usd_rub."""
        fixed = self.cfg.get("usd_rub", "auto")
        if isinstance(fixed, (int, float)) and fixed > 0:
            return {"rate": float(fixed), "source": "center.yaml"}
        steam = self.lis_config().get("steam") or {}
        if int(steam.get("walletCurrency") or 5) == 5:
            fx = _kv(self.lis_db, "fx")
            if isinstance(fx, dict) and (fx.get("rate") or 0) > 1:
                return {"rate": float(fx["rate"]), "source": "LisBot (Steam)", "ts": (fx.get("ts") or 0) / 1000}
        async with self._rate_lock:
            failed_recently = time.time() - self._rate_fail_at < 600
            if (not self._rate or time.time() - self._rate["ts"] > 6 * 3600) and not failed_recently:
                try:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as s:
                        async with s.get(CBR_URL) as r:
                            d = json.loads(await r.text())
                    self._rate = {"rate": float(d["Valute"]["USD"]["Value"]), "source": "ЦБ РФ", "ts": time.time()}
                except Exception as e:  # noqa: BLE001 — нет сети: берём запасной курс
                    log.debug("курс ЦБ: %s", e)
                    self._rate_fail_at = time.time()    # без сети не ждём таймаут на каждом запросе панели
            if not self._rate:
                return {"rate": float(self.cfg.get("fallback_usd_rub") or 95), "source": "запасной (center.yaml)"}
        return dict(self._rate)

    # ------------------------------------------------------------------ сделки
    def lis_deals(self, since: float = 0) -> list[dict]:
        rows = _rows(self.lis_db, "SELECT * FROM items WHERE dry_run=0 AND status='sold' AND sold_at>=? ORDER BY sold_at",
                     (since * 1000,))
        out = []
        for r in rows:
            recv = r["received_at"] or r["created_at"]
            out.append({
                "bot": "lisbot", "id": f"L{r['id']}", "name": r["name"], "qty": 1, "currency": "USD",
                "bought_at": (r["created_at"] or 0) / 1000, "sold_at": r["sold_at"] / 1000,
                "cost": r["buy_price_usd"] or 0, "revenue": r["sold_usd"] or 0, "fee": None,
                "profit": r["profit_usd"] if r["profit_usd"] is not None else (r["sold_usd"] or 0) - (r["buy_price_usd"] or 0),
                "expected": r["expected_net_usd"], "hold_days": (r["sold_at"] - recv) / 86400000 if recv else None,
                "mode": r["mode"], "counterparty": r["partner_steamid"] or "", "status": r["status"],
            })
        return out

    def mann_costs(self) -> tuple[float, float]:
        rt = self.mann_effective_config().get("rtba") or {}
        return float(rt.get("cost_per_key") or 0), float(rt.get("funpay_fee_pct") or 0) / 100

    def mann_deals(self, since: float = 0) -> list[dict]:
        cost_key, fee = self.mann_costs()
        rows = _rows(self.mann_db, "SELECT * FROM orders WHERE delivered_at IS NOT NULL AND delivered_at>=? AND "
                     "status<>'refunded' ORDER BY delivered_at", (since,))
        out = []
        for r in rows:
            cur = str(r["currency"] or "₽").strip()
            cur = "USD" if cur in ("$", "USD") else "RUB"
            price = float(r["price"] or 0)
            keys = int(r["keys_qty"] or r["quantity"] or 0)
            fee_sum = price * fee
            cost = cost_key * keys if cost_key else None
            out.append({
                "bot": "mannbot", "id": f"M{r['order_id']}", "name": r["description"] or "Mann Co. Key", "qty": keys,
                "currency": cur, "bought_at": r["paid_at"] or r["created_at"], "sold_at": r["delivered_at"],
                "cost": cost, "revenue": price, "fee": fee_sum,
                "profit": price - fee_sum - cost if cost is not None else None,
                "expected": None, "hold_days": None, "mode": "funpay", "counterparty": r["buyer_username"] or "",
                "status": r["status"],
            })
        return out

    def deals(self, since: float = 0) -> list[dict]:
        return sorted(self.lis_deals(since) + self.mann_deals(since), key=lambda d: d["sold_at"])

    # ------------------------------------------------------------------ P&L
    @staticmethod
    def _conv(v, cur: str, to: str, rate: float):
        if v is None or cur == to:
            return v
        return v * rate if to == "RUB" else v / rate

    def _capital(self) -> dict:
        """Капитал в работе по ботам (в валюте бота): LisBot — баланс LIS + купленное непроданное,
        MannBot — ключи на складе по себестоимости."""
        bal = _kv(self.lis_db, "balance.last") or {}
        held = _rows(self.lis_db, f"SELECT COALESCE(SUM(buy_price_usd),0) s, COUNT(*) n FROM items WHERE dry_run=0 AND "
                     f"status IN ({','.join('?' * len(LIS_HELD))})", LIS_HELD)
        exposure = held[0]["s"] if held else 0
        cost_key, _ = self.mann_costs()
        stock = _kv(self.mann_db, "last_stock")
        stock_value = cost_key * stock if cost_key and isinstance(stock, (int, float)) else None
        return {
            "lisbot": {"currency": "USD", "balance": bal.get("balance"), "goods": exposure, "goods_n": held[0]["n"] if held else 0,
                       "total": (bal.get("balance") or 0) + exposure},
            "mannbot": {"currency": "RUB", "stock": stock, "goods": stock_value, "total": stock_value},
        }

    async def pnl(self, days: float | None = 30) -> dict:
        since = time.time() - days * DAY if days else 0
        rate = await self.usd_rub()
        cap = self._capital()
        cost_key, fee = self.mann_costs()
        bots: dict[str, dict] = {}
        for name, deals in (("lisbot", self.lis_deals(since)), ("mannbot", self.mann_deals(since))):
            b = {"deals": len(deals), "units": sum(d["qty"] for d in deals)}
            for to in ("USD", "RUB"):
                c = lambda v, d: self._conv(v, d["currency"], to, rate["rate"])  # noqa: E731
                rev = sum(c(d["revenue"], d) for d in deals)
                known = [d for d in deals if d["profit"] is not None]
                goods = sum(c(d["cost"], d) or 0 for d in known)
                fees = sum(c(d["fee"] or 0, d) for d in deals)
                cost = goods + sum(c(d["fee"] or 0, d) for d in known)        # расходы: товар + комиссии
                profit = sum(c(d["profit"], d) for d in known)
                capital = self._conv(cap[name]["total"], cap[name]["currency"], to, rate["rate"])
                b[to] = {"revenue": _r(rev), "cost": _r(cost), "goods": _r(goods), "fees": _r(fees),
                         "profit": _r(profit) if known or not deals else None,
                         "roi": _r(profit / cost * 100, 1) if cost else None, "capital": _r(capital),
                         # оборачиваемость: сколько раз за период через сделки прошёл капитал бота
                         "turnover": _r(goods / capital, 2) if capital and goods else None}
            b["cost_known"] = name == "lisbot" or bool(cost_key) or not deals
            bots[name] = b
        total = {"deals": sum(b["deals"] for b in bots.values())}
        for to in ("USD", "RUB"):
            t = {k: sum(b[to][k] or 0 for b in bots.values()) for k in ("revenue", "cost", "goods", "fees", "profit", "capital")}
            t["roi"] = _r(t["profit"] / t["cost"] * 100, 1) if t["cost"] else None
            t["turnover"] = _r(t["goods"] / t["capital"], 2) if t["capital"] and t["goods"] else None
            total[to] = {k: _r(v) for k, v in t.items()}
        warnings = []
        if not bots["mannbot"]["cost_known"]:
            warnings.append("MannBot: не задана себестоимость ключа (rtba.cost_per_key) — прибыль и ROI MannBot "
                            "не считаются, в итог вошла только выручка.")
        if not fee and bots["mannbot"]["deals"]:
            warnings.append("MannBot: комиссия FunPay (rtba.funpay_fee_pct) равна 0 — если FunPay удерживает её "
                            "из цены, прибыль завышена.")
        return {"days": days, "rate": rate, "bots": bots, "total": total, "capital": cap, "warnings": warnings,
                "generated_at": time.time()}

    async def daily(self, days: int = 30, currency: str = "RUB") -> dict:
        rate = (await self.usd_rub())["rate"]
        today = dt.date.today()
        keys = [(today - dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        series = {k: {"day": k, "lisbot_revenue": 0.0, "lisbot_profit": 0.0, "lisbot_deals": 0, "lisbot_spent": 0.0,
                      "mannbot_revenue": 0.0, "mannbot_profit": 0.0, "mannbot_deals": 0} for k in keys}
        since = dt.datetime.combine(today - dt.timedelta(days=days - 1), dt.time()).timestamp()
        for d in self.deals(since):
            row = series.get(_day(d["sold_at"]))
            if row is None:
                continue
            p = d["bot"]
            row[f"{p}_revenue"] += self._conv(d["revenue"], d["currency"], currency, rate)
            row[f"{p}_profit"] += self._conv(d["profit"] or 0, d["currency"], currency, rate)
            row[f"{p}_deals"] += 1
        # закупки LisBot — отток капитала, полезно видеть рядом с выручкой
        for r in _rows(self.lis_db, "SELECT created_at, buy_price_usd FROM items WHERE dry_run=0 AND status<>'failed' "
                       "AND created_at>=?", (since * 1000,)):
            row = series.get(_day(r["created_at"] / 1000))
            if row:
                row["lisbot_spent"] += self._conv(r["buy_price_usd"] or 0, "USD", currency, rate)
        out = []
        for row in series.values():
            row = {k: _r(v) for k, v in row.items()}
            row["revenue"] = _r(row["lisbot_revenue"] + row["mannbot_revenue"])
            row["profit"] = _r(row["lisbot_profit"] + row["mannbot_profit"])
            out.append(row)
        return {"days": days, "currency": currency, "rate": rate, "series": out}

    # ------------------------------------------------------------------ детали по ботам
    def lisbot_details(self, days: float | None = None) -> dict:
        since_ms = (time.time() - days * DAY) * 1000 if days else 0
        items = [dict(r) for r in _rows(self.lis_db, f"""
            SELECT name, COUNT(*) bought, SUM(status='sold') sold,
                   COALESCE(SUM(buy_price_usd),0) spent,
                   COALESCE(SUM(CASE WHEN status='sold' THEN profit_usd END),0) profit,
                   AVG(CASE WHEN status='sold' AND buy_price_usd>0 THEN profit_usd / buy_price_usd END) * 100 roi,
                   AVG(CASE WHEN status='sold' THEN (sold_at - COALESCE(received_at, created_at)) / 86400000.0 END) hold_days,
                   AVG(CASE WHEN status='sold' AND expected_net_usd>0 THEN sold_usd / expected_net_usd END) * 100 accuracy,
                   SUM(status IN ({','.join('?' * len(LIS_HELD))})) open
            FROM items WHERE dry_run=0 AND status<>'failed' AND created_at>=? GROUP BY name
            ORDER BY profit DESC""", (*LIS_HELD, since_ms))]
        for it in items:
            for k in ("spent", "profit", "roi", "hold_days", "accuracy"):
                it[k] = _r(it[k], 1 if k in ("roi", "hold_days", "accuracy") else 2)
        acc = _rows(self.lis_db, "SELECT sold_usd, expected_net_usd FROM items WHERE dry_run=0 AND status='sold' AND "
                    "expected_net_usd>0 AND sold_at>=?", (since_ms,))
        ratios = [r["sold_usd"] / r["expected_net_usd"] for r in acc if r["sold_usd"] is not None]
        mape = statistics.mean(abs(x - 1) for x in ratios) * 100 if ratios else None
        holds = [d["hold_days"] for d in self.lis_deals(since_ms / 1000)]
        by_mode = [dict(r) for r in _rows(self.lis_db, """
            SELECT COALESCE(mode,'—') mode, COUNT(*) n, SUM(status='sold') sold, COALESCE(SUM(buy_price_usd),0) spent,
                   COALESCE(SUM(CASE WHEN status='sold' THEN profit_usd END),0) profit
            FROM items WHERE dry_run=0 AND status<>'failed' AND created_at>=? GROUP BY mode""", (since_ms,))]
        funnel = {r["status"]: r["n"] for r in _rows(self.lis_db, "SELECT status, COUNT(*) n FROM items WHERE dry_run=0 "
                                                     "AND created_at>=? GROUP BY status", (since_ms,))}
        return {
            "items": items, "by_mode": by_mode, "funnel": funnel,
            "forecast": {"n": len(ratios), "fact_to_forecast": _r(statistics.mean(ratios) * 100, 1) if ratios else None,
                         "mean_abs_error": _r(mape, 1)},
            "hold": {"median_days": _r(_median(holds), 1), "avg_days": _r(statistics.mean([h for h in holds if h is not None]), 1)
                     if any(h is not None for h in holds) else None},
        }

    def mannbot_details(self, days: float | None = 30) -> dict:
        since = time.time() - days * DAY if days else 0
        rows = _rows(self.mann_db, "SELECT * FROM orders WHERE created_at>=?", (since,))
        n = len(rows)
        cnt = lambda f: sum(1 for r in rows if f(r))  # noqa: E731
        funnel = [("Оплачено", n), ("Ссылка получена", cnt(lambda r: r["link_received_at"])),
                  ("Трейд отправлен", cnt(lambda r: r["offer_sent_at"])), ("Ключи выданы", cnt(lambda r: r["delivered_at"])),
                  ("Подтверждено", cnt(lambda r: r["status"] == "completed"))]
        delivered = [r for r in rows if r["delivered_at"]]
        completed = [r for r in rows if r["status"] == "completed"]

        def med(a, b, src):
            return _r(_median([r[b] - r[a] for r in src if r[a] and r[b]]), 0)

        statuses: dict[str, int] = {}
        for r in rows:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        return {
            "orders": n, "funnel": [{"stage": s, "n": v, "pct": _r(v / n * 100, 1) if n else None} for s, v in funnel],
            "statuses": statuses,
            "times": {"link": med("paid_at", "link_received_at", rows), "offer": med("link_received_at", "offer_sent_at", rows),
                      "accept": med("offer_sent_at", "delivered_at", delivered), "total": med("paid_at", "delivered_at", delivered),
                      "confirm": med("delivered_at", "completed_at", completed)},
            "confirm_rate": _r(len(completed) / len(delivered) * 100, 1) if delivered else None,
            "delivery_rate": _r(len(delivered) / n * 100, 1) if n else None,
            "refund_rate": _r(statuses.get("refunded", 0) / n * 100, 1) if n else None,
            "avg_check": _r(statistics.mean([r["price"] or 0 for r in rows]), 2) if rows else None,
            "keys_sold": sum(int(r["keys_qty"] or 0) for r in delivered if r["status"] != "refunded"),
        }

    # ------------------------------------------------------------------ экспорт
    EXPORT_COLS = [("bot", "Бот"), ("id", "ID"), ("name", "Предмет"), ("qty", "Кол-во"), ("currency", "Валюта"),
                   ("bought_at", "Куплено"), ("sold_at", "Продано"), ("cost", "Себестоимость"), ("fee", "Комиссия"),
                   ("revenue", "Выручка"), ("profit", "Прибыль"), ("profit_usd", "Прибыль, $"), ("profit_rub", "Прибыль, ₽"),
                   ("hold_days", "Удержание, дн"), ("mode", "Режим"), ("counterparty", "Контрагент"), ("status", "Статус")]

    async def export_rows(self, days: float | None = None) -> list[list]:
        rate = (await self.usd_rub())["rate"]
        out = [[t for _, t in self.EXPORT_COLS]]
        for d in self.deals(time.time() - days * DAY if days else 0):
            d = dict(d)
            d["profit_usd"] = _r(self._conv(d["profit"], d["currency"], "USD", rate))
            d["profit_rub"] = _r(self._conv(d["profit"], d["currency"], "RUB", rate))
            for k in ("bought_at", "sold_at"):
                d[k] = dt.datetime.fromtimestamp(d[k]).strftime("%Y-%m-%d %H:%M") if d[k] else ""
            d["hold_days"] = _r(d["hold_days"], 1)
            out.append([d[k] if not isinstance(d[k], float) else round(d[k], 4) for k, _ in self.EXPORT_COLS])
        return out

    @staticmethod
    def to_csv(rows: list[list]) -> bytes:
        buf = io.StringIO()
        csv.writer(buf, delimiter=";").writerows(rows)        # «;» — так Excel с русской локалью сразу делит столбцы
        return ("﻿" + buf.getvalue()).encode("utf-8")

    @staticmethod
    def to_xlsx(rows: list[list], sheet: str = "Сделки") -> bytes:
        """Минимальный XLSX без сторонних библиотек: один лист, строки как inline-строки, числа как числа."""
        def col(i: int) -> str:
            s = ""
            i += 1
            while i:
                i, m = divmod(i - 1, 26)
                s = chr(65 + m) + s
            return s

        xml_rows = []
        for ri, row in enumerate(rows, 1):
            cells = []
            for ci, v in enumerate(row):
                ref = f"{col(ci)}{ri}"
                if isinstance(v, bool) or v is None or v == "":
                    continue
                if isinstance(v, (int, float)):
                    cells.append(f'<c r="{ref}"><v>{v}</v></c>')
                else:
                    style = ' s="1"' if ri == 1 else ""
                    cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{escape(str(v))}</t></is></c>')
            xml_rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
        sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                     '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews>'
                     f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
        files = {
            "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
            "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
            "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
            "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>',
            "xl/worksheets/sheet1.xml": sheet_xml,
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, body in files.items():
                z.writestr(name, body)
        return buf.getvalue()
