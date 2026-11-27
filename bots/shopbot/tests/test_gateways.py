"""P2P-шлюзы оплаты для физлиц (CrystalPAY, AAIO) в способе «Картой / СБП»."""
import asyncio
import hashlib

from shopbot.payments import (CANCELED, PAID, PENDING, Aaio, CrystalPay, PaymentError, YooKassa, card_provider,
                               crystalpay_from)


class Cfg(dict):
    def get(self, key, default=None):
        node = self
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


SBP_ITEMS = {"BITCOIN": {"name": "Bitcoin", "currency": "BTC", "in": {"enabled": True,
                                                                     "commissions": {"percent": "1", "amount": "0"}}},
             "SBPP2P": {"name": "СБП", "currency": "RUB", "in": {"enabled": False}}}


def fake_api(state, items=None, calls=None):
    async def fetch(method, body):
        if calls is not None:
            calls.append((method, body))
        if method == "invoice/create":
            return {"error": False, "errors": [], "id": "inv1", "url": "https://pay.crystalpay.io/?i=inv1"}
        if method == "method/list":
            return {"error": False, "errors": [], "items": items or {}}
        if method == "balance/list":
            return {"error": False, "errors": [], "items": {"RUB": {"amount": "0"}}}
        return {"error": False, "errors": [], "type": "purchase", "currency": "RUB", **state}
    return fetch


def test_crystalpay_create_and_states():
    calls, state = [], {"state": "notpayed"}
    cp = CrystalPay("shop", "s1", fetch=fake_api(state, calls=calls))
    pay = asyncio.run(cp.create("O1", 1234.5, "100 ⭐ · заказ O1", ttl=1800, payer={"telegram_id": 5, "email": ""}))
    assert pay["id"] == "inv1" and pay["url"].endswith("inv1")
    body = calls[0][1]
    assert body["auth_login"] == "shop" and body["auth_secret"] == "s1" and body["amount"] == 1234.5
    assert body["lifetime"] == 30 and body["extra"] == "O1" and body["type"] == "purchase" and body["currency"] == "RUB"
    assert body["subtract_from"] == "balance" and "required_method" not in body and "callback_url" not in body
    assert body["payer_details"] == {"telegram_id": 5}
    for st, want in [("created", PENDING), ("notpayed", PENDING), ("processing", PENDING), ("wrongamount", PENDING),
                     ("failed", PENDING), ("payed", PAID), ("unavailable", CANCELED)]:
        state["state"] = st
        assert asyncio.run(cp.check("inv1")) == want, st
    state.update(state="payed", currency="USD")          # чужая валюта — не засчитываем
    assert asyncio.run(cp.check("inv1")) == PENDING
    state.update(currency="RUB", type="topup")           # topup мог быть оплачен частично
    assert asyncio.run(cp.check("inv1")) == PENDING


def test_crystalpay_error_raises():
    async def fetch(method, body):
        return {"error": True, "errors": ["Неверный auth_secret"]}
    cp = CrystalPay("shop", "bad", fetch=fetch)
    try:
        asyncio.run(cp.check("x"))
    except PaymentError as e:
        assert "Неверный auth_secret" in str(e)
    else:
        raise AssertionError("ошибка API должна стать PaymentError")
    assert not CrystalPay("", "").configured


def test_crystalpay_sbp_required_method():
    items = {k: {**v, "in": dict(v["in"])} for k, v in SBP_ITEMS.items()}
    calls = []
    cp = CrystalPay("shop", "s1", required_method="auto", callback_url="https://x/api/pay/crystalpay",
                    fetch=fake_api({}, items, calls))
    assert asyncio.run(cp.sbp_method()) == ""               # СБП в кассе ещё не включён
    try:
        asyncio.run(cp.create("O1", 500, "Premium"))
    except PaymentError as e:
        assert "Профессиональный" in str(e)
    else:
        raise AssertionError("без СБП счёт под СБП не создаём")
    items["SBPP2P"]["in"]["enabled"] = True
    cp._methods = (0.0, {})
    assert asyncio.run(cp.sbp_method()) == "SBPP2P"
    asyncio.run(cp.create("O2", 500, "Premium"))
    body = calls[-1][1]
    assert body["required_method"] == "SBPP2P" and body["callback_url"] == "https://x/api/pay/crystalpay"
    exact = CrystalPay("shop", "s1", required_method="BITCOIN", fetch=fake_api({}, items))
    assert asyncio.run(exact.sbp_method()) == "BITCOIN"
    s = asyncio.run(cp.summary())
    assert s["sbp"] == "SBPP2P" and {r["key"] for r in s["methods"]} == {"BITCOIN", "SBPP2P"}
    assert next(r for r in s["methods"] if r["key"] == "BITCOIN")["commission_pct"] == "1"


def test_crystalpay_callback_signature():
    cp = CrystalPay("shop", "s1", salt="pepper")
    good = hashlib.sha1(b"inv1:pepper").hexdigest()
    assert cp.verify_callback({"id": "inv1", "signature": good})
    assert not cp.verify_callback({"id": "inv1", "signature": "0" * 40})
    assert not cp.verify_callback({"id": "inv2", "signature": good})
    assert not CrystalPay("shop", "s1").verify_callback({"id": "inv1", "signature": good})   # без Salt — не верим


