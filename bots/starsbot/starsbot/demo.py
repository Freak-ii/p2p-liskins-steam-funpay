"""Демо/симуляция: эмуляторы FunPay, Fragment и TON-кошелька с тем же интерфейсом, что у настоящих клиентов.

Запуск: python StarsBot.py --demo          (приложение с эмуляцией)
        python -m starsbot --console-demo  (сценарий текстом в консоли)
"""
from __future__ import annotations

import itertools
import random
import time

from starsbot.core.models import ChatMessage, CompetitorLot, FragmentTx, FunPayOrder, Recipient, SentTx, TonMessage
from starsbot.fragment.client import FragmentError
from starsbot.funpay.parsers import ChatBookmark, OrderPage
from starsbot.ton.wallet import TonError, from_nano, to_nano

_ids = itertools.count(1000)
FRAGMENT_ADDR = "EQBAjaOyi2wGWlk-EDkSabqqnF-MrrwMadnwqrurKpkla9nE"
DEMO_TON_PER_STAR = 0.0115
DEMO_PREMIUM_TON = {3: 3.85, 6: 5.15, 12: 9.35}      # подарок Premium на Fragment (≈ $11.99 / $15.99 / $28.99)


class FakeFunPay:
    dry_run = False               # эмулятор всегда «боевой»; тест может включить тестовый режим
    def __init__(self):
        self.user_id = 1
        self.username = "StarsSeller"
        self.sales: dict[str, FunPayOrder] = {}
        self.params: dict[str, dict] = {}
        self.chats: dict[str, list[ChatMessage]] = {}
        self.chat_users: dict[str, str] = {}
        self.outbox: list[tuple[str, str]] = []
        self.lots: dict[str, dict] = {}
        self._orders_tag = 0
        self.refunds: list[str] = []
        self.reviews: dict[str, int] = {}
        self.review_replies: dict[str, str] = {}
        self.balance_rub = 15000.0
        self.withdrawals: list[tuple] = []

    async def login(self):
        return None

    def _lot(self, offer_id) -> dict:
        oid = str(offer_id)
        if oid in ("301", "302", "303"):          # лоты Telegram Premium (раздел 1391)
            months = {"301": 3, "302": 6, "303": 12}[oid]
            return self.lots.setdefault(oid, {"offer_id": oid, "node_id": "1391",
                                              "price": f"{months * 140 + 300:.2f}", "amount": "5", "active": "on"})
        stars = {str(i + 1): s for i, s in enumerate((50, 75, 100, 150, 250, 350, 500))}.get(oid, 50)
        return self.lots.setdefault(oid, {"offer_id": oid, "node_id": "2418", "price": f"{stars * 1.45:.2f}",
                                          "amount": "10", "active": "on"})

    # --- симуляция действий покупателя
    def buyer_pays(self, buyer: str, buyer_id: int, stars: int, qty: int = 1, username: str | None = None,
                   price_per_star: float = 1.45, method: str = "По username") -> str:
        oid = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        chat = str(500 + buyer_id)
        self.chat_users[chat] = buyer
        desc = f"{stars} звёзд, {method}"
        self.sales[oid] = FunPayOrder(order_id=oid, buyer_username=buyer, buyer_id=buyer_id, description=desc,
                                      price=round(qty * stars * price_per_star, 2), quantity=qty, status="paid",
                                      chat_id=chat)
        self.params[oid] = {"Краткое описание": desc, "Количество": f"{qty} шт."}
        if username:
            self.params[oid]["Username в Telegram"] = username
        self._sys(chat, f"Покупатель {buyer} оплатил заказ #{oid}. {desc}, {qty} шт.")
        self._orders_tag += 1
        return oid

    def buyer_pays_premium(self, buyer: str, buyer_id: int, months: int, price: float, username: str | None = None,
                           qty: int = 1, method: str = "Подарком по username") -> str:
        oid = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        chat = str(500 + buyer_id)
        self.chat_users[chat] = buyer
        word = {3: "3 месяца", 6: "6 месяцев", 12: "12 месяцев"}.get(months, f"{months} мес.")
        desc = f"Telegram Premium, {word}, {method}"
        self.sales[oid] = FunPayOrder(order_id=oid, buyer_username=buyer, buyer_id=buyer_id, description=desc,
                                      price=round(price * qty, 2), quantity=qty, status="paid", chat_id=chat)
        self.params[oid] = {"Краткое описание": desc, "Количество": f"{qty} шт."}
        if username:
            self.params[oid]["Username получателя"] = username
        self._sys(chat, f"Покупатель {buyer} оплатил заказ #{oid}. {desc}, {qty} шт.")
        self._orders_tag += 1
        return oid

    def buyer_says(self, buyer_id: int, text: str) -> None:
        chat = str(500 + buyer_id)
        self.chat_users.setdefault(chat, f"user{buyer_id}")
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

    async def get_bookmarks_page(self):
        return []

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
        return OrderPage(order_id=oid, status=so.status, params=dict(self.params.get(oid, {})), quantity=so.quantity,
                         chat_id=str(500 + so.buyer_id), buyer_id=so.buyer_id, buyer_username=so.buyer_username,
                         review_stars=self.reviews.get(oid, 5), review_answered=oid in self.review_replies)

    async def reply_review(self, oid, text):
        self.review_replies[oid] = text
        return True

    async def refund(self, oid):
        self.refunds.append(oid)
        self.sales[oid].status = "refunded"
        return True

    async def get_lot_fields(self, offer_id, node_id):
        return dict(self._lot(offer_id))

    async def save_lot(self, fields):
        self._lot(fields.get("offer_id")).update(fields)

    async def update_lot(self, offer_id, node_id, *, price=None, amount=None, active=None):
        lot = self._lot(offer_id)
        if price is not None:
            lot["price"] = f"{price:.2f}"
        if amount is not None:
            lot["amount"] = str(amount)
        if active is not None:
            lot["active"] = "on" if active else ""
        return dict(lot)

    async def raise_lots(self, game_id, node_id):
        return True, "Предложения подняты"

    # --- выставление лотов и баланс (эмуляция форм FunPay)
    async def get_lot_form(self, node_id, offer_id=0):
        qty = [("1", "50 звёзд"), ("2", "75 звёзд"), ("3", "100 звёзд"), ("4", "13 звёзд"), ("5", "21 звезда"),
               ("6", "43 звезды"), ("7", "85 звёзд"), ("8", "150 звёзд"), ("9", "250 звёзд"), ("10", "350 звёзд"),
               ("11", "500 звёзд"), ("99", "Другое количество")]
        method = [("1", "По username"), ("2", "Подарком"), ("3", "С заходом на аккаунт")]
        base = {"offer_id": str(offer_id or 0), "node_id": str(node_id), "fields[quantity]": "", "fields[method]": "",
                "fields[desc][ru]": "", "price": "", "amount": "", "active": "on"}
        return base, {"fields[quantity]": qty, "fields[method]": method}

    async def create_lot(self, fields, force=False):
        new_id = str(1000 + len(self.lots) + 1)
        self.lots[new_id] = {**fields, "offer_id": new_id}
        return new_id

    async def get_own_lots(self, node_id):
        return []

    async def get_balance(self):
        from starsbot.funpay.parsers import BalanceInfo
        return BalanceInfo(total_rub=self.balance_rub, available_rub=self.balance_rub, form_action="/withdraw/withdraw",
                           fields={"currency_id": "rub"}, methods=[("card_rub", "Банковская карта"),
                                                                   ("ton", "Toncoin (TON)")],
                           method_field="ext_currency_id", amount_field="amount_int", wallet_field="wallet")

    async def withdraw(self, info, method, wallet, amount):
        self.withdrawals.append((method, wallet, amount))
        self.balance_rub -= amount
        return "Заявка на вывод создана"

    async def get_public_lots(self, node_id, filter_regex=None):
        if int(node_id) == 1391:
            return self._premium_lots()
        out = []
        sellers = [("StarShop", 1.40, 40), ("FastStars", 1.43, 15), ("Dumper", 1.05, 1), ("TgMarket", 1.48, 200),
                   ("ProStars", 1.52, 30)]
        n = 10
        for i, stars in enumerate((50, 75, 100, 150, 250, 350, 500)):
            for name, per, amount in sellers:
                n += 1
                out.append(CompetitorLot(offer_id=str(n), seller=name, price=round(stars * per * random.uniform(0.98, 1.02), 2),
                                         amount=amount, description=f"{stars} звёзд, По username",
                                         fields={"quantity": f"{stars} звёзд", "method": "по username"}))
            lot = self._lot(i + 1)
            out.append(CompetitorLot(offer_id=str(i + 1), seller=self.username, price=float(lot["price"]),
                                     amount=int(lot["amount"] or 0), description=f"{stars} звёзд, По username",
                                     fields={"quantity": f"{stars} звёзд", "method": "по username"}))
            out.append(CompetitorLot(offer_id=str(900 + i), seller="GiftGuy", price=round(stars * 1.2, 2), amount=5,
                                     description=f"{stars} звёзд, Подарком",
                                     fields={"quantity": f"{stars} звёзд", "method": "подарком"}))
        return out

    def _premium_lots(self) -> list:
        out, n = [], 3000
        sellers = [("PremShop", 1.00, "Подарком по username"), ("TgPlus", 1.04, "Подарком, без входа"),
                   ("CheapLogin", 0.70, "С заходом на аккаунт"), ("FragStore", 1.09, "По username через Fragment"),
                   ("Premka", 1.15, "Подарком")]
        base = {3: 620.0, 6: 830.0, 12: 1480.0}
        for months, word in ((3, "3 месяца"), (6, "6 месяцев"), (12, "12 месяцев")):
            for name, k, method in sellers:
                n += 1
                out.append(CompetitorLot(offer_id=str(n), seller=name, price=round(base[months] * k, 2), amount=10,
                                         description=f"Telegram Premium {word}, {method}",
                                         fields={"period": word.lower(), "method": method.lower()}))
            oid = {3: "301", 6: "302", 12: "303"}[months]
            lot = self._lot(oid)
            out.append(CompetitorLot(offer_id=oid, seller=self.username, price=round(float(lot["price"]) * 1.14, 2),
                                     amount=int(lot["amount"] or 0), description=f"Telegram Premium {word}, Подарком",
                                     fields={"period": word.lower(), "method": "подарком"}))
        return out

    async def close(self):
        return None


