"""Bybit V5 REST: ровно то, что нужно казначейству — балансы, курс TON/USDT, покупка TON, перевод между
счетами и вывод TON на свой кошелёк.

Подпись запроса (V5, HMAC): sign = HMAC_SHA256(secret, timestamp + api_key + recv_window + payload), где payload —
строка запроса для GET или тело JSON для POST. Ответ: {"retCode": 0, "retMsg": "OK", "result": {...}}.

Безопасность:
  * ключ API создаётся с правами «Спот — торговля» и «Активы — перевод/вывод», с привязкой к IP компьютера;
  * вывод TON разрешён только на адреса из белого списка Bybit (адресная книга) — добавляете их вы сами;
  * клиент дополнительно отказывается выводить на адрес, которого нет в allowed_addresses из настроек.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from urllib.parse import urlencode

import aiohttp


class BybitError(Exception):
    def __init__(self, text: str, code: int | str = "bybit", *, sent: bool = False):
        super().__init__(text)
        self.code = code
        self.sent = sent          # запрос мог дойти до биржи (сетевая ошибка после отправки) — повторять нельзя


@dataclass
class Ticker:
    bid: float
    ask: float
    last: float


@dataclass
class Instrument:
    base_precision: str = "0.01"
    tick_size: str = "0.0001"
    min_order_qty: float = 0.0
    min_order_amt: float = 0.0


@dataclass
class Balances:
    """Остатки по счетам: unified (торговый) и fund (финансирование: сюда приходят покупки P2P и депозиты)."""
    unified: dict[str, float] = field(default_factory=dict)
    fund: dict[str, float] = field(default_factory=dict)

    def get(self, account: str, coin: str) -> float:
        return float((self.unified if account == "UNIFIED" else self.fund).get(coin, 0.0))

    def total(self, coin: str) -> float:
        return self.get("UNIFIED", coin) + self.get("FUND", coin)


@dataclass
class OrderState:
    order_id: str
    status: str               # New / PartiallyFilled / Filled / Cancelled / PartiallyFilledCanceled / Rejected
    qty: float                # исполнено, базовая монета (TON)
    avg_price: float
    fee: float = 0.0

    @property
    def final(self) -> bool:
        return self.status in ("Filled", "Cancelled", "PartiallyFilledCanceled", "Rejected", "Deactivated")


@dataclass
class WithdrawState:
    withdraw_id: str
    status: str               # success / fail / Pending / … (как отдаёт Bybit)
    amount: float
    fee: float
    tx_id: str = ""

    @property
    def done(self) -> bool:
        return self.status.lower() in ("success", "successful")

    @property
    def failed(self) -> bool:
        return self.status.lower() in ("fail", "failed", "reject", "rejected", "cancelbyuser", "cancelled")


def round_down(value: float, step: str) -> str:
    """Округляет вниз до шага биржи и отдаёт строкой (Bybit принимает числа строками)."""
    q = Decimal(str(step)).normalize()
    v = (Decimal(str(value)) / q).to_integral_value(rounding=ROUND_DOWN) * q
    return format(v.normalize() if v != 0 else Decimal(0), "f")


def sign(secret: str, ts: str, key: str, recv: str, payload: str) -> str:
    return hmac.new(secret.encode(), (ts + key + recv + payload).encode(), hashlib.sha256).hexdigest()


class BybitClient:
    def __init__(self, api_key: str, api_secret: str, *, base_url: str = "https://api.bybit.com",
                 recv_window: int = 10000, allowed_addresses: list[str] | None = None, dry_run: bool = False):
        self.key = api_key or ""
        self.secret = api_secret or ""
        self.base = (base_url or "https://api.bybit.com").rstrip("/")
        self.recv = str(int(recv_window))
        self.allowed = {a.strip() for a in (allowed_addresses or []) if a and a.strip()}
        self.dry_run = dry_run
        self._session: aiohttp.ClientSession | None = None
        self._time_offset_ms = 0

    @property
    def configured(self) -> bool:
        return bool(self.key and self.secret)

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _call(self, method: str, path: str, params: dict | None = None, *, auth: bool = True,
                    mutating: bool = False) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"Content-Type": "application/json"}
        if method == "GET":
            payload = urlencode(params)
            url = f"{self.base}{path}" + (f"?{payload}" if payload else "")
            body = None
        else:
            payload = json.dumps(params, separators=(",", ":"))
            url, body = f"{self.base}{path}", payload
        if auth:
            if not self.configured:
                raise BybitError("ключ API Bybit не задан", "config")
            ts = str(int(time.time() * 1000) + self._time_offset_ms)
            headers.update({"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": self.recv,
                            "X-BAPI-SIGN": sign(self.secret, ts, self.key, self.recv, payload)})
        s = await self._sess()
        try:
            async with s.request(method, url, data=body, headers=headers) as r:
                text = await r.text()
        except Exception as e:  # noqa: BLE001
            raise BybitError(f"Bybit недоступен: {e}", "network", sent=mutating) from e
        try:
            data = json.loads(text)
        except ValueError as e:
            raise BybitError(f"Bybit ответил не JSON (HTTP {r.status}): {text[:200]}", "http", sent=mutating) from e
        code = data.get("retCode")
        if code == 10002 and auth:            # время компьютера расходится с биржей — подстроимся и повторим
            await self.sync_time()
            return await self._call(method, path, params, auth=auth, mutating=mutating)
        if code not in (0, "0"):
            raise BybitError(f"Bybit: {data.get('retMsg') or 'ошибка'} (код {code})", code)
        return data.get("result") or {}

    async def sync_time(self) -> None:
        res = await self._call("GET", "/v5/market/time", auth=False)
        server_ms = int(int(res.get("timeNano") or 0) / 1e6) or int(res.get("timeSecond") or 0) * 1000
        if server_ms:
            self._time_offset_ms = server_ms - int(time.time() * 1000)

    # ------------------------------------------------------------------ рынок
    async def ticker(self, symbol: str = "TONUSDT") -> Ticker:
        res = await self._call("GET", "/v5/market/tickers", {"category": "spot", "symbol": symbol}, auth=False)
        row = (res.get("list") or [{}])[0]
        last = float(row.get("lastPrice") or 0)
        return Ticker(bid=float(row.get("bid1Price") or last), ask=float(row.get("ask1Price") or last), last=last)

    async def instrument(self, symbol: str = "TONUSDT") -> Instrument:
        res = await self._call("GET", "/v5/market/instruments-info", {"category": "spot", "symbol": symbol},
                               auth=False)
        row = (res.get("list") or [{}])[0]
        lot, pf = row.get("lotSizeFilter") or {}, row.get("priceFilter") or {}
        return Instrument(base_precision=str(lot.get("basePrecision") or "0.01"),
                          tick_size=str(pf.get("tickSize") or "0.0001"),
                          min_order_qty=float(lot.get("minOrderQty") or 0),
                          min_order_amt=float(lot.get("minOrderAmt") or 0))

    async def coin_chain(self, coin: str = "TON", chain: str = "TON") -> dict:
        """Комиссия и минимум вывода для сети: {'withdraw_fee': 0.05, 'withdraw_min': 0.5, 'can_withdraw': True}."""
        res = await self._call("GET", "/v5/asset/coin/query-info", {"coin": coin})
        for row in res.get("rows") or []:
            for c in row.get("chains") or []:
                if str(c.get("chain", "")).upper() == chain.upper():
                    return {"withdraw_fee": float(c.get("withdrawFee") or 0),
                            "withdraw_min": float(c.get("withdrawMin") or 0),
                            "can_withdraw": str(c.get("chainWithdraw", "1")) == "1"}
        return {"withdraw_fee": 0.0, "withdraw_min": 0.0, "can_withdraw": False}

    # ------------------------------------------------------------------ балансы
    async def balances(self, coins: tuple[str, ...] = ("USDT", "TON")) -> Balances:
        b = Balances()
        res = await self._call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": ",".join(coins)})
        for acc in res.get("list") or []:
            for c in acc.get("coin") or []:
                free = float(c.get("walletBalance") or 0) - float(c.get("locked") or 0)
                b.unified[str(c.get("coin"))] = max(0.0, free)
        res = await self._call("GET", "/v5/asset/transfer/query-account-coins-balance",
                               {"accountType": "FUND", "coin": ",".join(coins)})
        for c in res.get("balance") or []:
            b.fund[str(c.get("coin"))] = float(c.get("transferBalance") or c.get("walletBalance") or 0)
        return b

    # ------------------------------------------------------------------ торговля
    async def buy_ton(self, qty: str, limit_price: str, symbol: str = "TONUSDT", link_id: str = "") -> str:
        """Лимитная покупка IOC: исполняется сразу по лучшим ценам, но не дороже limit_price; остаток отменяется."""
        if self.dry_run:
            return "dry-" + uuid.uuid4().hex[:12]
        res = await self._call("POST", "/v5/order/create", {
            "category": "spot", "symbol": symbol, "side": "Buy", "orderType": "Limit", "qty": qty,
            "price": limit_price, "timeInForce": "IOC", "orderLinkId": link_id or uuid.uuid4().hex[:30]},
            mutating=True)
        return str(res.get("orderId") or "")

    async def order(self, order_id: str, symbol: str = "TONUSDT") -> OrderState:
        res = await self._call("GET", "/v5/order/realtime", {"category": "spot", "symbol": symbol, "orderId": order_id})
        rows = res.get("list") or []
        if not rows:
            res = await self._call("GET", "/v5/order/history", {"category": "spot", "symbol": symbol,
                                                                  "orderId": order_id})
            rows = res.get("list") or []
        if not rows:
            raise BybitError(f"ордер {order_id} не найден", "not_found")
        r = rows[0]
        return OrderState(order_id=order_id, status=str(r.get("orderStatus") or ""),
                          qty=float(r.get("cumExecQty") or 0), avg_price=float(r.get("avgPrice") or 0),
                          fee=float(r.get("cumExecFee") or 0))

    async def transfer(self, coin: str, amount: str, src: str, dst: str) -> str:
        if self.dry_run:
            return "dry-" + uuid.uuid4().hex[:12]
        tid = str(uuid.uuid4())
        res = await self._call("POST", "/v5/asset/transfer/inter-transfer", {
            "transferId": tid, "coin": coin, "amount": amount, "fromAccountType": src, "toAccountType": dst},
            mutating=True)
        return str(res.get("transferId") or tid)

    async def withdraw(self, coin: str, chain: str, address: str, amount: str, memo: str = "") -> str:
        if self.allowed and address not in self.allowed:
            raise BybitError(f"адрес {address} не в списке разрешённых для вывода", "address")
        if self.dry_run:
            return "dry-" + uuid.uuid4().hex[:12]
        res = await self._call("POST", "/v5/asset/withdraw/create", {
            "coin": coin, "chain": chain, "address": address, "tag": memo or None, "amount": amount,
            "timestamp": int(time.time() * 1000), "forceChain": 1, "accountType": "FUND"}, mutating=True)
        return str(res.get("id") or "")

    async def withdraw_state(self, withdraw_id: str) -> WithdrawState:
        res = await self._call("GET", "/v5/asset/withdraw/query-record", {"withdrawID": withdraw_id})
        rows = res.get("rows") or []
        if not rows:
            return WithdrawState(withdraw_id, "Pending", 0.0, 0.0)
        r = rows[0]
        return WithdrawState(withdraw_id, str(r.get("status") or ""), float(r.get("amount") or 0),
                             float(r.get("withdrawFee") or 0), str(r.get("txID") or ""))
