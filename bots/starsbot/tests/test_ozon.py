import asyncio

from test_engine import boot, make

from starsbot.config import load_config, validate_config
from starsbot.core.models import OrderStatus as S
from starsbot.crm.db import Database
from starsbot.ozon import texts as T
from starsbot.ozon.client import parse_posting
from starsbot.ozon.tgbot import ActivationBot

OZON = {"enabled": True, "products": [{"stars": n, "offer_id": f"stars-{n}"} for n in (50, 100, 250, 500)]}


def setup(tmp_path, **ozon):
    sup, e, fp, wallet = make(tmp_path, ozon={**OZON, **ozon})
    ch = e.channels["ozon"]
    e.cfg.rtba.fallback_ton_per_star = 0.0115     # цена звезды до первой покупки — нужна для остатков на Ozon
    ch.bot = ActivationBot("", ch)          # без токена: сообщения только складываются в bot.sent
    e.fragment.users.update({u: u.title() for u in ("alice_tg", "bob_tg", "good_user", "late_user", "mallory_tg",
                                                     *(f"user_{i}_tg" for i in range(10)))})
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


def test_parse_posting_v2():
    p = parse_posting({
        "posting_number": "1-2-3", "order_number": "1-2", "status": "awaiting_packaging",
        "waiting_deadline_for_digital_code": "2026-10-23T20:00:00Z",
        "products": [{"sku": 7, "offer_id": "stars-50", "name": "Stars", "quantity": 2,
                      "price": {"amount": 120, "currency_code": "RUB"}, "required_qty_for_digital_code": 2}],
        "financial_data": {"products": [{"product_id": 7, "payout": 216.0, "commission": {"amount": 24}}]}})
    assert p.needs_codes and not p.cancelled
    pr = p.products[0]
    assert (pr.sku, pr.offer_id, pr.price, pr.required_codes, pr.payout, pr.commission) == (7, "stars-50", 120.0, 2,
                                                                                             216.0, 24.0)
    assert parse_posting({"posting_number": "x", "status": "cancelled", "products": []}).cancelled


def test_codes():
    c = T.new_code("tgs")
    assert c.startswith("TGS-") and len(c) == 18 and not set(c) & set("01OIL")
    assert T.normalize_code(c.lower().replace("-", " ")) == T.normalize_code(c)
    assert T.find_code(f"мой код: {c.lower()} спасибо") == c
    assert T.find_code("durov2024abc") is None          # username не путаем с кодом
    assert T.find_code("ABCD2345EFGH") == "ABCD2345EFGH"


def test_ozon_order_full_cycle(tmp_path):
    sup, e, ch, oz, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        number = oz.buyer_buys("stars-100", 100, qty=2, price=170.0)
        await ch.poll()
        codes = [oz.code_of(number, i) for i in range(2)]
        # покупатель вводит код и username одним сообщением, второй код — в два шага
        r1 = await ch.bot.handle(11, f"{codes[0]} @alice_tg")
        await settle(e, wallet)
        r2a = await ch.bot.handle(12, codes[1])
        r2b = await ch.bot.handle(12, "bob_tg")
        await settle(e, wallet)
        return number, codes, r1, r2a, r2b
    number, codes, r1, r2a, r2b = asyncio.run(go())
    orders = sup.db.q("SELECT * FROM orders WHERE channel='ozon' ORDER BY order_id")
    assert [o["order_id"] for o in orders] == [f"OZ-{number}-1", f"OZ-{number}-2"]
    assert all(o["status"] == S.COMPLETED.value for o in orders), [o["status"] for o in orders]
    assert [o["tg_username"] for o in orders] == ["alice_tg", "bob_tg"]
    assert all(o["stars"] == 100 and o["price"] == 170.0 and o["payout"] == 153.0 for o in orders)
    assert oz.postings[number]["status"] == "delivered"          # коды приняты Ozon
    assert "username" in r2a.lower()
    sent = {cid: [t for c, t in ch.bot.sent if c == cid] for cid in (11, 12)}
    assert any("Готово" in t and "@alice_tg" in t for t in sent[11])
    assert any("Готово" in t and "@bob_tg" in t for t in sent[12])
    assert not any("подтвердите" in t.lower() for c, t in ch.bot.sent)   # на Ozon подтверждать нечего
    # ни одного сообщения в FunPay
    assert not e.fp.outbox
    # повторная активация того же кода — только статус, без второй покупки
    again = asyncio.run(ch.redeem(codes[0], "mallory_tg", tg_chat_id=99))
    assert "@alice_tg" in again.text
    assert sup.db.scalar("SELECT COUNT(*) FROM purchases WHERE order_id=?", (orders[0]["order_id"],)) == 1
    # экономика: комиссия Ozon по фактической выплате
    f = sup.econ.order_flow(sup.db.get_order(orders[0]["order_id"]))
    assert f.funpay_fee == 17.0 and f.net_rub == 153.0
    assert sup.econ.period(1)["by_channel"]["ozon"]["orders"] == 2


