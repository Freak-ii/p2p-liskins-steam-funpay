"""Эмуляторы для демо и тестов: биржа Bybit и кошелёк-хаб. Деньги «приходят» на следующем tick()."""
from __future__ import annotations

import itertools
import time

from starsbot.core.models import SentTx, TonMessage
from starsbot.treasury.bybit import Balances, BybitError, Instrument, OrderState, Ticker, WithdrawState
from starsbot.ton.wallet import TonError, from_nano, to_nano

_ids = itertools.count(1)


class FakeBybit:
    configured = True

    def __init__(self, usdt_fund: float = 0.0, usdt_unified: float = 1500.0, price: float = 1.21,
                 withdraw_fee: float = 0.05, wallets: dict | None = None):
        self.bal = Balances(unified={"USDT": usdt_unified, "TON": 0.0}, fund={"USDT": usdt_fund, "TON": 0.0})
        self.price = price
        self.withdraw_fee = withdraw_fee
        self.wallets = wallets if wallets is not None else {}   # адрес → объект с .balance в нанотонах
        self.orders: dict[str, OrderState] = {}
        self.withdrawals: dict[str, dict] = {}
        self.fail_next_withdraw: str = ""        # "network" — запрос «ушёл», ответа нет
        self.calls: list[tuple] = []

    async def ticker(self, symbol="TONUSDT") -> Ticker:
        return Ticker(bid=round(self.price * 0.9995, 4), ask=round(self.price * 1.0005, 4), last=self.price)

    async def instrument(self, symbol="TONUSDT") -> Instrument:
        return Instrument(base_precision="0.01", tick_size="0.0001", min_order_qty=0.1, min_order_amt=1)

    async def coin_chain(self, coin="TON", chain="TON") -> dict:
        return {"withdraw_fee": self.withdraw_fee, "withdraw_min": 1.0, "can_withdraw": True}

    async def balances(self, coins=("USDT", "TON")) -> Balances:
        return Balances(unified=dict(self.bal.unified), fund=dict(self.bal.fund))

    async def buy_ton(self, qty: str, limit_price: str, symbol="TONUSDT", link_id="") -> str:
        q, ask = float(qty), self.price * 1.0005
        if ask > float(limit_price):
            oid = f"o{next(_ids)}"
            self.orders[oid] = OrderState(oid, "Cancelled", 0.0, 0.0)
            return oid
        cost = q * ask
        if cost > self.bal.unified.get("USDT", 0) + 1e-9:
            raise BybitError("Insufficient balance (демо)", 170131)
        self.bal.unified["USDT"] -= cost
        fee = q * 0.001
        self.bal.unified["TON"] = self.bal.unified.get("TON", 0) + q - fee
        oid = f"o{next(_ids)}"
        self.orders[oid] = OrderState(oid, "Filled", q, round(ask, 6), fee)
        self.calls.append(("buy", q, ask))
        return oid

    async def order(self, order_id: str, symbol="TONUSDT") -> OrderState:
        return self.orders[order_id]

    async def transfer(self, coin: str, amount: str, src: str, dst: str) -> str:
        a = float(amount)
        s = self.bal.unified if src == "UNIFIED" else self.bal.fund
        d = self.bal.unified if dst == "UNIFIED" else self.bal.fund
        if s.get(coin, 0) + 1e-9 < a:
            raise BybitError("Insufficient balance (демо)", 131212)
        s[coin] -= a
        d[coin] = d.get(coin, 0) + a
        self.calls.append(("transfer", coin, a, src, dst))
        return f"t{next(_ids)}"

    async def withdraw(self, coin: str, chain: str, address: str, amount: str, memo: str = "") -> str:
        a = float(amount)
        if self.fail_next_withdraw == "network":
            self.fail_next_withdraw = ""
            raise BybitError("Bybit недоступен: timeout (демо)", "network", sent=True)
        if self.bal.fund.get(coin, 0) + 1e-9 < a:
            raise BybitError("Insufficient balance (демо)", 131001)
        self.bal.fund[coin] -= a
        wid = f"w{next(_ids)}"
        self.withdrawals[wid] = {"address": address, "amount": a, "status": "Pending", "at": time.time()}
        self.calls.append(("withdraw", a, address))
        return wid

    async def withdraw_state(self, withdraw_id: str) -> WithdrawState:
        w = self.withdrawals[withdraw_id]
        return WithdrawState(withdraw_id, w["status"], w["amount"] - self.withdraw_fee, self.withdraw_fee,
                             "tx" + withdraw_id if w["status"] == "success" else "")

    def tick(self) -> None:
        """Выводы доходят до кошелька получателя."""
        for w in self.withdrawals.values():
            if w["status"] == "Pending":
                w["status"] = "success"
                dest = self.wallets.get(w["address"])
                if dest is not None:
                    dest.balance += to_nano(w["amount"] - self.withdraw_fee)

    async def close(self) -> None:
        return None


