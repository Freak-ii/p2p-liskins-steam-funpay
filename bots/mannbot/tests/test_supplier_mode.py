"""MannBot: режим «Только поставщик», подтверждение трейда при 429, трейд без комментария, правки лота."""
import asyncio
import time

import pytest

from mannbot.__main__ import build
from mannbot.app import schema
from mannbot.config import DEFAULTS, load_config, validate_config
from mannbot.core.models import OrderStatus as S
from mannbot.demo import trade_url
from mannbot.funpay.client import FunPayClient, FunPayError
from mannbot.funpay.off import FunPayOff
from mannbot.steam.client import SteamError

BASE = {"general": {"demo": True, "dry_run": False, "database": ":memory:"},
        "funpay": {"lots": [{"offer_id": 1, "node_id": 1808, "game_id": 435}]},
        "rtba": {"enabled": True, "cost_per_key": 150}, "dashboard": {"token": "test-token-12345"}}


def make(**funpay):
    cfg = load_config(None, {**BASE, "funpay": {**BASE["funpay"], **funpay}})
    return build(cfg)


def test_supplier_mode_parks_lot_and_skips_funpay():
    db, notifier, fp, steam, engine, analytics = make(enabled=False)
    assert isinstance(fp, FunPayOff) and not engine.funpay_on
    fake = fp._factory()
    calls = []
    orig_poll = fake.poll_updates

    async def poll(*a, **k):
        calls.append("poll")
        return await orig_poll(*a, **k)
    fake.poll_updates = poll

    async def go():
        task = asyncio.create_task(engine.start())
        for _ in range(100):
            if engine._tasks:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        await engine.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await engine._sync_lots(10)                    # сверка склада FunPay не трогает
        return await engine.park_funpay()
    again = asyncio.run(go())
    assert not calls and fake.lot["active"] == "" and fake.lot["amount"] == "1"
    assert again == "лот FunPay уже выключен"
    assert any("Только поставщик" in t for _l, t in notifier.sent)


def test_golden_key_and_lot_not_required_in_supplier_mode():
    cfg = load_config(None, {"funpay": {"enabled": False, "auto_create_lot": False}})
    probs = validate_config(cfg)
    assert not any("golden_key" in p or "funpay.lots" in p for p in probs)
    active, idle = schema.split_sections(cfg)
    assert {s for s, _ in idle} == {"funpay", "rtba", "ai"} and "steam" in {s for s, _ in active}


def test_confirm_retries_on_steam_429():
    db, notifier, fp, steam, engine, analytics = make()
    engine.CONFIRM_RETRY_DELAYS = (0, 0, 0)
    answers = [SteamError("Steam rate limit (429)", "rate_limit")] * 2 + [True]

    async def confirm(offer_id, attempts=6):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    steam.confirm_offer = confirm
    assert asyncio.run(engine.confirm_with_retry("X1", "555")) is True
    assert db.scalar("SELECT COUNT(*) FROM order_events WHERE kind='confirm_rate_limit'") == 2

    calls = []

    async def always_429(offer_id, attempts=6):
        calls.append(offer_id)
        raise SteamError("Steam rate limit (429)", "rate_limit")
    steam.confirm_offer = always_429
    assert asyncio.run(engine.confirm_with_retry("X2", "556")) is False
    assert len(calls) == 4                                 # сразу + через 1, 2 и 4 минуты


def test_offer_without_comment_is_adopted_not_sent_twice():
    db, notifier, fp, steam, engine, analytics = make()
    engine.cfg.deal.message_rate_limit_seconds = 0
    assert engine.cfg.steam.offer_message == ""            # по умолчанию трейд без комментария

    async def go():
        await engine.funpay_poll()
        oid = fp.buyer_pays("Ann", 31, 2)
        await engine.funpay_poll()
        fp.buyer_says(31, trade_url(4321))
        await engine.funpay_poll()
        keys = await steam.get_tradable_keys()
        lost = {"tradeofferid": "4242", "accountid_other": 4321, "time_created": time.time(),
                "items_to_give": [{"assetid": keys[0]}, {"assetid": keys[1]}], "message": ""}
        orig = steam.get_active_offers

        async def active():
            sent, received = await orig()
            return sent + [lost], received
        steam.get_active_offers = active
        before = len(steam.offers)
        await engine.deliver(oid)
        return oid, before
    oid, before = asyncio.run(go())
    o = db.get_order(oid)
    assert o["status"] == S.OFFER_SENT.value and o["offer_id"] == "4242"
    assert len(steam.offers) == before                     # второй трейд не создан


def test_foreign_offer_is_not_adopted():
    db, notifier, fp, steam, engine, analytics = make()
    engine.cfg.deal.message_rate_limit_seconds = 0

    async def go():
        await engine.funpay_poll()
        oid = fp.buyer_pays("Bob", 32, 1)
        await engine.funpay_poll()
        fp.buyer_says(32, trade_url(5555))
        await engine.funpay_poll()
        other = {"tradeofferid": "9999", "accountid_other": 1234, "time_created": time.time(),
                 "items_to_give": [{"assetid": (await steam.get_tradable_keys())[0]}]}
        orig = steam.get_active_offers

        async def active():
            sent, received = await orig()
            return sent + [other], received
        steam.get_active_offers = active
        await engine.deliver(oid)
        return oid
    oid = asyncio.run(go())
    assert db.get_order(oid)["offer_id"] not in ("9999", None, "")


def test_lot_texts_and_hidden_amount():
    d = DEFAULTS["funpay"]
    assert len(d["lot_desc_en"]) >= 0.8 * len(d["lot_desc_ru"]) and "trade URL" in d["lot_desc_en"]
    fp = FunPayClient("k", "ua")
    saved = []

    async def fields(offer_id, node_id):
        return {"offer_id": str(offer_id), "amount": "0", "active": "on"}

    async def save(f):
        saved.append(dict(f))
    fp.get_lot_fields, fp.save_lot = fields, save
    asyncio.run(fp.update_lot(1, 1808, amount=0, active=False))
    assert saved[0]["amount"] == "1" and "active" not in saved[0]


def test_hold_lots_hides_with_amount_one():
    db, notifier, fp, steam, engine, analytics = make()
    asyncio.run(engine.hold_lots("тест"))
    assert fp.lot["active"] == "" and fp.lot["amount"] == "1"


def test_funpay_off_raises():
    with pytest.raises(FunPayError):
        asyncio.run(FunPayOff().get_sales())
