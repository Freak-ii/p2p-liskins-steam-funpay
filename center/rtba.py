"""RTBA центра: мгновенные покупки LisBot (WebSocket LIS) и автоцена лота MannBot (FunPay) на одной странице,
общий бюджет и лимиты центра и бэктест правил RTBA на накопленной истории цен.

Правила работы:
- данные берутся из JSON-API ботов (LisBot /api/rtba, MannBot /api/rtba), а если бот не запущен — из их баз
  только на чтение;
- лимиты, которые центр хочет видеть у ботов, хранятся в center.yaml (раздел budget) как план. Боту они уходят
  только по кнопке «Применить» (LisBot — /api/settings, MannBot — /api/rtba/config; остановленному боту — в его
  файл настроек). Режим DRY-RUN/боевой центр не меняет никогда;
- бэктест прогоняет те же функции правил, что и боты: planBuys LisBot (node src/backtest.js) и RTBAEngine MannBot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import yaml

from center.analytics import DAY, _kv, _rows
from center.config import BOTS_DIR

log = logging.getLogger("center.rtba")

LIS_EXAMPLE = BOTS_DIR / "lisbot" / "config.example.json"

# Лимиты и правила, которыми управляет центр: ключ плана -> (бот, настройка бота, подпись, единица, мин, макс)
LIMITS: dict[str, tuple] = {
    "lisbot.max_exposure_usd": ("lisbot", "strategy.maxOpenExposureUsd", "LisBot: сколько капитала держать в товаре", "$", 0, 1e8),
    "lisbot.max_spend_day_usd": ("lisbot", "strategy.maxSpendPerDayUsd", "LisBot: лимит трат в день", "$", 0, 1e7),
    "lisbot.max_spend_hour_usd": ("lisbot", "strategy.maxSpendPerHourUsd", "LisBot: лимит трат в час", "$", 0, 1e6),
    "lisbot.min_roi": ("lisbot", "strategy.minRoi", "LisBot: минимальный ROI покупки", "доля", 0.02, 1),
    "lisbot.balance_reserve": ("lisbot", "strategy.balanceReservePct", "LisBot: резерв баланса", "доля", 0, 0.95),
    "mannbot.min_margin_pct": ("mannbot", "min_margin_pct", "MannBot: минимальная маржа над себестоимостью", "%", 0, 100),
    "mannbot.cost_per_key": ("mannbot", "cost_per_key", "MannBot: себестоимость ключа", "₽", 0, 1e6),
    "mannbot.funpay_fee_pct": ("mannbot", "funpay_fee_pct", "MannBot: комиссия FunPay", "%", 0, 100),
    "mannbot.undercut_abs": ("mannbot", "undercut_abs", "MannBot: дешевле конкурента на", "₽", 0, 1e4),
    "mannbot.ceiling_price": ("mannbot", "ceiling_price", "MannBot: потолок цены лота (0 — нет)", "₽", 0, 1e6),
}
MANN_STRATEGIES = ("top1", "topN", "median")


def _get(obj: dict, path: str):
    for k in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _set(obj: dict, path: str, value) -> None:
    ks = path.split(".")
    for k in ks[:-1]:
        obj = obj.setdefault(k, {})
    obj[ks[-1]] = value


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _pctl(xs: list, q: float):
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def mannbot_modules():
    """Модули MannBot (правила RTBA и конфиг) — импортируются из bots/mannbot, логика не дублируется."""
    src = str(BOTS_DIR / "mannbot")
    if src not in sys.path:
        sys.path.insert(0, src)
    from mannbot import config as mcfg  # noqa: PLC0415
    from mannbot.core import rtba as mrtba  # noqa: PLC0415
    from mannbot.core.models import CompetitorLot  # noqa: PLC0415
    return mcfg, mrtba, CompetitorLot


class Rtba:
    def __init__(self, center):
        self.center = center
        self.an = center.analytics

    @property
    def lis(self):
        return self.center.workers["lisbot"]

    @property
    def mann(self):
        return self.center.workers["mannbot"]

    def _running(self, w) -> bool:
        return w.state == "running"

    async def _api(self, w, method: str, path: str, body: dict | None = None) -> dict | None:
        if not self._running(w):
            return None
        try:
            code, d = await w.api(method, path, body)
        except Exception as e:  # noqa: BLE001 — бот перезапускается или занят
            log.debug("%s %s: %s", w.name, path, e)
            return None
        return d if code == 200 else None

    # ------------------------------------------------------------------ настройки ботов
    def lis_settings_file(self) -> dict:
        """Настройки LisBot без запуска: config.example.json + config.json центра."""
        try:
            base = json.loads(LIS_EXAMPLE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            base = {}
        return _merge(base, self.an.lis_config())

    def mann_settings_file(self) -> dict:
        try:
            mcfg, _, _ = mannbot_modules()
            defaults = mcfg.DEFAULTS
        except Exception:  # noqa: BLE001 — нет пакетов MannBot
            defaults = {}
        return _merge(defaults, self.an.mann_config())

    async def current_settings(self) -> dict:
        lis = self.lis_settings_file()
        live = await self._api(self.lis, "GET", "/api/settings")
        if live:
            for f in live.get("fields") or []:
                _set(lis, f["key"], f.get("value"))
        mann = self.mann_settings_file().get("rtba") or {}
        live_m = await self._api(self.mann, "GET", "/api/rtba?limit=1")
        if live_m:
            mann = {**mann, **(live_m.get("config") or {})}
        return {"lisbot": lis, "mannbot": mann}

    # ------------------------------------------------------------------ страница RTBA
    def _lis_from_db(self, days: float) -> dict:
        since = (time.time() - days * DAY) * 1000
        rows = _rows(self.an.lis_db, "SELECT e.ts, e.data, i.name, i.buy_price_usd price, i.expected_profit_usd profit, "
                     "i.dry_run FROM events e JOIN items i ON i.id=e.item_id WHERE e.type IN ('buy.ok','buy.dry_run') "
                     "AND i.mode='rtba' AND e.ts>=? ORDER BY e.id DESC LIMIT 500", (since,))
        buys = []
        for r in rows:
            try:
                lat = (json.loads(r["data"] or "{}") or {}).get("latencyMs")
            except ValueError:
                lat = None
            buys.append({"ts": r["ts"], "name": r["name"], "price": r["price"], "profit": r["profit"],
                         "dry": bool(r["dry_run"]), "latencyMs": lat})
        lat = [b["latencyMs"] for b in buys if isinstance(b["latencyMs"], (int, float))]
        by_mode = [dict(r) for r in _rows(self.an.lis_db, "SELECT mode, dry_run, COUNT(*) n, COALESCE(SUM(buy_price_usd),0) "
                                          "spent FROM items WHERE created_at>=? GROUP BY mode, dry_run", (since,))]
        st = self.lis_settings_file()
        return {"enabled": bool(_get(st, "strategy.rtbaEnabled")), "dryRun": bool(st.get("dryRun", True)), "days": days,
                "ws": None, "buyer": None, "recent": [], "buys": buys[:100], "byMode": by_mode,
                "latency": {"n": len(lat), "p50": _pctl(lat, .5), "p90": _pctl(lat, .9), "max": max(lat) if lat else None}}

    def _mann_from_db(self) -> dict:
        db = self.an.mann_db
        hist = [dict(r) for r in _rows(db, "SELECT * FROM price_history ORDER BY id DESC LIMIT 100")]
        snap = _rows(db, "SELECT * FROM competitor_snapshots ORDER BY id DESC LIMIT 1")
        day = _rows(db, "SELECT COUNT(*) checks, COALESCE(SUM(applied),0) applied FROM price_history WHERE ts>=?",
                    (time.time() - DAY,))
        rt = self.mann_settings_file().get("rtba") or {}
        try:
            mcfg, mrtba, _ = mannbot_modules()
            floor = mrtba.price_floor(mcfg.load_config(None, {"rtba": rt}).rtba)
        except Exception:  # noqa: BLE001
            floor = None
        dry = _get(self.an.mann_config(), "general.dry_run")
        return {"config": rt, "markup": None, "floor": floor, "updates_last_hour": None, "state": self.mann.state,
                "restart_required": False, "dry_run": True if dry is None else bool(dry),
                "stock": _kv(db, "last_stock"), "history": hist, "day": dict(day[0]) if day else {},
                "competitors": json.loads(snap[0]["data"]) if snap else [], "competitors_ts": snap[0]["ts"] if snap else None}

    async def status(self, days: float = 7) -> dict:
        lis, mann = await asyncio.gather(self._api(self.lis, "GET", f"/api/rtba?days={days:g}"),
                                         self._api(self.mann, "GET", "/api/rtba"))
        out_l = {"live": bool(lis), **(lis or self._lis_from_db(days))}
        out_m = {"live": bool(mann), **(mann or self._mann_from_db())}
        return {"lisbot": out_l, "mannbot": out_m, "generated_at": time.time()}

    # ------------------------------------------------------------------ бюджет и лимиты
    @property
    def budget_cfg(self) -> dict:
        return self.center.cfg.data.get("budget") or {}

    async def plan(self) -> dict:
        """План лимитов центра против текущих настроек ботов. Ничего не меняет."""
        cur = await self.current_settings()
        rate = (await self.an.usd_rub())["rate"]
        cap = self.an._capital()
        mann_cap_usd = (cap["mannbot"]["total"] or 0) / rate
        lis_cap = cap["lisbot"]["total"] or 0
        total = float(self.budget_cfg.get("total_usd") or 0)
        planned = dict(self.budget_cfg.get("plan") or {})
        rows = []
        for key, (bot, path, label, unit, _lo, _hi) in LIMITS.items():
            now = _get(cur["lisbot"], path) if bot == "lisbot" else cur["mannbot"].get(path)
            want, source = planned.get(key), "план центра" if key in planned else ""
            if key == "lisbot.max_exposure_usd" and total > 0:
                share = max(0.0, total - mann_cap_usd)          # всё, что не лежит в ключах MannBot
                if want is None or share < float(want):
                    want, source = round(share, 2), f"общий бюджет {total:g} $ − склад MannBot {mann_cap_usd:.2f} $"
            rows.append({"key": key, "bot": bot, "setting": path, "label": label, "unit": unit, "current": now,
                         "planned": want, "source": source,
                         "differs": want is not None and (now is None or abs(float(want) - float(now)) > 1e-9)})
        strategy = planned.get("mannbot.strategy")
        rows.append({"key": "mannbot.strategy", "bot": "mannbot", "setting": "strategy", "label": "MannBot: стратегия цены",
                     "unit": "", "current": cur["mannbot"].get("strategy"), "planned": strategy,
                     "source": "план центра" if strategy else "", "differs": bool(strategy) and strategy != cur["mannbot"].get("strategy")})
        return {"total_usd": total, "rate": rate, "rows": rows,
                "capital": {"lisbot_usd": round(lis_cap, 2), "mannbot_usd": round(mann_cap_usd, 2),
                            "total_usd": round(lis_cap + mann_cap_usd, 2)},
                "running": {n: self._running(w) for n, w in self.center.workers.items()}, "last_apply": self.last_apply()}

    def save_plan(self, data: dict) -> list[str]:
        """Сохраняет план в center.yaml (без применения). None / '' — убрать пункт из плана."""
        errors, plan = [], dict(self.budget_cfg.get("plan") or {})
        upd: dict = {}
        if "total_usd" in data:
            try:
                upd["total_usd"] = max(0.0, float(data["total_usd"] or 0))
            except (TypeError, ValueError):
                errors.append("общий бюджет: не число")
        for key, v in (data.get("plan") or {}).items():
            if v in (None, ""):
                plan.pop(key, None)
                continue
            if key == "mannbot.strategy":
                if v not in MANN_STRATEGIES:
                    errors.append("стратегия: top1, topN или median")
                else:
                    plan[key] = v
                continue
            if key not in LIMITS:
                errors.append(f"{key}: центр этим не управляет")
                continue
            _, _, label, _, lo, hi = LIMITS[key]
            try:
                num = float(v)
            except (TypeError, ValueError):
                errors.append(f"{label}: не число")
                continue
            if not lo <= num <= hi:
                errors.append(f"{label}: допустимо {lo:g}…{hi:g}")
                continue
            plan[key] = num
        if errors:
            return errors
        upd["plan"] = plan
        # план целиком заменяет прежний (слияние в CenterConfig.save вернуло бы удалённые пункты)
        cfg = self.center.cfg
        cfg.user["budget"] = {**(cfg.user.get("budget") or {}), **upd}
        cfg.save({})
        self.center.events.add("center", "info", "budget.plan", "План лимитов центра изменён (ещё не применён к ботам)")
        return []

    async def apply(self, keys: list[str] | None = None) -> dict:
        """Отправляет ботам отличающиеся пункты плана. Только по явной команде из панели."""
        p = await self.plan()
        todo = [r for r in p["rows"] if r["differs"] and (not keys or r["key"] in keys)]
        if not todo:
            return {"ok": True, "message": "Настройки ботов уже совпадают с планом", "results": []}
        results = []
        lis = {r["setting"]: r["planned"] for r in todo if r["bot"] == "lisbot"}
        mann = {r["setting"]: r["planned"] for r in todo if r["bot"] == "mannbot"}
        if lis:
            results.append(await self._apply_lis(lis))
        if mann:
            results.append(await self._apply_mann(mann))
        ok = all(r["ok"] for r in results)
        for r in results:
            self.center.events.add(r["bot"], "info" if r["ok"] else "warn", "budget.apply", r["message"])
        done = {r["bot"] for r in results if r["ok"]}
        undo = [{k: r[k] for k in ("key", "bot", "setting", "label", "unit")} | {"previous": r["current"], "applied": r["planned"]}
                for r in todo if r["bot"] in done and r["current"] is not None]
        if undo:                                  # что было до применения — для кнопки «Отменить»
            self.center.monitor.kv_set("rtba_last_apply", json.dumps({"ts": time.time(), "rows": undo}, ensure_ascii=False))
        return {"ok": ok, "message": "; ".join(r["message"] for r in results), "results": results}

    def last_apply(self) -> dict | None:
        try:
            return json.loads(self.center.monitor.kv_get("rtba_last_apply") or "null")
        except ValueError:
            return None

    async def undo(self) -> dict:
        """Вернуть ботам значения, которые были до последнего «Применить». План в center.yaml не меняется."""
        last = self.last_apply()
        if not last or not last.get("rows"):
            return {"ok": False, "message": "Отменять нечего: последнее применение уже отменено или его не было", "results": []}
        results = []
        lis = {r["setting"]: r["previous"] for r in last["rows"] if r["bot"] == "lisbot"}
        mann = {r["setting"]: r["previous"] for r in last["rows"] if r["bot"] == "mannbot"}
        if lis:
            results.append(await self._apply_lis(lis))
        if mann:
            results.append(await self._apply_mann(mann))
        ok = all(r["ok"] for r in results)
        for r in results:
            self.center.events.add(r["bot"], "info" if r["ok"] else "warn", "budget.undo", "Отмена применения лимитов. " + r["message"])
        if ok:
            self.center.monitor.kv_set("rtba_last_apply", "null")
        return {"ok": ok, "message": "; ".join(r["message"] for r in results), "results": results}

    async def _apply_lis(self, changes: dict) -> dict:
        names = ", ".join(f"{k}={v}" for k, v in changes.items())
        if self._running(self.lis):
            try:
                code, d = await self.lis.api("POST", "/api/settings", {"changes": changes})
            except Exception as e:  # noqa: BLE001
                return {"bot": "lisbot", "ok": False, "message": f"LisBot не ответил: {e}"}
            if code != 200:
                return {"bot": "lisbot", "ok": False, "message": f"LisBot отклонил: {d.get('error') or code}"}
            return {"bot": "lisbot", "ok": True, "message": f"LisBot: применено сразу ({names})"}
        user = self.an.lis_config()
        for k, v in changes.items():
            _set(user, k, v)
        path = self.lis.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(user, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return {"bot": "lisbot", "ok": True, "message": f"LisBot не запущен: записано в config.json, действует со следующего запуска ({names})"}

    async def _apply_mann(self, changes: dict) -> dict:
        names = ", ".join(f"{k}={v}" for k, v in changes.items())
        if self._running(self.mann):
            try:
                code, d = await self.mann.api("POST", "/api/rtba/config", {"changes": changes})
            except Exception as e:  # noqa: BLE001
                return {"bot": "mannbot", "ok": False, "message": f"MannBot не ответил: {e}"}
            if code != 200 or not d.get("ok"):
                return {"bot": "mannbot", "ok": False, "message": f"MannBot отклонил: {d.get('error') or code}"}
            how = "применено сразу" if d.get("live") else "сохранено, действует со следующего запуска торговли"
            return {"bot": "mannbot", "ok": True, "message": f"MannBot: {how} ({names})"}
        path = self.mann._cfg_path
        raw = self.an.mann_config()
        raw.setdefault("rtba", {}).update(changes)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("# Настройки MannBot (редактируются в приложении)\n" +
                       yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(path)
        return {"bot": "mannbot", "ok": True, "message": f"MannBot не запущен: записано в config.yaml ({names})"}

    # ------------------------------------------------------------------ бэктест
    async def backtest_lisbot(self, variants: list[dict], days: float = 7) -> dict:
        """Правила закупки LisBot на истории пола LIS (lis_floor; бот хранит 7 дней) через node src/backtest.js."""
        db = self.an.lis_db
        since = (time.time() - days * DAY) * 1000
        floors = [[r["name"], r["ts"], r["floor_usd"]] for r in
                  _rows(db, "SELECT name, ts, floor_usd FROM lis_floor WHERE ts>=? ORDER BY ts", (since,))]
        if not floors:
            return {"error": "в базе LisBot ещё нет истории цен LIS (lis_floor): она копится, пока бот сканирует вотчлист"}
        node = self.lis.node() or shutil.which("node")
        if not node:
            return {"error": "для бэктеста LisBot нужен Node.js (тот же, что запускает LisBot)"}
        payload = {
            "config": self.an.lis_config(), "floors": floors,
            "valuations": {r["name"]: dict(r) for r in _rows(db, "SELECT * FROM valuations")},
            "watch": {r["name"]: dict(r) for r in _rows(db, "SELECT * FROM watchlist")},
            "balance": (_kv(db, "balance.last") or {}).get("balance") or 0, "variants": variants,
        }
        proc = await asyncio.create_subprocess_exec(
            node, str(BOTS_DIR / "lisbot" / "src" / "backtest.js"), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(BOTS_DIR / "lisbot"))
        try:
            out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), 120)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": "бэктест LisBot не уложился в 2 минуты"}
        try:
            res = json.loads(out.decode() or "{}")
        except ValueError:
            return {"error": f"бэктест LisBot завершился с ошибкой: {err.decode(errors='replace')[-400:]}"}
        res.update({"samples": len(floors), "items": len({f[0] for f in floors}), "from": floors[0][1] / 1000,
                    "to": floors[-1][1] / 1000})
        return res

    def backtest_mannbot(self, variants: list[dict], days: float = 30) -> dict:
        """Правила автоцены MannBot (RTBAEngine) на снимках рынка FunPay (competitor_snapshots)."""
        try:
            mcfg, mrtba, CompetitorLot = mannbot_modules()
        except Exception as e:  # noqa: BLE001
            return {"error": f"не удалось загрузить модули MannBot: {e}"}
        db = self.an.mann_db
        since = time.time() - days * DAY
        snaps = _rows(db, "SELECT ts, data FROM competitor_snapshots WHERE ts>=? ORDER BY ts", (since,))
        if not snaps:
            return {"error": "в базе MannBot ещё нет снимков рынка FunPay: они копятся, пока включена автоцена (RTBA)"}
        markets = []
        for s in snaps:
            try:
                lots = [CompetitorLot(**{k: v for k, v in d.items() if k in CompetitorLot.__dataclass_fields__})
                        for d in json.loads(s["data"] or "[]")]
            except (ValueError, TypeError):
                continue
            markets.append((float(s["ts"]), lots))
        orders = [dict(r) for r in _rows(db, "SELECT delivered_at, price, keys_qty, quantity FROM orders WHERE "
                                         "delivered_at>=? AND status<>'refunded' ORDER BY delivered_at", (since,))]
        hist = [dict(r) for r in _rows(db, "SELECT ts, new_price, old_price, position, applied FROM price_history "
                                       "WHERE ts>=? ORDER BY ts", (since,))]
        base_raw = self.an.mann_effective_config()
        own = {str(l.get("offer_id")) for l in (_get(base_raw, "funpay.lots") or []) if l.get("offer_id")}
        stock = int(_kv(db, "last_stock") or 0)
        start = next((h["old_price"] for h in hist if h["old_price"]), None)
        out = []
        for v in variants or [{"label": "текущие правила", "changes": {}}]:
            cfg = mcfg.load_config(None, _merge(base_raw, {"rtba": v.get("changes") or {}}))
            out.append({"label": v.get("label") or "вариант", "changes": v.get("changes") or {},
                        **self._sim_mann(mrtba, cfg.rtba, markets, orders, own, stock, start)})
        actual_rev = sum(float(o["price"] or 0) for o in orders)
        positions = [h["position"] for h in hist if h["position"]]
        return {"variants": out, "snapshots": len(markets), "orders": len(orders), "from": markets[0][0] if markets else None,
                "to": markets[-1][0] if markets else None,
                "actual": {"revenue": round(actual_rev, 2), "updates": sum(1 for h in hist if h["applied"]),
                           "avg_position": round(sum(positions) / len(positions), 2) if positions else None}}

    @staticmethod
    def _sim_mann(mrtba, rcfg, markets, orders, own: set, stock: int, start: float | None) -> dict:
        eng = mrtba.RTBAEngine(rcfg)
        fee = float(rcfg.funpay_fee_pct or 0) / 100
        cost = float(rcfg.cost_per_key or 0)
        current = start
        timeline, updates, floor_hits, first = [], 0, 0, 0
        for ts, lots in markets:
            d = eng.decide(lots, own_offer_ids=own, own_username="", current_lot_price=current, stock=stock, now=ts)
            if d.apply and d.target_lot_price is not None:
                current = d.target_lot_price
                eng.mark_applied(ts)
                updates += 1
            buyer = (current or d.target_lot_price or 0) * eng.markup
            comps = sorted(l.price for l in mrtba.filter_competitors(lots, rcfg, own, ""))
            pos = 1 + sum(1 for p in comps if p < buyer)
            first += pos == 1
            floor_hits += any("пола" in n for n in d.notes)
            timeline.append((ts, round(buyer, 2), pos))
        # заказы: цена варианта в момент продажи × ключи (оценка при том же спросе)
        est, risky, i, keys_total = 0.0, 0, 0, 0
        for o in orders:
            while i + 1 < len(timeline) and timeline[i + 1][0] <= o["delivered_at"]:
                i += 1
            keys = int(o["keys_qty"] or o["quantity"] or 0)
            keys_total += keys
            _, price, pos = timeline[i]
            est += price * keys
            risky += pos > 1
        prices = [t[1] for t in timeline]
        n = len(timeline) or 1
        avg = sum(prices) / n if prices else None
        return {"updates": updates, "avg_price": round(avg, 2) if avg else None, "min_price": min(prices) if prices else None,
                "max_price": max(prices) if prices else None, "avg_position": round(sum(t[2] for t in timeline) / n, 2),
                "share_first": round(first / n * 100, 1), "floor_hits": floor_hits,
                "est_revenue": round(est, 2), "est_profit": round(est * (1 - fee) - cost * keys_total, 2) if cost else None,
                "margin_per_key": round(avg * (1 - fee) - cost, 2) if cost and avg else None,
                "orders_not_first": risky, "floor": mrtba.price_floor(rcfg),
                "series": [[t[0], t[1], t[2]] for t in timeline[:: max(1, len(timeline) // 200)]]}
