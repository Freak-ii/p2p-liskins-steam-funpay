"""TG Gift: NFT-подарки (Fragment через StarsBot или рынок Telegram), раздел «Звёзды → Подарком», автоподбор."""
import asyncio

from starsbot.core.models import CompetitorLot, FunPayOrder
from starsbot.giftshop.collections import find_collection, season_active
from starsbot.giftshop.engine import is_rent
from starsbot.giftshop.lots import build_nft_fields
from tests.test_giftshop import shop_sup, stars_sup


def test_find_collection_on_real_descriptions():
    assert find_collection("【Мишка / Медведь】【Toy Bear Gift】#ВСЕ ВИДЫ").slug == "toybear"
    assert find_collection("Lol Pops NFT подарок").slug == "lolpop"
    assert find_collection("Vice Cream — любая модель").slug == "vicecream"       # не Ice Cream
    assert find_collection("🧸 Мишка — подарок Telegram") is None
    assert find_collection("Lol Pop + Toy Bear набор") is None                  # набор — не угадываем
    assert season_active("winter", 12, 31) and season_active("winter", 1, 10) and not season_active("winter", 3, 1)
    assert is_rent(CompetitorLot("1", "x", 10, 1, fields={"type2": "аренда"}))


def test_nft_form_picks_type_and_sale():
    base = {"offer_id": "0", "fields[type]": "", "fields[type2]": "", "fields[summary][ru]": "", "price": ""}
    selects = {"fields[type]": [("", "-"), ("5", "Мишка"), ("6", "Леденец"), ("9", "Прочее")],
               "fields[type2]": [("", "-"), ("1", "Продажа"), ("2", "Аренда")]}
    f, notes = build_nft_fields(base, selects, {"kind": "nft", "collection": "lolpop", "name": "Леденец"}, price=470)
    assert f["fields[type]"] == "6" and f["fields[type2]"] == "1" and f["price"] == "470.00" and not notes
    assert "Lol Pop" in f["fields[summary][ru]"] and "active" not in f


def test_tggift_takes_stars_gift_section(tmp_path):
    shop = shop_sup(tmp_path).engine
    stars_gift = FunPayOrder("BBBB2222", "y", 2, "50 звёзд, Подарком", 90.0, subcategory="Telegram, Звёзды")
    by_name = FunPayOrder("BBBB3333", "y", 2, "50 звёзд, По username", 90.0, subcategory="Telegram, Звёзды")
    login = FunPayOrder("BBBB4444", "y", 2, "50 звёзд, С заходом на аккаунт", 90.0, subcategory="Telegram, Звёзды")
    assert shop.accepts_sale(stars_gift)
    assert not shop.accepts_sale(by_name) and not shop.accepts_sale(login)
    stars = stars_sup(tmp_path, legacy=False).engine
    assert not stars.accepts_sale(stars_gift)       # StarsBot отдал «Подарком» TG Gift
    assert stars.accepts_sale(by_name)


def test_demo_assortment_and_nft_order_via_telegram(tmp_path):
    async def go():
        sup = shop_sup(tmp_path)
        e = sup.engine
        e.cfg.giftshop["nft"] = {"rub_per_ton": 80.0}
        await e.lot_manager.create_missing()
        from starsbot.giftshop.demo import run_shop_demo
        ids = await run_shop_demo(e, e.fp)
        table = {r["slug"]: r for r in e.gifts.assortment.table()}
        assert table["winterwreath"]["chosen"] and table["lolpop"]["chosen"]
        assert not table["toybear"]["chosen"]                       # маржи нет
        assert not table["plushpepe"]["chosen"]                     # дорогой / нет пути — только вручную
        assert all(l.get("offer_id") for l in e.gifts.nft_lots)     # выбранные выставлены
        c = e.db.get_order(ids[2])
        assert c["status"] == "delivered" and c["stars"] == 43     # «Подарком» из раздела «Звёзды»
        nft = e.db.get_order(ids[3])
        assert nft["status"] == "delivered"
        sends = e.db.q("SELECT * FROM nft_sends WHERE order_id=?", (ids[3],))
        assert [s["route"] for s in sends] == ["telegram"] and sends[0]["state"] == "sent"
        assert e.gifts.tg.bought and e.gifts.tg.bought[0][0] == "ned_nft"
        nft_prices = e.db.q("SELECT * FROM price_history WHERE stars=1")
        assert nft_prices and all(p["new_price"] >= p["floor"] for p in nft_prices)
    asyncio.run(go())


def test_nft_no_margin_blocks_lot(tmp_path):
    async def go():
        sup = shop_sup(tmp_path)
        e, g = sup.engine, sup.engine.gifts
        e.cfg.giftshop["nft"] = {"rub_per_ton": 80.0}
        await e.funpay_poll()
        e.lot_manager.add_nft(g.nft.collection("toybear"))
        await g.nft.quote(g.nft.collection("toybear"))
        await e.lot_manager.create_missing(kinds=["shop"])
        lot = next(l for l in g.nft_lots if l["collection"] == "toybear")
        oid = e.fp.buyer_buys_nft("Ann", 46, lot, "ann_tg", price=900.0)     # дешевле себестоимости
        for _ in range(3):
            await e.funpay_poll()
            while not e.queue.empty():
                x = e.queue.get_nowait()
                e._queued.discard(x)
                await e.purchase(x)
        o = e.db.get_order(oid)
        assert o["status"] == "manual" and not g.tg.bought
        assert g.nft.blocked(lot) and g.nft.stock_amount(lot) == 0
    asyncio.run(go())


