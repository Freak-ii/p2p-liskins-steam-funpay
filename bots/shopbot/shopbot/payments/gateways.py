"""P2P-шлюзы для оплаты картой и СБП без ИП (payments.card.provider).

Покупатель платит шлюзу по карте или СБП. Шлюз копит рубли на своём балансе магазина, а мы выводим их
в USDT на хаб TON, и деньги возвращаются в круг (loop: card → to_sweep). Комиссию шлюза клиент видит строкой
«Комиссия за оплату картой/СБП» (payments.card.fee_pct). Она должна покрывать приём шлюза, его вывод в USDT
и запас на спред.

Все шлюзы работают опросом статуса (check), как остальные способы. Белый IP и вебхук не нужны, магазин
работает с домашнего ПК. Вебхук шлюза можно не настраивать или направить на любую заглушку.

  crystalpay  CrystalPAY v3: invoice/create → ссылка, invoice/info → state=payed. Логин кассы и секрет.
  aaio        AAIO: ссылка оплаты подписана sha256(merchant_id:amount:currency:secret1:order_id),
              статус — POST /api/info-pay (X-Api-Key) → status=success|hold.
  freekassa   FreeKassa API v1: orders/create, затем orders (по paymentId). Подпись — HMAC-SHA256 от
              значений, отсортированных по ключу, через «|». method_id — код способа оплаты (СБП/карта) из кабинета.

Методы API взяты из публичной документации шлюзов. При подключении первый платёж нужно провести на минимальную
сумму и сверить статусы: шлюзы меняют API без предупреждения.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import aiohttp

from shopbot.payments import CANCELED, PAID, PENDING, PaymentError

TIMEOUT = aiohttp.ClientTimeout(total=20)


class Gateway:
    """Общий вид шлюза: create(order) → {id, url}, check(id) → pending|paid|canceled."""
    name = "gateway"
    title = "шлюз"
    test = False

    @property
    def configured(self) -> bool:
        raise NotImplementedError

    async def _post(self, url: str, *, json: dict | None = None, data: dict | None = None,
                    headers: dict | None = None) -> dict:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(url, json=json, data=data, headers=headers) as r:
                d = await r.json(content_type=None)
                if r.status >= 400:
                    raise PaymentError(f"{self.title} {r.status}: {d}")
                return d if isinstance(d, dict) else {"data": d}


class CrystalPay(Gateway):
    name, title = "crystalpay", "CrystalPAY"
    API = "https://api.crystalpay.io/v3"
    PAID_STATES = {"payed"}
    DEAD_STATES = {"failed", "expired", "unavailable"}

    def __init__(self, login: str, secret: str, *, salt: str = "", lifetime_min: int = 30):
        self.login, self.secret, self.salt = str(login or ""), str(secret or ""), str(salt or "")
        self.lifetime = int(lifetime_min or 30)

    @property
    def configured(self) -> bool:
        return bool(self.login and self.secret)

    def _auth(self) -> dict:
        return {"auth_login": self.login, "auth_secret": self.secret}

    async def create(self, order_id: str, amount_rub: float, description: str, *, ttl: int = 0, **_) -> dict:
        if not self.configured:
            raise PaymentError("CrystalPAY не настроен: логин кассы и секретный ключ")
        d = await self._post(f"{self.API}/invoice/create/", json={
            **self._auth(), "amount": round(amount_rub, 2), "amount_currency": "RUB", "type": "purchase",
            "lifetime": max(5, int((ttl or self.lifetime * 60) / 60)), "description": description[:250],
            "extra": order_id})
        if d.get("error"):
            raise PaymentError(f"CrystalPAY: {d.get('errors')}")
        return {"id": str(d["id"]), "url": d.get("url", ""), "status": PENDING}

    async def check(self, payment_id: str) -> str:
        d = await self._post(f"{self.API}/invoice/info/", json={**self._auth(), "id": payment_id})
        st = str(d.get("state") or "")
        return PAID if st in self.PAID_STATES else CANCELED if st in self.DEAD_STATES else PENDING

    def verify_callback(self, body: dict) -> bool:
        """Подпись вебхука: sha1(id:salt). Пригодится, если когда-нибудь будет публичный адрес."""
        want = hashlib.sha1(f"{body.get('id')}:{self.salt}".encode()).hexdigest()
        return bool(self.salt) and hmac.compare_digest(want, str(body.get("signature") or ""))


class Aaio(Gateway):
    name, title = "aaio", "AAIO"
    PAY = "https://aaio.so/merchant/pay"
    INFO = "https://aaio.so/api/info-pay"

    def __init__(self, merchant_id: str, secret1: str, api_key: str, *, currency: str = "RUB"):
        self.merchant_id, self.secret1, self.api_key = str(merchant_id or ""), str(secret1 or ""), str(api_key or "")
        self.currency = currency

    @property
    def configured(self) -> bool:
        return bool(self.merchant_id and self.secret1 and self.api_key)

    def sign(self, amount: str, order_id: str) -> str:
        return hashlib.sha256(f"{self.merchant_id}:{amount}:{self.currency}:{self.secret1}:{order_id}"
                              .encode()).hexdigest()

    async def create(self, order_id: str, amount_rub: float, description: str, **_) -> dict:
        if not self.configured:
            raise PaymentError("AAIO не настроен: ID магазина, секретный ключ №1 и API-ключ")
        from urllib.parse import urlencode
        amount = f"{amount_rub:.2f}"
        q = {"merchant_id": self.merchant_id, "amount": amount, "currency": self.currency, "order_id": order_id,
             "sign": self.sign(amount, order_id), "desc": description[:250], "lang": "ru"}
        return {"id": order_id, "url": f"{self.PAY}?{urlencode(q)}", "status": PENDING}

    async def check(self, payment_id: str) -> str:
        d = await self._post(self.INFO, data={"merchant_id": self.merchant_id, "order_id": payment_id},
                             headers={"Accept": "application/json", "X-Api-Key": self.api_key})
        if d.get("type") != "success":
            return PENDING                     # счёт ещё не открыт клиентом — AAIO его не знает
        st = str(d.get("status") or "")
        return PAID if st in ("success", "hold") else CANCELED if st == "expired" else PENDING


class FreeKassa(Gateway):
    name, title = "freekassa", "FreeKassa"
    API = "https://api.fk.life/v1"

    def __init__(self, shop_id: str, api_key: str, *, method_id: int = 0, email_domain: str = "telegram.org",
                 ip: str = "127.0.0.1"):
        self.shop_id, self.api_key = str(shop_id or ""), str(api_key or "")
        self.method_id, self.email_domain, self.ip = int(method_id or 0), email_domain, ip

    @property
    def configured(self) -> bool:
        return bool(self.shop_id and self.api_key and self.method_id)

    def signed(self, body: dict) -> dict:
        body = {"shopId": int(self.shop_id), "nonce": time.time_ns() // 1000 + secrets.randbelow(1000), **body}
        msg = "|".join(str(body[k]) for k in sorted(body))
        body["signature"] = hmac.new(self.api_key.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return body

    async def create(self, order_id: str, amount_rub: float, description: str, **_) -> dict:
        if not self.configured:
            raise PaymentError("FreeKassa не настроена: ID магазина, API-ключ и код способа оплаты (method_id)")
        d = await self._post(f"{self.API}/orders/create", json=self.signed({
            "paymentId": order_id, "i": self.method_id, "email": f"{order_id.lower()}@{self.email_domain}",
            "ip": self.ip, "amount": round(amount_rub, 2), "currency": "RUB"}))
        if d.get("type") != "success":
            raise PaymentError(f"FreeKassa: {d.get('message') or d}")
        return {"id": order_id, "url": d.get("location", ""), "status": PENDING}

    async def check(self, payment_id: str) -> str:
        d = await self._post(f"{self.API}/orders", json=self.signed({"paymentId": payment_id}))
        orders = d.get("orders") or []
        if not orders:
            return PENDING
        st = int(orders[0].get("status") or 0)          # 1 — оплачен, 8/9 — ошибка/отмена
        return PAID if st == 1 else CANCELED if st in (8, 9) else PENDING


GATEWAYS = {"crystalpay": "CrystalPAY", "aaio": "AAIO", "freekassa": "FreeKassa"}


def build_gateway(cfg):
    """Провайдер способа «card» по payments.card.provider; None — значит ЮKassa (собирается в build_providers)."""
    p = str(cfg.get("payments.card.provider") or "yookassa").lower()
    g = cfg.get(f"payments.card.{p}") or {}
    if p == "crystalpay":
        return CrystalPay(g.get("login"), g.get("secret"), salt=g.get("salt") or "",
                          lifetime_min=int(cfg.get("payments.card.ttl_minutes") or 30))
    if p == "aaio":
        return Aaio(g.get("merchant_id"), g.get("secret1"), g.get("api_key"))
    if p == "freekassa":
        return FreeKassa(g.get("shop_id"), g.get("api_key"), method_id=int(g.get("method_id") or 0))
    return None
