"""Готовность к живой продаже на общем со StarsBot аккаунте FunPay: чаты без runner, ответ без заказа,
лот без ручной настройки, golden_key у StarsBot."""
import asyncio

import yaml

from mannbot.__main__ import build
from mannbot.config import inherit_shared, load_config
from mannbot.coord import Coordinator
from mannbot.core.models import ChatMessage, OrderStatus as S
from mannbot.demo import trade_url
from mannbot.funpay.lotmaker import ensure_key_lot
from mannbot.funpay.parsers import OwnLot


def make(lots=None, **extra):
    over = {"general": {"demo": True, "dry_run": False, "database": ":memory:"},
            "funpay": {"lots": [{"offer_id": 1, "node_id": 1808, "game_id": 435}] if lots is None else lots},
            "deal": {"message_rate_limit_seconds": 0},
            "dashboard": {"token": "test-token-12345"}}
    for k, v in extra.items():
        over.setdefault(k, {}).update(v)
    return build(load_config(None, over))


class NoRunnerBookmarks:
    """Как на живом аккаунте: runner после первого ответа больше не присылает список диалогов."""

    def __init__(self, fp):
        self.fp = fp
        self.calls = 0
        self.page_calls = 0

    async def poll_updates(self):
        upd = await self.fp.__class__.poll_updates(self.fp)
        self.calls += 1
        if self.calls > 1:
            upd["bookmarks"] = []
        return upd

    async def get_bookmarks_page(self):
        self.page_calls += 1
        upd = await self.fp.__class__.poll_updates(self.fp)
        return upd["bookmarks"]


def test_chat_fallback_and_link_before_greeting():
    db, notifier, fp, steam, engine, analytics = make()
    nr = NoRunnerBookmarks(fp)
    fp.poll_updates = nr.poll_updates
    fp.get_bookmarks_page = nr.get_bookmarks_page

    async def go():
        fp.chats["900"] = []
        fp.chat_users["900"] = "Old"
        fp._sys("900", "старая переписка")
        await engine.funpay_poll()                       # инициализация
        oid = fp.buyer_pays("Lena", 40, 1)
        fp.buyer_says(40, "держи " + trade_url(404040))   # ссылка пришла раньше, чем бот увидел оплату
        engine._runner_bm_at = 0
        for _ in range(3):
            engine._page_bm_at = 0
            await engine.funpay_poll()
            while not engine.queue.empty():
                o = engine.queue.get_nowait()
                engine._queued.discard(o)
                await engine.deliver(o)
        return oid
    oid = asyncio.run(go())
    assert nr.page_calls >= 1
    o = db.get_order(oid)
    assert o["trade_url"] and o["status"] in (S.OFFER_SENT.value, S.DELIVERED.value)


def test_presale_reply_once_and_shared_claim(tmp_path):
    db, notifier, fp, steam, engine, analytics = make()
    engine.coord = Coordinator(tmp_path / "coord.sqlite3", "mannbot")
    other = Coordinator(tmp_path / "coord.sqlite3", "starsbot")
    db.kv_set("last_stock", 12)

    async def go():
        await engine.funpay_poll()
        fp.chat_users["601"] = "Q"
        fp.chats["601"] = [ChatMessage(10**6, "601", 601, "Q", "есть ключи манн ко?")]
        await engine.funpay_poll()
        fp.chats["601"].append(ChatMessage(10**6 + 1, "601", 601, "Q", "а ключи точно есть?"))
        await engine.funpay_poll()
        # вопрос о звёздах — не наш: молчим
        fp.chat_users["602"] = "S"
        fp.chats["602"] = [ChatMessage(10**6 + 2, "602", 602, "S", "сколько стоят 100 звёзд?")]
        await engine.funpay_poll()
    asyncio.run(go())
    to601 = [t for c, t in fp.outbox if c == "601"]
    assert len(to601) == 1 and "12 шт." in to601[0]
    assert not [t for c, t in fp.outbox if c == "602"]
    assert other.claim_presale("601") is False       # StarsBot в этот чат уже не ответит


class LotFP:
    def __init__(self, own=None, created="77001"):
        self.own = own or []
        self.created = created
        self.sent = None
        self.dry_run = False

    async def get_own_lots(self, node):
        return self.own

    async def get_lot_form(self, node, offer_id=0):
        return ({"offer_id": "0", "node_id": str(node), "csrf_token": "x", "fields[summary][ru]": "",
                 "fields[summary][en]": "", "fields[desc][ru]": "", "fields[type]": "", "price": "", "amount": ""},
                {"fields[type]": [("", "Выберите"), ("11", "Шапки"), ("12", "Ключи")]})

    async def create_lot(self, fields):
        self.sent = fields
        return self.created


def test_lot_created_when_missing():
    db, notifier, fp, steam, engine, analytics = make(lots=[], rtba={"cost_per_key": 130, "min_margin_pct": 5})
    saved = {}
    engine.save_config = lambda upd, rebuild=False: saved.update(upd)
    engine.fp = LotFP()
    rep = asyncio.run(ensure_key_lot(engine))
    assert "77001" in rep
    f = engine.fp.sent
    assert f["fields[type]"] == "12" and f["active"] == "on" and "Mann Co" in f["fields[summary][ru]"]
    assert float(f["price"]) >= 150
    assert saved["funpay"]["lots"][0]["offer_id"] == 77001 and engine.cfg.funpay.lots[0]["node_id"] == 1808


def test_existing_key_lot_adopted():
    db, notifier, fp, steam, engine, analytics = make(lots=[])
    engine.fp = LotFP(own=[OwnLot("555", "Насмешка: Конга", 300.0, 1, True),
                           OwnLot("556", "Ключ от ящика Манн Ко", 160.0, 20, True)])
    rep = asyncio.run(ensure_key_lot(engine))
    assert "556" in rep and engine.fp.sent is None


def test_golden_key_from_starsbot(tmp_path):
    (tmp_path / "starsbot").mkdir()
    (tmp_path / "starsbot" / "config.yaml").write_text(yaml.safe_dump(
        {"funpay": {"golden_key": "gk-from-stars"},
         "notifications": {"telegram": {"bot_token": "123:abc", "admin_chat_ids": [5], "enabled": True}}}),
        encoding="utf-8")
    (tmp_path / "mannbot").mkdir()
    cfg = load_config(None, {})
    taken = inherit_shared(cfg, tmp_path / "mannbot")
    assert cfg.funpay.golden_key == "gk-from-stars" and "golden_key" in taken
    assert cfg.notifications.telegram.commands is False      # один токен — команды слушает только StarsBot
    cfg2 = load_config(None, {"funpay": {"golden_key": "own"}})
    inherit_shared(cfg2, tmp_path / "mannbot")
    assert cfg2.funpay.golden_key == "own"
