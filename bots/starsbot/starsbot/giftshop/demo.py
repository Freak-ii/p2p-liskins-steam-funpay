"""Демо TG Gift: эмулятор раздела FunPay «Подарки» и каталога подарков Telegram."""
from __future__ import annotations

import random

from starsbot.core.models import CompetitorLot, FunPayOrder
from starsbot.demo import FakeFunPay
from starsbot.gifts.planner import Gift
from starsbot.giftshop.profile import DEFAULT_SHOP_LOTS
from starsbot.giftshop.service import lot_title


def shop_catalog() -> tuple[float, list[Gift]]:
    """Каталог как у Telegram: обычные подарки + один распроданный лимитированный."""
    import time
    gifts = [Gift(5000 + i, int(l["stars"]), int(int(l["stars"]) * 0.85), "", emoji=l["emoji"])
             for i, l in enumerate(DEFAULT_SHOP_LOTS)]
    gifts.append(Gift(5999, 350, 300, "Плюшевый Пепе", limited=True, sold_out=True, emoji="🐸"))
    return time.time(), gifts


class FakeShopFunPay(FakeFunPay):
    def __init__(self):
        super().__init__()
        self.username = "GiftSeller"
        self.created: list[dict] = []

    def _lot(self, offer_id) -> dict:
        oid = str(offer_id)
        return self.lots.setdefault(oid, {"offer_id": oid, "node_id": "3064", "price": "30.00", "amount": "1",
                                          "active": ""})

    def buyer_buys_gift(self, buyer: str, buyer_id: int, lot: dict, qty: int = 1, username: str | None = None,
                        price: float = 0.0) -> str:
        oid = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        chat = str(500 + buyer_id)
        self.chat_users[chat] = buyer
        desc = f"{lot_title(lot)} — подарок Telegram на ваш аккаунт | Автовыдача 1–5 мин | По username"
        self.sales[oid] = FunPayOrder(order_id=oid, buyer_username=buyer, buyer_id=buyer_id,
                                      description=desc + (f", {qty} шт." if qty > 1 else ""),
                                      price=round(price or int(lot["stars"]) * 1.9 * qty, 2), quantity=qty,
                                      status="paid", chat_id=chat, subcategory="Telegram, Подарки")
        self.params[oid] = {"Краткое описание": desc, "Количество": f"{qty} шт."}
        if username:
            self.params[oid]["Username получателя"] = username
        self._sys(chat, f"Покупатель {buyer} оплатил заказ #{oid}. {desc}, {qty} шт.")
        self._orders_tag += 1
        return oid

    def stars_order_elsewhere(self) -> str:
        """Заказ звёзд (раздел «Звёзды») — его ведёт StarsBot, TG Gift его не трогает."""
        oid = self.buyer_pays("Max", 77, 50, method="Подарком")
        self.sales[oid].subcategory = "Telegram, Звёзды"
        return oid

    async def get_lot_form(self, node_id, offer_id=0):
        base = {"offer_id": str(offer_id or 0), "node_id": str(node_id), "fields[type]": "",
                "fields[summary][ru]": "", "fields[summary][en]": "", "fields[desc][ru]": "", "fields[desc][en]": "",
                "price": "", "amount": "", "active": "on"}
        return base, {"fields[type]": [("", "Выберите"), ("1", "Обычные подарки"), ("2", "NFT (улучшенные)")]}

    async def create_lot(self, fields, force=False):
        new_id = str(7000 + len(self.created) + 1)
        self.created.append(dict(fields))
        self.lots[new_id] = {**fields, "offer_id": new_id}
        return new_id

    async def get_public_lots(self, node_id, filter_regex=None):
        out, n = [], 100
        rivals = [("GiftKing", 1.75, 50), ("TgPresents", 1.82, 12), ("Cheapo", 1.2, 1), ("BearShop", 1.9, 30)]
        for lot in DEFAULT_SHOP_LOTS:
            for name, per, amount in rivals:
                n += 1
                out.append(CompetitorLot(offer_id=str(n), seller=name, amount=amount,
                                         price=round(int(lot["stars"]) * per * random.uniform(0.98, 1.02), 2),
                                         description=f"{lot['emoji']} {lot['name']} подарок Telegram"))
        for oid, l in self.lots.items():
            if l.get("fields[summary][ru]"):
                out.append(CompetitorLot(offer_id=oid, seller=self.username, price=float(l.get("price") or 0) * 1.14,
                                         amount=int(l.get("amount") or 0), description=l["fields[summary][ru]"]))
        return out


async def run_shop_demo(engine, fp: FakeShopFunPay) -> list[str]:
    """Заказы подарков: username в заказе, username в чате, два подарка в одном заказе, чужой заказ звёзд."""
    async def drain() -> None:
        for _ in range(3):
            await engine.funpay_poll()
            while not engine.queue.empty():
                oid = engine.queue.get_nowait()
                engine._queued.discard(oid)
                await engine.purchase(oid)

    g = engine.gifts
    await engine.funpay_poll()             # первый опрос: старые заказы и чаты не трогаем
    await g.watch()
    lots = [l for l in g.lots if l.get("offer_id")] or g.lots
    by_name = {l["name"]: l for l in lots}
    a = fp.buyer_buys_gift("Kira", 41, by_name.get("Мишка", lots[0]), username="kira_tg")
    b = fp.buyer_buys_gift("Leo", 42, by_name.get("Роза", lots[0]), qty=2)
    fp.stars_order_elsewhere()
    await drain()
    fp.buyer_says(42, "@leo_gifts")
    await drain()
    fp.buyer_says(43, "Здравствуйте, а мишки есть?")
    await drain()
    fp.buyer_confirms(a)
    await drain()
    await g.watch()
    await engine.rtba_step()
    return [a, b]