class FakeWallet:
    """Кошелёк с «блокчейном»: транзакция исполняется на следующем tick()."""

    version = "v4r2"

    def __init__(self, balance: float = 100.0):
        self.balance = to_nano(balance)
        self.seqno = 1
        self.raw_address = "0:8874ffa2f02fa9cbbb11e1ae82c533989c5f2f9a9b6675573390341e5d51a609"
        self.friendly_address = "UQCIdP-i8C-py7sR4a6CxTOYnF8vm2Z1VzOQNB5dUaYJ-demo"
        self.public_key = b"\x01" * 32
        self.pending: list[dict] = []
        self.history: list[dict] = []
        self.drop_next = False            # следующая транзакция «потеряется» (не попадёт в сеть)

    async def refresh(self):
        return self

    async def balance_ton(self) -> float:
        return from_nano(self.balance)

    async def current_seqno(self) -> int:
        return self.seqno

    def state_init_b64(self) -> str:
        return ""

    async def transfer(self, messages: list[TonMessage], valid_until: int, *, on_signed=None) -> SentTx:
        total = sum(m.amount for m in messages)
        if self.balance < total + to_nano(0.02):
            raise TonError(f"на кошельке {from_nano(self.balance):.4f} TON, нужно {from_nano(total):.4f} TON",
                           "insufficient_funds")
        h = f"{next(_ids):064x}"
        if on_signed is not None:
            await on_signed(self.seqno, valid_until, h)
        if self.drop_next:
            self.drop_next = False
        else:
            self.pending.append({"seqno": self.seqno, "messages": messages, "hash": h})
        return SentTx(seqno=self.seqno, valid_until=valid_until, msg_hash=h, boc="")

    def tick(self) -> None:
        for p in self.pending:
            if p["seqno"] != self.seqno:
                continue
            fee = to_nano(0.0055)
            self.balance -= sum(m.amount for m in p["messages"]) + fee
            self.seqno += 1
            for m in p["messages"]:
                self.history.append({"dest": m.address, "value": m.amount, "hash": p["hash"], "utime": time.time(),
                                     "fee": fee})
        self.pending = []

    async def find_outgoing(self, dest, amount, since, comment_part=""):
        for h in reversed(self.history):
            if h["dest"] == dest and h["value"] == int(amount):
                return {"hash": h["hash"], "utime": h["utime"], "fee": h["fee"], "comment": ""}
        return None

    async def close(self):
        return None


