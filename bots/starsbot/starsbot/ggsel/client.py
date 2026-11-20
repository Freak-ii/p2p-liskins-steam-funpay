"""Клиент GGSEL Seller API (https://seller.ggsel.com/docs).

У GGSEL два контура API на одном ключе продавца (кабинет → Настройки → API):
  • v1 (/api_sellers/api/...): вход POST /api_sellers/api/apilogin с seller_id, timestamp и
    sign = SHA256(api_key + timestamp) → token (живёт до valid_thru), дальше token передаётся в query;
      - GET  seller-last-sales             — последние продажи (invoice_id, товар);
      - GET  purchase/info/{invoice_id}     — заказ: invoice_state, сумма, количество, options (то, что
                                              покупатель ввёл при оформлении, — здесь его @username);
      - GET  debates/v2/chats               — чаты заказов (filter_new=1 — только с новыми сообщениями);
      - GET  debates/v2?id_i=…              — сообщения чата заказа;
      - POST debates/v2?id_i=…  {message}   — написать покупателю в чат заказа;
  • v2 (/api_sellers/v2/...): ключ в заголовке Authorization (без «Bearer»):
      - PATCH offers/{id} {price, quantity, …} — цена и остаток товара.

invoice_state: 1 — создан, 2 — отменён, 3 — оплачен, 4 — завершён (покупатель подтвердил/закрыт), 5 — возврат.

Сайт seller.ggsel.com из контейнера разработки недоступен: формы ответов взяты из опубликованной документации
(снимок OpenAPI 2026-11-06) и разбираются с запасом. Если GGSEL поменяет поля — править только этот файл.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiohttp

log = logging.getLogger("ggsel")
BASE = "https://seller.ggsel.com"

PAID, COMPLETED, REFUNDED, CANCELLED, CREATED = 3, 4, 5, 2, 1


class GgselError(Exception):
    def __init__(self, text: str, code: str = "api"):
        super().__init__(text)
        self.code = code


class GgselAuthError(GgselError):
    """Неверный seller_id / ключ API или у ключа нет прав."""

    def __init__(self, text: str, code: str = "auth"):
        super().__init__(text, code)


@dataclass
class GgselSale:
    invoice_id: int
    product_id: int
    product_name: str = ""
    date: str = ""
    price: float | None = None


@dataclass
class GgselOrder:
    invoice_id: int
    state: int
    product_id: int = 0
    amount: float = 0.0
    currency: str = "RUB"
    quantity: int = 1
    options: list[dict] = field(default_factory=list)     # [{"id", "name", "value"}]
    email: str = ""
    purchase_date: str = ""
    agent_percent: float | None = None

    @property
    def paid(self) -> bool:
        return self.state in (PAID, COMPLETED)


@dataclass
class GgselMessage:
    id: int
    text: str
    from_buyer: bool
    date: str = ""


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _items(data: Any, *keys: str) -> list:
    """Список из ответа: корень-массив или первый найденный ключ (у GGSEL разные методы оборачивают по-разному)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = _items(v, *keys)
                if inner:
                    return inner
    return []


def parse_sale(raw: dict) -> GgselSale | None:
    inv = int(_num(raw.get("invoice_id") or raw.get("id_i") or raw.get("inv"), 0))
    prod = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    pid = int(_num(prod.get("id") or raw.get("product_id") or raw.get("id_goods") or raw.get("item_id"), 0))
    if not inv:
        return None
    price = prod.get("price_rub", prod.get("price", raw.get("amount")))
    return GgselSale(invoice_id=inv, product_id=pid, product_name=str(prod.get("name") or raw.get("name") or ""),
                     date=str(raw.get("date") or ""), price=_num(price) if price is not None else None)


