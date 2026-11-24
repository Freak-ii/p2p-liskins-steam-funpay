"""Демо-данные агента трендов: без сети, цены медленно «гуляют» от прогона к прогону."""
from __future__ import annotations

import math
import time

from center.agents.trends import FRAGMENT_GIFTS, LIS_FEED, MCSGO_ORDERS, MCSGO_PRICES

GIFTS = {"plushpepe": 4200.0, "durovscap": 650.0, "lootbag": 48.0, "swisswatch": 33.0, "santahat": 6.5,
         "snowglobe": 5.2, "diamondring": 14.0, "lovepotion": 9.0}
SKINS = [("AK-47 | Redline (Field-Tested)", 14.2, 900), ("AWP | Asiimov (Field-Tested)", 98.0, 300),
         ("Glock-18 | Water Elemental (Minimal Wear)", 3.1, 450), ("USP-S | Cortex (Field-Tested)", 2.4, 700)]


def demo_fetch(clock=time.time):
    calls = {"n": 0}

    async def fetch(url: str, params=None):
        calls["n"] += 1
        t = clock() / 86400
        if url.startswith("https://fragment.com/gifts/"):
            slug = url.rsplit("/", 1)[-1]
            base = GIFTS.get(slug, 10.0)
            p = round(base * (1 + 0.12 * math.sin(t + len(slug))), 2)
            return (f'<a href="/gift/{slug}-1" class="tm-grid-item"><div class="tm-grid-item-value">{p}</div>'
                    f'<div>For sale</div></a>')
        if url == LIS_FEED:
            return [{"name": n, "unlocked_price": round(p * 0.9, 2), "count": 12} for n, p, _ in SKINS]
        if url == MCSGO_PRICES:
            return {"items": [{"market_hash_name": n, "price": round(p * (1 + 0.05 * math.sin(t)), 2), "volume": v}
                              for n, p, v in SKINS]}
        if url == MCSGO_ORDERS:
            return {"items": [{"market_hash_name": n, "price": round(p * 0.97, 2), "volume": 30} for n, p, _ in SKINS]}
        raise RuntimeError(f"демо: нет данных для {url}")

    return fetch


__all__ = ["demo_fetch", "FRAGMENT_GIFTS"]