class FakeHub:
    """Кошелёк-хаб: перевод доходит до кошелька бота на следующем tick()."""

    version = "v5r1"

    def __init__(self, balance: float = 0.0, address: str = "UQHUB000000000000000000000000000000000000000000-demo",
                 wallets: dict | None = None):
        self.balance = to_nano(balance)
        self.friendly_address = address
        self.wallets = wallets if wallets is not None else {}
        self.seqno = 1
        self.pending: list[dict] = []
        self.history: list[dict] = []

    async def balance_ton(self) -> float:
        return from_nano(self.balance)

    async def transfer(self, messages: list[TonMessage], valid_until: int, *, on_signed=None) -> SentTx:
        total = sum(m.amount for m in messages)
        if self.balance < total + to_nano(0.02):
            raise TonError(f"на хабе {from_nano(self.balance):.4f} TON, нужно {from_nano(total):.4f}",
                           "insufficient_funds")
        h = f"hub{next(_ids):061x}"
        self.pending.append({"messages": messages, "hash": h})
        return SentTx(seqno=self.seqno, valid_until=valid_until, msg_hash=h, boc="")

    def tick(self) -> None:
        for p in self.pending:
            fee = to_nano(0.004)
            self.balance -= sum(m.amount for m in p["messages"]) + fee
            self.seqno += 1
            for m in p["messages"]:
                self.history.append({"dest": m.address, "value": m.amount, "hash": p["hash"], "utime": time.time(),
                                     "fee": fee})
                dest = self.wallets.get(m.address)
                if dest is not None:
                    dest.balance += m.amount
        self.pending = []

    async def find_outgoing(self, dest, amount, since, comment_part=""):
        for h in reversed(self.history):
            if h["dest"] == dest and h["value"] == int(amount) and h["utime"] >= since - 5:
                return {"hash": h["hash"], "utime": h["utime"], "fee": h["fee"], "comment": ""}
        return None

    async def close(self) -> None:
        return None


async def run_treasury_demo(sup) -> list[str]:
    """Демо: кошелёк бота почти пуст, на Bybit есть USDT, на Ozon и GGSEL накопились рубли.
    Казначейство покупает TON, выводит на хаб, хаб пополняет кошелёк бота; советник просит купить USDT на P2P и
    говорит, откуда взять рубли."""
    tr = sup.engine.treasury
    if tr is None or not tr.enabled:
        return []
    bybit, hub = getattr(sup, "demo_treasury", (None, None))
    w = sup.engine.wallet
    old = from_nano(w.balance)
    if old > 60 and sup.econ is not None:          # «вывели лишнее» — честно в журнал, чтобы сверка сходилась
        sup.econ.add_cashflow("withdraw_ton", 0, round(old - 60, 6), "демо: забрали TON с кошелька бота")
    w.balance = to_nano(60.0)
    tr.set_source_balance("ozon", 85000)
    tr.set_source_balance("ggsel", 23000)
    log: list[str] = []
    for _ in range(12):
        await tr.tick()
        if tr.snap.get("plan"):
            log.append(tr.snap["plan"])
        if bybit is not None:
            bybit.tick()
        if hub is not None:
            hub.tick()
        w.tick()
    await sup.engine.wallet_watch()
    return log
