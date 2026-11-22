"""MannBot как поставщик ключей Steam для Telegram-магазина ShopBot (/api/supply/*)."""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

from mannbot.app.launcher import DEMO_OVERRIDES
from mannbot.app.supervisor import Supervisor
from mannbot.core.models import OrderStatus as S
from mannbot.demo import trade_url
from mannbot.web.server import make_app

TOKEN = "supply-token-mann-12345"


def _sup(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=DEMO_OVERRIDES)
    sup.save({"supply": {"token": TOKEN}, "rtba": {"cost_per_key": 150}}, rebuild=False)
    sup.cfg.supply.token = TOKEN
    sup.engine.cfg.rtba["cost_per_key"] = 150
    sup.state = "running"                      # процесс бота «запущен»; циклы гоняем вручную
    return sup


def test_shop_keys_order_delivered_by_trade(tmp_path):
    sup = _sup(tmp_path)
    e = sup.engine

    async def go():
        c = TestClient(TestServer(make_app(sup)))
        await c.start_server()
        h = {"X-Supply-Token": TOKEN}
        try:
            assert (await c.get("/api/supply/ping")).status == 401          # без токена — нельзя
            ping = await (await c.get("/api/supply/ping", headers=h)).json()
            assert ping["app"] == "mannbot" and ping["products"] == ["steam_keys"]
            q = await (await c.get("/api/supply/quote?product=steam_keys&amount=3", headers=h)).json()
            assert q["available"] and q["cost_rub"] == 450 and q["free"] >= 3, q
            free_before = await e.available_keys(force=True)
            body = {"ref": "K0001", "product": "steam_keys", "username": trade_url(111111), "amount": 3}
            r = await (await c.post("/api/supply/orders", json=body, headers=h)).json()
            assert r["ok"] and r["order_id"] == "SHOP-K0001" and r["status"] == "queued", r
            again = await (await c.post("/api/supply/orders", json=body, headers=h)).json()
            assert again["order_id"] == "SHOP-K0001"
            assert await e.available_keys(force=True) == free_before - 3            # ключи обещаны заказу
            await e.deliver("SHOP-K0001")
            st = await (await c.get("/api/supply/orders/K0001", headers=h)).json()
            assert st["status"] == "delivering" and st["offer_id"], st
            e.steam.tick()
            await e.watch_offers()
            st = await (await c.get("/api/supply/orders/K0001", headers=h)).json()
            assert st["status"] == "delivered", st
            assert not e.fp.outbox                                   # в FunPay никому не пишем
            assert not [a for a in sup.db.q("SELECT * FROM alerts WHERE code='no_chat'")]
            bad = await c.post("/api/supply/orders", json={**body, "ref": "K0002", "username": "@user"}, headers=h)
            assert bad.status == 400
        finally:
            await c.close()
    asyncio.run(go())


def test_shop_bad_link_and_declined_trade_fail_cleanly(tmp_path):
    sup = _sup(tmp_path)
    e = sup.engine

    async def go():
        c = TestClient(TestServer(make_app(sup)))
        await c.start_server()
        h = {"X-Supply-Token": TOKEN}
        try:
            free = await e.available_keys(force=True)
            body = {"ref": "K0003", "product": "steam_keys", "username": trade_url(222222, "Expired1"), "amount": 1}
            r = await (await c.post("/api/supply/orders", json=body, headers=h)).json()
            assert r["status"] == "failed" and r["detail"] == "invalid_token", r
            link = trade_url(333333)
            e.steam.behaviour[str(76561197960265728 + 333333)] = "decline"
            r = await (await c.post("/api/supply/orders", json={**body, "ref": "K0004", "username": link},
                                    headers=h)).json()
            await e.deliver("SHOP-K0004")
            e.steam.tick()
            await e.watch_offers()
            st = await (await c.get("/api/supply/orders/K0004", headers=h)).json()
            assert st["status"] == "failed" and st["detail"] == "offer_declined", st
            assert e.db.get_order("SHOP-K0004")["status"] == S.REFUNDED.value
            assert await e.available_keys(force=True) == free                      # ключи вернулись на склад
            sup.state = "stopped"
            q = await (await c.get("/api/supply/quote?product=steam_keys&amount=1", headers=h)).json()
            assert not q["available"] and "не запущен" in q["reason"]
        finally:
            await c.close()
    asyncio.run(go())
