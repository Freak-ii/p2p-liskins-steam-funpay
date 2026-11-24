"""Агент трендов: следит за ценами подарков, скинов и звёзд и подсказывает, что поднять, что не брать впрок.

Источники (все публичные, без входа и без денег):
  * gifts  — NFT-подарки Telegram: самая дешёвая выставленная цена коллекции на fragment.com/gifts/<slug> (TON);
  * skins  — CS2: прайс Lis-Skins (самый дешёвый лот без блокировки, $) и Market.CSGO (аск и лучшая заявка, $);
  * stars  — FunPay «Звёзды»: цена 3-го места по номиналам из снимков стакана StarsBot (его база, только чтение).

Каждый прогон пишет точки в <home>/agents/trends.sqlite3, считает изменения за 24 ч и 7 дней, выбирает лидеров
роста и падения и формирует рекомендации по правилам. Если в Центре задан ключ Claude API, к отчёту добавляется
короткий вывод ИИ (бэкенд координатора). Агент ничего не покупает и не меняет цены — только советует.

Запуск вручную: python -m center.agents.trends --home <center-data> [--demo]
Агенты не стартуют сами вместе с Центром (правило пользователя), только кнопкой.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("agents.trends")
HOUR = 3600.0
DAY = 24 * HOUR

LIS_FEED = "https://lis-skins.com/market_export_json/csgo.json"
MCSGO_PRICES = "https://market.csgo.com/api/v2/prices/USD.json"
MCSGO_ORDERS = "https://market.csgo.com/api/v2/prices/orders/USD.json"
FRAGMENT_GIFTS = "https://fragment.com/gifts/{slug}"

GIFT_ITEM_RE = re.compile(r'<a href="/gift/([a-z0-9]+-\d+)[^"]*"\s+class="tm-grid-item"(.*?)</a>', re.S)
GIFT_VALUE_RE = re.compile(r'tm-grid-item-value[^>]*>\s*([\d][\d,\.]*)\s*<')

DEFAULTS: dict[str, Any] = {
    "interval_minutes": 180,
    "gifts": {"enabled": True, "max_per_run": 20,
              # популярные коллекции; сезонные добавляются сами в свой сезон (см. SEASONS)
              "collections": ["plushpepe", "durovscap", "lootbag", "swisswatch", "signetring", "preciouspeach",
                              "lovepotion", "toybear", "diamondring", "eternalrose", "lunarsnake", "kissedfrog"]},
    "skins": {"enabled": True, "top": 300, "min_price_usd": 1.0, "max_price_usd": 200.0, "min_volume": 20,
              "sale_fee_pct": 5.0, "withdraw_pct": 2.5},     # Market.CSGO продажа 5% + вывод BTC 1% и запас 1.5%
    "stars": {"enabled": True, "position": 3, "denominations": [50, 100, 250, 500, 1000]},
    "thresholds": {"rise_pct": 10.0, "fall_pct": -10.0, "stars_move_pct": 3.0, "arb_roi_pct": 5.0},
    "keep_days": 60,
}
SEASONS = {  # месяц → коллекции, на которые растёт спрос (Новый год, 14 февраля, Хэллоуин)
    12: ["santahat", "snowglobe", "candycane", "jinglebells", "sleighbell", "winterwreath", "snowmittens"],
    1: ["santahat", "snowglobe", "lunarsnake", "bigyear"],
    2: ["diamondring", "lovepotion", "eternalrose", "cupidcharm", "bondedring", "cookieheart"],
    10: ["spookyskull", "witchhat", "electricskull", "voodoodoll", "madpumpkin"],
}


def merge(base: dict, over: dict | None) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def parse_gift_floor(page: str) -> tuple[float | None, int]:
    """(самая дешёвая цена «For sale» в TON, сколько таких на первой странице)."""
    prices = []
    for _item, body in GIFT_ITEM_RE.findall(page or ""):
        m = GIFT_VALUE_RE.search(body)
        if not m or ("For sale" not in body and "tm-status-avail" not in body):
            continue
        try:
            p = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if p > 0:
            prices.append(p)
    return (min(prices) if prices else None), len(prices)


@dataclass
class Point:
    source: str
    key: str
    price: float
    volume: float = 0.0
    extra: dict = field(default_factory=dict)


class Store:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS points (ts REAL, source TEXT, key TEXT, price REAL, volume REAL, extra TEXT);
    CREATE INDEX IF NOT EXISTS points_key ON points(source, key, ts);
    CREATE TABLE IF NOT EXISTS reports (ts REAL PRIMARY KEY, data TEXT);
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(self.SCHEMA)

    def add(self, ts: float, pts: list[Point]) -> None:
        self.con.executemany("INSERT INTO points VALUES(?,?,?,?,?,?)",
                             [(ts, p.source, p.key, p.price, p.volume, json.dumps(p.extra, ensure_ascii=False))
                              for p in pts])
        self.con.commit()

    def price_at(self, source: str, key: str, ts: float, tolerance: float) -> float | None:
        r = self.con.execute("SELECT price FROM points WHERE source=? AND key=? AND ts BETWEEN ? AND ? "
                             "ORDER BY ABS(ts-?) LIMIT 1", (source, key, ts - tolerance, ts + tolerance, ts)).fetchone()
        return float(r["price"]) if r else None

    def save_report(self, ts: float, rep: dict) -> None:
        self.con.execute("INSERT OR REPLACE INTO reports VALUES(?,?)", (ts, json.dumps(rep, ensure_ascii=False)))
        self.con.commit()

    def last_report(self) -> dict | None:
        r = self.con.execute("SELECT data FROM reports ORDER BY ts DESC LIMIT 1").fetchone()
        return json.loads(r["data"]) if r else None

    def prune(self, keep_days: float, now: float) -> None:
        self.con.execute("DELETE FROM points WHERE ts<?", (now - keep_days * DAY,))
        self.con.execute("DELETE FROM reports WHERE ts<?", (now - keep_days * DAY,))
        self.con.commit()


Fetch = Callable[[str, dict | None], Awaitable[Any]]     # (url, params) → json | text


async def http_fetch(url: str, params: dict | None = None) -> Any:
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90),
                                     headers={"User-Agent": "Mozilla/5.0 (StarsCenter trends)",
                                              "Accept-Language": "en,ru;q=0.8"}) as s:
        async with s.get(url, params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"{url}: HTTP {r.status}")
            if "json" in (r.headers.get("Content-Type") or "") or url.endswith(".json"):
                return await r.json(content_type=None)
            return await r.text()


class TrendsEngine:
    """Сбор и анализ; не зависит от Центра (тесты и ручной запуск)."""

    def __init__(self, home: str | Path, cfg: dict | None = None, *, fetch: Fetch | None = None,
                 store: Store | None = None, backend=None, clock: Callable[[], float] = time.time):
        self.home = Path(home)
        self.cfg = merge(DEFAULTS, cfg)
        self.fetch = fetch or http_fetch
        self.store = store or Store(self.home / "agents" / "trends.sqlite3")
        self.backend = backend               # бэкенд координатора Центра (complete(user, schema)) или None
        self.clock = clock
        self.running = False
        self.last_error = ""
        self.last_run = 0.0

    # ------------------------------------------------------------------ интерфейс агента
    def report(self) -> dict | None:
        return self.store.last_report()

    async def run_once(self) -> dict:
        self.running = True
        now = self.clock()
        errors: dict[str, str] = {}
        pts: list[Point] = []
        try:
            for name, fn in (("gifts", self.collect_gifts), ("skins", self.collect_skins),
                             ("stars", self.collect_stars)):
                if not self.cfg[name].get("enabled", True):
                    continue
                try:
                    pts += await fn(now)
                except Exception as e:  # noqa: BLE001 — один источник не валит остальные
                    errors[name] = str(e)[:200]
                    log.warning("trends %s: %s", name, e)
            self.store.add(now, pts)
            rep = self.analyze(now, pts)
            rep["errors"] = errors
            rep["ai"] = await self.ai_summary(rep)
            self.store.save_report(now, rep)
            self.store.prune(float(self.cfg["keep_days"]), now)
            self.last_error = "; ".join(f"{k}: {v}" for k, v in errors.items())
            self.last_run = now
            return rep
        finally:
            self.running = False

    # ------------------------------------------------------------------ источники
    def gift_collections(self, now: float) -> list[str]:
        month = time.localtime(now).tm_mon
        seen, out = set(), []
        for slug in [*SEASONS.get(month, []), *self.cfg["gifts"]["collections"]]:
            if slug not in seen:
                seen.add(slug)
                out.append(slug)
        return out[: int(self.cfg["gifts"]["max_per_run"])]

    async def collect_gifts(self, now: float) -> list[Point]:
        out = []
        for slug in self.gift_collections(now):
            page = await self.fetch(FRAGMENT_GIFTS.format(slug=slug), {"sort": "price_asc", "filter": "sale"})
            floor, n = parse_gift_floor(page if isinstance(page, str) else "")
            if floor:
                out.append(Point("gifts", slug, floor, n, {"unit": "TON"}))
            await asyncio.sleep(0 if self.fetch is not http_fetch else 1.2)
        return out

    async def collect_skins(self, now: float) -> list[Point]:
        c = self.cfg["skins"]
        lis = await self.fetch(LIS_FEED, None)
        asks = await self.fetch(MCSGO_PRICES, None)
        bids = await self.fetch(MCSGO_ORDERS, None)
        lis_map = {x["name"]: (float(x.get("unlocked_price") or 0), int(x.get("count") or 0)) for x in lis or []
                   if float(x.get("unlocked_price") or 0) > 0}
        ask_map = {x["market_hash_name"]: (float(x["price"]), int(x.get("volume") or 0))
                   for x in (asks or {}).get("items", [])}
        bid_map = {x["market_hash_name"]: float(x["price"]) for x in (bids or {}).get("items", [])}
        rows = []
        for name, (ask, vol) in ask_map.items():
            if vol < c["min_volume"] or not c["min_price_usd"] <= ask <= c["max_price_usd"]:
                continue
            lp = lis_map.get(name)
            rows.append((vol, name, ask, lp, bid_map.get(name)))
        rows.sort(reverse=True)
        out = []
        for vol, name, ask, lp, bid in rows[: int(c["top"])]:
            extra = {"lis": lp[0] if lp else None, "lis_count": lp[1] if lp else 0, "bid": bid, "unit": "USD"}
            out.append(Point("skins", name, ask, vol, extra))
        return out

    async def collect_stars(self, now: float) -> list[Point]:
        db = self.home / "starsbot" / "data" / "starsbot.sqlite3"
        if not db.exists():
            return []
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        pos = int(self.cfg["stars"]["position"])
        out = []
        try:
            for n in self.cfg["stars"]["denominations"]:
                r = con.execute("SELECT ts, data FROM competitor_snapshots WHERE stars=? ORDER BY id DESC LIMIT 1",
                                (n,)).fetchone()
                if not r or now - float(r["ts"]) > 6 * HOUR:
                    continue
                prices = sorted(float(x.get("price") or 0) for x in json.loads(r["data"] or "[]")
                                if isinstance(x, dict) and float(x.get("price") or 0) > 0)
                if len(prices) >= pos:
                    out.append(Point("stars", str(n), prices[pos - 1], len(prices), {"unit": "RUB", "pos": pos}))
        finally:
            con.close()
        return out

    # ------------------------------------------------------------------ анализ
    def change(self, p: Point, now: float, hours: float) -> float | None:
        old = self.store.price_at(p.source, p.key, now - hours * HOUR, tolerance=max(3 * HOUR, hours * HOUR * 0.25))
        if not old or old <= 0:
            return None
        return round((p.price / old - 1) * 100, 2)

    def analyze(self, now: float, pts: list[Point]) -> dict:
        th = self.cfg["thresholds"]
        rows = []
        for p in pts:
            rows.append({"source": p.source, "key": p.key, "price": p.price, "volume": p.volume,
                         "d1": self.change(p, now, 24), "d7": self.change(p, now, 168), **p.extra})
        recs: list[dict] = []
        month = time.localtime(now).tm_mon
        seasonal = set(SEASONS.get(month, []))
        for r in rows:
            d7, d1 = r["d7"], r["d1"]
            if r["source"] == "gifts":
                if d7 is not None and d7 >= th["rise_pct"]:
                    recs.append(rec("gifts", r["key"], f"Подарок {r['key']}: пол на Fragment +{d7:g}% за 7 дней"
                                    + (" (сезон)" if r["key"] in seasonal else ""),
                                    "TG Gift: держать коллекцию в лотах и поднять цену вслед за рынком; закупать "
                                    "впрок только под заказы.", score=d7))
                elif d7 is not None and d7 <= th["fall_pct"]:
                    recs.append(rec("gifts", r["key"], f"Подарок {r['key']}: пол на Fragment {d7:g}% за 7 дней",
                                    "Не закупать впрок, цену лота держать от себестоимости — рынок падает.",
                                    score=-d7))
            elif r["source"] == "skins":
                lis, bid = r.get("lis"), r.get("bid")
                if lis and bid:
                    k = (1 - self.cfg["skins"]["sale_fee_pct"] / 100) * (1 - self.cfg["skins"]["withdraw_pct"] / 100)
                    roi = (bid * k / lis - 1) * 100
                    r["arb_roi_pct"] = round(roi, 2)
                    if roi >= th["arb_roi_pct"] and r.get("lis_count", 0) >= 3:
                        recs.append(rec("skins", r["key"], f"Скин {r['key']}: Lis {lis:g} $ → заявка Market.CSGO "
                                        f"{bid:g} $, после комиссий {roi:+.1f}%",
                                        "LisBot арбитраж: кандидат даже при продаже в заявку (через 7 дней холда — "
                                        "проверить, что заявки держатся).", score=roi))
                if d7 is not None and d7 >= th["rise_pct"] and r["volume"] >= 50:
                    recs.append(rec("skins", r["key"], f"Скин {r['key']}: +{d7:g}% за 7 дней при {r['volume']:g} "
                                    "продаж", "Рост на объёме: LisBot может брать с запасом по ROI, не на пике.",
                                    score=d7 / 2))
            elif r["source"] == "stars":
                if d1 is not None and abs(d1) >= th["stars_move_pct"]:
                    recs.append(rec("stars", r["key"], f"FunPay {r['key']}⭐: цена {r.get('pos', 3)}-го места "
                                    f"{d1:+g}% за сутки ({r['price']:g} ₽)",
                                    "RTBA подстроится сам; если рынок ушёл ниже пола — звёзды продавать не выгодно, "
                                    "проверьте закупку GRAM." if d1 < 0 else
                                    "Рынок подрос: RTBA поднимет цену, маржа на звёздах растёт.", score=abs(d1)))
        recs.sort(key=lambda x: -x["score"])
        movers = {src: {"up": top(rows, src, True), "down": top(rows, src, False)} for src in ("gifts", "skins", "stars")}
        counts = {src: sum(1 for r in rows if r["source"] == src) for src in ("gifts", "skins", "stars")}
        return {"ts": now, "counts": counts, "movers": movers, "recs": recs[:25],
                "season": sorted(seasonal), "rows": [r for r in rows if r["source"] != "skins"][:100]}

    async def ai_summary(self, rep: dict) -> str:
        if self.backend is None or type(self.backend).__name__ == "StubBackend" or not rep["recs"]:
            return ""
        brief = {"recs": rep["recs"][:12], "movers": rep["movers"], "season": rep["season"]}
        prompt = ("Ты аналитик рынка цифровых товаров (звёзды и подарки Telegram, скины CS2) для продавца на FunPay и "
                  "в своём Telegram-магазине. По данным ниже дай 3–5 коротких практичных выводов по-русски: что "
                  "выставить или поднять, что не закупать, какие риски. Без воды.\n\n"
                  + json.dumps(brief, ensure_ascii=False)[:6000])
        schema = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
                  "additionalProperties": False}
        try:
            out, _usage = await self.backend.complete(prompt, schema)
            return str(out.get("summary") or "")[:2000]
        except Exception as e:  # noqa: BLE001
            log.info("trends ai: %s", e)
            return ""


def rec(source: str, key: str, title: str, action: str, score: float) -> dict:
    return {"source": source, "key": key, "title": title, "action": action, "score": round(float(score), 2)}


def top(rows: list[dict], source: str, up: bool, n: int = 5) -> list[dict]:
    xs = [r for r in rows if r["source"] == source and r["d7"] is not None]
    xs.sort(key=lambda r: r["d7"], reverse=up)
    return [{"key": r["key"], "price": r["price"], "d7": r["d7"], "d1": r["d1"]} for r in xs[:n]
            if (r["d7"] > 0) == up]


from center.agents import Agent, register  # noqa: E402 — после движка, чтобы движок жил без Центра


@register
class TrendsAgent(Agent):
    id = "trends"
    title = "Тренды подарков, скинов и звёзд"
    description = ("Раз в несколько часов снимает цены NFT-подарков (Fragment), скинов CS2 (Lis-Skins, Market.CSGO) и "
                   "звёзд на FunPay; находит рост, падение и связки для арбитража и советует, что делать")
    default_interval_minutes = DEFAULTS["interval_minutes"]

    def __init__(self, center, store):
        super().__init__(center, store)
        self._engine: TrendsEngine | None = None

    @property
    def engine(self) -> TrendsEngine:
        if self._engine is None:
            demo = bool(getattr(self.center, "demo", False))
            fetch = None
            if demo:
                from center.agents.trends_demo import demo_fetch
                fetch = demo_fetch()
            self._engine = TrendsEngine(self.center.home, self.cfg, fetch=fetch,
                                        store=Store(":memory:") if demo else None)
        self._engine.cfg = merge(DEFAULTS, self.cfg)
        coord = getattr(self.center, "coordinator", None)
        if coord is not None and self.cfg.get("ai", True):
            try:
                self._engine.backend = coord.backend()
            except Exception:  # noqa: BLE001
                self._engine.backend = None
        return self._engine

    async def run_once(self) -> str:
        rep = await self.engine.run_once()
        day = time.strftime("%Y-%m-%d", time.localtime(rep["ts"]))
        new = 0
        for r in rep["recs"]:
            iid = self.add_item(f"trend_{r['source']}", r["title"], r["action"], status="info",
                                key=f"trend:{r['source']}:{r['key']}:{day}", meta={"score": r["score"]})
            new += iid is not None
        if rep.get("ai"):
            self.add_item("trend_summary", "Вывод ИИ по рынку", rep["ai"], status="info", key=f"trend:ai:{day}")
        c = rep["counts"]
        if new:
            self.log("info", f"{self.title}: новых находок {new}")
        err = f"; ошибки: {', '.join(rep['errors'])}" if rep.get("errors") else ""
        return (f"подарков {c.get('gifts', 0)}, скинов {c.get('skins', 0)}, номиналов звёзд {c.get('stars', 0)}; "
                f"находок {len(rep['recs'])} (новых {new}){err}")

    def status_extra(self) -> dict:
        rep = self.engine.report() or {}
        return {"trends": {"counts": rep.get("counts", {}), "recs": len(rep.get("recs", [])),
                           "season": rep.get("season", []), "errors": rep.get("errors", {})}}

    def routes(self) -> list:
        from aiohttp import web

        async def report(request):
            return web.json_response(self.engine.report() or {})
        return [web.get("/api/agents/trends/report", report)]


def main() -> None:  # pragma: no cover — ручной запуск
    ap = argparse.ArgumentParser(description="Агент трендов: один прогон и отчёт")
    ap.add_argument("--home", default="center-data")
    ap.add_argument("--demo", action="store_true", help="без сети, на синтетических данных")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if a.demo:
        from center.agents.trends_demo import demo_fetch
        agent = TrendsEngine(a.home, fetch=demo_fetch(), store=Store(":memory:"))
    else:
        agent = TrendsEngine(a.home)
    rep = asyncio.run(agent.run_once())
    print(json.dumps({k: rep[k] for k in ("counts", "recs", "errors")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":  # pragma: no cover
    main()
