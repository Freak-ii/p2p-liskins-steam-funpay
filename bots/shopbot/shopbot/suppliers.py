"""Поставщики товара — боты экосистемы. Магазин сам ничего не покупает: он передаёт оплаченный заказ боту,
у которого общий кошелёк и очередь (так не бывает двойной траты TON).

  stars, premium → StarsBot   (http://127.0.0.1:8803, /api/supply/*, X-Supply-Token)
  gifts          → TG Gift    (профиль giftshop StarsBot, http://127.0.0.1:8805, тот же контракт)
  steam_keys     → MannBot    (пока kind=manual: оператор отдаёт ключи обменом и отмечает выдачу в панели;
                               когда у MannBot появится /api/supply — kind=http, контракт тот же)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger("suppliers")
FINAL = {"delivered", "failed"}


class SupplierError(Exception):
    pass


class HttpSupplier:
    kind = "http"

    def __init__(self, name: str, url: str, token: str):
        self.name, self.url, self.token = name, url.rstrip("/"), token

    async def _call(self, method: str, path: str, **kw) -> dict:
        if len(self.token or "") < 16:
            raise SupplierError(f"{self.name}: не задан suppliers.*.token (≥16 символов), такой же как supply.token "
                                "в config.yaml бота-поставщика")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                             headers={"X-Supply-Token": self.token}) as s:
                async with s.request(method, self.url + path, **kw) as r:
                    if r.status != 200:
                        raise SupplierError(f"{self.name}: HTTP {r.status} {(await r.text())[:120]}")
                    return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise SupplierError(f"{self.name} недоступен ({self.url}): {e}") from e

    async def ping(self) -> dict:
        return await self._call("GET", "/api/supply/ping")

    @staticmethod
    def _gift(gift: dict | None) -> dict:
        """Какой именно подарок выдать (TG Gift): эмодзи и название из каталога магазина."""
        if not gift:
            return {}
        return {"gift": str(gift.get("emoji") or ""), "gift_title": str(gift.get("title") or "")}

    async def quote(self, product: str, amount: int = 0, months: int = 0, gift: dict | None = None) -> dict:
        return await self._call("GET", "/api/supply/quote", params={"product": product, "amount": amount,
                                                                   "months": months, **self._gift(gift)})

    async def order(self, ref: str, product: str, recipient: str, amount: int = 0, months: int = 0,
                    gift: dict | None = None) -> dict:
        return await self._call("POST", "/api/supply/orders", json={"ref": ref, "product": product,
                                                                   "username": recipient, "amount": amount,
                                                                   "months": months, **self._gift(gift)})

    async def status(self, ref: str) -> dict:
        return await self._call("GET", f"/api/supply/orders/{ref}")


class ManualSupplier:
    """Выдачу делает человек (или MannBot по кнопке): заказ ждёт отметки «выдано» в панели магазина."""

    kind = "manual"

    def __init__(self, name: str, cost_rub: float):
        self.name, self.cost_rub = name, float(cost_rub or 0)
        self.done: dict[str, str] = {}

    async def ping(self) -> dict:
        return {"ok": True, "manual": True}

    async def quote(self, product: str, amount: int = 0, months: int = 0, gift: dict | None = None) -> dict:
        if self.cost_rub <= 0:
            return {"available": False, "reason": "не задана себестоимость (suppliers.*.cost_rub)"}
        return {"available": True, "cost_rub": round(self.cost_rub * max(1, amount), 2), "manual": True}

    async def order(self, ref: str, product: str, recipient: str, amount: int = 0, months: int = 0,
                    gift: dict | None = None) -> dict:
        return {"ok": True, "status": "manual", "order_id": ref, "detail": "ждёт ручной выдачи"}

    async def status(self, ref: str) -> dict:
        return {"status": self.done.get(ref, "manual")}

    def mark(self, ref: str, status: str) -> None:
        self.done[ref] = status


class DemoSupplier:
    """Эмуляция ботов экосистемы для демо: цены как в жизни, выдача за пару «тиков», сбои по заказу."""

    kind = "demo"
    COST = {"stars": 1.256, "premium": {3: 1049.0, 6: 1399.0, 12: 2537.0}, "steam_keys": 165.0}

    def __init__(self, name: str, *, fail_usernames: tuple[str, ...] = ("nobody_here",)):
        self.name = name
        self.orders: dict[str, dict] = {}
        self.fail_usernames = fail_usernames
        self.down = False
        self.cost_mult = 1.0

    async def ping(self) -> dict:
        if self.down:
            raise SupplierError(f"{self.name}: недоступен (демо)")
        return {"ok": True, "demo": True}

    async def quote(self, product: str, amount: int = 0, months: int = 0, gift: dict | None = None) -> dict:
        await self.ping()
        if product in ("stars", "gift"):
            cost = amount * self.COST["stars"] * (1.12 if product == "gift" else 1.0)
        elif product == "premium":
            cost = self.COST["premium"].get(months)
            if cost is None:
                return {"available": False, "reason": "нет такого срока"}
        else:
            cost = self.COST["steam_keys"] * max(1, amount)
        return {"available": True, "cost_rub": round(cost * self.cost_mult, 2)}

    async def order(self, ref: str, product: str, recipient: str, amount: int = 0, months: int = 0,
                    gift: dict | None = None) -> dict:
        await self.ping()
        if ref not in self.orders:
            st = "failed" if recipient.lstrip("@").lower() in self.fail_usernames else "queued"
            self.orders[ref] = {"status": st, "ticks": 0, "detail": "not_found" if st == "failed" else "",
                                "product": product, "gift": (gift or {}).get("emoji", ""), "ts": time.time()}
        return {"ok": True, "order_id": ref, **self.orders[ref]}

    async def status(self, ref: str) -> dict:
        o = self.orders.get(ref)
        if o is None:
            return {"status": "unknown"}
        if o["status"] not in FINAL and o["status"] != "manual":
            o["ticks"] += 1
            o["status"] = "delivered" if o["ticks"] >= 2 else "delivering"
        return dict(o)


def build_suppliers(cfg, *, demo: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("stars", "premium", "gifts", "steam_keys"):
        s = cfg.get(f"suppliers.{name}") or {}
        if demo:
            out[name] = DemoSupplier(name)
        elif s.get("kind") == "manual":
            out[name] = ManualSupplier(name, s.get("cost_rub") or 0)
        else:
            out[name] = HttpSupplier(name, s.get("url") or "", s.get("token") or "")
    return out