def test_bad_username_and_code(tmp_path):
    sup, e, ch, oz, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        number = oz.buyer_buys("stars-50", 50)
        await ch.poll()
        code = oz.code_of(number)
        bad = await ch.bot.handle(5, "TGS-AAAA-BBBB-CCCC")
        await ch.bot.handle(5, code)
        nf = await ch.bot.handle(5, "@nobody_here")          # FakeFragment: «nobody» — не найден
        ok = await ch.bot.handle(5, "@good_user")
        await settle(e, wallet)
        return number, bad, nf, ok
    number, bad, nf, ok = asyncio.run(go())
    assert bad == T.BOT["bad_code"]
    o = sup.db.get_order(f"OZ-{number}-1")
    assert o["status"] == S.COMPLETED.value and o["tg_username"] == "good_user"


def test_cancel_before_activation_and_stock(tmp_path):
    sup, e, ch, oz, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        number = oz.buyer_buys("stars-250", 250)
        await ch.poll()
        code = oz.code_of(number)
        committed = sup.db.stars_committed()
        oz.cancel(number)
        await ch.poll()
        res = await ch.redeem(code, "late_user")
        await e.wallet_watch()
        return number, committed, res
    number, committed, res = asyncio.run(go())
    assert committed == 250                      # невыкупленный код резервирует звёзды на кошельке
    o = sup.db.get_order(f"OZ-{number}-1")
    assert o["status"] == S.REFUNDED.value and o["fail_reason"] == "ozon_cancelled"
    assert not res.ok and res.text == T.BOT["cancelled_code"]
    assert sup.db.stars_committed() == 0
    # остатки на Ozon выставлены по балансу кошелька: 50 ⭐ хватает на больше раз, чем 500 ⭐
    assert oz.stocks["stars-50"] >= oz.stocks["stars-500"] > 0


def test_upload_retry_and_no_reminders(tmp_path):
    sup, e, ch, oz, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        oz.fail_uploads = 2
        number = oz.buyer_buys("stars-50", 50)
        await ch.poll()
        st1 = sup.db.one("SELECT codes_state FROM ozon_postings WHERE posting_number=?", (number,))["codes_state"]
        await ch.poll()
        st2 = sup.db.one("SELECT codes_state FROM ozon_postings WHERE posting_number=?", (number,))["codes_state"]
        sup.db.exec("UPDATE orders SET paid_at=paid_at-7200")
        await e.timers()
        return number, st1, st2
    number, st1, st2 = asyncio.run(go())
    assert (st1, st2) == ("pending", "uploaded")
    assert oz.code_of(number)
    kinds = [r["kind"] for r in sup.db.events(f"OZ-{number}-1")]
    assert "reminder" not in kinds and "sla_alert" not in kinds


def test_risk_does_not_lump_ozon_buyers(tmp_path):
    sup, e, ch, oz, wallet = setup(tmp_path)

    async def go():
        await boot(e)
        nums = [oz.buyer_buys("stars-50", 50) for _ in range(8)]
        await ch.poll()
        for i, n in enumerate(nums):
            await ch.redeem(oz.code_of(n), f"user_{i}_tg")
        await settle(e, wallet)
        return nums
    nums = asyncio.run(go())
    st = [sup.db.get_order(f"OZ-{n}-1")["status"] for n in nums]
    assert st.count(S.COMPLETED.value) == 8, st


