"""Демо TG Gift: эмулятор раздела FunPay «Подарки» и каталога подарков Telegram."""
from __future__ import annotations

import random

from starsbot.core.models import CompetitorLot, FunPayOrder
from starsbot.demo import FakeFunPay
from starsbot.gifts.planner import Gift
from starsbot.giftshop.nft import FakePublicFragment
from starsbot.giftshop.profile import DEFAULT_SHOP_LOTS, TOPUP_DENOMINATIONS
from starsbot.giftshop.service import lot_title


def shop_catalog() -> tuple[float, list[Gift]]:
    """Каталог как у Telegram: обычные подарки + один распроданный лимитированный."""
    import time
    gifts = [Gift(5000 + i, int(l["stars"]), int(int(l["stars"]) * 0.85), "", emoji=l["emoji"])
             for i, l in enumerate(DEFAULT_SHOP_LOTS)]
    gifts.append(Gift(5999, 350, 300, "Плюшевый Пепе", limited=True, sold_out=True, emoji="🐸"))
    return time.time(), gifts


# Демо-рынок NFT: (slug, название, цены на Fragment в TON, цены на рынке Telegram в ⭐, цена у конкурентов FunPay ₽)
DEMO_NFT = [
    ("lolpop", "Lol Pop", [5.0, 5.2, 5.6], [300, 320], 540),
    ("icecream", "Ice Cream", [5.0, 5.3], [290], 520),
    ("toybear", "Toy Bear", [43.0, 44.0], [3000], 4400),
    ("snoopdogg", "Snoop Dogg", [7.0, 7.4], [400, 420], 690),
    ("winterwreath", "Winter Wreath", [6.0, 6.5], [380], 720),     # сезон: Новый год
    ("deskcalendar", "Desk Calendar", [4.0], [], 330),           # только Fragment, а StarsBot в демо не подключён
    ("plushpepe", "Plush Pepe", [7500.0], [], 800000),            # ультра-дорогой: только вручную
]


