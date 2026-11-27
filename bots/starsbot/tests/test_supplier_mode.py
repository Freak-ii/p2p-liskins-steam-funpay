"""Режим «Только поставщик»: FunPay выключен, бот снабжает магазин и на FunPay не заходит."""
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from starsbot.app import schema
from starsbot.app.launcher import DEMO_OVERRIDES
from starsbot.app.supervisor import Supervisor
from starsbot.config import load_config, validate_config
from starsbot.funpay.client import FunPayClient, FunPayError
from starsbot.funpay.off import FunPayOff
from starsbot.web.server import make_app

OFF = {**DEMO_OVERRIDES, "funpay": {**DEMO_OVERRIDES["funpay"], "enabled": False}}


def supplier(tmp_path, **over):
    sup = Supervisor(tmp_path, demo=True, overrides={**OFF, **over})
    fake = sup.engine.fp._factory()          # эмулятор FunPay под FunPayOff
    calls = []
    for name in ("login", "poll_updates", "get_public_lots", "raise_lots", "get_lot_fields"):
        orig = getattr(fake, name)

        def spy(*a, _n=name, _o=orig, **k):
            calls.append(_n)
            return _o(*a, **k)
        setattr(fake, name, spy)
    return sup, fake, calls


async def run_briefly(e, seconds=0.5):
    task = asyncio.create_task(e.start())
    for _ in range(100):
        if e._tasks:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(seconds)
    await e.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_supplier_mode_parks_lots_once_and_never_polls_funpay(tmp_path):
    sup, fake, calls = supplier(tmp_path)
    e = sup.engine
    assert isinstance(e.fp, FunPayOff) and not e.funpay_on

    async def go():
        await run_briefly(e)
        first = list(calls)
        await run_briefly(e)                 # второй запуск: лоты уже выключены — на FunPay ни шагу
        return first
    first = asyncio.run(go())
    assert "poll_updates" not in calls and "get_public_lots" not in calls and "raise_lots" not in calls
    assert first.count("login") == 1 and calls.count("login") == 1
    lots = {k: v for k, v in fake.lots.items() if v}
    assert lots and all(not l["active"] and l["amount"] == "1" for l in lots.values())
    parked = sup.db.kv_get("funpay_parked")
    assert {"1", "7", "301"} <= set(parked["lots"])          # лоты звёзд и Premium
    assert sup.db.scalar("SELECT COUNT(*) FROM alerts WHERE code='funpay_park'") == 1


def test_supplier_mode_in_dry_run_does_not_touch_funpay(tmp_path):
    sup, fake, calls = supplier(tmp_path, general={**DEMO_OVERRIDES["general"], "dry_run": True})
    msg = asyncio.run(sup.engine.park_funpay())
    assert "тестовый режим" in msg and not calls and not sup.db.kv_get("funpay_parked")


def test_supplier_mode_without_golden_key_warns(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=OFF)
    sup.engine.fp = FunPayOff(None)          # как на ПК без golden_key
    msg = asyncio.run(sup.engine.park_funpay())
    assert msg == "нет golden_key"
    assert "вручную" in sup.db.scalar("SELECT text FROM alerts WHERE code='funpay_park'")


def test_funpay_off_never_reaches_network():
    async def go():
        fp = FunPayOff()
        await fp.login()
        with pytest.raises(FunPayError):
            await fp.get_sales()
        with pytest.raises(FunPayError):
            await fp.update_lot(1, 2418, active=False)
        assert fp.username == "" and fp.park_client() is None
    asyncio.run(go())


def test_stock_sync_and_orbit_skip_funpay(tmp_path):
    sup, fake, calls = supplier(tmp_path)
    e = sup.engine

    async def go():
        await e.stock_sync(50.0, 0.0001, 0)
        await e.rtba_step()
        await e.price_guard()
        return await e.orbit.close("stars")
    assert "FunPay выключен" in asyncio.run(go())
    assert not calls


def test_golden_key_not_required_in_supplier_mode(tmp_path):
    cfg = load_config(None, {"funpay": {"enabled": False}})
    assert not any("golden_key" in p for p in validate_config(cfg))
    cfg = load_config(None, {})
    assert any("golden_key" in p for p in validate_config(cfg))


