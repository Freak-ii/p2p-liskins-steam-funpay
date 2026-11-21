"""TG Gift (профиль giftshop): заказы подарков, разделение с StarsBot и снабжение звёздами."""
import asyncio
import time

from starsbot.app.supervisor import Supervisor
from starsbot.coord import Coordinator
from starsbot.core.models import FunPayOrder
from starsbot.giftshop.profile import DEFAULT_SHOP_LOTS
from starsbot.giftshop.service import match_lot

BASE = {"general": {"demo": True, "dry_run": False, "database": ":memory:", "log_file": ""},
        "notifications": {"telegram": {"enabled": False}}, "gifts": {"pause_between_gifts": 0},
        "deal": {"message_rate_limit_seconds": 0}}


def shop_sup(tmp_path, coord_db=""):
    over = {**BASE, "general": {**BASE["general"], "profile": "giftshop"}, "funpay": {"coord_db": coord_db}}
    return Supervisor(tmp_path / "tggift", demo=True, overrides=over)


def stars_sup(tmp_path, coord_db=""):
    over = {**BASE, "funpay": {"coord_db": coord_db, "lots": [{"stars": n, "offer_id": i + 1} for i, n in
                                                             enumerate((50, 75, 100, 150, 250, 350, 500))]},
            "gifts": {"enabled": True, "pause_between_gifts": 0,
                      "lots": [{"stars": 50, "offer_id": 150}]}}
    return Supervisor(tmp_path / "starsbot", demo=True, overrides=over)


def test_match_lot_names_and_ambiguity():
    L = DEFAULT_SHOP_LOTS
    assert match_lot("🧸 Мишка — подарок в Telegram", L)["name"] == "Мишка"
    assert match_lot("Подарок мишка 15 звезд", L)["name"] == "Мишка"
    assert match_lot("Шампанского бутылка", L)["name"] == "Шампанское"
    assert match_lot("Telegram gift Teddy Bear", L)["name"] == "Мишка"
    assert match_lot("Мишка, который порадует", L)["name"] == "Мишка"     # «тор» в «который» — не торт
    assert match_lot("Мишка и Роза", L) is None                            # два подарка — не угадываем
    assert match_lot("50 звёзд, Подарком", L) is None


def test_shop_demo_orders_delivered_and_stars_order_ignored(tmp_path):
    async def go():
        sup = shop_sup(tmp_path)
        e = sup.engine
        assert type(e).__name__ == "GiftShopEngine" and sup.problems == []
        report = await e.lot_manager.create_missing()
        assert all("выставлен" in r for r in report) and len(report) == len(DEFAULT_SHOP_LOTS)
        from starsbot.giftshop.demo import run_shop_demo
        a, b = await run_shop_demo(e, e.fp)
        oa, ob = e.db.get_order(a), e.db.get_order(b)
        assert oa["status"] == "completed" and oa["tg_username"] == "kira_tg" and oa["stars"] == 15
        assert ob["status"] == "delivered" and ob["quantity"] == 2 and ob["gift_cost"] == 50
        sends = e.db.q("SELECT * FROM gift_sends WHERE state='sent'")
        assert len(sends) == 3 and {s["stars"] for s in sends} == {15, 25}
        assert e.db.scalar("SELECT COUNT(*) FROM orders") == 2          # заказ звёзд оставлен StarsBot
        texts = [t for _, t in e.fp.outbox]
        assert any("🌹 Роза × 2" in t for t in texts)
        assert any("Подарки Telegram выдаются автоматически" in t for t in texts)   # пресейл о подарках
        assert not any("Fragment" in t for t in texts)
        s = sup.analytics.summary(1)
        assert s["profit"] is not None and s["profit"] > 0                 # себестоимость — звёзды по ₽/звезду
        prices = e.db.q("SELECT * FROM price_history WHERE applied=1")
        assert prices and all(p["new_price"] >= p["floor"] for p in prices)
    asyncio.run(go())


def test_stars_bot_skips_gift_section(tmp_path):
    sup = stars_sup(tmp_path)
    e = sup.engine
    gift_cat = FunPayOrder("AAAA1111", "x", 1, "🎂 Торт 50 ⭐ подарок", 90.0, subcategory="Telegram, Подарки")
    stars_gift = FunPayOrder("BBBB2222", "y", 2, "50 звёзд, Подарком", 90.0, subcategory="Telegram, Звёзды")
    assert not e.accepts_sale(gift_cat)
    assert e.accepts_sale(stars_gift)
    shop = shop_sup(tmp_path).engine
    assert shop.accepts_sale(FunPayOrder("CCCC3333", "z", 3, "🎂 Торт — подарок Telegram", 90.0,
                                         subcategory="Telegram, Подарки"))
    assert not shop.accepts_sale(stars_gift)


def test_presale_claim_one_bot_answers(tmp_path):
    path = tmp_path / "coord.sqlite3"
    a, b = Coordinator(path, "starsbot"), Coordinator(path, "tggift")
    assert a.claim_presale("777")
    assert not b.claim_presale("777")
    assert a.claim_presale("777")


