"""Демо/симуляция: эмуляторы FunPay и Steam с тем же интерфейсом, что у настоящих клиентов.

Запуск: python -m mannbot --demo        (сценарий в консоли + веб-панель)
        python -m mannbot --demo --no-web
"""
from __future__ import annotations

import asyncio
import itertools
import random
import time

from mannbot.core.models import ChatMessage, CompetitorLot, FunPayOrder, SentOffer, TradeLinkCheck, \
    TradeOfferState as T
from mannbot.funpay.parsers import ChatBookmark, OrderPage
from mannbot.steam.client import SteamError
from mannbot.steam.tradelink import TradeLink

_ids = itertools.count(1000)


class FakeFunPay:
    def __init__(self):
        self.user_id = 1
        self.username = "MannSeller"
        self.sales: dict[str, FunPayOrder] = {}
        self.chats: dict[str, list[ChatMessage]] = {}
        self.chat_users: dict[str, str] = {}
        self.outbox: list[tuple[str, str]] = []
        self.lot = {"offer_id": "1", "node_id": "1808", "price": "190.00", "amount": "10", "active": "on"}
        self._orders_tag = 0
        self.refunds: list[str] = []

    async def login(self):
        return None

    # --- симуляция действий покупателя
    def buyer_pays(self, buyer: str, buyer_id: int, qty: int, price_per: float = 185.0) -> str:
        oid = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        chat = str(500 + buyer_id)
        self.chat_users[chat] = buyer
        self.sales[oid] = FunPayOrder(order_id=oid, buyer_username=buyer, buyer_id=buyer_id,
                                      description=f"Mann Co. Supply Crate Key, {qty} шт.", price=qty * price_per,
                                      quantity=qty, status="paid", chat_id=chat)
        self._sys(chat, f"Покупатель {buyer} оплатил заказ #{oid}. Mann Co. Supply Crate Key, {qty} шт.")
        self._orders_tag += 1
        return oid

    def buyer_says(self, buyer_id: int, text: str) -> None:
        chat = str(500 + buyer_id)
        self.chats.setdefault(chat, []).append(ChatMessage(next(_ids), chat, buyer_id, self.chat_users[chat], text))

    def buyer_confirms(self, oid: str) -> None:
        so = self.sales[oid]
        so.status = "closed"
        self._sys(str(500 + so.buyer_id), f"Покупатель {so.buyer_username} подтвердил успешное выполнение заказа "
                                          f"#{oid} и отправил деньги продавцу {self.username}.")
        self._orders_tag += 1

    def _sys(self, chat: str, text: str) -> None:
        self.chats.setdefault(chat, []).append(ChatMessage(next(_ids), chat, 0, "FunPay", text, is_system=True))

    # --- интерфейс клиента
    async def poll_updates(self):
        bms = [ChatBookmark(c, self.chat_users.get(c, "?"), msgs[-1].message_id, msgs[-1].text, True)
               for c, msgs in self.chats.items() if msgs]
        changed, self._orders_tag = self._orders_tag > 0, 0
        return {"orders_changed": changed, "bookmarks": bms, "counters": {}}

    async def get_chat_history(self, chat_id, last_message=0):
        return [m for m in self.chats.get(chat_id, []) if m.message_id > last_message]

    async def send_message(self, chat_id, text):
        self.outbox.append((chat_id, text))
        self.chats.setdefault(chat_id, []).append(ChatMessage(next(_ids), chat_id, self.user_id, self.username, text))
        return True

    async def get_sales(self, state=None):
        return list(self.sales.values())

    async def get_order(self, oid):
        so = self.sales[oid]
        return OrderPage(order_id=oid, status=so.status, quantity=so.quantity, chat_id=str(500 + so.buyer_id),
                         buyer_id=so.buyer_id, buyer_username=so.buyer_username, review_stars=5)

    async def refund(self, oid):
        self.refunds.append(oid)
        self.sales[oid].status = "refunded"
        return True

    async def get_lot_fields(self, offer_id, node_id):
        return dict(self.lot)

    async def save_lot(self, fields):
        self.lot.update(fields)

    async def update_lot(self, offer_id, node_id, *, price=None, amount=None, active=None):
        if price is not None:
            self.lot["price"] = f"{price:.2f}"
        if amount is not None:
            self.lot["amount"] = str(amount)
        if active is not None:
            self.lot["active"] = "on" if active else ""
        return dict(self.lot)

    async def raise_lots(self, game_id, node_id):
        return True, "Предложения подняты"

    async def get_public_lots(self, node_id, filter_regex=None):
        base = [("KeyKing", 189.0, 40), ("TF2Shop", 192.5, 15), ("Dumper", 120.0, 1), ("FastKeys", 195.0, 200),
                ("ProTrader", 199.0, 30), ("MannSeller", float(self.lot["price"]), int(self.lot["amount"] or 0))]
        return [CompetitorLot(offer_id=str(i if n != "MannSeller" else 1), seller=n,
                              price=p + random.uniform(-1.5, 1.5) if n != "MannSeller" else p, amount=a)
                for i, (n, p, a) in enumerate(base, start=10)]

    async def close(self):
        return None