def parse_order(invoice_id: int, raw: dict) -> GgselOrder:
    c = raw.get("content") if isinstance(raw.get("content"), dict) else raw
    opts = []
    for o in c.get("options") or []:
        if not isinstance(o, dict):
            continue
        opts.append({"id": o.get("id"), "name": str(o.get("name") or ""),
                     "value": str(o.get("user_data") if o.get("user_data") is not None else o.get("value") or "")})
    buyer = c.get("buyer_info") if isinstance(c.get("buyer_info"), dict) else {}
    ap = c.get("agent_percent")
    return GgselOrder(
        invoice_id=int(invoice_id), state=int(_num(c.get("invoice_state"), 0)),
        product_id=int(_num(c.get("item_id") or c.get("product_id") or c.get("id_goods") or c.get("content_id"), 0)),
        amount=_num(c.get("amount")), currency=str(c.get("currency_type") or c.get("currency") or "RUB"),
        quantity=max(1, int(_num(c.get("cnt_goods") or c.get("quantity"), 1))), options=opts,
        email=str(buyer.get("email") or ""), purchase_date=str(c.get("purchase_date") or c.get("date_pay") or ""),
        agent_percent=_num(ap) if ap is not None else None)


def parse_message(raw: dict) -> GgselMessage:
    """buyer/seller в ответе — признаки автора; если оба выставлены, автор — тот, у кого значение больше нуля
    и нет пометки seller (на практике у сообщений продавца seller=1, buyer=0)."""
    buyer, seller = int(_num(raw.get("buyer"), 0)), int(_num(raw.get("seller"), 0))
    return GgselMessage(id=int(_num(raw.get("id"), 0)), text=str(raw.get("message") or raw.get("text") or ""),
                        from_buyer=bool(buyer) and not bool(seller), date=str(raw.get("date_written") or ""))


def _valid_thru(s: str) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time() + 3600


