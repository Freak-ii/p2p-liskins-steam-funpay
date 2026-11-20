import asyncio

from aiohttp.test_utils import TestClient, TestServer

from starsbot.app.launcher import DEMO_OVERRIDES
from starsbot.app.supervisor import Supervisor
from starsbot.demo import run_scenario
from starsbot.web.server import make_app

TOKEN = "supply-token-1234567890"


def test_supply_contract_for_shopbot(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=DEMO_OVERRIDES)
    sup.save({"dashboard": {"token": "admin-token-12345"}, "supply": {"token": TOKEN}}, rebuild=False)

    async def go():
        await run_scenario(sup.engine, sup.engine.fp, sup.engine.wallet, verbose=False)
        c = TestClient(TestServer(make_app(sup)))
        await c.start_server()
        h = {"X-Supply-Token": TOKEN}
        try:
            assert (await c.get("/api/supply/ping")).status == 401
            assert (await c.get("/api/supply/ping", headers={"X-Supply-Token": "wrong-token-1234567"})).status == 401
            ping = await (await c.get("/api/supply/ping", headers=h)).json()
            assert ping["app"] == "starsbot" and "stars" in ping["products"]
            q = await (await c.get("/api/supply/quote?product=stars&amount=100", headers=h)).json()
            assert q["available"] and q["cost_rub"] > 0 and q["cost_ton"] > 0
            assert not (await (await c.get("/api/supply/quote?product=stars&amount=10", headers=h)).json())["available"]
            assert not (await (await c.get("/api/supply/quote?product=premium&months=3", headers=h)).json())["available"]
            body = {"ref": "T0001", "product": "stars", "username": "alice_tg", "amount": 100}
            r = await (await c.post("/api/supply/orders", json=body, headers=h)).json()
            assert r["ok"] and r["order_id"] == "SHOP-T0001"
            again = await (await c.post("/api/supply/orders", json=body, headers=h)).json()
            assert again["order_id"] == "SHOP-T0001"                      # идемпотентно
            assert sup.db.scalar("SELECT COUNT(*) FROM orders WHERE channel='shop'") == 1
            st = await (await c.get("/api/supply/orders/T0001", headers=h)).json()
            assert st["status"] == "queued"
            await sup.engine.purchase("SHOP-T0001")              # то, что делает deliver_worker
            for _ in range(5):
                sup.engine.wallet.tick()
                await sup.engine.watch_purchases()
            st = await (await c.get("/api/supply/orders/T0001", headers=h)).json()
            assert st["status"] == "delivered", st
            assert sup.db.get_order("SHOP-T0001")["status"] == "completed"
            bad = await c.post("/api/supply/orders", json={**body, "username": "x"}, headers=h)
            assert bad.status == 400
        finally:
            await c.close()
            await sup.stop()
    asyncio.run(go())


def test_supply_bad_username_is_failed(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=DEMO_OVERRIDES)
    sup.save({"dashboard": {"token": "admin-token-12345"}, "supply": {"token": TOKEN}}, rebuild=False)

    async def go():
        await run_scenario(sup.engine, sup.engine.fp, sup.engine.wallet, verbose=False)
        c = TestClient(TestServer(make_app(sup)))
        await c.start_server()
        h = {"X-Supply-Token": TOKEN}
        try:
            body = {"ref": "T0002", "product": "stars", "username": "nobody_here", "amount": 50}
            await c.post("/api/supply/orders", json=body, headers=h)
            st = await (await c.get("/api/supply/orders/T0002", headers=h)).json()
            assert st["status"] == "failed" and st["detail"] == "not_found"
        finally:
            await c.close()
            await sup.stop()
    asyncio.run(go())
