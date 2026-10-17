import asyncio

from mannbot.__main__ import build
from mannbot.config import load_config
from mannbot.core.models import OrderStatus as S
from mannbot.demo import run_scenario, trade_url


def make():
    cfg = load_config(None, {"general": {"demo": True, "dry_run": False, "database": ":memory:"},
                             "funpay": {"lots": [{"offer_id": 1, "node_id": 1808, "game_id": 435}]},
                             "risk": {"manual_review_score": 40},
                             "rtba": {"enabled": True, "cost_per_key": 150, "ceiling_price": 260},
                             "dashboard": {"token": "test-token-12345"}})
    return build(cfg)


def test_full_scenario():
    db, notifier, fp, steam, engine, analytics = make()
    res = asyncio.run(run_scenario(engine, fp, steam, verbose=False))
    a, b, c, d, e, f, g = (db.get_order(x) for x in res["orders"])
    assert a["status"] == S.COMPLETED.value
    assert b["status"] == S.DELIVERED.value and b["link_attempts"] == 2
    assert c["status"] == S.DELIVERED.value and c["resends"] == 1
    assert d["status"] == S.DELIVERED.value and d["escrow_days"] == 15
    assert e["status"] == S.AWAITING_LINK.value
    assert f["status"] == S.AWAITING_LINK.value          # после API-скама ждём !повтор
    assert g["status"] == S.DELIVERED.value and g["approved"] == 1
    texts = [t for _, t in fp.outbox]
    assert any("с вашими данными проблема" in t for t in texts)
    assert any("подтвердите выполнение заказа" in t for t in texts)
    assert any("API-ключ" in t for t in texts)
    # благодарность с просьбой подтвердить — только после выдачи (п. 2.1.2 правил FunPay)
    for oid in res["orders"]:
        chat = [t for ch, t in fp.outbox if f"#{oid}" in t]
        succ = [i for i, t in enumerate(chat) if "успешно завершена" in t]
        sent = [i for i, t in enumerate(chat) if "Обмен отправлен" in t]
        if succ:
            assert sent and min(sent) < min(succ)
    # никаких двойных трейдов: на каждый заказ максимум 1 активный
    assert db.scalar("SELECT COUNT(*) FROM offers WHERE state=2") == 0
    assert analytics.summary(1)["delivered"] == 5


def test_bad_data_limit_goes_manual():
    db, notifier, fp, steam, engine, analytics = make()
    engine.cfg.deal.message_rate_limit_seconds = 0

    async def go():
        await engine.funpay_poll()
        oid = fp.buyer_pays("Zed", 20, 1)
        await engine.funpay_poll()
        for _ in range(3):
            fp.buyer_says(20, "https://steamcommunity.com/id/zed")
            await engine.funpay_poll()
        return oid
    oid = asyncio.run(go())
    o = db.get_order(oid)
    assert o["status"] == S.MANUAL.value and o["link_attempts"] == 3
    assert any(l == "WARNING" and "неверные данные" in t for l, t in notifier.sent)


def test_refund_cancels_offer_and_out_of_stock():
    db, notifier, fp, steam, engine, analytics = make()
    engine.cfg.deal.message_rate_limit_seconds = 0
    steam.behaviour[str(777 + 76561197960265728)] = "ignore"

    async def go():
        await engine.funpay_poll()
        oid = fp.buyer_pays("Ann", 21, 1)
        await engine.funpay_poll()
        fp.buyer_says(21, trade_url(777))
        await engine.funpay_poll()
        await engine.deliver(oid)
        assert db.get_order(oid)["status"] == S.OFFER_SENT.value
        fp.sales[oid].status = "refunded"
        fp._orders_tag += 1
        await engine.funpay_poll()
        big = fp.buyer_pays("Max", 22, 500)
        await engine.funpay_poll()
        return oid, big
    oid, big = asyncio.run(go())
    o = db.get_order(oid)
    assert o["status"] == S.REFUNDED.value
    assert db.get_offer(o["offer_id"])["canceled_by_bot"] == 1
    assert db.get_order(big)["fail_reason"] == "out_of_stock"
    assert any(l == "CRITICAL" and "Нет ключей" in t for l, t in notifier.sent)


def test_foreign_offer_alert():
    db, notifier, fp, steam, engine, analytics = make()
    steam.offers["31337"] = {"state": 2, "to": "1", "assets": [], "ts": 0}
    asyncio.run(engine.security_watch())
    assert any(l == "CRITICAL" and "НЕ ботом" in t for l, t in notifier.sent)


def test_adopts_existing_offer_instead_of_double_send():
    db, notifier, fp, steam, engine, analytics = make()
    engine.cfg.deal.message_rate_limit_seconds = 0

    async def go():
        await engine.funpay_poll()
        oid = fp.buyer_pays("Kim", 23, 1)
        await engine.funpay_poll()
        steam.behaviour[str(888 + 76561197960265728)] = "ignore"
        # трейд «уже создан» в Steam (ответ потерялся), но бот о нём не знает
        steam.offers["424242"] = {"state": 2, "to": str(888 + 76561197960265728), "assets": ["9000"], "ts": 0}
        orig = steam.get_active_offers

        async def active():
            sent, rec = await orig()
            for s in sent:
                if s["tradeofferid"] == "424242":
                    s["message"] = f"FunPay заказ #{oid}"
            return sent, rec
        steam.get_active_offers = active
        fp.buyer_says(23, trade_url(888))
        await engine.funpay_poll()
        await engine.deliver(oid)
        return oid
    oid = asyncio.run(go())
    o = db.get_order(oid)
    assert o["offer_id"] == "424242" and o["status"] == S.OFFER_SENT.value
    assert len([k for k, v in steam.offers.items() if v["to"] == str(888 + 76561197960265728)]) == 1