def test_settings_hide_unused_sections():
    cfg = load_config(None, {"funpay": {"enabled": False}, "ggsel": {"enabled": True}})
    active, idle = schema.split_sections(cfg)
    a, i = {s for s, _ in active}, {s for s, _ in idle}
    assert {"funpay", "rtba", "autopilot", "ozon", "avito", "wb", "gifts"} <= i
    assert {"general", "fragment", "ton", "ggsel"} <= a and not a & i
    on = load_config(None, {})
    a2, i2 = schema.split_sections(on)
    assert "funpay" in {s for s, _ in a2} and "ggsel" in {s for s, _ in i2}


def test_settings_page_keeps_idle_toggles(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=OFF)
    sup.save({"dashboard": {"token": "admin-token-12345"}}, rebuild=False)

    async def go():
        c = TestClient(TestServer(make_app(sup)))
        await c.start_server()
        try:
            await c.get("/login?token=admin-token-12345")
            html = await (await c.get("/settings")).text()
            dash = await (await c.get("/")).text()
            return html, dash
        finally:
            await c.close()
    html, dash = asyncio.run(go())
    assert 'id="idle"' in html and 'name="funpay__enabled"' in html     # свёрнуто, но в форме
    assert "Только поставщик" in dash and 'href="/pricing"' not in dash


def test_supply_api_accepts_center_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ORBITA_SUPPLY_TOKEN", "center-secret-1234567890")
    sup = Supervisor(tmp_path, demo=True, overrides=OFF)
    sup.save({"dashboard": {"token": "admin-token-12345"}, "supply": {"token": "own-token-1234567890"}}, rebuild=False)

    async def go():
        c = TestClient(TestServer(make_app(sup)))
        await c.start_server()
        try:
            codes = [(await c.get("/api/supply/ping", headers={"X-Supply-Token": t})).status
                     for t in ("center-secret-1234567890", "own-token-1234567890", "wrong-token-123456789")]
        finally:
            await c.close()
        return codes
    assert asyncio.run(go()) == [200, 200, 401]


def test_hidden_lot_keeps_amount_one():
    """FunPay не сохраняет лот с наличием 0 даже выключенным."""
    fp = FunPayClient("k", "ua")
    saved = []

    async def fields(offer_id, node_id):
        return {"offer_id": str(offer_id), "amount": "0", "active": "on", "price": "10"}

    async def save(f):
        saved.append(dict(f))
    fp.get_lot_fields, fp.save_lot = fields, save
    asyncio.run(fp.update_lot(1, 2418, amount=0, active=False))
    asyncio.run(fp.update_lot(1, 2418, amount=5, active=True))
    assert saved[0]["amount"] == "1" and "active" not in saved[0]
    assert saved[1]["amount"] == "5" and saved[1]["active"] == "on"


def test_tggift_supplier_mode(tmp_path):
    """TG Gift тоже может только снабжать магазин подарками: лоты FunPay выключаются, цены не трогаются."""
    over = {"general": {"demo": True, "dry_run": False, "database": ":memory:", "log_file": "", "profile": "giftshop"},
            "notifications": {"telegram": {"enabled": False}}, "funpay": {"enabled": False}}
    sup = Supervisor(tmp_path / "tggift", demo=True, overrides=over)
    e = sup.engine
    assert isinstance(e.fp, FunPayOff) and "Только поставщик" in e.start_banner()
    fake = e.fp._factory()
    for l in e.gifts.lots[:2]:
        l["offer_id"] = l.get("offer_id") or 900 + e.gifts.lots.index(l)

    async def go():
        await e.rtba_step()
        await e.gifts.stock_sync(1000)
        return await e.park_funpay()
    msg = asyncio.run(go())
    assert msg.startswith("выключено 2")
    assert all(not fake.lots[str(l["offer_id"])]["active"] for l in e.gifts.lots[:2])
    from starsbot.giftshop.profile import validate_giftshop
    assert not any("golden_key" in p for p in validate_giftshop(load_config(None, {"general": {"profile": "giftshop"},
                                                                                    "funpay": {"enabled": False}})))
