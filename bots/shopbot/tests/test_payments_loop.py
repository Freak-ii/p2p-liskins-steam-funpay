"""Оплата на общий кошелёк TON, подарками и скинами; учёт круга денег; мини-приложение."""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

from shopbot.config import Config
from shopbot.db import Database
from shopbot.demo import ALICE, BOB, buy, last_button, settle
from shopbot.engine import Shop
from shopbot.channel import Channel
from shopbot.payments import DEMO_CHAIN, TonWallet
from shopbot.pricing import apply_method, wallet_spread
from shopbot.tg import ShopTelegram


def make(tmp_path, **over):
    cfg = Config(tmp_path, demo=True, overrides=over)
    shop = Shop(cfg, Database(":memory:"), demo=True)
    bot = ShopTelegram(shop)
    Channel(shop)
    return shop, bot


def test_wallet_spread_in_rate_and_ton_payment_lands_in_loop(tmp_path):
    shop, bot = make(tmp_path)
    DEMO_CHAIN.clear()

    async def go():
        await shop.rtba.tick()
        assert shop.rtba.is_open("pay.wallet")
        await bot.on_text(ALICE, "alice", "Alice", "/start")
        oid = await buy(bot, ALICE, ["buy:stars", "st:250", "to:me"], "pay:wallet:TON")
        o = shop.db.order(oid)
        assert o["pay_method"] == "wallet" and o["pay_currency"] == "TON" and o["fee_rub"] == 0
        # спред внутри курса: курс клиента = биржа × (1 − 3%)
        market = shop.rates.rub_per("TON")
        assert abs(o["rate"] - round(market * (1 - wallet_spread(shop.cfg) / 100), 2)) < 0.01
        assert o["status"] == "delivered"
        msg = next(m["text"] for m in bot.outbox if m["chat"] == ALICE and "Комментарий" in m["text"])
        assert f"DPB-{oid}" in msg
        inflow = shop.db.one("SELECT * FROM inflows WHERE order_id=?", (oid,))
        assert inflow["state"] == "in_loop" and inflow["market_rub"] > inflow["rub"] * 0.99
    asyncio.run(go())


def test_wallet_usdt_amounts_are_unique_and_wrong_comment_is_ignored(tmp_path):
    shop, bot = make(tmp_path)

    async def go():
        await shop.rtba.tick()
        for uid in (ALICE, BOB):
            await bot.on_text(uid, f"u{uid}", "U", "/start")
        a = await buy(bot, ALICE, ["buy:stars", "st:100", "to:me"], "pay:wallet:USDT", pay=False)
        b = await buy(bot, BOB, ["buy:stars", "st:100", "to:me"], "pay:wallet:USDT", pay=False)
        oa, ob = shop.db.order(a), shop.db.order(b)
        assert oa["pay_amount"] != ob["pay_amount"]
        # TON-перевод с чужим комментарием не засчитывается
        w = TonWallet("UQ" + "A" * 46)
        DEMO_CHAIN.clear()

        async def fetch(path, params):
            return {"transactions": [{"in_msg": {"value": str(10 ** 10), "message_content": {
                "decoded": {"comment": "DPB-other"}}}}]}
        w._fetch = fetch
        pay = await w.create("X1", 100, "t", asset="TON", pay_amount=1.5)
        assert await w.check(pay["id"]) == "pending"
        assert pay["url"].startswith("https://app.tonkeeper.com/transfer/") and "text=DPB-X1" in pay["url"]
    asyncio.run(go())


def test_gift_and_skins_payment_need_operator_and_route_to_loop(tmp_path):
    shop, bot = make(tmp_path)

    async def go():
        await shop.rtba.tick()
        await bot.on_text(ALICE, "alice", "Alice", "/start")
        g = await buy(bot, ALICE, ["buy:stars", "st:100", "to:me"], "pay:gift", pay=False)
        o = shop.db.order(g)
        assert o["status"] == "awaiting_asset" and o["pay_currency"] == "GIFT" and o["pay_amount"] > 0
        assert await shop.confirm_asset(g, who="test") == "оплата активом принята, выдаю"
        await settle(shop)
        assert shop.db.order(g)["status"] == "delivered"
        assert shop.db.one("SELECT state FROM inflows WHERE order_id=?", (g,))["state"] == "to_convert"
        # скины: отклонённая неоплаченная заявка не даёт возврат на баланс
        s = await buy(bot, ALICE, ["buy:stars", "st:100", "to:me"], "pay:skins", pay=False)
        assert shop.db.order(s)["status"] == "awaiting_asset"
        assert await shop.reject(s, "обмен не пришёл") == "отменено"
        assert shop.db.user(ALICE)["balance_rub"] == 0
        summ = shop.loop.summary()
        assert summ["idle_rub"] > 0 and "Вне круга" in shop.loop.todo_text()
        iid = shop.db.scalar("SELECT id FROM inflows WHERE order_id=?", (g,))
        assert shop.loop.mark_done(iid) == "отмечено: вернулось в круг"
        assert shop.loop.summary()["idle_rub"] == 0
    asyncio.run(go())


def test_sold_stars_are_frozen_21_days_then_ready(tmp_path):
    shop, bot = make(tmp_path)

    async def go():
        await shop.rtba.tick()
        await bot.on_text(BOB, "bob", "Bob", "/start")
        o, pay = await shop.create_sell(BOB, "stars", 500, "usdt")
        shop.providers["stars"].pay(pay["id"])
        await settle(shop)
        r = shop.db.one("SELECT * FROM inflows WHERE order_id=?", (o["id"],))
        assert r["method"] == "stars" and r["asset"] == "XTR" and r["state"] == "hold"
        shop.db.exec("UPDATE inflows SET ready_at=0")
        shop.loop.tick()
        assert shop.db.one("SELECT state FROM inflows WHERE id=?", (r["id"],))["state"] == "ready"
    asyncio.run(go())


def test_card_is_off_by_default_outside_demo(tmp_path):
    cfg = Config(tmp_path)
    assert cfg.get("payments.card.enabled") is False
    assert cfg.get("payments.wallet.spread_pct") == 3.0


def test_miniapp_page_public_and_menu_button(tmp_path):
    from shopbot.__main__ import App
    from shopbot.miniapp import TUNNEL_RE, page_query
    from shopbot.web.server import make_app
    app = App(tmp_path, demo=True)
    calls = []

    async def fake_call(method, **params):
        calls.append((method, params))
        return True
    app.bot.call = fake_call

    async def go():
        c = TestClient(TestServer(make_app(app)))
        await c.start_server()
        try:
            r = await c.get("/app", allow_redirects=False)
            assert r.status == 200 and "openTelegramLink" in await r.text()
            assert (await c.get("/loop", allow_redirects=False)).status == 302     # панель — только с токеном
        finally:
            await c.close()
        app.cfg.data["miniapp"]["url"] = "https://example.github.io/dpb-fastchange-app"
        await app.miniapp.start()
        assert app.miniapp.mode == "url" and app.miniapp.url.startswith("https://example.github.io/")
        m, p = calls[-1]
        assert m == "setChatMenuButton" and p["menu_button"]["type"] == "web_app"
        assert "bot=" in p["menu_button"]["web_app"]["url"]
    asyncio.run(go())
    assert TUNNEL_RE.search("INF |  https://quick-fox-12.trycloudflare.com  |")
    cfg = Config(tmp_path, overrides={"channel": {"chat_id": "@DPBFastchange"}})
    assert "channel=DPBFastchange" in page_query(cfg, "DPBFastchange_bot")
