"""Курс TON → RUB для расчёта себестоимости, прибыли и нижней границы цены (RTBA)."""
from __future__ import annotations

import logging
import time

import aiohttp

log = logging.getLogger("rates")
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=rub"


class RateProvider:
    def __init__(self, cfg, db=None):
        self.cfg = cfg                       # секция rates
        self.db = db
        self.value: float | None = float(cfg.ton_rub or 0) or None
        self.updated_at = 0.0
        self.source = "manual" if self.value else ""
        if db is not None and not self.value:
            saved = db.kv_get("ton_rub")
            if saved:
                self.value, self.updated_at, self.source = float(saved["v"]), float(saved["ts"]), "cache"

    async def refresh(self, force: bool = False) -> float | None:
        if self.cfg.source == "manual":
            self.value, self.source = float(self.cfg.ton_rub or 0) or None, "manual"
            return self.value
        if not force and time.time() - self.updated_at < float(self.cfg.refresh_minutes) * 60:
            return self.value
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(COINGECKO) as r:
                    data = await r.json(content_type=None)
            v = float(data["the-open-network"]["rub"])
            if v > 0:
                self.value, self.updated_at, self.source = v, time.time(), "coingecko"
                if self.db is not None:
                    self.db.kv_set("ton_rub", {"v": v, "ts": self.updated_at})
        except Exception as e:  # noqa: BLE001
            log.warning("курс TON/RUB не обновлён: %s", e)
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

    async def refresh(self, force: bool = False) -> float | None:
        return self.value