def test_migration_adds_channel(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = Database(path)                  # база прошлой версии: без колонок channel и payout
    db.conn.execute("DROP INDEX idx_orders_channel")
    db.conn.execute("ALTER TABLE orders DROP COLUMN channel")
    db.conn.execute("ALTER TABLE orders DROP COLUMN payout")
    db.create_order(order_id="A1", status="completed")
    db.conn.close()
    db = Database(path)
    assert db.get_order("A1")["channel"] == "funpay"
    assert db.get_order("A1")["payout"] is None


def test_validate_ozon():
    cfg = load_config(None, {"general": {"demo": False}, "ozon": {"enabled": True}})
    probs = [p for p in validate_config(cfg) if p.startswith("ozon")]
    assert len(probs) == 3


def test_panel_and_activation_page(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from starsbot.app.launcher import DEMO_OVERRIDES
    from starsbot.app.supervisor import Supervisor
    from starsbot.demo import run_scenario
    from starsbot.ozon.fake import DEMO_PRODUCTS, run_ozon_demo
    from starsbot.ozon.web import ActivationWeb
    from starsbot.web.server import make_app

    sup = Supervisor(tmp_path, demo=True, overrides={**DEMO_OVERRIDES, "ozon": {"enabled": True,
                                                                                "products": DEMO_PRODUCTS}})
    e = sup.engine
    e.cfg.deal.message_rate_limit_seconds = 0
    e.cfg.deal.purchase_pause_seconds = 0

    async def go():
        await run_scenario(e, e.fp, e.wallet, verbose=False)
        res = await run_ozon_demo(e, e.wallet)
        a, b, c = res["postings"]
        client = TestClient(TestServer(make_app(sup)))
        await client.start_server()
        await client.get(f"/login?token={sup.ensure_token()}")
        for path in ["/ozon", "/money", "/orders?channel=ozon", f"/orders/OZ-{a}-1", f"/orders/OZ-{c}-1", "/"]:
            r = await client.get(path)
            assert r.status == 200, path
        page = await (await client.get("/ozon")).text()
        assert "tg-stars-200" in page and "Инструкция" in page
        assert "Калькулятор: Ozon" in await (await client.get("/money")).text()
        calc = await (await client.post("/api/calc", json={"prices": {"100": 200}, "channel": "ozon"})).json()
        row = next(x for x in calc["rows"] if x["stars"] == 100)
        assert row["funpay_fee"] == 20.0                            # комиссия Ozon 10%
        r = await client.post("/ozon", data={"action": "products", "offer_50": "new-50", "extra_stars": "1000",
                                             "extra_offer": "tg-1000"})
        assert r.status == 200
        prods = {p["stars"]: p["offer_id"] for p in sup.raw["ozon"]["products"]}   # в демо поверх — DEMO_PRODUCTS
        assert prods[50] == "new-50" and prods[1000] == "tg-1000"
        await client.close()

        # публичная страница активации
        ch = e.channels["ozon"]
        web_ = TestClient(TestServer(ActivationWeb(ch).app()))
        await web_.start_server()
        code = ch.client.code_of(b)
        assert (await web_.get(f"/s/{T.normalize_code(code)}")).status == 200
        assert code not in await (await web_.get(f"/s/{T.normalize_code(code)}")).text()   # до активации — пусто
        r = await web_.post("/", data={"code": "TGS-AAAA-BBBB-CCCC", "username": "@petr_tg"})
        assert "Код не найден" in await r.text()
        r = await web_.post("/", data={"code": code.lower(), "username": "@petr_tg"})
        assert r.status == 200 and "200 ⭐" in await r.text()
        await web_.close()
        for _ in range(3):
            while not e.queue.empty():
                oid = e.queue.get_nowait()
                e._queued.discard(oid)
                await e.purchase(oid)
                e.wallet.tick()
                await e.watch_purchases()
            e.wallet.tick()
            await e.watch_purchases()
        return a, b, c
    a, b, c = asyncio.run(go())
    assert sup.db.get_order(f"OZ-{a}-1")["status"] == S.COMPLETED.value
    assert sup.db.get_order(f"OZ-{b}-1")["status"] == S.COMPLETED.value
    assert sup.db.get_order(f"OZ-{b}-1")["tg_username"] == "petr_tg"
    assert sup.db.get_order(f"OZ-{c}-1")["status"] == S.REFUNDED.value