class FakeFragment:
    def __init__(self, wallet: FakeWallet, ton_per_star: float = DEMO_TON_PER_STAR):
        self.wallet = wallet
        self.ton_per_star = ton_per_star
        self.users = {"my_shop_account": "Магазин (аккаунт подарков)", "durov": "Pavel Durov", "alice_tg": "Alice", "bob_real": "Bob", "carol_star": "Carol",
                      "dave_d": "Dave", "eve_e": "Eve", "greg_big": "Greg", "helen_h": "Helen"}
        self.requests: dict[str, dict] = {}
        self.logged_in = True

    async def login(self):
        if not self.logged_in:
            from starsbot.fragment.client import FragmentAuthError
            raise FragmentAuthError("сессия Fragment истекла (демо)")
        return {"logged_in": True, "address": self.wallet.raw_address}

    async def search_recipient(self, username: str, quantity=""):
        u = username.lstrip("@")
        if u.lower().endswith("bot"):
            raise FragmentError("This account can't receive Stars", "cannot_receive")
        name = next((v for k, v in self.users.items() if k.lower() == u.lower()), None)
        if name is None:
            raise FragmentError("No Telegram users found.", "not_found")
        return Recipient(username=u, recipient=f"rcp_{u.lower()}", name=name)

    async def init_buy(self, recipient: str, quantity: int):
        rid = f"req{next(_ids)}"
        amount = round(int(quantity) * self.ton_per_star, 4)
        self.requests[rid] = {"recipient": recipient, "quantity": int(quantity), "amount": amount}
        return rid, amount

    async def get_buy_link(self, req_id: str, amount_ton: float = 0.0):
        r = self.requests[req_id]
        comment = f"{r['quantity']} Telegram Stars\n\nRef#{req_id[-6:]}"
        return FragmentTx(req_id=req_id, valid_until=int(time.time()) + 600,
                          messages=[TonMessage(FRAGMENT_ADDR, to_nano(r["amount"]), "")], amount_ton=r["amount"],
                          comment=comment)

    # --- Telegram Premium
    async def search_premium_recipient(self, username: str, months: int):
        rec = await self.search_recipient(username)
        if int(months) not in DEMO_PREMIUM_TON:
            raise FragmentError("Invalid period", "bad_quantity")
        return Recipient(username=rec.username, recipient=f"prm_{rec.username.lower()}", name=rec.name)

    async def init_premium(self, recipient: str, months: int):
        rid = f"prq{next(_ids)}"
        amount = DEMO_PREMIUM_TON[int(months)]
        self.requests[rid] = {"recipient": recipient, "months": int(months), "amount": amount}
        return rid, amount

    async def get_premium_link(self, req_id: str, amount_ton: float = 0.0):
        r = self.requests[req_id]
        comment = f"Telegram Premium for {r['months']} months\n\nRef#{req_id[-6:]}"
        return FragmentTx(req_id=req_id, valid_until=int(time.time()) + 600,
                          messages=[TonMessage(FRAGMENT_ADDR, to_nano(r["amount"]), "")], amount_ton=r["amount"],
                          comment=comment)

    # --- коллекционные подарки (NFT) на fragment.com/gifts
    def _nft_market(self) -> dict:
        if not hasattr(self, "nft_market"):
            self.nft_market = {"lolpop": [{"item": "lolpop-236254", "price": 5.0},
                                          {"item": "lolpop-238099", "price": 5.2}],
                               "toybear": [{"item": "toybear-1001", "price": 43.0}],
                               "deskcalendar": [{"item": "deskcalendar-5", "price": 7.0}]}
            self.nft_owned: set[str] = set()
            self.nft_sent: list[tuple[str, str]] = []
        return self.nft_market

    async def gift_listings(self, collection: str, limit: int = 10):
        return sorted(self._nft_market().get(collection, []), key=lambda x: x["price"])[:limit]

    async def gift_item(self, item: str):
        for rows in self._nft_market().values():
            for x in rows:
                if x["item"] == item:
                    return {"type": 5, "item": item, "title": item, "for_sale": True, "price": x["price"],
                            "owner": "", "ton_usd": 0.95}
        return {"type": 5, "item": item, "title": item, "for_sale": False, "price": 0.0, "owner": "", "ton_usd": 0.95}

    async def gift_buy_link(self, item: str, amount: float):
        for rows in self._nft_market().values():
            for x in list(rows):
                if x["item"] == item:
                    rows.remove(x)
        self.nft_owned.add(item)
        return FragmentTx(req_id=f"buy:{item}", valid_until=int(time.time()) + 600,
                          messages=[TonMessage(FRAGMENT_ADDR, to_nano(amount), "")], amount_ton=amount,
                          comment=f"Buy {item}")

    async def gift_transfer_recipient(self, username: str):
        u = username.lstrip("@")
        if u.lower().endswith("bot") or u.lower().startswith("nobody"):
            raise FragmentError("No Telegram users found.", "not_found")
        return Recipient(username=u, recipient=f"nft_{u.lower()}", name=self.users.get(u, u.title()))

    async def gift_transfer_init(self, recipient: str, item: str):
        self._nft_market()
        if item not in self.nft_owned:
            raise FragmentError("This gift is not in your wallet", "fragment")
        self.requests[f"tr:{item}"] = {"recipient": recipient, "item": item}
        return f"tr:{item}"

    async def gift_transfer_link(self, req_id: str, item: str):
        r = self.requests[req_id]
        self.nft_owned.discard(item)
        self.nft_sent.append((item, r["recipient"]))
        return FragmentTx(req_id=req_id, valid_until=int(time.time()) + 600,
                          messages=[TonMessage(FRAGMENT_ADDR, to_nano(0.1), "")], amount_ton=0.1,
                          comment=f"Transfer {item}")

    async def confirm(self, tx, boc):
        return None

    async def close(self):
        return None