def test_aaio_sign_url_and_states():
    res = {"d": {"type": "error"}}

    async def fetch(form):
        return res["d"]
    a = Aaio("m1", "sec", "key", fetch=fetch)
    pay = asyncio.run(a.create("O2", 500, "Premium"))
    sign = hashlib.sha256(b"m1:500.00:RUB:sec:O2").hexdigest()
    assert pay["id"] == "O2" and f"sign={sign}" in pay["url"] and "amount=500.00" in pay["url"]
    assert asyncio.run(a.check("O2")) == PENDING          # счёт ещё не открыт
    res["d"] = {"type": "success", "status": "hold"}
    assert asyncio.run(a.check("O2")) == PENDING          # hold — деньги на проверке, не выдаём
    res["d"] = {"type": "success", "status": "success"}
    assert asyncio.run(a.check("O2")) == PAID
    res["d"] = {"type": "success", "status": "expired"}
    assert asyncio.run(a.check("O2")) == CANCELED


def test_card_provider_choice():
    assert isinstance(card_provider(Cfg()), YooKassa)
    cfg = Cfg(payments={"card": {"provider": "crystalpay", "crystalpay": {"login": "x", "secret": "y"}}})
    assert isinstance(card_provider(cfg), CrystalPay) and card_provider(cfg).configured     # старое место ключей
    assert card_provider(cfg).required_method == "auto"                                     # карта — это СБП
    cfg = Cfg(payments={"card": {"provider": "crystalpay"},
                        "crystalpay": {"login": "x", "secret": "y", "salt": "z", "sbp_method": "SBPP2P"}})
    cp = card_provider(cfg)
    assert cp.configured and cp.salt == "z" and cp.required_method == "SBPP2P"
    assert crystalpay_from(cfg, sbp=False).required_method == ""
    cfg = Cfg(payments={"card": {"provider": "aaio", "aaio": {"merchant_id": "1"}}})
    assert isinstance(card_provider(cfg), Aaio) and not card_provider(cfg).configured


def test_crystalpay_sbp_order_callback_and_late_payment(tmp_path):
    """Заказ «Картой / СБП» через кассу: RTBA закрыт без СБП, вебхук с подписью только запускает проверку
    через API, оплата на границе срока (истёкший заказ) всё равно засчитывается."""
    import time

    from aiohttp.test_utils import TestClient, TestServer

    from shopbot.__main__ import App
    from shopbot.demo import ALICE
    from shopbot.web.server import make_app

    items = {k: {**v, "in": dict(v["in"])} for k, v in SBP_ITEMS.items()}
    state = {"state": "notpayed"}
    app = App(tmp_path, demo=True)
    shop = app.shop
    cp = CrystalPay("shop", "s1", salt="pepper", required_method="auto", fetch=fake_api(state, items))

    async def go():
        await app.start()                                            # демо-сценарий идёт на демо-провайдерах
        shop.providers["card"] = cp
        c = TestClient(TestServer(make_app(app)))
        await c.start_server()
        try:
            await shop.rtba.tick()
            assert not shop.rtba.is_open("pay.card")                 # СБП в кассе ещё нет
            items["SBPP2P"]["in"]["enabled"] = True
            cp._methods = (0.0, {})
            await shop.rtba.tick()
            assert shop.rtba.is_open("pay.card")
            shop.db.touch_user(ALICE, "alice", "Alice")
            o, pay = await shop.create_buy(ALICE, "stars", "", 100, "alice", "card")
            assert pay["id"] == "inv1" and o["payment_id"] == "inv1"
            # неверная подпись — 403, заказ не трогаем
            r = await c.post("/api/pay/crystalpay", json={"id": "inv1", "state": "payed", "signature": "0" * 40})
            assert r.status == 403 and shop.db.order(o["id"])["status"] == "awaiting_payment"
            # верная подпись, но API говорит «не оплачен» — телу не верим
            sig = hashlib.sha1(b"inv1:pepper").hexdigest()
            r = await c.post("/api/pay/crystalpay", json={"id": "inv1", "state": "payed", "signature": sig})
            assert r.status == 200 and shop.db.order(o["id"])["status"] == "awaiting_payment"
            # срок вышел, пока банк проводил платёж
            shop.db.update_order(o["id"], expires_at=time.time() - 5)
            state["state"] = "processing"
            await shop.poll_payments()
            assert shop.db.order(o["id"])["status"] == "expired"
            state["state"] = "payed"
            await shop.poll_payments()                               # поздний приход в течение часа
            assert shop.db.order(o["id"])["status"] not in ("expired", "awaiting_payment")
            inflow = shop.db.one("SELECT * FROM inflows WHERE order_id=?", (o["id"],))
            assert inflow is not None and "CrystalPAY" in inflow["route"]
        finally:
            await c.close()
            await shop.stop()
    asyncio.run(go())
