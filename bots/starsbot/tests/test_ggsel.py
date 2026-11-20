import asyncio
import hashlib

from test_engine import boot, make

from starsbot.config import load_config, validate_config
from starsbot.core.models import OrderStatus as S
from starsbot.ggsel import texts as T
from starsbot.ggsel.channel import username_from_options
from starsbot.ggsel.client import GgselClient, parse_message, parse_order, parse_sale
from starsbot.ggsel.fake import DEMO_PRODUCTS, run_ggsel_demo

GGSEL = {"enabled": True, "products": DEMO_PRODUCTS, "price_mode": "manual", "commission_pct": 10.0}


def setup(tmp_path, **gg):
    sup, e, fp, wallet = make(tmp_path, ggsel={**GGSEL, **gg})
    ch = e.channels["ggsel"]
    e.cfg.rtba.fallback_ton_per_star = 0.0115
    e.fragment.users.update({u: u.title() for u in ("alice_tg", "bob_tg", "nika_tg", "ivan_tg", "lena_tg")})
    sup.db.kv_set("ggsel_initialized", True)
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


def test_parsers():
    s = parse_sale({"invoice_id": 15, "date": "2026-11-20", "product": {"id": 5100100, "name": "Stars", "price_rub": "19.99"}})
    assert (s.invoice_id, s.product_id, s.price) == (15, 5100100, 19.99)
    o = parse_order(15, {"retval": 0, "content": {"invoice_state": 3, "item_id": 7, "amount": "175.5", "cnt_goods": "2",
                                                 "options": [{"id": 1, "name": "Username", "user_data": "@durov"}]}})
    assert o.paid and o.quantity == 2 and o.amount == 175.5 and o.options[0]["value"] == "@durov"
    assert parse_message({"id": 3, "message": "hi", "buyer": 1, "seller": 0}).from_buyer
    assert not parse_message({"id": 4, "message": "ok", "buyer": 0, "seller": 1}).from_buyer
    assert username_from_options([{"name": "Ваш ник в Telegram", "value": "t.me/durov"}])[0] == "durov"
    assert username_from_options([{"name": "Комментарий", "value": "@durov"}])[0] == "durov"   # единственное поле
    assert username_from_options([{"name": "Ник", "value": "дуров"}]) == (None, "дуров")


def test_login_sign():
    c = GgselClient(123, "secret")
    seen = {}

    async def fake_raw(method, path, *, params=None, body=None, headers=None):
        seen.update(body)
        return {"retval": 0, "token": "tok", "valid_thru": "2099-01-01T00:00:00Z"}
    c._raw = fake_raw
    assert asyncio.run(c.login()) == "tok"
    assert seen["seller_id"] == 123
    assert seen["sign"] == hashlib.sha256(("secret" + seen["timestamp"]).encode()).hexdigest()


def test_texts_follow_ggsel_rules():
    for key, text in T.TEMPLATES.items():
        low = text.lower()
        assert "funpay" not in low and "http" not in low and "t.me" not in low, key
        assert "вечн" not in low, key                                     # п. 1.13
    assert "http" not in T.PRODUCT_DESCRIPTION and "t.me" not in T.PRODUCT_DESCRIPTION
    assert T.sanitize("Пишите на FunPay https://funpay.com/x или t.me/bot") == "Пишите на GGSEL  или"


def test_full_cycle(tmp_path):
    sup, e, ch, gg, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        res = await run_ggsel_demo(e, wallet)
        return res["invoices"]
    a, b, c, d = asyncio.run(go())
    o = {x: sup.db.get_order(f"GG-{x}") for x in (a, b, c, d)}
    assert o[a]["status"] == S.COMPLETED.value and o[a]["tg_username"] == "nika_tg"     # username из формы, подтвердил
    assert o[b]["status"] == S.DELIVERED.value and o[b]["tg_username"] == "lena_tg"     # username из чата
    assert o[c]["status"] == S.DELIVERED.value and o[c]["tg_username"] == "ivan_tg"     # со второй попытки
    assert o[c]["username_attempts"] == 1
    assert o[d]["status"] == S.REFUNDED.value                                            # возврат до выдачи
    assert o[a]["price"] == 175.0 and o[a]["payout"] == 157.5 and o[a]["stars"] == 100
    # покупателю писали только в чат GGSEL, ни слова на FunPay
    assert not e.fp.outbox
    for inv in (a, b, c, d):
        for t in gg.seller_texts(inv):
            assert "funpay" not in t.lower() and "http" not in t.lower(), t
    assert any("Готово" in t and "@nika_tg" in t for t in gg.seller_texts(a))
    assert any("пришлите" in t.lower() for t in gg.seller_texts(b))
    assert any("Спасибо за подтверждение" in t for t in gg.seller_texts(a))
    assert any(f"#{a}" in t for t in gg.seller_texts(a))                                 # номер счёта GGSEL
    assert len([t for t in gg.seller_texts(a) if "Заказ" in t or "когда" in t.lower()]) >= 1   # ответ на вопрос
    # одна покупка на заказ
    for inv in (a, b, c):
        assert sup.db.scalar("SELECT COUNT(*) FROM purchases WHERE order_id=?", (f"GG-{inv}",)) == 1
    assert sup.econ.period(1)["by_channel"]["ggsel"]["orders"] == 3
    # остатки на GGSEL выставлены по балансу кошелька
    assert gg.offers and all("quantity" in v for v in gg.offers.values())