def test_supply_round_trip(tmp_path):
    """TG Gift просит звёзды → StarsBot покупает их на Fragment на @username аккаунта подарков."""
    async def go():
        coord_db = str(tmp_path / "coord.sqlite3")
        shop = shop_sup(tmp_path, coord_db)
        g = shop.engine.gifts
        g.tg.balance = 100                                     # мало звёзд: порог 300
        await g.watch()
        req = shop.engine.coord.supply_requests()
        assert req and req[0]["need"] == 1000 and req[0]["username"] == "my_shop_account"

        stars = stars_sup(tmp_path, coord_db)
        e = stars.engine
        assert e.supply.enabled
        await e.wallet_watch()
        await e.supply.tick()
        o = e.db.one("SELECT * FROM orders WHERE order_id LIKE 'SUPPLY-%'")
        assert o is not None and o["stars"] == 1000 and o["tg_username"] == "my_shop_account"
        await e.supply.tick()                                  # пока поставка идёт — новой нет
        assert e.db.scalar("SELECT COUNT(*) FROM orders WHERE order_id LIKE 'SUPPLY-%'") == 1
        for _ in range(4):
            while not e.queue.empty():
                oid = e.queue.get_nowait()
                e._queued.discard(oid)
                await e.purchase(oid)
            e.wallet.tick()
            await e.watch_purchases()
        o = e.db.get_order(o["order_id"])
        assert o["status"] == "completed" and o["cost_rub"]
        hist = shop.engine.coord.supply_history(g.account_key)
        assert hist and hist[0]["state"] == "done"
        assert abs(g.cost_per_star_rub() - o["cost_rub"] / 1000) < 1e-3
        await e.supply.tick()                                  # пауза после поставки
        assert e.db.scalar("SELECT COUNT(*) FROM orders WHERE order_id LIKE 'SUPPLY-%'") == 1
        assert e.supply.last[g.account_key]["result"].startswith("пауза")
    asyncio.run(go())


def test_shop_web_pages(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from starsbot.web.server import make_app

    async def go():
        sup = shop_sup(tmp_path)
        token = sup.ensure_token()
        async with TestClient(TestServer(make_app(sup))) as c:
            r = await c.get(f"/login?token={token}", allow_redirects=False)
            assert "tg_token" in r.cookies
            for path in ("/", "/shop", "/orders", "/gifts", "/settings", "/api/status"):
                assert (await c.get(path)).status == 200, path
            assert "giftshop" in await (await c.get("/api/ping")).text()
            r = await c.post("/shop", data={"action": "save_lot", "i": "0", "offer_id": "123", "position": "2",
                                            "price": "", "sell": "on"}, allow_redirects=False)
            assert r.status == 302
            assert sup.cfg.giftshop.lots[0]["offer_id"] == 123 and sup.cfg.giftshop.lots[0]["position"] == 2
    asyncio.run(go())


def test_shopbot_gift_supply_via_tggift(tmp_path):
    """ShopBot → TG Gift: заказ магазина «🌹 Роза» выдаётся именно этим подарком, а не набором на 25 ⭐."""
    from aiohttp.test_utils import TestClient, TestServer
    from starsbot.web.server import make_app
    token = "supply-token-1234567890"

    async def go():
        sup = shop_sup(tmp_path)
        sup.save({"dashboard": {"token": "admin-token-12345"}, "supply": {"token": token}}, rebuild=False)
        e = sup.engine
        await e.lot_manager.create_missing()
        await e.gifts.watch()
        c = TestClient(TestServer(make_app(sup)))
        await c.start_server()
        h = {"X-Supply-Token": token}
        try:
            ping = await (await c.get("/api/supply/ping", headers=h)).json()
            assert ping["app"] == "tggift" and "gift" in ping["products"]
            q = await (await c.get("/api/supply/quote", params={"product": "gift", "amount": 25, "gift": "🌹",
                                                                 "gift_title": "Роза"}, headers=h)).json()
            assert q["available"] and q["cost_rub"] > 0 and q["gift"]["name"] == "Роза", q
            # 15 ⭐ без эмодзи — Мишка или Сердце: без уточнения берётся свой лот, но не что попало
            q15 = await (await c.get("/api/supply/quote?product=gift&amount=15", headers=h)).json()
            assert q15["available"] and q15["gift"]["stars"] == 15
            none = await (await c.get("/api/supply/quote", params={"product": "gift", "amount": 15, "gift": "🐸"},
                                      headers=h)).json()
            assert not none["available"] and "каталоге" in none["reason"]
            body = {"ref": "G0001", "product": "gift", "username": "rose_fan", "amount": 25, "gift": "🌹",
                    "gift_title": "Роза"}
            r = await (await c.post("/api/supply/orders", json=body, headers=h)).json()
            assert r["ok"] and r["order_id"] == "SHOP-G0001", r
            o = e.db.get_order("SHOP-G0001")
            assert o["delivery"] == "gift" and "Роза" in o["description"] and o["lot_offer_id"]
            await e.purchase("SHOP-G0001")
            st = await (await c.get("/api/supply/orders/G0001", headers=h)).json()
            assert st["status"] == "delivered", (st, e.db.get_order("SHOP-G0001")["fail_reason"])
            sends = e.db.q("SELECT * FROM gift_sends WHERE order_id='SHOP-G0001' AND state='sent'")
            assert len(sends) == 1 and sends[0]["stars"] == 25
            raw = e.gifts.tg._catalog
            gift_ids = {g.id: g.emoji for g in (raw[1] if isinstance(raw, tuple) else raw)}
            assert gift_ids[int(sends[0]["gift_id"])] == "🌹"
            assert not [t for _, t in e.fp.outbox if "G0001" in t]       # покупателю пишет ShopBot, не FunPay-чат
        finally:
            await c.close()
            await sup.stop()
    asyncio.run(go())
