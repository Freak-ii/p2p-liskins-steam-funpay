"""Способы оплаты магазина.

  card    — ЮKassa, страница оплаты (confirmation.type=redirect). Тестовый магазин ЮKassa: shop_id тестового
            магазина + секретный ключ вида test_…; деньги не списываются, карты из документации ЮKassa.
  crypto  — Crypto Pay API (@CryptoBot): счёт в USDT/TON на сумму, посчитанную по курсу Bybit + комиссия ≥2%.
            testnet=True → testnet-pay.crypt.bot (тестовые монеты). Через тот же API — выплаты при выкупе.
  stars   — счёт Telegram в звёздах (XTR) прямо в боте; так же клиент «продаёт» нам звёзды.
  balance — списание с баланса магазина (возвраты, бонусы рефералки).
  wallet  — крипта сразу на общий кошелёк TON (хаб казначейства): TON с комментарием DPB-<заказ> или USDT (TON)
            на уникальную сумму; приход видит опрос toncenter v3. Деньги сразу в круге, без посредника.
  gift    — оплата подарками Telegram на аккаунт приёма; skins — скинами Steam обменом на ссылку магазина.
            Оба подтверждает оператор (кнопка в Telegram/панели), позже — автоматический приём.
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


USDT_TON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"


def wallet_comment(order_id: str) -> str:
    return f"DPB-{order_id}"


class TonWallet:
    """Приём на собственный кошелёк TON (общий кошелёк круга). Ничего не подписывает — только смотрит входящие.

    Платёж «создаётся» локально: адрес + сумма + комментарий; ссылки ton:// и Tonkeeper открывают перевод уже
    заполненным. Проверка: TON — входящая транзакция с нашим комментарием и суммой ≥ счёта; USDT — входящий
    перевод жетона USDT на точную уникальную сумму (её подбирает магазин), после создания счёта.
    """

    name = "ton_wallet"
    API = "https://toncenter.com/api/v3"

    def __init__(self, address: str, *, api_key: str = "", fetch=None):
        self.address = str(address or "").strip()
        self.api_key = str(api_key or "")
        self._fetch = fetch                       # тесты/демо подменяют запрос
        self.invoices: dict[str, dict] = {}

    @property
    def configured(self) -> bool:
        return len(self.address) >= 48

    def links(self, asset: str, amount: float, comment: str) -> dict:
        from urllib.parse import quote
        if asset == "TON":
            nano = int(round(amount * 1e9))
            q = f"amount={nano}&text={quote(comment)}"
            return {"ton": f"ton://transfer/{self.address}?{q}",
                    "url": f"https://app.tonkeeper.com/transfer/{self.address}?{q}"}
        units = int(round(amount * 1e6))
        q = f"amount={units}&jetton={USDT_TON_MASTER}&text={quote(comment)}"
        return {"ton": f"ton://transfer/{self.address}?{q}",
                "url": f"https://app.tonkeeper.com/transfer/{self.address}?{q}"}

    async def create(self, order_id: str, amount_rub: float, description: str, *, asset: str = "USDT",
                     pay_amount: float = 0, **_) -> dict:
        if not self.configured:
            raise PaymentError("не указан адрес общего кошелька TON (payments.wallet.address)")
        asset = asset.upper()
        comment = wallet_comment(order_id)
        pid = f"w-{order_id}"
        self.invoices[pid] = {"asset": asset, "amount": float(pay_amount), "comment": comment, "ts": time.time()}
        return {"id": pid, "status": PENDING, "address": self.address, "comment": comment, "asset": asset,
                "amount": float(pay_amount), **self.links(asset, float(pay_amount), comment)}

    @property
    def payments(self) -> dict:                   # демо-кнопка «Оплатить» видит счета только в демо
        return self.invoices if self._fetch is _demo_ton_fetch else {}

    def pay(self, pid: str) -> None:
        """Демо: «перевод» нужной суммы с нужным комментарием появляется в демо-блокчейне."""
        inv = self.invoices[pid]
        if inv["asset"] == "TON":
            DEMO_CHAIN.append({"kind": "ton", "hash": f"demo-{pid}", "in_msg": {
                "value": str(int(inv["amount"] * 1e9)),
                "message_content": {"decoded": {"type": "text_comment", "comment": inv["comment"]}}}})
        else:
            DEMO_CHAIN.append({"kind": "usdt", "transaction_hash": f"demo-{pid}",
                               "amount": str(int(round(inv["amount"] * 1e6)))})

    def restore(self, pid: str, asset: str, amount: float, created_at: float) -> None:
        """После перезапуска счета восстанавливаются из БД."""
        if pid not in self.invoices:
            self.invoices[pid] = {"asset": asset.upper(), "amount": float(amount),
                                  "comment": wallet_comment(pid[2:]), "ts": float(created_at)}

    async def _get(self, path: str, params: dict) -> dict:
        if self._fetch is not None:
            return await self._fetch(path, params)
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(f"{self.API}/{path}", params=params, headers=headers) as r:
                    return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise PaymentError(f"toncenter недоступен: {e}") from e

    @staticmethod
    def _comment(msg: dict) -> str:
        dec = ((msg or {}).get("message_content") or {}).get("decoded") or {}
        return str(dec.get("comment") or "")

    async def check(self, payment_id: str) -> str:
        inv = self.invoices.get(payment_id)
        if inv is None:
            return PENDING
        since = int(inv["ts"]) - 60
        if inv["asset"] == "TON":
            d = await self._get("transactions", {"account": self.address, "limit": 100, "sort": "desc",
                                                 "start_utime": since})
            for tx in d.get("transactions") or []:
                msg = tx.get("in_msg") or {}
                if self._comment(msg).strip() == inv["comment"] and \
                        int(msg.get("value") or 0) >= int(inv["amount"] * 1e9 * 0.999):
                    inv["tx"] = tx.get("hash")
                    return PAID
            return PENDING
        d = await self._get("jetton/transfers", {"owner_address": self.address, "direction": "in",
                                                 "jetton_master_address": USDT_TON_MASTER, "limit": 100,
                                                 "sort": "desc", "start_utime": since})
        want = int(round(inv["amount"] * 1e6))
        for t in d.get("jetton_transfers") or []:
            if int(t.get("amount") or 0) == want and not t.get("transaction_aborted"):
                inv["tx"] = t.get("transaction_hash")
                return PAID
        return PENDING


class ManualAsset:
    """Оплата активом (подарок Telegram, скины Steam): счёт — это инструкция, подтверждает оператор."""

    def __init__(self, name: str, ready: bool):
        self.name = name
        self.configured = ready
        self.confirmed: set[str] = set()

    async def create(self, order_id: str, amount_rub: float, description: str, **_) -> dict:
        return {"id": f"{self.name}-{order_id}", "url": "", "status": PENDING, "manual": True}

    def confirm(self, pid: str) -> None:
        self.confirmed.add(pid)

    async def check(self, payment_id: str) -> str:
        return PAID if payment_id in self.confirmed else PENDING


def build_providers(cfg, *, demo: bool) -> dict:
    gift_acc = cfg.get("payments.gift.account") or cfg.get("buyback.gifts.gift_account")
    if demo:
        wallet = TonWallet("UQ" + "D" * 46, fetch=_demo_ton_fetch)
        return {"card": DemoPay("card"), "crypto": DemoPay("crypto"), "stars": DemoPay("stars"),
                "wallet": wallet, "gift": ManualAsset("gift", True), "skins": ManualAsset("skins", True)}
    y = cfg.get("payments.card.yookassa") or {}
    c = cfg.get("payments.crypto.cryptopay") or {}
    from shopbot.payments.gateways import build_gateway
    gw = build_gateway(cfg)
    return {"card": gw or YooKassa(y.get("shop_id"), y.get("secret_key"), return_url=y.get("return_url") or "",
                             test=bool(y.get("test", True)), send_receipt=bool(y.get("send_receipt")),
                             vat_code=int(y.get("vat_code") or 1), tax_system_code=int(y.get("tax_system_code") or 0)),
            "crypto": CryptoPay(c.get("token"), testnet=bool(c.get("testnet", True))),
            "stars": TelegramStars(),
            "wallet": TonWallet(cfg.get("payments.wallet.address") or "",
                                api_key=cfg.get("payments.wallet.toncenter_api_key") or ""),
            "gift": ManualAsset("gift", bool(gift_acc)),
            "skins": ManualAsset("skins", bool(cfg.get("payments.skins.trade_url")))}


DEMO_CHAIN: list[dict] = []        # демо-«блокчейн»: сюда демо кладёт переводы, TonWallet их «видит»


async def _demo_ton_fetch(path: str, params: dict) -> dict:
    if path == "transactions":
        return {"transactions": [t for t in DEMO_CHAIN if t.get("kind") == "ton"]}
    return {"jetton_transfers": [t for t in DEMO_CHAIN if t.get("kind") == "usdt"]}