class GgselClient:
    def __init__(self, seller_id: int | str, api_key: str, *, base: str = BASE, dry_run: bool = False,
                 min_interval: float = 0.5, locale: str = "ru"):
        self.seller_id = int(_num(seller_id, 0))
        self.api_key = str(api_key or "").strip()
        self.base = (base or BASE).rstrip("/")
        self.dry_run = dry_run
        self.min_interval = min_interval
        self.locale = locale
        self._session: aiohttp.ClientSession | None = None
        self._token = ""
        self._token_until = 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40),
                                                  headers={"Accept": "application/json", "locale": self.locale})
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ транспорт
    async def _raw(self, method: str, path: str, *, params: dict | None = None, body: Any = None,
                   headers: dict | None = None) -> Any:
        if not self.seller_id or not self.api_key:
            raise GgselAuthError("не заданы ID продавца и ключ API GGSEL", "config")
        async with self._lock:                       # GGSEL не любит частые запросы: не чаще min_interval
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()
        sess = await self._sess()
        for attempt in range(3):
            try:
                async with sess.request(method, self.base + path, params=params, json=body, headers=headers) as r:
                    text = await r.text()
                    if r.status in (401, 403):
                        raise GgselAuthError(f"GGSEL {r.status}: {text[:300]}")
                    if r.status == 429 or r.status >= 500:
                        if attempt < 2:
                            await asyncio.sleep(3 * (attempt + 1))
                            continue
                        raise GgselError(f"GGSEL {r.status}: {text[:300]}",
                                         "rate_limit" if r.status == 429 else "network")
                    try:
                        data = await r.json(content_type=None) if text.strip() else {}
                    except ValueError as e:
                        raise GgselError(f"GGSEL вернул не JSON ({r.status}): {text[:200]}", "layout") from e
                    if r.status >= 400:
                        msg = data.get("message") or data.get("desc") if isinstance(data, dict) else None
                        raise GgselError(f"GGSEL {r.status}: {msg or text[:300]}", "api")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 2:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                raise GgselError(f"GGSEL недоступен: {e}", "network") from e
        raise GgselError("GGSEL недоступен", "network")  # pragma: no cover

    async def login(self) -> str:
        ts = str(int(time.time()))
        sign = hashlib.sha256((self.api_key + ts).encode()).hexdigest()
        data = await self._raw("POST", "/api_sellers/api/apilogin",
                               body={"seller_id": self.seller_id, "timestamp": ts, "sign": sign})
        if not isinstance(data, dict) or int(_num(data.get("retval"), -1)) != 0 or not data.get("token"):
            desc = data.get("desc") or data.get("message") if isinstance(data, dict) else data
            raise GgselAuthError(f"GGSEL: вход по API не удался ({desc or 'нет токена'}). Проверьте ID продавца и "
                                 "ключ API")
        self._token = str(data["token"])
        self._token_until = _valid_thru(str(data.get("valid_thru") or "")) - 120
        return self._token

    async def v1(self, method: str, path: str, *, params: dict | None = None, body: Any = None) -> Any:
        """Запрос к контуру v1 с токеном; просроченный токен обновляется один раз."""
        for attempt in range(2):
            if not self._token or time.time() > self._token_until:
                await self.login()
            try:
                data = await self._raw(method, path, params={**(params or {}), "token": self._token}, body=body)
            except GgselAuthError:
                if attempt == 0:
                    self._token = ""
                    continue
                raise
            if isinstance(data, dict) and int(_num(data.get("retval"), 0)) != 0:
                desc = str(data.get("desc") or data.get("retdesc") or data.get("message") or "")
                if attempt == 0 and ("token" in desc.lower() or "токен" in desc.lower()):
                    self._token = ""
                    continue
                raise GgselError(f"GGSEL {path}: {desc or data.get('retval')}", "api")
            return data
        raise GgselAuthError("GGSEL: токен не принят")  # pragma: no cover

    async def v2(self, method: str, path: str, *, params: dict | None = None, body: Any = None) -> Any:
        return await self._raw(method, path, params=params, body=body, headers={"Authorization": self.api_key})

    # ------------------------------------------------------------------ продажи и заказы
    async def last_sales(self, top: int = 50) -> list[GgselSale]:
        data = await self.v1("GET", "/api_sellers/api/seller-last-sales",
                             params={"seller_id": self.seller_id, "top": int(top), "group": "false"})
        out = [parse_sale(r) for r in _items(data, "sales", "items", "rows", "content") if isinstance(r, dict)]
        return [s for s in out if s is not None]

    async def order(self, invoice_id: int) -> GgselOrder:
        data = await self.v1("GET", f"/api_sellers/api/purchase/info/{int(invoice_id)}")
        return parse_order(invoice_id, data if isinstance(data, dict) else {})

    # ------------------------------------------------------------------ чат заказа
    async def chats_with_new(self) -> list[int]:
        """invoice_id чатов, где есть непрочитанные продавцом сообщения."""
        data = await self.v1("GET", "/api_sellers/api/debates/v2/chats", params={"filter_new": 1, "pagesize": 50})
        return [int(_num(c.get("id_i") or c.get("invoice_id"), 0)) for c in _items(data, "items", "chats")
                if isinstance(c, dict) and _num(c.get("id_i") or c.get("invoice_id"), 0)]

    async def messages(self, invoice_id: int, count: int = 50) -> list[GgselMessage]:
        data = await self.v1("GET", "/api_sellers/api/debates/v2", params={"id_i": int(invoice_id), "count": count})
        msgs = [parse_message(m) for m in _items(data, "items", "messages") if isinstance(m, dict)]
        return sorted(msgs, key=lambda m: m.id)

    async def send(self, invoice_id: int, text: str) -> None:
        if not text.strip():
            return
        if self.dry_run:
            log.info("[dry-run] GGSEL %s ← %s", invoice_id, text[:200])
            return
        await self.v1("POST", "/api_sellers/api/debates/v2", params={"id_i": int(invoice_id)},
                      body={"message": text[:4000]})

    # ------------------------------------------------------------------ товары
    async def update_offer(self, offer_id: int, **fields: Any) -> dict:
        """PATCH /api_sellers/v2/offers/{id}: price (₽), quantity, is_unlimited_quantity, …"""
        if self.dry_run:
            log.info("[dry-run] GGSEL товар %s не изменён: %s", offer_id, fields)
            return {"dry_run": True, **fields}
        data = await self.v2("PATCH", f"/api_sellers/v2/offers/{int(offer_id)}", body=fields)
        return data if isinstance(data, dict) else {}

    async def offer(self, offer_id: int) -> dict:
        data = await self.v2("GET", f"/api_sellers/v2/offers/{int(offer_id)}")
        return data.get("data", data) if isinstance(data, dict) else {}
