"""P2P-шлюзы карты/СБП: подписи, статусы, сборка провайдера, маршрут в круг."""
import asyncio
import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

from shopbot.config import Config
from shopbot.db import Database
from shopbot.engine import Shop
from shopbot.payments import CANCELED, PAID, PENDING, PaymentError
from shopbot.payments.gateways import Aaio, CrystalPay, FreeKassa, build_gateway
from shopbot.pricing import Quote, apply_method


def test_aaio_link_signature_and_status():
    g = Aaio("m1", "s1", "key")

    async def go():
        pay = await g.create("B42", 1234.5, "100 ⭐")
        q = parse_qs(urlparse(pay["url"]).query)
        assert q["amount"] == ["1234.50"] and q["order_id"] == ["B42"]
        assert q["sign"][0] == hashlib.sha256(b"m1:1234.50:RUB:s1:B42").hexdigest()
        replies = iter([{"type": "error"}, {"type": "success", "status": "in_process"},
                        {"type": "success", "status": "success"}, {"type": "success", "status": "expired"}])

        async def post(url, **kw):
            assert kw["headers"]["X-Api-Key"] == "key" and kw["data"]["order_id"] == "B42"
            return next(replies)
        g._post = post
        assert [await g.check("B42") for _ in range(4)] == [PENDING, PENDING, PAID, CANCELED]
    asyncio.run(go())


def test_crystalpay_create_check_and_callback():
    g = CrystalPay("login", "secret", salt="salt")
    sent = []

    async def post(url, **kw):
        sent.append((url, kw["json"]))
        if url.endswith("/invoice/create/"):
            return {"error": False, "id": "inv1", "url": "https://pay.crystalpay.io/?i=inv1"}
        return {"error": False, "state": "payed"}
    g._post = post

    async def go():
        pay = await g.create("B1", 500, "заказ", ttl=1800)
        assert pay["id"] == "inv1" and sent[0][1]["lifetime"] == 30 and sent[0][1]["auth_login"] == "login"
        assert await g.check("inv1") == PAID
    asyncio.run(go())
    sig = hashlib.sha1(b"inv1:salt").hexdigest()
    assert g.verify_callback({"id": "inv1", "signature": sig}) and not g.verify_callback({"id": "inv1", "signature": "x"})


def test_freekassa_hmac_signature_and_status():
    g = FreeKassa("77", "apikey", method_id=44)
    body = g.signed({"paymentId": "B9", "amount": 100})
    sig = body.pop("signature")
    msg = "|".join(str(body[k]) for k in sorted(body))
    assert sig == hmac.new(b"apikey", msg.encode(), hashlib.sha256).hexdigest()

    async def post(url, **kw):
        if url.endswith("/orders/create"):
            return {"type": "success", "location": "https://pay.fk.money/x"}
        return {"orders": [{"status": 1}]}
    g._post = post

    async def go():
        assert (await g.create("B9", 100, "x"))["url"].startswith("https://pay.fk")
        assert await g.check("B9") == PAID
        try:
            await FreeKassa("77", "apikey").create("B9", 100, "x")
            raise AssertionError
        except PaymentError as e:
            assert "method_id" in str(e)
    asyncio.run(go())


def test_gateway_is_card_provider_and_inflow_goes_to_sweep(tmp_path):
    over = {"payments": {"card": {"enabled": True, "provider": "aaio", "fee_pct": 9,
                                  "aaio": {"merchant_id": "m", "secret1": "s", "api_key": "k"}}}}
    cfg = Config(tmp_path, overrides=over)
    assert isinstance(build_gateway(cfg), Aaio)
    shop = Shop(cfg, Database(":memory:"), demo=False)
    assert shop.providers["card"].name == "aaio" and shop.providers["card"].configured

    async def go():
        await shop.rtba.tick()
        assert shop.rtba.is_open("pay.card")
    asyncio.run(go())
    q = apply_method(cfg, Quote(product="stars", amount=100, cost_rub=900.0, title="100 ⭐", price_rub=1000.0), "card", shop.rates)
    assert q.total_rub == 1090 and any("картой/СБП 9%" in l for l, _ in q.lines)
    where, step, state, _ = shop.loop.route("card", "RUB")
    assert state == "to_sweep" and "aaio" in where and "USDT" in step
    assert build_gateway(Config(tmp_path)) is None          # по умолчанию — ЮKassa