def test_nft_via_fragment_through_starsbot(tmp_path):
    """TG Gift просит NFT у StarsBot через общий файл: тот покупает на Fragment и передаёт получателю."""
    async def go():
        coord = str(tmp_path / "coord.sqlite3")
        ssup = stars_sup(tmp_path, coord_db=coord, legacy=False)
        se = ssup.engine
        se.fragment_ok = True
        se._fragment_state(session="ok", checked_at=__import__("time").time())
        await se.wallet_watch()
        await se.nft.tick()                              # StarsBot сообщает курс и готовность
        sup = shop_sup(tmp_path, coord_db=coord)
        e, g = sup.engine, sup.engine.gifts
        assert g.nft.fragment_ready()
        g.tg.resale.clear()                              # на рынке Telegram нет — только Fragment
        await e.funpay_poll()
        e.lot_manager.add_nft(g.nft.collection("lolpop"))
        q = await g.nft.quote(g.nft.collection("lolpop"), fresh=True)
        assert q["best"] == "fragment"
        await e.lot_manager.create_missing(kinds=["shop"])
        lot = next(l for l in g.nft_lots if l["collection"] == "lolpop")
        price = round(float(q["cost_rub"]) * 1.5, 2)
        oid = e.fp.buyer_buys_nft("Bob", 47, lot, "bob_tg", price=price)

        async def drain(engine, wallet=None):
            for _ in range(4):
                if engine is e:
                    await engine.funpay_poll()
                while not engine.queue.empty():
                    x = engine.queue.get_nowait()
                    engine._queued.discard(x)
                    await engine.purchase(x)
                    if wallet is not None:
                        wallet.tick()
                        await engine.watch_purchases()
                if wallet is not None:
                    wallet.tick()
                    await engine.watch_purchases()

        await drain(e)
        assert e.db.get_order(oid)["status"] == "purchasing"
        job = se.coord.nft_jobs(("pending",))
        assert job and job[0]["collection"] == "lolpop" and job[0]["username"] == "bob_tg"
        for _ in range(4):
            await se.nft.tick()
            await drain(se, se.wallet)
            for row in se.db.q("SELECT order_id FROM orders WHERE order_id LIKE 'NFT-%'"):
                se._queued.discard(row["order_id"])
                se.enqueue(row["order_id"])
            await drain(se, se.wallet)
        done = se.coord.nft_job(job[0]["ref"])
        assert done["state"] == "done", done
        await g.watch()
        o = e.db.get_order(oid)
        assert o["status"] == "delivered" and float(o["cost_rub"] or 0) > 0
        assert "lolpop-236254" in se.fragment.nft_owned or done["item"] == "lolpop-236254"
    asyncio.run(go())


def test_fragment_gift_parsers():
    from pathlib import Path
    from starsbot.fragment.client import parse_gift_item, parse_gift_listings
    fx = Path(__file__).parent / "fixtures"
    rows = parse_gift_listings((fx / "fragment_gift_collection.html").read_text(encoding="utf-8"))
    assert [r["item"] for r in rows][:2] == ["lolpop-236254", "lolpop-238099"] and rows[0]["price"] == 5.0
    item = parse_gift_item((fx / "fragment_gift_item.html").read_text(encoding="utf-8"))
    assert item["for_sale"] and item["price"] and item["owner"]


def test_custom_denomination_uses_other_quantity():
    from starsbot.funpay.lots import build_lot_fields
    base = {"fields[quantity]": "", "fields[quantity2]": "", "fields[method]": "", "price": "", "amount": ""}
    selects = {"fields[quantity]": [("1", "50 звёзд"), ("2", "100 звёзд"), ("99", "Другое количество")],
               "fields[method]": [("1", "По username"), ("2", "Подарком")]}
    f = build_lot_fields(base, selects, stars=750, kind="username", price=1100, amount=5)
    assert f["fields[quantity]"] == "99" and f["fields[quantity2]"] == "750" and f["fields[method]"] == "1"
    from starsbot.core.rtba import lot_stars
    assert lot_stars(CompetitorLot("1", "x", 1, 1, fields={"quantity": "другое количество", "quantity2": "750"})) == 750


def test_shop_page_shows_nft_and_topup(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from starsbot.web.server import make_app

    async def go():
        sup = shop_sup(tmp_path)
        e = sup.engine
        e.cfg.giftshop["nft"] = {"rub_per_ton": 80.0}
        await e.lot_manager.create_missing()
        from starsbot.giftshop.demo import run_shop_demo
        await run_shop_demo(e, e.fp)
        token = sup.ensure_token()
        async with TestClient(TestServer(make_app(sup))) as c:
            await c.get(f"/login?token={token}", allow_redirects=False)
            for path in ("/", "/shop"):
                r = await c.get(path)
                assert r.status == 200, path
            html = await (await c.get("/shop")).text()
            assert "Подбор NFT по рынку" in html and "Звёзды → Подарком" in html and "Winter Wreath" in html
            r = await c.post("/shop", data={"action": "save_lot", "list": "topup", "i": "0", "offer_id": "555",
                                            "position": "2", "price": "", "sell": "on"}, allow_redirects=False)
            assert r.status == 302 and sup.cfg.giftshop.topup_lots[0]["offer_id"] == 555
            r = await c.post("/shop", data={"action": "add_nft", "collection": "Plush Pepe"}, allow_redirects=False)
            assert r.status == 302
            assert any(l.get("collection") == "plushpepe" and l.get("manual") for l in sup.cfg.giftshop.lots)
            r = await c.post("/shop", data={"action": "assort_scan"}, allow_redirects=False)
            assert r.status == 302
    asyncio.run(go())