def test_first_run_does_not_deliver_old_sales(tmp_path):
    sup, e, ch, gg, wallet = setup(tmp_path)
    sup.db.kv_set("ggsel_initialized", False)

    async def go():
        await boot(e)
        old = gg.buyer_buys(5100100, 175.0, "@alice_tg")
        await ch.poll()                                  # первый запуск: старую продажу не трогаем
        new = gg.buyer_buys(5100100, 175.0, "@bob_tg")
        await ch.poll()
        await settle(e, wallet)
        skipped = ch.summary()["skipped"]
        msg = await ch.take(old)
        await settle(e, wallet)
        return old, new, skipped, msg
    old, new, skipped, msg = asyncio.run(go())
    assert [r["invoice_id"] for r in skipped] == [old]
    assert "в работу" in msg
    assert sup.db.get_order(f"GG-{new}")["status"] == S.DELIVERED.value
    assert sup.db.get_order(f"GG-{old}")["status"] == S.DELIVERED.value


def test_shared_wallet_no_double_spend(tmp_path):
    """Кошелёк общий с FunPay: заказы GGSEL резервируют звёзды и уменьшают остатки на всех площадках."""
    sup, e, ch, gg, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        e.cfg.ton.fee_reserve_ton = 0
        before = sup.db.stars_committed()
        inv = gg.buyer_buys(5100500, 849.0, "")             # ждёт username — звёзды уже зарезервированы
        await ch.poll()
        mid = sup.db.stars_committed()
        await e.wallet_watch()
        return before, mid, inv
    before, mid, inv = asyncio.run(go())
    assert mid - before == 500
    assert sup.db.get_order(f"GG-{inv}")["status"] == S.AWAITING_USERNAME.value


def test_margin_pricing_and_low_margin_hides(tmp_path):
    sup, e, ch, gg, wallet = setup(tmp_path, price_mode="margin", margin_pct=6.0, min_margin_pct=3.0)

    async def go():
        await boot(e)
        await e.wallet_watch()
    asyncio.run(go())
    p = next(x for x in ch.products() if x["stars"] == 100)
    price = ch.target_price(p)
    assert price and ch.margin_at(p, price) >= 6.0
    assert gg.offers[int(p["offer_id"])]["price"] == price
    # вручную ниже минимальной маржи — товар скрыт
    sup2, e2, ch2, gg2, w2 = setup(tmp_path / "b", products=[{"stars": 100, "offer_id": "5100100", "price": 50}])

    async def go2():
        await boot(e2)
        await e2.wallet_watch()
    asyncio.run(go2())
    assert gg2.offers[5100100]["quantity"] == 0


def test_too_small_and_refund(tmp_path):
    sup, e, ch, gg, wallet = setup(tmp_path, products=[{"stars": 1, "offer_id": "5200001"}, *DEMO_PRODUCTS])

    async def go():
        await boot(e)
        small = gg.buyer_buys(5200001, 30.0, "@alice_tg", qty=20)          # 20 ⭐ — меньше минимума Fragment
        big = gg.buyer_buys(5200001, 150.0, "@bob_tg", qty=100)            # 100 ⭐ «за звезду»
        wait = gg.buyer_buys(5100250, 429.0, "")
        await ch.poll()
        await settle(e, wallet)
        msg = await e.admin_refund(f"GG-{wait}")
        gg.refund(wait)
        ch.c.refresh_seconds = 0
        await ch.poll()
        return small, big, wait, msg
    small, big, wait, msg = asyncio.run(go())
    assert sup.db.get_order(f"GG-{small}")["status"] == S.MANUAL.value
    b = sup.db.get_order(f"GG-{big}")
    assert b["status"] == S.DELIVERED.value and b["stars"] == 100
    assert "кабинете GGSEL" in msg
    assert sup.db.get_order(f"GG-{wait}")["status"] == S.REFUNDED.value


def test_validate_ggsel():
    cfg = load_config(None, {"general": {"demo": False}, "ggsel": {"enabled": True}})
    probs = [p for p in validate_config(cfg) if p.startswith("ggsel")]
    assert any("ID продавца" in p for p in probs) and any("ID товара" in p for p in probs)


def test_web_pages(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from starsbot.web.server import make_app
    sup, e, ch, gg, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        res = await run_ggsel_demo(e, wallet)
        a = res["invoices"][0]
        client = TestClient(TestServer(make_app(sup)))
        await client.start_server()
        await client.get(f"/login?token={sup.ensure_token()}")
        for path in ["/ggsel", "/money", "/orders?channel=ggsel", f"/orders/GG-{a}", "/", "/settings"]:
            r = await client.get(path)
            assert r.status == 200, path
        page = await (await client.get("/ggsel")).text()
        assert "5100100" in page and "Как оформить товар" in page
        assert "Калькулятор: GGSEL" in await (await client.get("/money")).text()
        calc = await (await client.post("/api/calc", json={"prices": {"100": 200}, "channel": "ggsel"})).json()
        assert next(x for x in calc["rows"] if x["stars"] == 100)["funpay_fee"] == 20.0
        r = await client.post("/ggsel", data={"action": "products", "offer_50": "777", "price_50": "99",
                                              "price_mode": "manual", "min_margin_pct": "4"})
        assert r.status == 200
        gg_raw = sup.raw["ggsel"]
        assert gg_raw["price_mode"] == "manual" and gg_raw["min_margin_pct"] == 4
        assert {p["stars"]: p["offer_id"] for p in gg_raw["products"]}[50] == "777"
        await client.close()
    asyncio.run(go())
