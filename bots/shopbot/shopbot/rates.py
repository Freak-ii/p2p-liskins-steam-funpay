"""Курсы для оплаты криптой и выплат: «реальный курс» берём с Bybit (публичные данные, без ключей).

  TON/USDT — спот Bybit  GET https://api.bybit.com/v5/market/tickers?category=spot&symbol=GRAMUSDT (Toncoin → Gram)
  USDT/RUB — P2P Bybit   POST https://api2.bybit.com/fiat/otc/item/online (средняя цена первых объявлений)
Если источник недоступен, используется последний удачный курс, а когда он старше rates.max_age_minutes,
RTBA закрывает оплату и выплаты криптой (клиент не получит устаревший курс).
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time

import aiohttp

log = logging.getLogger("rates")
BYBIT_TICKER = "https://api.bybit.com/v5/market/tickers"
RAPIRA = "https://api.rapira.net/open/market/rates"
TON_SYMBOLS = ("GRAMUSDT", "TONUSDT")
BYBIT_P2P = "https://api2.bybit.com/fiat/otc/item/online"


class Rates:
    def __init__(self, cfg, db, *, fetch: bool = True):
        self.cfg = cfg
        self.db = db
        self.fetch = fetch and cfg.get("rates.source") == "bybit"
        saved = db.kv_get("rates") or {}
        self.usdt_rub = float(saved.get("usdt_rub") or cfg.get("rates.usdt_rub"))
        self.ton_usdt = float(saved.get("ton_usdt") or cfg.get("rates.ton_usdt"))
        self.ts = float(saved.get("ts") or 0)
        self.source = saved.get("source") or "config"
        if not self.fetch:                       # фиксированный курс (демо или source=fixed) всегда «свежий»
            self.ts = time.time()

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.ts) / 60 if self.ts else 1e9

    @property
    def fresh(self) -> bool:
        return not self.fetch or self.age_minutes <= float(self.cfg.get("rates.max_age_minutes") or 10)

    def rub_per(self, asset: str) -> float:
        asset = asset.upper()
        if asset == "USDT":
            return self.usdt_rub
        if asset == "TON":
            return self.usdt_rub * self.ton_usdt
        if asset == "RUB":
            return 1.0
        raise ValueError(f"нет курса для {asset}")

    def to_asset(self, rub: float, asset: str) -> float:
        digits = 2 if asset.upper() == "USDT" else 4
        return round(rub / self.rub_per(asset), digits)

    def snapshot(self) -> dict:
        return {"usdt_rub": round(self.usdt_rub, 2), "ton_usdt": round(self.ton_usdt, 4),
                "ton_rub": round(self.rub_per("TON"), 2), "source": self.source,
                "age_minutes": round(self.age_minutes, 1) if self.ts else None, "fresh": self.fresh}

    async def refresh(self) -> bool:
        if not self.fetch:
            self.ts = time.time()
            return True
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                ton = 0.0
                for symbol in TON_SYMBOLS:            # пару TONUSDT Bybit снял после переименования монеты
                    async with s.get(BYBIT_TICKER, params={"category": "spot", "symbol": symbol}) as r:
                        d = await r.json(content_type=None)
                    rows = (d.get("result") or {}).get("list") or []
                    if rows:
                        ton = float(rows[0]["lastPrice"])
                        break
                body = {"tokenId": "USDT", "currencyId": "RUB", "side": "1", "size": "10", "page": "1",
                        "amount": "", "authMaker": False, "canTrade": False}
                prices = []
                try:                                  # P2P с ПК в РФ часто не отвечает — не ждём его дольше 4 секунд
                    async with s.post(BYBIT_P2P, json=body, timeout=aiohttp.ClientTimeout(total=4)) as r:
                        p = await r.json(content_type=None)
                    prices = [float(x["price"]) for x in (p.get("result") or {}).get("items", [])[:5]]
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError) as e:
                    log.info("P2P Bybit: %s", e or type(e).__name__)
                source = "bybit"
                if not prices:                        # P2P Bybit по рублю пуст — берём биржевой USDT/RUB Rapira
                    async with s.get(RAPIRA) as r:
                        q = await r.json(content_type=None)
                    prices = [float(x["askPrice"]) for x in q.get("data") or [] if x.get("symbol") == "USDT/RUB"]
                    source = "rapira"
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, ValueError, TypeError) as e:
            log.warning("курсы недоступны: %s", e or type(e).__name__)
            return False
        if not prices or ton <= 0:
            return False
        self.ton_usdt, self.usdt_rub = ton, statistics.median(prices)
        self.ts, self.source = time.time(), source
        self.db.kv_set("rates", {"usdt_rub": self.usdt_rub, "ton_usdt": self.ton_usdt, "ts": self.ts,
                                 "source": self.source})
        return True
