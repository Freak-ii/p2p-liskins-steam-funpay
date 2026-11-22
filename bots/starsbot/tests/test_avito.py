import asyncio

from test_engine import boot, make

from starsbot.avito.client import parse_chat, parse_message
from starsbot.config import load_config, validate_config
from starsbot.core.models import OrderStatus as S

AVITO = {"enabled": True, "products": [{"stars": n, "item_id": 900 + n, "price": p} for n, p in
                                       ((50, 95), (100, 180), (250, 440))]}


def setup(tmp_path, **avito):
    sup, e, fp, wallet = make(tmp_path, avito={**AVITO, **avito})
    ch = e.channels["avito"]
    e.cfg.rtba.fallback_ton_per_star = 0.0115
    e.fragment.users.update({u: u.title() for u in ("anna_tg", "ivan_tg", "petr_tg")})
    return sup, e, ch, ch.client, wallet


async def settle(e, wallet):
    for _ in range(3):
        while not e.queue.empty():
            oid = e.queue.get_nowait()
            e._queued.discard(oid)
            await e.purchase(oid)
            wallet.tick()
            await e.watch_purchases()
        wallet.tick()
        await e.watch_purchases()


def test_parse():
    c = parse_chat({"id": "u2i-1", "context": {"type": "item", "value": {"id": 42, "title": "Stars 100"}},
                    "users": [{"id": 7, "name": "me"}, {"id": 9, "name": "Анна"}],
                    "last_message": {"id": "m1", "created": 1700000000, "direction": "in"}, "updated": 1700000001}, 7)
    assert (c.chat_id, c.item_id, c.buyer_id, c.buyer_name, c.last_created) == ("u2i-1", 42, 9, "Анна", 1700000000.0)
    m = parse_message({"id": "m2", "author_id": 0, "content": {"text": "Покупатель оплатил заказ"}, "type": "system",
                       "created": 5, "direction": "in"})
    assert m.is_system and m.text.startswith("Покупатель")


def test_presale_then_paid_with_username(tmp_path):
    sup, e, ch, av, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        cid = av.open_chat(1000, "Анна")                  # объявление 100 ⭐
        av.buyer_says(cid, "Здравствуйте, как купить?")
        await ch.poll()
        av.buyer_says(cid, "вот мой @anna_tg")
        await ch.poll()
        av.buyer_pays(cid)
        await ch.poll()
        await settle(e, wallet)
        av.buyer_confirms(cid)
        await ch.poll()
        return cid
    cid = asyncio.run(go())
    out = av.sent_to(cid)
    assert "Нажмите «Купить»" in out[0]                        # как купить — без ссылок и карт
    assert "@anna_tg" in out[1]                                 # username запомнили до оплаты
    assert any("отправлены на @anna_tg" in t for t in out)
    o = sup.db.one("SELECT * FROM orders WHERE channel='avito'")
    assert o["status"] == S.COMPLETED.value and o["tg_username"] == "anna_tg" and o["stars"] == 100
    assert o["chat_id"] == f"avito:{cid}" and o["price"] >= 180
    assert not any("FunPay" in t for t in out)


def test_paid_then_username_and_no_duplicate_order(tmp_path):
    sup, e, ch, av, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        cid = av.open_chat(1150, "Иван")
        av.buyer_pays(cid)
        av.buyer_pays(cid)                               # Авито повторил уведомление
        await ch.poll()
        av.buyer_says(cid, "ivan_tg")
        await ch.poll()
        await settle(e, wallet)
        return cid
    cid = asyncio.run(go())
    rows = sup.db.q("SELECT * FROM orders WHERE channel='avito'")
    assert len(rows) == 1 and rows[0]["stars"] == 250 and rows[0]["status"] == S.DELIVERED.value
    out = av.sent_to(cid)
    assert "Пришлите в этот чат username" in out[0]


def test_foreign_item_chats_ignored_and_seller_not_interrupted(tmp_path):
    sup, e, ch, av, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        other = av.open_chat(123456, "Чужой")            # объявление не со звёздами
        av.buyer_says(other, "Велосипед ещё продаётся?")
        mine = av.open_chat(950, "Пётр")
        av.seller_says(mine, "Добрый день, чем помочь?")
        av.buyer_says(mine, "А скидка будет?")
        await ch.poll()
        return other, mine
    other, mine = asyncio.run(go())
    assert av.sent_to(other) == []
    assert av.sent_to(mine) == ["Добрый день, чем помочь?"]     # продавец ведёт диалог сам


def test_cancel_before_delivery_refunds(tmp_path):
    sup, e, ch, av, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        cid = av.open_chat(950, "Пётр")
        av.buyer_pays(cid)
        await ch.poll()
        av.buyer_cancels(cid)
        await ch.poll()
    asyncio.run(go())
    o = sup.db.one("SELECT * FROM orders WHERE channel='avito'")
    assert o["status"] == S.REFUNDED.value


def test_price_sync_keeps_min_margin(tmp_path):
    sup, e, ch, av, wallet = setup(tmp_path, products=[{"stars": 100, "item_id": 1000, "price": 10}])

    async def go():
        await boot(e)
        await ch.price_sync()
    asyncio.run(go())
    fl = sup.econ.flow(100, float(av.prices[1000]), channel="avito")
    assert av.prices[1000] > 10 and fl.margin_pct >= 3.0 - 0.1          # цена 10 ₽ поднята до пола маржи
    floor = sup.econ.min_price(100, "avito", 3.0)
    assert abs(sup.econ.flow(100, floor, channel="avito").margin_pct - 3.0) < 0.15


def test_validation(tmp_path):
    cfg = load_config(None, overrides={"avito": {"enabled": True}})
    problems = validate_config(cfg)
    assert any(p.startswith("avito:") for p in problems) and any(p.startswith("avito.products") for p in problems)
