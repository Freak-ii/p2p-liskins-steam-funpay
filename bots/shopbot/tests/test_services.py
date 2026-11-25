import asyncio
import json

from shopbot.config import Config
from shopbot.db import Database
from shopbot.engine import Shop
from shopbot.tg import ShopTelegram

ADMIN, U = 900, 2001


def make(tmp_path, **over):
    over.setdefault("telegram", {"admin_chat_ids": [ADMIN]})
    cfg = Config(tmp_path, demo=True, overrides=over)
    shop = Shop(cfg, Database(":memory:"), demo=True)
    bot = ShopTelegram(shop)
    shop.bot = bot
    return shop, bot


def texts(bot, uid):
    return [m["text"] for m in bot.outbox if m["chat"] == uid]


def buttons(bot, uid):
    out = []
    for m in bot.outbox:
        if m["chat"] == uid and m["buttons"]:
            out += [b.get("callback_data") for row in m["buttons"]["inline_keyboard"] for b in row]
    return out


async def flow(shop, bot, steps, inputs=(), method="pay:balance", money=10000):
    db = shop.db
    db.touch_user(U, "buyer", "B")
    db.credit(U, "rub", money, "topup", "", "тест")
    await shop.rtba.tick()
    for st in steps:
        await bot.on_callback(U, st)
    for t in inputs:
        await bot.on_text(U, "buyer", "B", t)
    assert method in buttons(bot, U), buttons(bot, U)[-6:]
    await bot.on_callback(U, method)
    oid = db.scalar("SELECT id FROM orders WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (U,))
    for _ in range(3):
        await shop.poll_deliveries()
    return db.order(oid)


def test_vpn_from_own_panel_with_consent(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:vpn", "sx:vpn:3", "sa:vpn:3"]))
    assert o["status"] == "delivered" and o["price_rub"] == 449
    out = "\n".join(texts(bot, U))
    assert "vless://" in out and "Happ" in out
    ev = {e["kind"]: json.loads(e["data"]) for e in shop.db.events(o["id"])}
    assert ev["terms_accepted"]["shown"] and ev["terms_accepted"]["notice_hash"]
    assert ev["service_payload"]["source"] == "panel"
    # себестоимость своего сервера: 560 ₽ / 100 ключей × 3 мес
    assert abs(o["cost_rub"] - 16.8) < 0.01


def test_appstore_from_stock_fifo_cost(tmp_path):
    shop, bot = make(tmp_path)
    shop.services.stock.add("appstore_us:20", ["AAAA-1111", "BBBB-2222"], 1780, "Bitrefill USDT")
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:appstore_us", "sx:appstore_us:20", "sa:appstore_us:20"]))
    assert o["status"] == "delivered"
    assert "AAAA-1111" in "\n".join(texts(bot, U))
    assert o["cost_rub"] == 1780 and o["margin_rub"] > 100
    assert shop.services.stock.count("appstore_us:20") == 1


def test_claude_resale_operator_delivers_code(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:claude_pro", "sx:claude_pro:1", "sa:claude_pro:1"]))
    assert o["status"] == "manual"
    adm = "\n".join(texts(bot, ADMIN))
    assert "ggsel.net/catalog/itunes-us" in adm and f"adm:svc:{o['id']}" in buttons(bot, ADMIN)
    # без кода выдать нельзя
    assert "пришлите код" in asyncio.run(shop.admin_service_deliver(o["id"], "готово"))
    asyncio.run(bot.on_callback(ADMIN, f"adm:svc:{o['id']}"))
    asyncio.run(bot.on_text(ADMIN, "", "", "XYZ-CLAUDE-20"))
    o = shop.db.order(o["id"])
    assert o["status"] == "delivered"
    out = "\n".join(texts(bot, U))
    assert "XYZ-CLAUDE-20" in out and "Upgrade to Pro" in out
    # «Мои заказы» → код можно показать снова
    asyncio.run(bot.on_callback(U, f"code:{o['id']}"))
    assert texts(bot, U)[-1].count("XYZ-CLAUDE-20") == 1


def test_spotify_login_password_encrypted_and_wiped(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:spotify", "sx:spotify:3", "sa:spotify:3"],
                         inputs=["buyer@mail.com", "S3cret-pass"]))
    assert o["status"] == "manual" and o["recipient"] == "buyer@mail.com"
    raw = shop.db.scalar("SELECT data FROM order_secrets WHERE order_id=?", (o["id"],))
    assert raw and "S3cret-pass" not in raw                              # зашифровано
    sess = shop.db.q("SELECT data FROM sessions")
    assert all("S3cret-pass" not in (r["data"] or "") for r in sess)    # и в сессии не лежит открытым
    assert "S3cret-pass" in "\n".join(texts(bot, ADMIN))                # оператор видит (под спойлером)
    assert o["cost_rub"] == 846 and o["price_rub"] == 973                # 846 × 1.15
    asyncio.run(shop.admin_service_deliver(o["id"], "готово"))
    assert shop.db.order(o["id"])["status"] == "delivered"
    assert shop.db.scalar("SELECT COUNT(*) FROM order_secrets") == 0


def test_steam_topup_amount_and_login(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:steam_topup", "sx:steam_topup:rub", "sa:steam_topup:rub"],
                         inputs=["50", "1000", "gaben_login"]))
    assert "Сумма от 100" in "\n".join(texts(bot, U))
    assert o["amount"] == 1000 and o["recipient"] == "gaben_login"
    assert o["price_rub"] == 1100 and o["cost_rub"] == 1070


def test_operator_refund_and_rtba_closes_overpriced(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:claude_pro", "sx:claude_pro:1", "sa:claude_pro:1"]))
    asyncio.run(bot.on_callback(ADMIN, f"adm:svcfail:{o['id']}"))
    assert shop.db.order(o["id"])["status"] == "refunded"
    assert shop.db.user(U)["balance_rub"] == 10000
    # ChatGPT: перепродажа карты дороже рынка → закрыт; карта со склада (куплена за USDT) — снова открыт
    pt = {p["key"]: p for p in shop.rtba.points()}
    assert pt["svc.vpn"]["state"] == "open"
    shop.cfg.data["catalog"]["services"]["max_over_market_pct"] = 0.5
    gpt = next(i for i in shop.cfg.data["catalog"]["services"]["items"] if i["id"] == "chatgpt_plus")
    gpt["offers"][0]["rub_per_usd"] = 115           # карты подорожали: 2300 ₽ + 3% > рынка 2222 ₽
    asyncio.run(shop.rtba.tick())
    assert not shop.rtba.is_open("svc.chatgpt_plus")
    assert "дороже рынка" in shop.rtba.point("svc.chatgpt_plus")["reason"]
    shop.services.stock.add("appstore_us:20", ["C1"], 1760)
    asyncio.run(shop.rtba.tick())
    assert shop.rtba.is_open("svc.chatgpt_plus")


def test_economics_table(tmp_path):
    shop, _ = make(tmp_path)
    rows = {r["key"]: r for r in shop.services.economics()}
    assert rows["vpn:12"]["source"] == "panel" and rows["vpn:12"]["margin_pct"] > 1000
    assert rows["appstore_us:20"]["cost_rub"] == 2120 and rows["appstore_us:20"]["price_rub"] == 2187   # floor 3%
    shop.services.stock.add("appstore_us:20", ["S1"], 1771)
    r = {r["key"]: r for r in shop.services.economics()}["appstore_us:20"]
    assert r["source"] == "stock" and r["price_rub"] == 2078 and r["margin_pct"] > 17      # 2120 − 2%
    assert rows["spotify:12"]["price_rub"] == 2474


def test_vpn_renew_keeps_link_and_reminder(tmp_path):
    import time as _t
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:vpn", "sx:vpn:1", "sa:vpn:1"]))
    j = shop.db.one("SELECT * FROM service_jobs WHERE order_id=?", (o["id"],))
    meta1 = json.loads(j["meta"])
    assert j["expires_at"] > _t.time() + 29 * 86400
    # за 3 дня до конца — напоминание с кнопкой продления, один раз
    shop.db.exec("UPDATE service_jobs SET expires_at=? WHERE ref=?", (_t.time() + 2 * 86400, j["ref"]))
    asyncio.run(shop.poll_deliveries())
    asyncio.run(shop.poll_deliveries())
    assert sum("закончится через" in t for t in texts(bot, U)) == 1
    assert f"rn:{o['id']}" in buttons(bot, U)
    asyncio.run(bot.on_callback(U, f"rn:{o['id']}"))
    asyncio.run(bot.on_callback(U, f"rv2:{o['id']}:3"))
    asyncio.run(bot.on_callback(U, "pay:balance"))
    o2 = shop.db.scalar("SELECT id FROM orders WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (U,))
    j2 = shop.db.one("SELECT * FROM service_jobs WHERE order_id=?", (o2,))
    meta2 = json.loads(j2["meta"])
    assert shop.db.order(o2)["status"] == "delivered"
    assert meta2["uuid"] == meta1["uuid"] and meta2["sub_id"] == meta1["sub_id"]       # тот же ключ
    assert abs(j2["expires_at"] - (_t.time() + 2 * 86400 + 90 * 86400)) < 60            # срок прибавился
    assert "продлён до" in texts(bot, U)[-3] or any("продлён до" in t for t in texts(bot, U))


def test_3xui_two_servers_create_and_renew(tmp_path):
    """Настоящий HTTP-протокол 3x-ui на фейковой панели: вход, addClient, inbound для ссылки, updateClient."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    calls = []

    def panel_app(name):
        clients = {}

        async def login(r):
            f = await r.post()
            ok = f.get("username") == "u" and f.get("password") == "p"
            resp = web.json_response({"success": ok})
            if ok:
                resp.set_cookie("3x-ui", "s")
            return resp

        def authed(r):
            if r.cookies.get("3x-ui") != "s":
                raise web.HTTPUnauthorized()

        async def add(r):
            authed(r)
            b = await r.json()
            c = json.loads(b["settings"])["clients"][0]
            clients[c["id"]] = c
            calls.append((name, "add", c["email"], c["expiryTime"]))
            return web.json_response({"success": True})

        async def upd(r):
            authed(r)
            b = await r.json()
            c = json.loads(b["settings"])["clients"][0]
            assert r.match_info["cid"] in clients
            clients[c["id"]] = c
            calls.append((name, "update", c["email"], c["expiryTime"]))
            return web.json_response({"success": True})

        async def get(r):
            authed(r)
            st = {"network": "tcp", "security": "reality",
                  "realitySettings": {"serverNames": ["www.microsoft.com"], "shortIds": ["abcd"],
                                      "settings": {"publicKey": "PUBKEY", "fingerprint": "chrome", "spiderX": "/"}}}
            return web.json_response({"success": True, "obj": {"port": 443, "streamSettings": json.dumps(st)}})

        app = web.Application()
        app.add_routes([web.post("/x/login", login), web.post("/x/panel/api/inbounds/addClient", add),
                        web.post("/x/panel/api/inbounds/updateClient/{cid}", upd),
                        web.get("/x/panel/api/inbounds/get/{iid}", get)])
        return app

    async def go():
        s1, s2 = TestServer(panel_app("nl1")), TestServer(panel_app("de1"))
        await s1.start_server()
        await s2.start_server()
        try:
            shop, bot = make(tmp_path)
            shop.services.panel.demo = False
            shop.cfg.data["services"]["panels"] = [
                {"name": "nl1", "kind": "3xui", "url": f"http://127.0.0.1:{s1.port}/x", "username": "u",
                 "password": "p", "inbound_id": 1, "host": "1.2.3.4", "sub_base": "http://1.2.3.4:2096/sub"},
                {"name": "de1", "kind": "3xui", "url": f"http://127.0.0.1:{s2.port}/x", "username": "u",
                 "password": "p", "inbound_id": 1, "host": "5.6.7.8"}]
            text, meta = await shop.services.panel.create("B1", 1)
            lines = text.splitlines()
            assert lines[0] == f"http://1.2.3.4:2096/sub/{meta['sub_id']}"
            assert lines[1].startswith(f"vless://{meta['uuid']}@1.2.3.4:443?") and "pbk=PUBKEY" in lines[1]
            assert "flow=xtls-rprx-vision" in lines[1] and "sni=www.microsoft.com" in lines[1]
            assert lines[2].startswith(f"vless://{meta['uuid']}@5.6.7.8:443?") and meta["servers"] == 2
            _, meta2 = await shop.services.panel.create("B2", 3, meta)
            assert [c[1] for c in calls] == ["add", "add", "update", "update"]
            assert meta2["expires_at"] - meta["expires_at"] == 90 * 86400
            # один сервер лежит — ключ всё равно выдаётся с другого
            await s2.close()
            text, meta3 = await shop.services.panel.create("B3", 1)
            assert meta3["servers"] == 1 and meta3["errors"]
        finally:
            await s1.close()
            await s2.close()
    asyncio.run(go())


def test_operator_real_cost_corrects_margin_and_is_learned(tmp_path):
    from shopbot.services import split_cost
    assert split_cost("AB-12 цена 2050") == ("AB-12", 2050.0)
    assert split_cost("готово cost: 240,5 ₽") == ("готово", 240.5)
    assert split_cost("CODE-2050") == ("CODE-2050", None)
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:claude_pro", "sx:claude_pro:1", "sa:claude_pro:1"]))
    sku = shop.services.catalog.sku("claude_pro:1")
    before = [x for x in shop.services.options("claude_pro:1") if x["source"] == "resale"][0]["cost_rub"]
    asyncio.run(bot.on_callback(ADMIN, f"adm:svc:{o['id']}"))
    asyncio.run(bot.on_text(ADMIN, "", "", "XYZ-CLAUDE-20 цена 1990"))
    o = shop.db.order(o["id"])
    assert o["status"] == "delivered" and o["cost_rub"] == 1990
    assert abs(o["margin_rub"] - (o["price_rub"] - o["discount_rub"] - 1990)) < 0.01
    out = "\n".join(texts(bot, U))
    assert "XYZ-CLAUDE-20" in out and "цена 1990" not in out
    assert shop.services.learned_cost(sku) == 1990
    r = [x for x in shop.services.options("claude_pro:1") if x["source"] == "resale"][0]
    assert r["cost_rub"] == 1990 and r["learned"] and r["config_cost_rub"] == before
    # выключатель
    shop.cfg.data["services"]["learned_cost_days"] = 0
    assert shop.services.learned_cost(sku) is None
