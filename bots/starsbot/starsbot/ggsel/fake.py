"""Эмулятор GGSEL Seller API для демо и тестов: тот же интерфейс, что у GgselClient."""
from __future__ import annotations

import itertools
import time

from starsbot.ggsel.client import COMPLETED, PAID, REFUNDED, GgselError, GgselMessage, GgselSale, parse_order

_inv = itertools.count(1)


class FakeGgsel:
    def __init__(self):
        self.dry_run = False
        self.orders: dict[int, dict] = {}            # «сырые» ответы purchase/info
        self.products: dict[int, int] = {}           # invoice → product_id
        self.chats: dict[int, list[dict]] = {}       # invoice → сообщения
        self.unread: set[int] = set()
        self.offers: dict[int, dict] = {}            # product_id → последние PATCH-поля
        self.fail_next = 0

    # --- симуляция покупателя
    def buyer_buys(self, product_id: int, amount: float, username: str | None = "", qty: int = 1,
                   option_name: str = "Username в Telegram (например, @username)") -> int:
        inv = 70000000 + next(_inv)
        opts = [{"id": 1, "name": option_name, "user_data": username}] if username is not None else []
        self.orders[inv] = {"retval": 0, "content": {
            "invoice_state": PAID, "item_id": product_id, "amount": amount, "currency_type": "RUB",
            "cnt_goods": qty, "options": opts, "purchase_date": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "buyer_info": {"email": f"buyer{inv}@example.com"}}}
        self.products[inv] = product_id
        self.chats[inv] = []
        return inv

    def buyer_says(self, inv: int, text: str) -> None:
        msgs = self.chats.setdefault(inv, [])
        msgs.append({"id": len(msgs) + 1, "message": text, "buyer": 1, "seller": 0,
                     "date_written": time.strftime("%Y-%m-%d %H:%M:%S")})
        self.unread.add(inv)

    def confirm(self, inv: int) -> None:
        self.orders[inv]["content"]["invoice_state"] = COMPLETED

    def refund(self, inv: int) -> None:
        self.orders[inv]["content"]["invoice_state"] = REFUNDED

    def seller_texts(self, inv: int) -> list[str]:
        return [m["message"] for m in self.chats.get(inv, []) if m["seller"]]

    def _maybe_fail(self) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise GgselError("GGSEL 503: эмуляция сбоя", "network")

    # --- интерфейс клиента
    async def last_sales(self, top: int = 50):
        self._maybe_fail()
        invs = sorted(self.orders, reverse=True)[:top]
        return [GgselSale(invoice_id=i, product_id=self.products[i]) for i in invs]

    async def order(self, invoice_id: int):
        self._maybe_fail()
        if invoice_id not in self.orders:
            raise GgselError("GGSEL: счёт не найден", "api")
        return parse_order(invoice_id, self.orders[invoice_id])

    async def chats_with_new(self):
        out = sorted(self.unread)
        self.unread.clear()
        return out

    async def messages(self, invoice_id: int, count: int = 50):
        return [GgselMessage(id=m["id"], text=m["message"], from_buyer=bool(m["buyer"]) and not m["seller"])
                for m in self.chats.get(invoice_id, [])][-count:]

    async def send(self, invoice_id: int, text: str) -> None:
        self._maybe_fail()
        msgs = self.chats.setdefault(invoice_id, [])
        msgs.append({"id": len(msgs) + 1, "message": text, "buyer": 0, "seller": 1, "date_written": ""})

    async def update_offer(self, offer_id: int, **fields) -> dict:
        self.offers.setdefault(int(offer_id), {}).update(fields)
        return {"id": offer_id, **fields}

    async def offer(self, offer_id: int) -> dict:
        return {"id": offer_id, **self.offers.get(int(offer_id), {})}

    async def close(self) -> None:
        return None


DEMO_PRODUCTS = [{"stars": n, "offer_id": str(5100000 + n)} for n in (50, 100, 250, 500)]
DEMO_PREMIUM_PRODUCTS = [{"months": m, "offer_id": str(5200000 + m)} for m in (3, 6, 12)]


async def run_ggsel_demo(engine, wallet) -> dict:
    """Демо GGSEL через настоящий движок: username из формы, username в чате, ошибка в username и возврат."""
    ch = engine.channels["ggsel"]
    gg: FakeGgsel = ch.client
    engine.fragment.users.update({"nika_tg": "Ника", "ivan_tg": "Иван", "lena_tg": "Лена"})
    engine.db.kv_set("ggsel_initialized", True)        # в демо нет «старых» продаж

    async def settle() -> None:
        for _ in range(3):
            while not engine.queue.empty():
                oid = engine.queue.get_nowait()
                engine._queued.discard(oid)
                await engine.purchase(oid)
                wallet.tick()
                await engine.watch_purchases()
            wallet.tick()
            await engine.watch_purchases()

    a = gg.buyer_buys(5100100, 175.0, "@nika_tg")                 # username указан при оформлении
    b = gg.buyer_buys(5100250, 429.0, "")                          # поле пустое — бот спросит в чате
    c = gg.buyer_buys(5100050, 89.0, "иван тг")                    # ошибка в username
    d = gg.buyer_buys(5100500, 849.0, "")                          # покупатель передумал до выдачи
    await ch.poll()
    await settle()
    gg.buyer_says(b, "@lena_tg")
    gg.buyer_says(c, "ой, правильно @ivan_tg")
    gg.buyer_says(a, "а когда придут?")
    ch.c.refresh_seconds = 0
    await ch.poll()
    await settle()
    gg.confirm(a)
    gg.refund(d)
    await ch.poll()
    invoices = [a, b, c, d]
    if getattr(engine, "premium", None) is not None and engine.premium.enabled and \
            any(p.get("months") for p in ch.products()):
        e = gg.buyer_buys(5200006, 990.0, "@nika_tg")                  # Telegram Premium на 6 месяцев
        await ch.poll()
        await settle()
        invoices.append(e)
    await engine.wallet_watch()
    return {"invoices": invoices}
