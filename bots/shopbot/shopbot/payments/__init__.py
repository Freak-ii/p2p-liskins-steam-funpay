"""Способы оплаты магазина.

  card    — «Картой / СБП»: ЮKassa (нужен ИП/самозанятость) или P2P-шлюз для физлиц (CrystalPAY, AAIO) —
            см. card_provider. Шлюз берёт 5–12 %, выводит в USDT → хаб; ЮKassa — страница оплаты (confirmation.type=redirect). Тестовый магазин ЮKassa: shop_id тестового
            магазина + секретный ключ вида test_…; деньги не списываются, карты из документации ЮKassa.
  crypto  — Crypto Pay API (@CryptoBot): счёт в USDT/TON на сумму, посчитанную по курсу Bybit + комиссия ≥2%.
            testnet=True → testnet-pay.crypt.bot (тестовые монеты). Через тот же API — выплаты при выкупе.
  stars   — счёт Telegram в звёздах (XTR) прямо в боте; так же клиент «продаёт» нам звёзды.
  balance — списание с баланса магазина (возвраты, бонусы рефералки).
  wallet  — крипта сразу на общий кошелёк TON (хаб казначейства): TON с комментарием DPB-<заказ> или USDT (TON)
            на уникальную сумму; приход видит опрос toncenter v3. Деньги сразу в круге, без посредника.
  gift    — оплата подарками Telegram на аккаунт приёма; skins — скинами Steam обменом на ссылку магазина.
            Оба подтверждает оператор (кнопка в Telegram/панели), позже — автоматический приём.
Статусы проверяются опросом: белый IP и вебхуки не нужны, магазин работает с домашнего компьютера
(у CrystalPAY уведомление можно включить дополнительно, см. CrystalPay).
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


class CrystalPay:
    """CrystalPAY (касса для физлиц): СБП/карта РФ → баланс кассы → вывод в TON/USDT на хаб.

    API v3 (docs.crystalpay.io): все методы — POST JSON на https://api.crystalpay.io/v3/<метод>/ с auth_login
    (логин кассы) + auth_secret (секретный ключ). Salt в запросах не передаётся: им подписываются уведомления
    (callback: signature = sha1("{id}:{salt}")) и выводы payoff (их бот не делает: вывод с кассы — вручную в
    @CrystalPAY_bot). Лимит API — 5 запросов/с, 50 за 10 с (429).

    Состояния счёта (invoice/info → state): created, notpayed, processing, wrongamount (недоплата), failed
    (ошибка, не конечный) — ждём; payed — оплачен (конечный); unavailable — метод недоступен (конечный) → отмена.
    Срок счёта = lifetime; после него заказ истекает по expires_at в engine.poll_payments.

    СБП открывается только кассе со статусом «Профессиональный» (проверка CrystalPAY по требованиям, проекту
    нужна история). Метод СБП задаётся required_method; "auto" — ищем включённый метод по method/list
    (ключ или название содержит SBP/СБП). Пока СБП в кассе нет, RTBA держит «Картой / СБП» закрытым.
    Основной приём — опрос invoice/info (вебхук не обязателен: магазин живёт на домашнем ПК без белого IP);
    callback_url можно указать, если панель доступна снаружи (сервер «Орбиты»): уведомление проверяется
    подписью и только запускает внеочередную проверку статуса через API, телу уведомления не верим."""

    name = "crystalpay"
    API = "https://api.crystalpay.io/v3"
    WAIT = ("created", "notpayed", "processing", "wrongamount", "failed")
    MIN_GAP = 0.25                      # ≤ 4 запросов/с — ниже лимита API

    def __init__(self, login: str, secret: str, *, salt: str = "", redirect_url: str = "", callback_url: str = "",
                 required_method: str = "", subtract_from: str = "balance", fetch=None):
        self.login, self.secret, self.salt = str(login or ""), str(secret or ""), str(salt or "")
        self.redirect_url = redirect_url or "https://t.me"
        self.callback_url = callback_url or ""
        self.required_method = str(required_method or "").strip()
        self.subtract_from = subtract_from if subtract_from in ("balance", "amount") else "balance"
        self._fetch = fetch
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._methods: tuple[float, dict] = (0.0, {})

    @property
    def configured(self) -> bool:
        return bool(self.login and self.secret)

    # ------------------------------------------------------------------ транспорт
    async def _call(self, method: str, body: dict) -> dict:
        if not self.configured:
            raise PaymentError("CrystalPAY не настроен: логин кассы и секретный ключ (@CrystalPAY_bot → Кассы)")
        body = {"auth_login": self.login, "auth_secret": self.secret, **body}
        async with self._lock:
            gap = self.MIN_GAP - (time.monotonic() - self._last)
            if gap > 0 and self._fetch is None:
                await asyncio.sleep(gap)
            self._last = time.monotonic()
            if self._fetch is not None:
                d = await self._fetch(method, body)
            else:
                try:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                        async with s.post(f"{self.API}/{method}/", json=body) as r:
                            if r.status == 429:
                                raise PaymentError("CrystalPAY: лимит запросов (429), повторим позже")
                            d = await r.json(content_type=None)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                    raise PaymentError(f"CrystalPAY недоступен: {e}") from e
        if not isinstance(d, dict):
            raise PaymentError(f"CrystalPAY {method}: неожиданный ответ")
        if d.get("error"):
            raise PaymentError(f"CrystalPAY {method}: {'; '.join(map(str, d.get('errors') or [])) or 'ошибка'}")
        return d

    # ------------------------------------------------------------------ подписи
    def sign(self, *parts) -> str:
        import hashlib
        return hashlib.sha1(":".join(str(p) for p in (*parts, self.salt)).encode()).hexdigest()

    def verify_callback(self, data: dict) -> bool:
        """Подпись уведомления: sha1("{id}:{salt}"), сравнение за постоянное время."""
        import hmac
        if not self.salt or not isinstance(data, dict) or not data.get("id"):
            return False
        return hmac.compare_digest(self.sign(data["id"]), str(data.get("signature") or ""))

    # ------------------------------------------------------------------ методы кассы
    async def methods(self, *, max_age: float = 600) -> dict:
        ts, items = self._methods
        if items and time.time() - ts < max_age:
            return items
        items = (await self._call("method/list", {})).get("items") or {}
        self._methods = (time.time(), items)
        return items

    @staticmethod
    def is_sbp(key: str, item: dict) -> bool:
        text = f"{key} {item.get('name') or ''}".upper()
        return "SBP" in text or "СБП" in text

    async def sbp_method(self) -> str:
        """Ключ метода СБП для required_method ('' — СБП в кассе не включён)."""
        items = await self.methods()
        if self.required_method and self.required_method.lower() != "auto":
            it = items.get(self.required_method) or {}
            return self.required_method if (it.get("in") or {}).get("enabled") else ""
        for key, it in items.items():
            if self.is_sbp(key, it) and (it.get("in") or {}).get("enabled"):
                return key
        return ""

    async def summary(self) -> dict:
        """Для панели: включённые методы приёма с комиссиями и лимитами, найденный СБП, балансы."""
        items = await self.methods(max_age=0)
        rows = []
        for key, it in items.items():
            i = it.get("in") or {}
            rows.append({"key": key, "name": it.get("name") or key, "currency": it.get("currency"),
                         "enabled": bool(i.get("enabled")), "sbp": self.is_sbp(key, it),
                         "commission_pct": (i.get("commissions") or {}).get("percent"),
                         "commission_fix": (i.get("commissions") or {}).get("amount"),
                         "extra_pct": (i.get("extra_commissions") or {}).get("percent"),
                         "min": (i.get("limits") or {}).get("min"), "max": (i.get("limits") or {}).get("max"),
                         "status_level": it.get("minimal_status_level")})
        try:
            bal = (await self._call("balance/list", {})).get("items") or {}
        except PaymentError as e:
            bal = {"error": str(e)}
        return {"methods": rows, "sbp": await self.sbp_method(), "balances": bal}

    # ------------------------------------------------------------------ счёт
    async def create(self, order_id: str, amount_rub: float, description: str, *, ttl: int = 1800,
                     payer: dict | None = None, **_) -> dict:
        body = {"amount": round(float(amount_rub), 2), "type": "purchase", "currency": "RUB",
                "lifetime": max(5, int(ttl // 60)), "subtract_from": self.subtract_from,
                "description": description[:256], "extra": order_id, "redirect_url": self.redirect_url}
        method = await self.sbp_method() if self.required_method else ""
        if self.required_method and not method:
            raise PaymentError("CrystalPAY: СБП в кассе не включён (нужен статус кассы «Профессиональный»)")
        if method:
            body["required_method"] = method
        if self.callback_url:
            body["callback_url"] = self.callback_url
        if payer:
            body["payer_details"] = {k: v for k, v in payer.items() if v}
        d = await self._call("invoice/create", body)
        return {"id": str(d["id"]), "url": d.get("url", ""), "status": PENDING}

    async def info(self, payment_id: str) -> dict:
        return await self._call("invoice/info", {"id": payment_id})

    async def check(self, payment_id: str) -> str:
        d = await self.info(payment_id)
        st = str(d.get("state") or "")
        if st == "payed":
            cur = str(d.get("currency") or "RUB").upper()
            if d.get("type", "purchase") != "purchase" or cur != "RUB":
                log.warning("CrystalPAY %s: оплачен, но type=%s currency=%s — не засчитываем", payment_id,
                            d.get("type"), cur)
                return PENDING
            return PAID
        return CANCELED if st in ("unavailable", "cancelled", "canceled") else PENDING


class Aaio:
    """AAIO (P2P-шлюз): ссылка на оплату с подписью sha256(merchant_id:amount:currency:secret1:order_id),
    статус — POST /api/info-pay (X-Api-Key). success → оплачен, expired → отмена, in_process/hold → ждём
    (hold — деньги у шлюза на проверке, товар не выдаём)."""

    name = "aaio"
    PAY = "https://aaio.so/merchant/pay"
    INFO = "https://aaio.so/api/info-pay"

    def __init__(self, merchant_id: str, secret1: str, api_key: str, *, lang: str = "ru", fetch=None):
        self.merchant_id, self.secret1, self.api_key = str(merchant_id or ""), str(secret1 or ""), str(api_key or "")
        self.lang = lang
        self._fetch = fetch

    @property
    def configured(self) -> bool:
        return bool(self.merchant_id and self.secret1 and self.api_key)

    def sign(self, amount: str, currency: str, order_id: str) -> str:
        import hashlib
        return hashlib.sha256(f"{self.merchant_id}:{amount}:{currency}:{self.secret1}:{order_id}".encode()).hexdigest()

    async def create(self, order_id: str, amount_rub: float, description: str, **_) -> dict:
        if not self.configured:
            raise PaymentError("AAIO не настроен: ID магазина, секретный ключ №1 и API-ключ (кабинет AAIO)")
        from urllib.parse import urlencode
        amount = f"{float(amount_rub):.2f}"
        q = {"merchant_id": self.merchant_id, "amount": amount, "currency": "RUB", "order_id": order_id,
             "sign": self.sign(amount, "RUB", order_id), "desc": description[:250], "lang": self.lang}
        return {"id": order_id, "url": f"{self.PAY}?{urlencode(q)}", "status": PENDING}

    async def check(self, payment_id: str) -> str:
        form = {"merchant_id": self.merchant_id, "order_id": payment_id}
        if self._fetch is not None:
            d = await self._fetch(form)
        else:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
                    async with s.post(self.INFO, data=form, headers={"Accept": "application/json",
                                                                     "X-Api-Key": self.api_key}) as r:
                        d = await r.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise PaymentError(f"AAIO недоступен: {e}") from e
        if d.get("type") == "error":
            return PENDING                         # счёт ещё не открывали — заказ просто ждёт оплаты
        st = str(d.get("status") or "")
        return PAID if st == "success" else CANCELED if st == "expired" else PENDING


GATEWAYS = ("crystalpay", "aaio")


def crystalpay_from(cfg, *, sbp: bool) -> CrystalPay:
    """Касса CrystalPAY: общие ключи payments.crystalpay (старое место — payments.card.crystalpay)."""
    c = {**(cfg.get("payments.card.crystalpay") or {}), **{k: v for k, v in (cfg.get("payments.crystalpay") or {}).items()
                                                           if v not in ("", None)}}
    return CrystalPay(c.get("login"), c.get("secret"), salt=c.get("salt") or "",
                      redirect_url=c.get("redirect_url") or "", callback_url=c.get("callback_url") or "",
                      required_method=(c.get("sbp_method") or "auto") if sbp else "",
                      subtract_from=c.get("subtract_from") or "balance")


def card_provider(cfg):
    """Провайдер способа «Картой / СБП»: ЮKassa (нужен ИП/самозанятость) или P2P-шлюз для физлиц."""
    name = str(cfg.get("payments.card.provider") or "yookassa").lower()
    if name == "crystalpay":
        return crystalpay_from(cfg, sbp=True)
    if name == "aaio":
        a = cfg.get("payments.card.aaio") or {}
        return Aaio(a.get("merchant_id"), a.get("secret1"), a.get("api_key"))
    y = cfg.get("payments.card.yookassa") or {}
    return YooKassa(y.get("shop_id"), y.get("secret_key"), return_url=y.get("return_url") or "",
                    test=bool(y.get("test", True)), send_receipt=bool(y.get("send_receipt")),
                    vat_code=int(y.get("vat_code") or 1), tax_system_code=int(y.get("tax_system_code") or 0))


def build_providers(cfg, *, demo: bool) -> dict:
    gift_acc = cfg.get("payments.gift.account") or cfg.get("buyback.gifts.gift_account")
    if demo:
        wallet = TonWallet("UQ" + "D" * 46, fetch=_demo_ton_fetch)
        return {"card": DemoPay("card"), "crypto": DemoPay("crypto"), "stars": DemoPay("stars"),
                "wallet": wallet, "gift": ManualAsset("gift", True), "skins": ManualAsset("skins", True)}
    c = cfg.get("payments.crypto.cryptopay") or {}
    return {"card": card_provider(cfg),
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
