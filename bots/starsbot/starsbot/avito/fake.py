"""Эмулятор API Авито (мессенджер + цены) для демо и тестов: тот же интерфейс, что у AvitoClient."""
from __future__ import annotations

import itertools
import time

from starsbot.avito.client import AvitoChat, AvitoMessage

ME = 1000001
_ids = itertools.count(1)


class FakeAvito:
    def __init__(self):
        self.dry_run = False
        self.user_id = ME
        self.chats: dict[str, dict] = {}      # chat_id → {item_id, title, buyer_id, buyer_name, messages: [...]}
        self.prices: dict[int, int] = {}
        self.read: list[str] = []
        self._t = time.time() - 300

    def _tick(self) -> float:
        self._t = max(self._t + 1, time.time())
        return self._t

    # --- симуляция покупателя и Авито
    def open_chat(self, item_id: int, buyer: str = "Анна", buyer_id: int | None = None,
                  title: str = "Telegram Stars") -> str:
        cid = f"u2i-fake{next(_ids):04d}"
        self.chats[cid] = {"item_id": int(item_id), "title": title, "buyer_id": buyer_id or 500000 + len(self.chats),
                           "buyer_name": buyer, "messages": []}
        return cid

    def _add(self, cid: str, text: str, *, direction: str, kind: str = "text", author: int | None = None) -> None:
        ch = self.chats[cid]
        ch["messages"].append(AvitoMessage(message_id=f"m{next(_ids)}", author_id=author if author is not None else (
            ch["buyer_id"] if direction == "in" else ME), text=text, created=self._tick(), direction=direction,
            kind=kind))

    def buyer_says(self, cid: str, text: str) -> None:
        self._add(cid, text, direction="in")

    def buyer_pays(self, cid: str) -> None:
        self._add(cid, "Покупатель оплатил заказ. Передайте товар в течение 24 часов.", direction="in",
                  kind="system", author=0)

    def buyer_confirms(self, cid: str) -> None:
        self._add(cid, "Покупатель подтвердил получение заказа", direction="in", kind="system", author=0)

    def buyer_cancels(self, cid: str) -> None:
        self._add(cid, "Покупатель отменил заказ", direction="in", kind="system", author=0)

    def seller_says(self, cid: str, text: str) -> None:
        """Продавец ответил сам из приложения Авито."""
        self._add(cid, text, direction="out")

    def sent_to(self, cid: str) -> list[str]:
        return [m.text for m in self.chats[cid]["messages"] if m.direction == "out"]

    # --- интерфейс клиента
    async def me(self) -> int:
        return ME

    async def list_chats(self, *, unread_only: bool = False, limit: int = 50) -> list[AvitoChat]:
        out = []
        for cid, ch in self.chats.items():
            last = ch["messages"][-1] if ch["messages"] else None
            out.append(AvitoChat(chat_id=cid, item_id=ch["item_id"], item_title=ch["title"], buyer_id=ch["buyer_id"],
                                 buyer_name=ch["buyer_name"], last_message_id=last.message_id if last else "",
                                 last_created=last.created if last else 0, last_direction=last.direction if last
                                 else "", updated=last.created if last else 0))
        return out

    async def messages(self, chat_id: str, limit: int = 50) -> list[AvitoMessage]:
        return list(self.chats[chat_id]["messages"])[-limit:]

    async def send(self, chat_id: str, text: str) -> str:
        self._add(chat_id, text, direction="out")
        return self.chats[chat_id]["messages"][-1].message_id

    async def mark_read(self, chat_id: str) -> None:
        self.read.append(chat_id)

    async def items(self) -> list[dict]:
        return [{"id": i, "price": p, "status": "active"} for i, p in self.prices.items()]

    async def update_price(self, item_id: int, price: int) -> None:
        self.prices[int(item_id)] = int(price)

    async def close(self) -> None:
        return None


DEMO_PRODUCTS = [{"stars": n, "item_id": 7000000000 + n, "price": p} for n, p in
                 ((50, 95), (100, 179), (250, 439), (500, 869))]


async def run_avito_demo(engine, wallet) -> dict:
    """Демо Авито через настоящий движок: вопрос до оплаты, username заранее, оплата → выдача; второй покупатель
    присылает username после оплаты."""
    ch = engine.channels["avito"]
    av: FakeAvito = ch.client
    engine.fragment.users.update({"anna_tg": "Анна", "ivan_tg": "Иван"})

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

    await ch.price_sync()
    a = av.open_chat(7000000100, "Анна", title="Telegram Stars 100 звёзд")
    av.buyer_says(a, "Здравствуйте, как купить?")
    await ch.poll()
    av.buyer_says(a, "@anna_tg")
    av.buyer_pays(a)
    await ch.poll()
    await settle()
    av.buyer_confirms(a)
    await ch.poll()
    b = av.open_chat(7000000250, "Иван", title="Telegram Stars 250 звёзд")
    av.buyer_pays(b)
    await ch.poll()
    av.buyer_says(b, "ivan_tg")
    await ch.poll()
    await settle()
    return {"chats": [a, b]}