async def run_scenario(engine, fp: FakeFunPay, wallet: FakeWallet, verbose: bool = True) -> dict:
    """Прогон типичных ситуаций через настоящий движок."""
    shown = 0

    async def step(title: str) -> None:
        nonlocal shown
        for _ in range(3):
            await engine.funpay_poll()
            while not engine.queue.empty():
                oid = engine.queue.get_nowait()
                engine._queued.discard(oid)
                await engine.purchase(oid)
                wallet.tick()
                await engine.watch_purchases()
            wallet.tick()
            await engine.watch_purchases()
        if verbose:
            print(f"\n━━━━━━━━ {title}")
            for chat, text in fp.outbox[shown:]:
                print(f"[чат {chat} · {fp.chat_users.get(chat)}] ⟵ бот:\n  " + text.replace("\n", "\n  "))
        shown = len(fp.outbox)

    engine.cfg.deal.message_rate_limit_seconds = 0
    engine.fragment_ok = True
    engine._fragment_state(session="ok", checked_at=time.time())
    await engine.wallet_watch()
    await engine.funpay_poll()  # инициализация

    # 1. username указан при оплате → выдача без вопросов
    a = fp.buyer_pays("Alice", 11, 100, username="@alice_tg")
    await step("Alice оплатила 100 ⭐ и указала username → звёзды отправлены, просьба подтвердить")
    fp.buyer_confirms(a)
    await step("Alice подтвердила заказ")

    # 2. username в чате, сначала с ошибками
    b = fp.buyer_pays("Bob", 12, 50)
    await step("Bob оплатил 50 ⭐ без username → бот просит username")
    fp.buyer_says(12, "Боб Иванов")
    await step("Bob прислал имя вместо username → бот переспрашивает")
    fp.buyer_says(12, "@bob_fake")
    await step("Bob прислал несуществующий username → Fragment не нашёл")
    fp.buyer_says(12, "мой ник @bob_real")
    await step("Bob прислал правильный username")

    # 3. Вопрос и FAQ
    c = fp.buyer_pays("Carol", 13, 250, qty=2)
    fp.buyer_says(13, "это безопасно? код из смс не нужен?")
    await step("Carol (2 × 250 ⭐) спрашивает о безопасности → FAQ")
    fp.buyer_says(13, "carol_star")
    await step("Carol прислала username без @")

    # 4. Попытка увести с площадки
    d = fp.buyer_pays("Dave", 14, 75)
    fp.buyer_says(14, "давай дешевле напрямую, пиши в тг")
    await step("Dave просит общаться вне FunPay → отказ по правилам")
    fp.buyer_says(14, "@dave_d")
    await step("Dave прислал username")

    # 5. Транзакция «потерялась» в сети → повтор только после истечения срока
    wallet.drop_next = True
    e = fp.buyer_pays("Eve", 15, 150, username="eve_e")
    await step("Eve: транзакция не попала в сеть — бот ждёт и не платит повторно")
    engine.db.exec("UPDATE purchases SET valid_until=? WHERE order_id=?", (int(time.time()) - 120, e))
    await step("Eve: срок транзакции вышел → безопасный повтор покупки")

    # 6. Пресейл-вопрос без заказа
    fp.buyer_says(19, "Здравствуйте, а звёзды на 500 есть? быстро приходят?")
    await step("Вопрос о звёздах без заказа → короткий ответ")

    # 7. Крупный заказ → ручная проверка риска
    g = fp.buyer_pays("Greg", 17, 500, qty=4, username="greg_big")
    await step("Greg: 2000 ⭐ первой покупкой → на проверку продавцу")
    if verbose:
        print("\nПродавец: " + await engine.handle_command("approve", [g]))
    else:
        await engine.handle_command("approve", [g])
    await step("Greg: после /approve")

    # 8. Пополнение кошелька — для калькулятора денег
    if engine.econ is not None:
        engine.econ.add_cashflow("topup", 10_900, 100, "демо: купили 100 TON на бирже и перевели на кошелёк")
        wallet.balance += to_nano(100)
    await engine.wallet_watch()
    engine.cfg.rtba.enabled = True
    await engine.rtba_step()
    if engine.analytics:
        await engine.analytics.tick()
    return {"orders": [a, b, c, d, e, g]}


