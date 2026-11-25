"""Оплата из Telegram Wallet (Wallet Pay): счёт в $, статусы, подпись вебхука, маршрут в круг."""
import asyncio
import base64
import hashlib
import hmac

from shopbot.config import Config
from shopbot.db import Database
from shopbot.demo import ALICE, buy, settle
from shopbot.engine import Shop
from shopbot.payments import CANCELED, PAID, PENDING, PaymentError
from shopbot.payments.tgwallet import WalletPay
from shopbot.tg import ShopTelegram


def test_walletpay_order_status_and_webhook():
    calls = []
    replies = iter([{"status": "SUCCESS", "data": {"id": 42, "directPayLink": "https://t.me/wallet/start?startapp=x"}},
                    {"status": "SUCCESS", "data": {"status": "ACTIVE"}}, {"status": "SUCCESS", "data": {"status": "PAID"}},
                    {"status": "SUCCESS", "data": {"status": "EXPIRED"}}])

    async def fetch(method, path, body):
        calls.append((method, path, body))
        return next(replies)
    w = WalletPay("key", fetch=fetch)

    async def go():
        pay = await w.create("B7", 1000, "100 ⭐", pay_amount=11.52, ttl=3600, user_id=555)
        assert pay["id"] == "42" and pay["url"].startswith("https://t.me/wallet")
        m, p, body = calls[0]
        assert (m, p) == ("POST", "/order") and body["amount"] == {"currencyCode": "USD", "amount": "11.52"}
        assert body["customerTelegramUserId"] == 555 and body["externalId"] == "B7"
        assert [await w.check("42") for _ in range(3)] == [PENDING, PAID, CANCELED]
        for bad in ({"user_id": 0, "pay_amount": 5}, {"user_id": 1, "pay_amount": 1.0}):
            try:
                await w.create("B8", 100, "x", **bad)
                raise AssertionError
            except PaymentError:
                pass
    asyncio.run(go())
    body = b'[{"eventId":1}]'
    msg = f"POST./hook.123.{base64.b64encode(body).decode()}"
    sig = base64.b64encode(hmac.new(b"key", msg.encode(), hashlib.sha256).digest()).decode()
    assert w.verify_webhook("POST", "/hook", "123", body, sig) and not w.verify_webhook("POST", "/hook", "124", body, sig)


def test_tgwallet_order_in_demo_goes_to_loop_hold(tmp_path):
    cfg = Config(tmp_path, demo=True, overrides={"payments": {"tgwallet": {"enabled": True, "fee_pct": 2}}})
    shop = Shop(cfg, Database(":memory:"), demo=True)
    bot = ShopTelegram(shop)

    async def go():
        await shop.rtba.tick()
        assert shop.rtba.is_open("pay.tgwallet")
        await bot.on_text(ALICE, "alice", "Alice", "/start")
        oid = await buy(bot, ALICE, ["buy:stars", "st:250", "to:me"], "pay:tgwallet")
        o = shop.db.order(oid)
        assert o["pay_method"] == "tgwallet" and o["pay_currency"] == "USD" and o["fee_rub"] > 0
        assert abs(o["pay_amount"] * o["rate"] - o["total_rub"]) < o["rate"] * 0.011
        assert o["status"] == "delivered"
        r = shop.db.one("SELECT * FROM inflows WHERE order_id=?", (oid,))
        assert r["state"] == "hold" and "Wallet Pay" in r["route"]
    asyncio.run(go())


def test_walt_contact_transfer_needs_operator_and_goes_to_sweep(tmp_path):
    cfg = Config(tmp_path, demo=True, overrides={"payments": {"walt": {"enabled": True, "account": "@dpb_pay"}}})
    shop = Shop(cfg, Database(":memory:"), demo=True)
    bot = ShopTelegram(shop)

    async def go():
        await shop.rtba.tick()
        assert shop.rtba.is_open("pay.walt")
        await bot.on_text(ALICE, "alice", "Alice", "/start")
        oid = await buy(bot, ALICE, ["buy:stars", "st:100", "to:me"], "pay:walt", pay=False)
        o = shop.db.order(oid)
        assert o["status"] == "awaiting_asset" and o["pay_currency"] == "USDT" and o["fee_rub"] == 0
        assert any("@dpb_pay" in m["text"] for m in bot.outbox if m["chat"] == ALICE)
        assert await shop.confirm_asset(oid, who="test") == "оплата активом принята, выдаю"
        await settle(shop)
        assert shop.db.order(oid)["status"] == "delivered"
        assert shop.db.one("SELECT state FROM inflows WHERE order_id=?", (oid,))["state"] == "to_sweep"
    asyncio.run(go())
