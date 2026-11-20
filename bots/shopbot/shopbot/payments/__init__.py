"""Способы оплаты магазина.

  card    — ЮKassa, страница оплаты (confirmation.type=redirect). Тестовый магазин ЮKassa: shop_id тестового
            магазина + секретный ключ вида test_…; деньги не списываются, карты из документации ЮKassa.
  crypto  — Crypto Pay API (@CryptoBot): счёт в USDT/TON на сумму, посчитанную по курсу Bybit + комиссия ≥2%.
            testnet=True → testnet-pay.crypt.bot (тестовые монеты). Через тот же API — выплаты при выкупе.
  stars   — счёт Telegram в звёздах (XTR) прямо в боте; так же клиент «продаёт» нам звёзды.
  balance — списание с баланса магазина (возвраты, бонусы рефералки).
Статусы проверяются опросом: белый IP и вебхуки не нужны, магазин работает с домашнего компьютера.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
import uuid

import aiohttp

log = logging.getLogger("payments")
PENDING, PAID, CANCELED = "pending", "paid", "canceled"


class PaymentError(Exception):
    pass


class YooKassa:
    name = "yookassa"
    API = "https://api.yookassa.ru/v3"

    def __init__(self, shop_id: str, secret_key: str, *, return_url: str = "", test: bool = True,
                 send_receipt: bool = False, vat_code: int = 1, tax_system_code: int = 0):
        self.shop_id, self.secret_key = str(shop_id or ""), str(secret_key or "")
        self.return_url = return_url or "https://t.me"
        self.test = test
        self.send_receipt, self.vat_code, self.tax_system_code = send_receipt, vat_code, tax_system_code

    @property
    def configured(self) -> bool:
        if not (self.shop_id and self.secret_key):
            return False
        return self.secret_key.startswith("test_") if self.test else True

    def _headers(self, idem: str | None = None) -> dict:
        auth = base64.b64encode(f"{self.shop_id}:{self.secret_key}".encode()).decode()
        h = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
        if idem:
            h["Idempotence-Key"] = idem
        return h

    def payload(self, order_id: str, amount_rub: float, description: str, email: str = "") -> dict:
        body = {"amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"}, "capture": True,
                "confirmation": {"type": "redirect", "return_url": self.return_url},
                "description": description[:128], "metadata": {"order_id": order_id}}
        if self.send_receipt and email:                       # чек 54-ФЗ, если включён в ЮKassa
            body["receipt"] = {"customer": {"email": email}, "items": [{
                "description": description[:128], "quantity": "1.00",
                "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"}, "vat_code": self.vat_code,
                "payment_mode": "full_payment", "payment_subject": "service"}]}
            if self.tax_system_code:
                body["receipt"]["tax_system_code"] = self.tax_system_code
        return body

    async def create(self, order_id: str, amount_rub: float, description: str, **_) -> dict:
        if not self.configured:
            raise PaymentError("ЮKassa не настроена: shop_id и секретный ключ (в тестовом режиме — test_…)")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.post(f"{self.API}/payments", json=self.payload(order_id, amount_rub, description),
                              headers=self._headers(f"shop-{order_id}")) as r:
                d = await r.json(content_type=None)
                if r.status not in (200, 201):
                    raise PaymentError(f"ЮKassa {r.status}: {d.get('description') or d}")
        return {"id": d["id"], "url": (d.get("confirmation") or {}).get("confirmation_url", ""), "status": PENDING}

    async def check(self, payment_id: str) -> str:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(f"{self.API}/payments/{payment_id}", headers=self._headers()) as r:
                d = await r.json(content_type=None)
        st = d.get("status")
        return PAID if st == "succeeded" else CANCELED if st == "canceled" else PENDING

    async def refund(self, payment_id: str, amount_rub: float) -> str:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.post(f"{self.API}/refunds", headers=self._headers(str(uuid.uuid4())),
                              json={"payment_id": payment_id,
                                    "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"}}) as r:
                d = await r.json(content_type=None)
        return str(d.get("status") or d)


class CryptoPay:
    name = "cryptopay"

    def __init__(self, token: str, *, testnet: bool = True):
        self.token = str(token or "")
        self.api = "https://testnet-pay.crypt.bot/api" if testnet else "https://pay.crypt.bot/api"

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def _call(self, method: str, params: dict) -> dict:
        if not self.configured:
            raise PaymentError("Crypto Pay не настроен: токен приложения из @CryptoBot (в тесте — @CryptoTestnetBot)")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.post(f"{self.api}/{method}", json=params,
                                  headers={"Crypto-Pay-API-Token": self.token}) as r:
                    d = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise PaymentError(f"Crypto Pay недоступен: {e}") from e
        if not d.get("ok"):
            raise PaymentError(f"Crypto Pay {method}: {d.get('error')}")
        return d["result"]

    async def create(self, order_id: str, amount_rub: float, description: str, *, asset: str = "USDT",
                     pay_amount: float = 0, ttl: int = 1800, **_) -> dict:
        res = await self._call("createInvoice", {"currency_type": "crypto", "asset": asset,
                                                 "amount": f"{pay_amount:g}", "description": description[:1024],
                                                 "payload": order_id, "expires_in": ttl,
                                                 "allow_comments": False, "allow_anonymous": True})
        return {"id": str(res["invoice_id"]), "url": res.get("bot_invoice_url") or res.get("pay_url", ""),
                "status": PENDING}

    async def check(self, payment_id: str) -> str:
        res = await self._call("getInvoices", {"invoice_ids": payment_id})
        items = res.get("items") or []
        st = items[0]["status"] if items else "expired"
        return PAID if st == "paid" else CANCELED if st == "expired" else PENDING

    async def balance(self, asset: str = "USDT") -> float:
        res = await self._call("getBalance", {})
        return sum(float(x.get("available") or 0) for x in res if x.get("currency_code") == asset)

    async def transfer(self, user_id: int, asset: str, amount: float, spend_id: str, comment: str = "") -> dict:
        """Выплата клиенту в @CryptoBot; spend_id делает выплату идемпотентной (повтор не заплатит дважды)."""
        return await self._call("transfer", {"user_id": int(user_id), "asset": asset, "amount": f"{amount:g}",
                                             "spend_id": spend_id[:64], "comment": comment[:1024],
                                             "disable_send_notification": False})


class TelegramStars:
    """Счёт в звёздах создаёт сам бот (sendInvoice, currency XTR); оплата приходит как successful_payment."""

    name = "telegram_stars"
    configured = True

    def __init__(self):
        self.paid: dict[str, str] = {}

    async def create(self, order_id: str, amount_rub: float, description: str, **_) -> dict:
        return {"id": f"xtr-{order_id}", "url": "", "status": PENDING, "invoice": True}

    async def check(self, payment_id: str) -> str:
        return PAID if payment_id in self.paid else PENDING


class DemoPay:
    """Демо: платёж «оплачивается» кнопкой в демо-чате/панели или автоматически через auto_pay_after секунд."""

    def __init__(self, name: str, auto_pay_after: float | None = None):
        self.name = f"demo-{name}"
        self.configured = True
        self.payments: dict[str, dict] = {}
        self.auto = auto_pay_after
        self.transfers: list[dict] = []
        self.balance_usdt = 250.0

    async def create(self, order_id: str, amount_rub: float, description: str, **kw) -> dict:
        pid = f"demo-{secrets.token_hex(4)}"
        self.payments[pid] = {"status": PENDING, "order_id": order_id, "ts": time.time()}
        return {"id": pid, "url": f"/demo/pay/{pid}", "status": PENDING, "invoice": self.name == "demo-stars"}

    def pay(self, pid: str) -> None:
        if pid in self.payments:
            self.payments[pid]["status"] = PAID

    async def check(self, payment_id: str) -> str:
        p = self.payments.get(payment_id)
        if p is None:
            return CANCELED
        if self.auto is not None and time.time() - p["ts"] >= self.auto:
            p["status"] = PAID
        return p["status"]

    async def balance(self, asset: str = "USDT") -> float:
        return self.balance_usdt

    async def transfer(self, user_id: int, asset: str, amount: float, spend_id: str, comment: str = "") -> dict:
        if any(t["spend_id"] == spend_id for t in self.transfers):
            return next(t for t in self.transfers if t["spend_id"] == spend_id)
        if amount > self.balance_usdt:
            raise PaymentError("NOT_ENOUGH_COINS (демо)")
        self.balance_usdt -= amount
        t = {"transfer_id": len(self.transfers) + 1, "user_id": user_id, "asset": asset, "amount": amount,
             "spend_id": spend_id, "status": "completed"}
        self.transfers.append(t)
        return t

    async def refund(self, payment_id: str, amount_rub: float) -> str:
        return "succeeded"


def build_providers(cfg, *, demo: bool) -> dict:
    if demo:
        return {"card": DemoPay("card"), "crypto": DemoPay("crypto"), "stars": DemoPay("stars")}
    y = cfg.get("payments.card.yookassa") or {}
    c = cfg.get("payments.crypto.cryptopay") or {}
    return {"card": YooKassa(y.get("shop_id"), y.get("secret_key"), return_url=y.get("return_url") or "",
                             test=bool(y.get("test", True)), send_receipt=bool(y.get("send_receipt")),
                             vat_code=int(y.get("vat_code") or 1), tax_system_code=int(y.get("tax_system_code") or 0)),
            "crypto": CryptoPay(c.get("token"), testnet=bool(c.get("testnet", True))),
            "stars": TelegramStars()}
