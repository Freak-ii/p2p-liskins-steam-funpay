"""Оплата из Telegram Wallet (@wallet) через Wallet Pay — касса кошелька Telegram.

Покупатель жмёт «Оплатить в Wallet», Telegram открывает его кошелёк с готовым счётом, он платит TON, USDT, BTC
или NOT со своего баланса в Wallet. Счёт выставляется в долларах (USD) по курсу USDT и привязан к Telegram ID
покупателя (заплатить может только он). Деньги падают на баланс магазина в Wallet Pay, через 48 часов их можно
вывести на хаб TON (круг: tgwallet → hold 48 ч → ready).

Нужен магазин в Wallet Pay (заявка и проверка, ключ выдаёт @WalletPay_Support_Bot) и ключ
payments.tgwallet.api_key. Статус проверяем опросом order/preview, вебхук не нужен.

API (v1, публичная бета): POST /wpay/store-api/v1/order, GET /wpay/store-api/v1/order/preview?id=…,
заголовок Wpay-Store-Api-Key. Статусы: ACTIVE, PAID, EXPIRED, CANCELLED. Минимум счёта около 1.3 $.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

import aiohttp

from shopbot.payments import CANCELED, PAID, PENDING, PaymentError

API = "https://pay.wallet.tg/wpay/store-api/v1"
MIN_USD = 1.3


class WalletPay:
    name = "tgwallet"
    title = "Telegram Wallet"

    def __init__(self, api_key: str, *, return_url: str = "", fetch=None):
        self.api_key = str(api_key or "")
        self.return_url = return_url
        self._fetch = fetch                    # для тестов: async (method, path, body|params) -> dict

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _call(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict:
        if self._fetch is not None:
            return await self._fetch(method, path, json if json is not None else params)
        headers = {"Wpay-Store-Api-Key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.request(method, f"{API}{path}", json=json, params=params, headers=headers) as r:
                d = await r.json(content_type=None)
                if r.status >= 400:
                    raise PaymentError(f"Wallet Pay {r.status}: {d}")
                return d

    async def create(self, order_id: str, amount_rub: float, description: str, *, pay_amount: float = 0,
                     ttl: int = 3600, user_id: int = 0, **_) -> dict:
        if not self.configured:
            raise PaymentError("Wallet Pay не настроен: нужен ключ магазина (payments.tgwallet.api_key)")
        if not user_id:
            raise PaymentError("Wallet Pay: нужен Telegram ID покупателя")
        if pay_amount < MIN_USD:
            raise PaymentError(f"Wallet Pay: минимальный счёт {MIN_USD} $")
        body = {"amount": {"currencyCode": "USD", "amount": f"{pay_amount:.2f}"},
                "description": description[:100].ljust(5, "."), "externalId": order_id,
                "timeoutSeconds": max(30, int(ttl)), "customerTelegramUserId": int(user_id)}
        if self.return_url:
            body["returnUrl"] = self.return_url
            body["failReturnUrl"] = self.return_url
        d = await self._call("POST", "/order", json=body)
        if d.get("status") not in ("SUCCESS", "ALREADY"):
            raise PaymentError(f"Wallet Pay: {d.get('message') or d.get('status')}")
        data = d.get("data") or {}
        return {"id": str(data["id"]), "url": data.get("directPayLink") or data.get("payLink", ""), "status": PENDING}

    async def check(self, payment_id: str) -> str:
        d = await self._call("GET", "/order/preview", params={"id": payment_id})
        st = str((d.get("data") or {}).get("status") or "")
        return PAID if st == "PAID" else CANCELED if st in ("EXPIRED", "CANCELLED") else PENDING

    def verify_webhook(self, method: str, path: str, timestamp: str, body: bytes, signature: str) -> bool:
        """Подпись вебхука: Base64(HMAC-SHA256(METHOD.PATH.TIMESTAMP.Base64(body), api_key))."""
        msg = f"{method}.{path}.{timestamp}.{base64.b64encode(body).decode()}"
        want = base64.b64encode(hmac.new(self.api_key.encode(), msg.encode(), hashlib.sha256).digest()).decode()
        return hmac.compare_digest(want, signature or "")
