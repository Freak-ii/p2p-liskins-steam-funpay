"""Эмулятор Ozon Seller API (цифровые товары) для демо и тестов: тот же интерфейс, что у OzonClient."""
from __future__ import annotations

import itertools
import time

from starsbot.ozon.client import OzonError, iso, parse_posting

_num = itertools.count(1)


class FakeOzon:
    def __init__(self):
        self.dry_run = False
        self.postings: dict[str, dict] = {}       # «сырые» ответы в формате /v2/posting/digital/list
        self.uploaded: dict[str, list[dict]] = {}
        self.stocks: dict[str, int] = {}
        self.fail_uploads = 0                       # сколько следующих загрузок кодов «упадут»
        self.commission_pct = 10.0

    # --- симуляция покупателя
    def buyer_buys(self, offer_id: str, stars: int, qty: int = 1, price: float | None = None, sku: int | None = None,
                   name: str | None = None) -> str:
        n = next(_num)
        number = f"{40000000 + n}-0001-1"
        price = price if price is not None else round(stars * 1.55, 2)
        sku = sku or 900000 + stars
        payout = round(price * qty * (1 - self.commission_pct / 100), 2)
        self.postings[number] = {
            "posting_number": number, "order_number": f"{40000000 + n}-0001", "order_id": n,
            "status": "awaiting_packaging", "created_at": iso(time.time()), "in_process_at": iso(time.time()),
            "waiting_deadline_for_digital_code": iso(time.time() + 3600),
            "products": [{"sku": sku, "offer_id": offer_id, "name": name or f"Telegram Stars {stars} звёзд",
                          "quantity": qty, "price": {"amount": price, "currency_code": "RUB"},
                          "required_qty_for_digital_code": qty}],
            "financial_data": {"products": [{"product_id": sku, "price": price, "payout": payout,
                                             "commission": {"amount": round(price * qty - payout, 2),
                                                            "percent": int(self.commission_pct)}}]},
        }
        return number

    def cancel(self, number: str, initiator: str = "Покупатель") -> None:
        p = self.postings[number]
        p["status"] = "cancelled"
        p["cancellation"] = {"cancellation_type": "client", "cancellation_initiator": initiator}

    def code_of(self, number: str, i: int = 0) -> str:
        """Код, который Ozon показал бы покупателю в заказе."""
        keys = [k for item in self.uploaded.get(number, []) for k in item.get("exemplar_keys", [])]
        return keys[i]

    # --- интерфейс клиента
    async def list_postings(self, since: float, to: float | None = None, limit: int = 100):
        return [parse_posting(p) for p in self.postings.values()]

    async def upload_codes(self, posting_number: str, items: list[dict]) -> list[dict]:
        if self.fail_uploads > 0:
            self.fail_uploads -= 1
            raise OzonError("Ozon 503: эмуляция сбоя", "network")
        p = self.postings[posting_number]
        if p["status"] == "cancelled":
            raise OzonError("Ozon 400: posting is cancelled", "api")
        self.uploaded.setdefault(posting_number, []).extend(items)
        out = []
        for i in items:
            for prod in p["products"]:
                if int(prod["sku"]) == int(i["sku"]):
                    prod["required_qty_for_digital_code"] = 0
            out.append({"sku": i["sku"], "received_qty": len(i.get("exemplar_keys") or []), "rejected_qty": 0,
                        "failed_exemplars": []})
        if not any(pr["required_qty_for_digital_code"] for pr in p["products"]):
            p["status"] = "delivered"
        return out

    async def import_stocks(self, stocks: dict[str, int]) -> list[dict]:
        self.stocks.update({k: int(v) for k, v in stocks.items()})
        return [{"offer_id": k, "updated": True, "errors": []} for k in stocks]

    async def close(self) -> None:
        return None


DEMO_PRODUCTS = [{"stars": n, "offer_id": f"tg-stars-{n}"} for n in (50, 100, 200, 250, 500)]


async def run_ozon_demo(engine, wallet) -> dict:
    """Демо канала Ozon через настоящий движок: активированный код, код в ожидании и отменённый заказ."""
    from starsbot.ozon.tgbot import ActivationBot
    ch = engine.channels["ozon"]
    oz: FakeOzon = ch.client
    if ch.bot is None:
        ch.bot = ActivationBot("", ch)              # без токена: ответы покупателю видны в CRM
    engine.fragment.users.update({"olga_tg": "Ольга", "petr_tg": "Пётр"})

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

    a = oz.buyer_buys("tg-stars-100", 100, price=169.0)
    b = oz.buyer_buys("tg-stars-200", 200, price=329.0)
    c = oz.buyer_buys("tg-stars-50", 50, price=89.0)
    await ch.poll()
    await ch.bot.handle(7001, oz.code_of(a))                 # покупатель Ozon: код, потом username
    await ch.bot.handle(7001, "@olga_tg")
    await settle()
    oz.cancel(c)
    await ch.poll()
    await engine.wallet_watch()
    return {"postings": [a, b, c]}
