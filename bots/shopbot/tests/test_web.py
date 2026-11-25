import asyncio

from aiohttp.test_utils import TestClient, TestServer

from shopbot.__main__ import App
from shopbot.web.server import make_app


def test_panel_pages_roles_and_demo_chat(tmp_path):
    app = App(tmp_path, demo=True)
    app.cfg.save({"dashboard": {"token": "admin-token-12345", "viewer_token": "viewer-token-123"}})

    async def go():
        await app.start()
        c = TestClient(TestServer(make_app(app)))
        await c.start_server()
        try:
            assert (await c.get("/", allow_redirects=False)).status == 302
            assert (await (await c.get("/api/ping")).json())["app"] == "shopbot"
            await c.get("/login?token=admin-token-12345")
            oid = app.db.scalar("SELECT id FROM orders ORDER BY created_at LIMIT 1")
            for path in ["/", "/rtba", "/orders", "/orders?kind=sell", f"/orders/{oid}", "/customers",
                         "/customers/1001", "/channel", "/settings", "/demo", "/api/status", "/api/demo/chat?uid=1001"]:
                r = await c.get(path)
                assert r.status == 200, path
            st = await (await c.get("/api/status")).json()
            assert st["summary"]["orders"] == 5 and "Продажа Premium" in st["rtba_closed"]
            # демо-чат: новый клиент проходит покупку звёзд кнопками
            await c.post("/api/demo/send", json={"uid": 4242, "text": "/start", "username": "alice_tg"})
            for data in ["buy:stars", "st:250", "to:me", "pay:crypto:USDT"]:
                await c.post("/api/demo/send", json={"uid": 4242, "data": data})
            msgs = (await (await c.get("/api/demo/chat?uid=4242")).json())["messages"]
            pay = next(b["callback_data"] for m in msgs if m["buttons"] for row in m["buttons"]["inline_keyboard"]
                       for b in row if b.get("callback_data", "").startswith("demo:pay:"))
            await c.post("/api/demo/send", json={"uid": 4242, "data": pay})
            for _ in range(3):
                await c.post("/api/demo/send", json={"uid": 4242, "text": "/menu"})
            assert app.db.scalar("SELECT status FROM orders WHERE user_id=4242") == "delivered"
            # настройки: комиссия крипты не опускается ниже 2%
            await c.post("/settings", data={"payments.crypto.fee_pct": "1"})
            assert app.cfg.get("payments.crypto.fee_pct") == 2.0
            # RTBA: выключить пункт из панели
            await c.post("/rtba", data={"key": "sell.gift", "mode": "off"})
            assert not app.shop.rtba.is_open("sell.gift")
            # ручная выплата из панели
            pend = app.db.scalar("SELECT id FROM orders WHERE status='payout_pending'")
            await c.post(f"/orders/{pend}", data={"action": "payout"})
            assert app.db.order(pend)["status"] == "paid_out"
            # зритель не может менять
            c.session.cookie_jar.clear()
            await c.get("/login?token=viewer-token-123")
            assert (await c.post("/rtba", data={"key": "sell.gift", "mode": "on"})).status == 403
        finally:
            await c.close()
            await app.shop.stop()
    asyncio.run(go())


def test_services_page_stock_and_delivery(tmp_path):
    app = App(tmp_path, demo=True)
    app.cfg.save({"dashboard": {"token": "admin-token-12345"}})

    async def go():
        await app.start()
        c = TestClient(TestServer(make_app(app)))
        await c.start_server()
        try:
            await c.get("/login?token=admin-token-12345")
            assert (await c.get("/services")).status == 200
            await c.post("/services", data={"action": "stock", "sku": "appstore_us:20", "cost_rub": "1780",
                                            "codes": "CODE-1\nCODE-2\nCODE-1", "note": "test"})
            assert app.shop.services.stock.count("appstore_us:20") == 2
            data = await (await c.get("/api/services")).json()
            row = next(r for r in data["economics"] if r["key"] == "appstore_us:20")
            assert row["source"] == "stock" and row["cost_rub"] == 1780
        finally:
            await c.close()
            await app.shop.stop()
    asyncio.run(go())