async def run_gift_demo(engine, fp: FakeFunPay, wallet: FakeWallet) -> list[str]:
    """Заказы «Подарком»: username в заказе, username в чате, пополнение звёзд аккаунта через Fragment."""
    async def drain() -> None:
        for _ in range(3):
            await engine.funpay_poll()
            while not engine.queue.empty():
                oid = engine.queue.get_nowait()
                engine._queued.discard(oid)
                await engine.purchase(oid)
                wallet.tick()
                await engine.watch_purchases()
            wallet.tick()
            await engine.watch_purchases()

    if engine.gifts is None or not engine.gifts.enabled:
        return []
    fp.users_note = "gifts"
    a = fp.buyer_pays("Kira", 31, 85, method="Подарком", username="kira_tg", price_per_star=1.7)
    b = fp.buyer_pays("Leo", 32, 13, method="Подарком", price_per_star=1.9)
    await drain()
    fp.buyer_says(32, "@leo_gifts")
    await drain()
    await drain()
    fp.buyer_confirms(a)
    await drain()
    await engine.gifts.watch()
    return [a, b]


async def run_premium_demo(engine, fp: FakeFunPay, wallet: FakeWallet) -> list[str]:
    """Telegram Premium: username в заказе, username в чате, лот «с заходом» (не наш), заказ магазина, цены по рынку."""
    async def drain() -> None:
        for _ in range(3):
            await engine.funpay_poll()
            while not engine.queue.empty():
                oid = engine.queue.get_nowait()
                engine._queued.discard(oid)
                await engine.purchase(oid)
                wallet.tick()
                await engine.watch_purchases()
            wallet.tick()
            await engine.watch_purchases()

    pr = engine.premium
    if pr is None or not pr.enabled:
        return []
    await pr.refresh_quotes()                     # котировки Fragment без оплаты: себестоимость известна заранее
    a = fp.buyer_pays_premium("Nina", 41, 3, 560.0, username="@alice_tg")
    b = fp.buyer_pays_premium("Oleg", 42, 12, 1350.0)
    login = fp.buyer_pays_premium("Pavel", 43, 3, 400.0, method="С заходом на аккаунт")
    await drain()
    fp.buyer_says(42, "а это точно premium без входа?")
    await drain()
    fp.buyer_says(42, "@bob_real")
    await drain()
    await drain()
    fp.buyer_confirms(a)
    await drain()
    fp.buyer_says(44, "Здравствуйте, премиум на полгода есть?")
    await drain()
    shop = await pr.supply_order("demo-shop-1", "carol_star", 6, channel="shop", price=900.0)
    await drain()
    await engine.wallet_watch()
    await pr.rtba_step()
    return [a, b, login, shop.get("order_id", "")]