class FakeSteam:
    def __init__(self, keys: int = 30):
        self.persona_name = "MannBot | FunPay"
        self.level = 23
        self.steam_id = "76561198000000001"
        self.inventory = [str(9000 + i) for i in range(keys)]
        self.offers: dict[str, dict] = {}
        self.behaviour: dict[str, str] = {}   # steam_id64 → accept|decline|escrow|ignore
        self.escrow: dict[str, int] = {}
        self.banned: set[str] = set()

    async def login(self):
        return None

    async def ensure_session(self):
        return None

    async def get_tradable_keys(self):
        return list(self.inventory)

    async def check_trade_link(self, link: TradeLink) -> TradeLinkCheck:
        if link.token == "Expired1":
            return TradeLinkCheck(False, link.steam_id64, reason_code="invalid_token",
                                  reason_text="This Trade URL is no longer valid")
        if link.steam_id64 in self.banned:
            return TradeLinkCheck(False, link.steam_id64, reason_code="trade_banned", reason_text="trade ban")
        return TradeLinkCheck(True, link.steam_id64, partner_name=f"Player{link.partner % 1000}",
                              their_escrow_days=self.escrow.get(link.steam_id64, 0))

    async def send_offer(self, link, asset_ids, message):
        if any(a not in self.inventory for a in asset_ids):
            raise SteamError("предметы недоступны", "invalid_items")
        oid = str(next(_ids) * 7)
        self.offers[oid] = {"state": T.ACTIVE, "to": link.steam_id64, "assets": asset_ids, "ts": time.time()}
        return SentOffer(oid, needs_confirmation=True, asset_ids=asset_ids)

    async def confirm_offer(self, offer_id, attempts=6):
        return offer_id in self.offers

    def tick(self) -> None:
        """Покупатели «реагируют» на трейды."""
        for oid, off in self.offers.items():
            if off["state"] != T.ACTIVE:
                continue
            b = self.behaviour.get(off["to"], "accept")
            if b == "accept":
                off["state"] = T.ACCEPTED
                self.inventory = [a for a in self.inventory if a not in off["assets"]]
            elif b == "decline":
                off["state"] = T.DECLINED
                self.behaviour[off["to"]] = "accept"   # во второй раз примет
            elif b == "escrow":
                off["state"] = T.IN_ESCROW
            elif b == "apiscam":
                off["state"] = T.CANCELED
                self.behaviour[off["to"]] = "accept"

    async def get_offer_state(self, offer_id):
        return self.offers[offer_id]["state"]

    async def get_offer(self, offer_id):
        off = self.offers[offer_id]
        return {"accountid_other": int(off["to"]) - 76561197960265728, "items_to_receive": [],
                "trade_offer_state": int(off["state"])}

    async def get_active_offers(self):
        return [{"tradeofferid": k} for k, v in self.offers.items() if v["state"] == T.ACTIVE], []

    async def cancel_offer(self, offer_id):
        if offer_id in self.offers:
            self.offers[offer_id]["state"] = T.CANCELED
        return True

    async def decline_offer(self, offer_id):
        return True

    async def close(self):
        return None


def trade_url(account_id: int, token: str = "AbCd1234") -> str:
    return f"https://steamcommunity.com/tradeoffer/new/?partner={account_id}&token={token}"


