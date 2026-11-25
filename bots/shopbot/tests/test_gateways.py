"""P2P-шлюзы оплаты для физлиц (CrystalPAY, AAIO) в способе «Картой / СБП»."""
import asyncio
import hashlib

from shopbot.payments import CANCELED, PAID, PENDING, Aaio, CrystalPay, YooKassa, card_provider


class Cfg(dict):
    def get(self, key, default=None):
        node = self
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def test_crystalpay_create_and_states():
    calls = []
    state = {"v": "notpayed"}

    async def fetch(method, body):
        calls.append((method, body))
        if method == "invoice/create":
            return {"error": False, "id": "inv1", "url": "https://pay.crystalpay.io/?i=inv1"}
        return {"error": False, "state": state["v"]}
    cp = CrystalPay("shop", "s1", fetch=fetch)
    pay = asyncio.run(cp.create("O1", 1234.5, "100 ⭐ · заказ O1", ttl=1800))
    assert pay["id"] == "inv1" and pay["url"].endswith("inv1")
    body = calls[0][1]
    assert body["auth_login"] == "shop" and body["amount"] == 1234.5 and body["lifetime"] == 30 and body["extra"] == "O1"
    assert asyncio.run(cp.check("inv1")) == PENDING
    state["v"] = "payed"
    assert asyncio.run(cp.check("inv1")) == PAID
    state["v"] = "cancelled"
    assert asyncio.run(cp.check("inv1")) == CANCELED


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
    assert isinstance(card_provider(cfg), CrystalPay) and card_provider(cfg).configured
    cfg = Cfg(payments={"card": {"provider": "aaio", "aaio": {"merchant_id": "1"}}})
    assert isinstance(card_provider(cfg), Aaio) and not card_provider(cfg).configured
