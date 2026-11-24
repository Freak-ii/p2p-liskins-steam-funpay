"""Курс GRAM (TON) → RUB для расчёта себестоимости, прибыли и нижней границы цены (RTBA).

Основной источник — «p2p»: GRAM/USDT на бирже × USDT/RUB, по которому USDT реально покупается за рубли (Rapira
или rates.usdt_rub). CoinGecko и tonapi пересчитывают рубли по форекс-курсу доллара, это на 5–7% дешевле
настоящей закупки: себестоимость по ним занижена, и бот продаёт в минус. Они остались запасными."""
from __future__ import annotations

import logging
import time

import aiohttp

log = logging.getLogger("rates")
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network,toncoin,gram&vs_currencies=rub"
TONAPI = "https://tonapi.io/v2/rates?tokens=ton&currencies=rub"


GRAM_TICKERS = (                              # после переименования Toncoin → Gram пара TONUSDT на Bybit снята
    ("bybit", "https://api.bybit.com/v5/market/tickers?category=spot&symbol=GRAMUSDT"),
    ("mexc", "https://api.mexc.com/api/v3/ticker/price?symbol=GRAMUSDT"),
    ("gate", "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=GRAM_USDT"),
)
RAPIRA = "https://api.rapira.net/open/market/rates"


def _gram_usdt(data) -> float:
    """Цена GRAM в USDT из ответа Bybit / MEXC / Gate (берём цену покупки, если она есть)."""
    row = data
    if isinstance(row, dict) and "result" in row:
        row = (row.get("result") or {}).get("list") or []
    if isinstance(row, list):
        row = row[0] if row else {}
    for key in ("ask1Price", "lowest_ask", "price", "lastPrice", "last"):
        v = float((row or {}).get(key) or 0)
        if v > 0:
            return v
    raise ValueError(f"нет цены GRAM/USDT ({str(data)[:120]})")


def _rapira_usdt(data) -> float:
    for row in (data or {}).get("data") or []:
        if str(row.get("symbol", "")).upper() == "USDT/RUB":
            v = float(row.get("askPrice") or row.get("close") or 0)
            if v > 0:
                return v
    raise ValueError(f"Rapira: нет USDT/RUB ({str(data)[:120]})")


def _from_coingecko(data) -> float:
    # после переименования Toncoin → Gram id монеты может отличаться; CoinGecko при лимите отдаёт {"status": …}
    for key in ("the-open-network", "toncoin", "gram"):
        v = (data.get(key) or {}).get("rub") if isinstance(data, dict) else None
        if v:
            return float(v)
    raise ValueError(f"CoinGecko: нет курса ({str(data)[:120]})")


def _from_tonapi(data) -> float:
    rates = (data or {}).get("rates") or {}
    for key in ("TON", "ton"):
        v = ((rates.get(key) or {}).get("prices") or {}).get("RUB")
        if v:
            return float(v)
    raise ValueError(f"tonapi: нет курса ({str(data)[:120]})")


SOURCES = (("coingecko", COINGECKO, _from_coingecko), ("tonapi", TONAPI, _from_tonapi))


class RateProvider:
    def __init__(self, cfg, db=None):
        self.cfg = cfg                       # секция rates
        self.db = db
        self.value: float | None = float(cfg.ton_rub or 0) or None
        self.updated_at = 0.0
        self.source = "manual" if self.value else ""
        self.gram_usdt: float | None = None
        self.usdt_rub: float | None = None
        if db is not None and not self.value:
            saved = db.kv_get("ton_rub")
            if saved:
                self.value, self.updated_at, self.source = float(saved["v"]), float(saved["ts"]), "cache"
                if saved.get("gram_usdt") and saved.get("usdt_rub"):
                    self.gram_usdt, self.usdt_rub = float(saved["gram_usdt"]), float(saved["usdt_rub"])
                    self.source = "p2p"

    @property
    def is_p2p(self) -> bool:
        """Курс — реальная цена закупки (спред P2P уже внутри) и ему не больше 3 часов."""
        return self.source == "p2p" and bool(self.value) and time.time() - self.updated_at < 3 * 3600

    async def _refresh_p2p(self, session) -> bool:
        errors = []

        async def get(url):
            async with session.get(url, headers={"Accept": "application/json"}) as r:
                return await r.json(content_type=None)
        gram = None
        for name, url in GRAM_TICKERS:
            try:
                gram = _gram_usdt(await get(url))
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
        usdt = float(self.cfg.get("usdt_rub") or 0) if hasattr(self.cfg, "get") else 0.0
        if gram and not usdt:
            try:
                usdt = _rapira_usdt(await get(RAPIRA))
            except Exception as e:  # noqa: BLE001
                errors.append(f"rapira: {e}")
        if not gram or not usdt:
            log.warning("курс закупки GRAM не получен: %s", "; ".join(errors))
            return False
        self.gram_usdt, self.usdt_rub = gram, usdt
        self.value, self.updated_at, self.source = round(gram * usdt, 4), time.time(), "p2p"
        if self.db is not None:
            self.db.kv_set("ton_rub", {"v": self.value, "ts": self.updated_at, "gram_usdt": gram, "usdt_rub": usdt})
        return True

    async def refresh(self, force: bool = False) -> float | None:
        if self.cfg.source == "manual":
            self.value, self.source = float(self.cfg.ton_rub or 0) or None, "manual"
            return self.value
        if not force and time.time() - self.updated_at < float(self.cfg.refresh_minutes) * 60:
            return self.value
        errors = []
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            if await self._refresh_p2p(session):
                return self.value
            for name, url, parse in SOURCES:
                try:
                    async with session.get(url, headers={"Accept": "application/json"}) as r:
                        v = parse(await r.json(content_type=None))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{name}: {e}")
                    continue
                if v > 0:
                    self.value, self.updated_at, self.source = v, time.time(), name
                    if self.db is not None:
                        self.db.kv_set("ton_rub", {"v": v, "ts": self.updated_at})
                    return self.value
        log.warning("курс TON/RUB не обновлён: %s", "; ".join(errors))
        if not self.value and float(self.cfg.ton_rub or 0) > 0:
            self.value, self.source = float(self.cfg.ton_rub), "manual"
        return self.value

    def rub(self, ton: float | None) -> float | None:
        if ton is None or not self.value:
            return None
        return round(float(ton) * self.value, 2)


class StaticRate(RateProvider):
    """Фиксированный курс (демо и тесты)."""

    def __init__(self, value: float):
        self.cfg = None
        self.db = None
        self.value = value
        self.updated_at = time.time()
        self.source = "static"
        self.gram_usdt = None
        self.usdt_rub = None

    async def refresh(self, force: bool = False) -> float | None:
        return self.value
