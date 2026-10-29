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


def test_bridge_gift_accepted_only_from_lisbot_with_keys_only():
    db, notifier, fp, steam, engine, analytics = make()
    lis = "76561198000000777"
    key = engine.cfg.steam.key_market_hash_name
    before = len(steam.inventory)
    steam.incoming["501"] = {"from": lis, "names": [key, key, key]}
    steam.incoming["502"] = {"from": "76561198000000999", "names": [key]}          # чужой аккаунт
    steam.incoming["503"] = {"from": lis, "names": [key, "Strange Scattergun"]}      # не только ключи
    steam.incoming["504"] = {"from": lis, "names": [key], "give": 1}                # просит наши предметы

    asyncio.run(engine.security_watch())                 # мост выключен: ничего не принимаем
    assert len(steam.inventory) == before and "501" in steam.incoming

    engine.cfg.security["accept_gifts_from"] = [lis]
    asyncio.run(engine.security_watch())
    assert len(steam.inventory) == before + 3
    assert "501" not in steam.incoming and "502" in steam.incoming and "503" in steam.incoming
    assert "504" not in steam.incoming                    # отклонён как запрос наших предметов
    assert db.kv_get("gift_done:501") == "accepted" and db.kv_get("gift_done:503") == "skipped"
    assert any("Мост ключей: принято 3" in t for _, t in notifier.sent)
    assert any("не только ключи" in t for _, t in notifier.sent)


class _Share:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def intent(self, order_id, partner, appid, asset_ids):
        from mannbot.steam.shared import ShareUnavailable
        self.calls.append(("intent", order_id, partner, appid, list(asset_ids)))
        if self.fail:
            raise ShareUnavailable("центр недоступен")
        return 7

    async def bind(self, intent_id, offer_id):
        self.calls.append(("bind", intent_id, offer_id))


def _pay_and_link(engine, fp, buyer=31):
    async def go():
        engine.cfg.deal.message_rate_limit_seconds = 0
        await engine.funpay_poll()
        oid = fp.buyer_pays("Ann", buyer, 1)
        await engine.funpay_poll()
        fp.buyer_says(buyer, trade_url(900 + buyer))
        await engine.funpay_poll()
        await engine.deliver(oid)
        return oid
    return go


def test_shared_account_registers_offer_in_center_before_sending():
    db, notifier, fp, steam, engine, analytics = make()
    engine.share = _Share()
    sent_before = set(steam.offers)
    oid = asyncio.run(_pay_and_link(engine, fp)())
    o = db.get_order(oid)
    assert o["status"] == S.OFFER_SENT.value
    kind, ref, partner, appid, assets = engine.share.calls[0]
    assert kind == "intent" and ref == oid and partner == str(931 + 76561197960265728) and appid == 440
    new = [k for k in steam.offers if k not in sent_before]
    assert len(new) == 1 and steam.offers[new[0]]["assets"] == assets
    assert engine.share.calls[1] == ("bind", 7, new[0])


def test_shared_account_does_not_send_when_center_is_down():
    db, notifier, fp, steam, engine, analytics = make()
    engine.share = _Share(fail=True)
    sent_before = set(steam.offers)
    oid = asyncio.run(_pay_and_link(engine, fp)())
    o = db.get_order(oid)
    assert set(steam.offers) == sent_before                     # обмен не отправлен
    assert o["status"] != S.MANUAL.value and not o["offer_id"]  # повтор позже, ключи не заняты
    assert not db.reserved_asset_ids()


def test_center_share_raises_only_on_shared_account():
    from mannbot.steam.shared import CenterShare, ShareUnavailable
    down = "http://127.0.0.1:9/api/steam-share"
    two = CenterShare(down, "t" * 40, shared=False)
    assert asyncio.run(two.intent("1", "76561197960266728", 440, ["1"])) is None
    one = CenterShare(down, "t" * 40, shared=True)
    try:
        asyncio.run(one.intent("1", "76561197960266728", 440, ["1"]))
        raise AssertionError("должно быть исключение")
    except ShareUnavailable:
        pass
    assert asyncio.run(CenterShare().intent("1", "76561197960266728", 440, ["1"])) is None