async def run_scenario(engine, fp: FakeFunPay, steam: FakeSteam, verbose: bool = True) -> dict:
    """Прогон типичных ситуаций через настоящий движок."""
    shown = 0

    async def step(title: str) -> None:
        nonlocal shown
        for _ in range(3):
            await engine.funpay_poll()
            while not engine.queue.empty():
                oid = engine.queue.get_nowait()
                engine._queued.discard(oid)
                await engine.deliver(oid)
            steam.tick()
            await engine.watch_offers()
        if verbose:
            print(f"\n━━━━━━━━ {title}")
            for chat, text in fp.outbox[shown:]:
                print(f"[чат {chat} · {fp.chat_users.get(chat)}] ⟵ бот:\n  " + text.replace("\n", "\n  "))
        shown = len(fp.outbox)

    engine.cfg.deal.message_rate_limit_seconds = 0
    await engine.funpay_poll()  # инициализация

    # 1. Идеальная сделка
    a = fp.buyer_pays("Alice", 11, 2)
    await step("Alice оплатила 2 ключа → бот присылает инструкцию")
    fp.buyer_says(11, "привет! вот ссылка " + trade_url(111111111))
    await step("Alice прислала ссылку → проверка → трейд → принят → благодарность")
    fp.buyer_confirms(a)
    await step("Alice подтвердила заказ")

    # 2. Неверные данные, потом правильные
    b = fp.buyer_pays("Bob", 12, 1)
    await step("Bob оплатил 1 ключ")
    fp.buyer_says(12, "https://steamcommunity.com/profiles/76561198011111111")
    await step("Bob прислал ссылку на профиль → «с данными беда»")
    fp.buyer_says(12, trade_url(222222222, "Expired1"))
    await step("Bob прислал устаревшую ссылку → Steam её отклоняет")
    fp.buyer_says(12, trade_url(222222222, "Fresh123"))
    await step("Bob прислал правильную ссылку")

    # 3. Отклонил трейд, потом !повтор
    steam.behaviour[str(333333333 + 76561197960265728)] = "decline"
    c = fp.buyer_pays("Carol", 13, 3)
    fp.buyer_says(13, trade_url(333333333))
    await step("Carol: трейд отклонён")
    fp.buyer_says(13, "!повтор")
    await step("Carol написала !повтор")

    # 4. Trade hold
    steam.escrow[str(444444444 + 76561197960265728)] = 15
    d = fp.buyer_pays("Dave", 14, 1)
    fp.buyer_says(14, trade_url(444444444))
    await step("Dave: задержка обмена 15 дней (политика require_fix)")
    fp.buyer_says(14, "!отправить")
    await step("Dave согласился на задержку")

    # 5. Попытка увести с площадки
    e = fp.buyer_pays("Eve", 15, 1)
    fp.buyer_says(15, "давай лучше в телеграм напишу, скинь контакт")
    await step("Eve просит контакт → отказ по правилам FunPay")

    # 6. API-скам у покупателя
    steam.behaviour[str(555555555 + 76561197960265728)] = "apiscam"
    f = fp.buyer_pays("Frank", 16, 1)
    fp.buyer_says(16, trade_url(555555555))
    await step("Frank: трейд отменён не ботом → предупреждение об API-скаме")

    # 7. Крупный заказ → ручная проверка риска
    g = fp.buyer_pays("Greg", 17, 25)
    fp.buyer_says(17, trade_url(666666666))
    await step("Greg: крупный заказ → на проверку админу")
    print("\nАдмин: " + await engine.handle_command("approve", [g]))
    await step("Greg: после /approve")

    # 8. Вопросы, которые бот не понял, → ИИ-агент (если включён; в демо — заглушка без сети)
    ai_orders = []
    if getattr(engine, "ai", None) and engine.ai.active:
        h = fp.buyer_pays("Hank", 18, 1)
        fp.buyer_says(18, "а где взять эту ссылку на обмен? не нахожу")
        await step("Hank спрашивает, где взять ссылку → черновик ИИ продавцу")
        fp.buyer_says(18, "если не получится, можно вернуть деньги?")
        await step("Hank спрашивает про возврат → ИИ зовёт продавца")
        ai_orders.append(h)

    await engine.stock_sync()
    await engine.rtba_step()
    if engine.analytics:
        await engine.analytics.tick()
    return {"orders": [a, b, c, d, e, f, g], "ai_orders": ai_orders}
