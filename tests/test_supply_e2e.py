"""Стыки ShopBot → TG Gift и ShopBot → MannBot по HTTP-контракту /api/supply/*."""
import asyncio
import sys
from pathlib import Path

from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "bots" / "starsbot", ROOT / "bots" / "shopbot", ROOT / "bots" / "mannbot"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

TOKEN = "supply-token-e2e-1234567"


def test_shopbot_http_supplier_orders_exact_gift_from_tggift(tmp_path):
    from shopbot.suppliers import HttpSupplier
    from starsbot.app.supervisor import Supervisor
    from starsbot.web.server import make_app

    over = {"general": {"demo": True, "dry_run": False, "database": ":memory:", "log_file": "", "profile": "giftshop"},
            "notifications": {"telegram": {"enabled": False}}, "gifts": {"pause_between_gifts": 0},
            "deal": {"message_rate_limit_seconds": 0}, "funpay": {"coord_db": ""}}

    async def go():
        sup = Supervisor(tmp_path / "tggift", demo=True, overrides=over)
        sup.save({"dashboard": {"token": "admin-token-12345"}, "supply": {"token": TOKEN}}, rebuild=False)
        e = sup.engine
        await e.lot_manager.create_missing()
        await e.gifts.watch()
        server = TestServer(make_app(sup))
        await server.start_server()
        try:
            shop = HttpSupplier("gifts", str(server.make_url("/")).rstrip("/"), TOKEN)
            assert (await shop.ping())["app"] == "tggift"
            item = {"id": "cake", "emoji": "🎂", "title": "Торт", "stars": 50}
            q = await shop.quote("gift", 50, gift=item)
            assert q["available"] and q["gift"]["emoji"] == "🎂", q
            r = await shop.order("SHOP77", "gift", "cake_lover", 50, gift=item)
            assert r["ok"], r
            await e.purchase(r["order_id"])
            st = await shop.status("SHOP77")
            assert st["status"] == "delivered", st
            sent = e.db.q("SELECT gift_id FROM gift_sends WHERE order_id=? AND state='sent'", (r["order_id"],))
            raw = e.gifts.tg._catalog
            emoji = {g.id: g.emoji for g in (raw[1] if isinstance(raw, tuple) else raw)}
            assert [emoji[int(s["gift_id"])] for s in sent] == ["🎂"]      # не 🚀/💐/🍾 той же цены
        finally:
            await server.close()
            await sup.stop()
    asyncio.run(go())


def test_shopbot_http_supplier_buys_steam_keys_from_mannbot(tmp_path):
    from mannbot.app.launcher import DEMO_OVERRIDES
    from mannbot.app.supervisor import Supervisor
    from mannbot.demo import trade_url
    from mannbot.web.server import make_app
    from shopbot.suppliers import HttpSupplier

    async def go():
        sup = Supervisor(tmp_path / "mannbot", demo=True, overrides=DEMO_OVERRIDES)
        sup.save({"supply": {"token": TOKEN}, "rtba": {"cost_per_key": 150}}, rebuild=False)
        sup.cfg.supply.token = TOKEN
        sup.engine.cfg.rtba["cost_per_key"] = 150
        sup.state = "running"
        server = TestServer(make_app(sup))
        await server.start_server()
        try:
            shop = HttpSupplier("steam_keys", str(server.make_url("/")).rstrip("/"), TOKEN)
            assert (await shop.ping())["app"] == "mannbot"
            q = await shop.quote("steam_keys", 2)
            assert q["available"] and q["cost_rub"] == 300
            r = await shop.order("B1234", "steam_keys", trade_url(424242), 2)
            assert r["ok"] and r["status"] == "queued", r
            await sup.engine.deliver(r["order_id"])
            sup.engine.steam.tick()
            await sup.engine.watch_offers()
            assert (await shop.status("B1234"))["status"] == "delivered"
            bad = await shop.order("B1235", "steam_keys", trade_url(424243, "Expired1"), 1)
            assert bad["status"] == "failed" and bad["detail"] == "invalid_token"
        finally:
            await server.close()
    asyncio.run(go())
