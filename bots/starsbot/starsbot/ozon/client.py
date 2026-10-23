"""Клиент Ozon Seller API для цифровых товаров (коды активации).

Как Ozon продаёт цифровые товары (отдельный кабинет «Продавец цифровых товаров», работа только через API):
  1. продавец сообщает остаток кодов: POST /v1/product/digital/stocks/import (offer_id → stock);
  2. покупатель оплачивает — появляется отправление в статусе awaiting_packaging:
     POST /v2/posting/digital/list (у товара required_qty_for_digital_code — сколько кодов нужно передать,
     у отправления waiting_deadline_for_digital_code — до какого времени);
  3. продавец передаёт коды: POST /v1/posting/digital/codes/upload (exemplars_by_sku: sku, exemplar_qty,
     exemplar_keys, not_available_exemplar_qty). Ozon показывает код покупателю в заказе и присылает на почту.
     Коды, которые передать нельзя, указываются в not_available_exemplar_qty — Ozon вернёт за них деньги.

Покупатель Ozon не сообщает продавцу свой @username, поэтому «код» здесь — одноразовый код активации:
покупатель вводит его вместе с @username в боте активации (Telegram или веб-страница), и только тогда бот
покупает звёзды на Fragment.

Авторизация: заголовки Client-Id и Api-Key (кабинет Ozon → Настройки → Seller API).
Если Ozon поменяет поля или версии методов, править нужно только этот файл.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

log = logging.getLogger("ozon")
BASE = "https://api-seller.ozon.ru"


class OzonError(Exception):
    def __init__(self, text: str, code: str = "api"):
        super().__init__(text)
        self.code = code


class OzonAuthError(OzonError):
    """Неверные Client-Id/Api-Key или у ключа нет прав на цифровые товары."""

    def __init__(self, text: str, code: str = "auth"):
        super().__init__(text, code)


@dataclass
class OzonProduct:
    sku: int
    offer_id: str
    name: str
    quantity: int
    price: float                      # цена товара для покупателя, ₽ за штуку
    required_codes: int               # сколько кодов нужно передать по этой строке
    payout: float | None = None       # выплата продавцу за строку (financial_data), ₽
    commission: float | None = None   # комиссия Ozon за строку, ₽


@dataclass
class OzonPosting:
    posting_number: str
    order_number: str
    status: str
    created_at: str = ""
    deadline: str = ""                # waiting_deadline_for_digital_code
    products: list[OzonProduct] = field(default_factory=list)
    cancelled: bool = False
    cancel_initiator: str = ""

    @property
    def needs_codes(self) -> bool:
        return not self.cancelled and any(p.required_codes > 0 for p in self.products)


def _money(v: Any) -> float:
    """Ozon отдаёт деньги то числом, то строкой, то объектом {amount|value, currency_code}."""
    if isinstance(v, dict):
        v = v.get("amount", v.get("value", v.get("units", 0)))
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_posting(raw: dict) -> OzonPosting:
    fin: dict[int, dict] = {}
    for f in ((raw.get("financial_data") or {}).get("products") or []):
        fin[int(f.get("product_id") or 0)] = f      # product_id здесь — SKU (см. описание метода)
    products = []
    for p in raw.get("products") or []:
        sku = int(p.get("sku") or 0)
        qty = int(p.get("quantity") or 1)
        f = fin.get(sku) or {}
        commission = f.get("commission")
        products.append(OzonProduct(
            sku=sku, offer_id=str(p.get("offer_id") or ""), name=str(p.get("name") or ""), quantity=qty,
            price=_money(p.get("price")),
            required_codes=int(p.get("required_qty_for_digital_code") if p.get("required_qty_for_digital_code")
                               is not None else 0),
            payout=_money(f["payout"]) if f.get("payout") is not None else None,
            commission=_money(commission.get("amount") if isinstance(commission, dict) else commission)
            if commission is not None else None))
    status = str(raw.get("status") or "")
    cancel = raw.get("cancellation") or {}
    cancelled = status == "cancelled" or bool(raw.get("cancel_reason_id")) or bool(cancel.get("cancellation_type"))
    return OzonPosting(posting_number=str(raw.get("posting_number") or ""), order_number=str(raw.get("order_number")
                       or ""), status=status, created_at=str(raw.get("created_at") or raw.get("in_process_at") or ""),
                       deadline=str(raw.get("waiting_deadline_for_digital_code") or ""), products=products,
                       cancelled=cancelled, cancel_initiator=str(cancel.get("cancellation_initiator") or ""))


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


class OzonClient:
    def __init__(self, client_id: str | int, api_key: str, *, base: str = BASE, dry_run: bool = False,
                 min_interval: float = 0.4):
        self.client_id = str(client_id or "").strip()
        self.api_key = str(api_key or "").strip()
        self.base = (base or BASE).rstrip("/")
        self.dry_run = dry_run
        self.min_interval = min_interval
        self._session: aiohttp.ClientSession | None = None
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), headers={
                "Client-Id": self.client_id, "Api-Key": self.api_key, "Content-Type": "application/json"})
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(self, path: str, body: dict) -> dict:
        if not self.client_id or not self.api_key:
            raise OzonAuthError("не заданы Client-Id и Api-Key Ozon", "config")
        async with self._lock:
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()
        sess = await self._sess()
        for attempt in range(3):
            try:
                async with sess.post(self.base + path, json=body) as r:
                    text = await r.text()
                    if r.status in (401, 403):
                        raise OzonAuthError(f"Ozon {r.status}: {text[:300]}")
                    if r.status == 429 or r.status >= 500:
                        if attempt < 2:
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        raise OzonError(f"Ozon {r.status}: {text[:300]}", "rate_limit" if r.status == 429 else "network")
                    try:
                        data = await r.json(content_type=None)
                    except ValueError as e:
                        raise OzonError(f"Ozon вернул не JSON ({r.status}): {text[:200]}", "layout") from e
                    if r.status >= 400:
                        raise OzonError(f"Ozon {r.status}: {data.get('message') or text[:300]}", "api")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise OzonError(f"Ozon недоступен: {e}", "network") from e
        raise OzonError("Ozon недоступен", "network")  # pragma: no cover

    # ------------------------------------------------------------------ методы
    async def list_postings(self, since: float, to: float | None = None, limit: int = 100) -> list[OzonPosting]:
        """Все отправления цифровых товаров за период (с постраничной выборкой по cursor)."""
        out: list[OzonPosting] = []
        cursor = ""
        for _ in range(50):
            body: dict[str, Any] = {"filter": {"since": iso(since), "to": iso(to or time.time() + 60)},
                                    "limit": limit, "sort_dir": "ASC", "with": {"financial_data": True}}
            if cursor:
                body["cursor"] = cursor
            data = await self.call("/v2/posting/digital/list", body)
            res = data.get("result") if isinstance(data.get("result"), dict) else data
            out += [parse_posting(p) for p in (res.get("postings") or [])]
            cursor = str(res.get("cursor") or "")
            if not res.get("has_next") or not cursor:
                break
        return out

    async def upload_codes(self, posting_number: str, items: list[dict]) -> list[dict]:
        """items: [{"sku", "exemplar_keys": [...], "not_available_exemplar_qty"}] → ответ Ozon по каждому SKU."""
        body = {"posting_number": posting_number, "exemplars_by_sku": [
            {"sku": int(i["sku"]), "exemplar_qty": len(i.get("exemplar_keys") or []),
             "exemplar_keys": list(i.get("exemplar_keys") or []),
             "not_available_exemplar_qty": int(i.get("not_available_exemplar_qty") or 0)} for i in items]}
        if self.dry_run:
            log.info("[dry-run] коды для %s не отправлены в Ozon: %s", posting_number,
                     [(i["sku"], i["exemplar_qty"], i["not_available_exemplar_qty"]) for i in body["exemplars_by_sku"]])
            return [{"sku": i["sku"], "received_qty": i["exemplar_qty"], "rejected_qty": 0, "failed_exemplars": [],
                     "dry_run": True} for i in body["exemplars_by_sku"]]
        data = await self.call("/v1/posting/digital/codes/upload", body)
        res = data.get("result") if isinstance(data.get("result"), dict) else data
        return list(res.get("exemplars_by_sku") or [])

    async def import_stocks(self, stocks: dict[str, int]) -> list[dict]:
        """offer_id → остаток. Возвращает статусы по товарам (errors — если Ozon не принял)."""
        if not stocks:
            return []
        body = {"stocks": [{"offer_id": str(k), "stock": max(0, int(v))} for k, v in stocks.items()]}
        if self.dry_run:
            log.info("[dry-run] остатки Ozon не обновлены: %s", stocks)
            return [{"offer_id": k, "updated": True, "dry_run": True} for k in stocks]
        data = await self.call("/v1/product/digital/stocks/import", body)
        return list(data.get("status") or data.get("result") or [])