def demo_nft(tg) -> FakePublicFragment:
    """Каталог Telegram получает подарки-основы коллекций (для рынка Telegram), Fragment — свои цены."""
    cat = tg._catalog if isinstance(tg._catalog, list) else list(tg._catalog or [])
    for i, (slug, title, frag, market, _) in enumerate(DEMO_NFT):
        gid = 8000 + i
        cat.append(Gift(gid, 350, 300, title, limited=True, sold_out=True, resale=len(market),
                        resell_min=min(market) if market else 0))
        tg.resale[gid] = [{"slug": f"{slug.title()}-{100 + k}", "stars": st, "title": title}
                          for k, st in enumerate(market)]
    tg._catalog = cat
    return FakePublicFragment({slug: list(frag) for slug, _, frag, _, _ in DEMO_NFT})


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
        oid = self.buyer_pays("Max", 77, 50, method="По username")
        self.sales[oid].subcategory = "Telegram, Звёзды"
        return oid

    async def _stars_section(self) -> list[CompetitorLot]:
        out, n = [], 900
        for stars in TOPUP_DENOMINATIONS:
            for name, per in (("StarGift", 1.9), ("FastStars", 1.98), ("Zvezdy", 2.05)):
                n += 1
                out.append(CompetitorLot(offer_id=str(n), seller=name, amount=50, reviews=300,
                                         price=round(stars * per, 2), fields={"quantity": f"{stars} звёзд",
                                                                              "method": "подарком"},
                                         description=f"{stars} звёзд, Подарком"))
        for oid, l in self.lots.items():
            if str(l.get("node_id")) == "2418" and l.get("fields[summary][ru]"):
                out.append(CompetitorLot(offer_id=oid, seller=self.username, price=float(l.get("price") or 0) * 1.14,
                                         amount=int(l.get("amount") or 0), description=l["fields[summary][ru]"],
                                         fields={"quantity": l.get("fields[quantity]", ""), "method": "подарком"}))
        return out

    def buyer_buys_topup(self, buyer: str, buyer_id: int, stars: int, username: str) -> str:
        """Заказ из раздела «Звёзды» способом «Подарком» — его теперь ведёт TG Gift."""
        oid = self.buyer_pays(buyer, buyer_id, stars, method="Подарком", username=username)
        self.sales[oid].subcategory = "Telegram, Звёзды"
        return oid

    def buyer_buys_nft(self, buyer: str, buyer_id: int, lot: dict, username: str, price: float) -> str:
        oid = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        chat = str(500 + buyer_id)
        self.chat_users[chat] = buyer
        desc = f"{lot.get('name_en')} ({lot.get('name')}) — коллекционный NFT-подарок Telegram | Любая модель"
        self.sales[oid] = FunPayOrder(order_id=oid, buyer_username=buyer, buyer_id=buyer_id, description=desc,
                                      price=price, quantity=1, status="paid", chat_id=chat,
                                      subcategory="Telegram, Подарки")
        self.params[oid] = {"Краткое описание": desc, "Количество": "1 шт.", "Username получателя": username}
        self._sys(chat, f"Покупатель {buyer} оплатил заказ #{oid}. {desc}, 1 шт.")
        self._orders_tag += 1
        return oid

    async def get_lot_form(self, node_id, offer_id=0):
        if str(node_id) == "2418":
            base = {"offer_id": str(offer_id or 0), "node_id": "2418", "fields[quantity]": "",
                    "fields[quantity2]": "", "fields[method]": "", "fields[summary][ru]": "",
                    "fields[summary][en]": "", "fields[desc][ru]": "", "fields[desc][en]": "", "price": "",
                    "amount": "", "active": "on"}
            qty = [("", "Выберите")] + [(str(i), f"{n} звёзд") for i, n in enumerate(TOPUP_DENOMINATIONS, 1)]
            qty.append(("99", "Другое количество"))
            return base, {"fields[quantity]": qty,
                          "fields[method]": [("", "Выберите"), ("1", "По username"), ("2", "Подарком"),
                                             ("3", "С заходом на аккаунт")]}
        base = {"offer_id": str(offer_id or 0), "node_id": str(node_id), "fields[type]": "",
                "fields[summary][ru]": "", "fields[summary][en]": "", "fields[desc][ru]": "", "fields[desc][en]": "",
                "price": "", "amount": "", "active": "on"}
        return base, {"fields[type]": [("", "Выберите"), ("1", "Обычные подарки"), ("2", "NFT (улучшенные)"),
                                       ("10", "Мишка"), ("11", "Леденец"), ("12", "Мороженое"), ("13", "Венок"),
                                       ("90", "Прочее")],
                      "fields[type2]": [("", "Выберите"), ("1", "Продажа"), ("2", "Аренда")]}

    async def create_lot(self, fields, force=False):
        new_id = str(7000 + len(self.created) + 1)
        self.created.append(dict(fields))
        self.lots[new_id] = {**fields, "offer_id": new_id}
        return new_id

    async def get_public_lots(self, node_id, filter_regex=None):
        if str(node_id) == "2418":
            return await self._stars_section()
        out, n = [], 100
        for slug, title, _, _, price in DEMO_NFT:
            for k, (name, per) in enumerate((("NftLord", 1.0), ("GiftKing", 1.04), ("TgPresents", 1.09))):
                n += 1
                out.append(CompetitorLot(offer_id=str(n), seller=name, amount=3, reviews=120 + k,
                                         price=round(price * per, 2), fields={"type2": "продажа"},
                                         description=f"【{title}】 NFT подарок Telegram #ВСЕ ВИДЫ"))
            n += 1
            out.append(CompetitorLot(offer_id=str(n), seller="RentMe", amount=9, reviews=40, price=round(price * 0.2),
                                     fields={"type2": "аренда"}, description=f"{title} аренда на 7 дней"))
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
    # раздел «Звёзды → Подарком»: номинал набором подарков
    c = fp.buyer_buys_topup("Mia", 44, 43, "mia_stars")
    await drain()
    # NFT: подбор ассортимента по рынку и заказ самого выгодного
    out = [a, b, c]
    if getattr(g, "assortment", None) is not None:
        await g.assortment.scan(force=True)
        nft = [l for l in g.nft_lots if l.get("offer_id") and l.get("sell", True)]
        await g.watch()
        if nft:
            out.append(fp.buyer_buys_nft("Ned", 45, nft[0], "ned_nft", price=560.0))
            await drain()
            await g.watch()
    await engine.rtba_step()
    return out
