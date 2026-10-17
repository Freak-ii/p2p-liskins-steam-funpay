import asyncio
import json
import sqlite3
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.web import make_app
from tests.test_analytics import seed

NOW = time.time()
DAY = 86400


def seed_crm(home):
    """База аналитики + то, что нужно CRM: заметки/теги LisBot, покупатели, сообщения и события MannBot."""
    seed(home)
    con = sqlite3.connect(home / "lisbot" / "data" / "bot.sqlite")
    con.executescript("""
        ALTER TABLE items ADD COLUMN notes TEXT; ALTER TABLE items ADD COLUMN tags TEXT; ALTER TABLE items ADD COLUMN relists INTEGER;
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER, type TEXT, item_id INTEGER, data TEXT);
        UPDATE items SET notes='редкий флоат', tags='hold,флоат' WHERE id=1;
    """)
    con.execute("INSERT INTO events(ts,type,item_id,data) VALUES(?,?,?,?)", (int(NOW * 1000), "item.sold", 1, '{"usd": 12}'))
    con.commit()
    con.close()

    con = sqlite3.connect(home / "mannbot" / "data" / "mannbot.sqlite3")
    con.executescript("""
        ALTER TABLE orders ADD COLUMN buyer_id INTEGER; ALTER TABLE orders ADD COLUMN chat_id TEXT;
        ALTER TABLE orders ADD COLUMN steam_id64 TEXT; ALTER TABLE orders ADD COLUMN risk_score INTEGER DEFAULT 0;
        ALTER TABLE orders ADD COLUMN risk_reasons TEXT DEFAULT '[]'; ALTER TABLE orders ADD COLUMN review_stars INTEGER;
        ALTER TABLE orders ADD COLUMN notes TEXT DEFAULT ''; ALTER TABLE orders ADD COLUMN trade_url TEXT;
        CREATE TABLE customers (funpay_id INTEGER PRIMARY KEY, username TEXT, first_seen REAL, last_seen REAL,
          orders_count INTEGER DEFAULT 0, completed_count INTEGER DEFAULT 0, refunded_count INTEGER DEFAULT 0,
          total_spent REAL DEFAULT 0, keys_bought INTEGER DEFAULT 0, steam_ids TEXT DEFAULT '[]',
          blacklisted INTEGER DEFAULT 0, notes TEXT DEFAULT '', tags TEXT DEFAULT '');
        CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id TEXT, order_id TEXT, direction TEXT, text TEXT, ts REAL);
        CREATE TABLE order_events (id INTEGER PRIMARY KEY, order_id TEXT, ts REAL, kind TEXT, data TEXT);
        -- buyer (id 7): A1 подтверждён с 5★, A2 выдан давно без подтверждения, A3 возврат; у A4 другой покупатель
        UPDATE orders SET buyer_id=7, chat_id='c7', steam_id64='765', review_stars=5, trade_url='secret' WHERE order_id='A1';
        UPDATE orders SET buyer_id=7, chat_id='c7', steam_id64='765', delivered_at=delivered_at-5*86400 WHERE order_id='A2';
        UPDATE orders SET buyer_id=7, chat_id='c7', risk_score=40, risk_reasons='["+40: тест"]' WHERE order_id='A3';
        UPDATE orders SET buyer_id=8, buyer_username='twin', chat_id='c8', steam_id64='765' WHERE order_id='A4';
    """)
    con.execute("INSERT INTO customers(funpay_id,username,first_seen,last_seen,steam_ids,tags) VALUES(7,'buyer',?,?,'[\"765\"]','опт')",
                (NOW - 3 * DAY, NOW))
    con.execute("INSERT INTO messages(chat_id,order_id,direction,text,ts) VALUES('c7','A1','in','привет',?)", (NOW,))
    con.execute("INSERT INTO order_events(order_id,ts,kind,data) VALUES('A1',?,'created','{}')", (NOW,))
    con.commit()
    con.close()


def test_deals_list_filters(tmp_path):
    seed_crm(tmp_path)
    center = Center(tmp_path)
    crm = center.crm
    d = crm.deals()
    assert d["total"] == 8                                     # 4 предмета LisBot без теста + 4 заказа MannBot
    assert d["groups"] == {"open": 2, "attention": 0, "done": 5, "failed": 1}
    assert crm.deals(dry=True)["total"] == 9
    assert crm.deals(bot="mannbot", group="done")["total"] == 2
    assert [r["id"] for r in crm.deals(q="флоат")["rows"]] == ["L1"]
    assert crm.deals(q="TWIN")["rows"][0]["id"] == "MA4"
    assert crm.deals(days=7)["total"] == 7                     # старая сделка AWP за 35–40 дней отсеяна
    rows = crm.deals(sort="profit")["rows"]
    assert rows[0]["id"] == "MA1" and rows[0]["profit"] == 60.0     # 400 - 40 комиссии - 2×150
    assert crm.deals(limit=3, offset=3)["rows"][0]["id"] == crm.deals()["rows"][3]["id"]
    t = crm.deals()["totals"]
    assert t["USD"]["profit"] == 2.0 and t["RUB"]["revenue"] == 600.0

    lis = crm.deal("lisbot", "1")
    assert lis["deal"]["tags"] == "hold,флоат" and lis["events"][0]["data"] == {"usd": 12}
    mann = crm.deal("mannbot", "A1")
    assert mann["messages"][0]["text"] == "привет" and mann["customer"]["username"] == "buyer"
    assert "trade_url" not in mann["raw"]
    rows = crm.export_rows(bot="lisbot")
    assert rows[0][0] == "Бот" and len(rows) == 5
    center.events.close()


def test_customers_and_risk(tmp_path):
    seed_crm(tmp_path)
    center = Center(tmp_path)
    crm = center.crm
    cs = crm.customers()
    assert cs["total"] == 2 and cs["tags"] == ["опт"]          # twin есть только в заказах — тоже попадает в список
    c = crm.customer(7)
    st = c["stats"]
    assert st["orders"] == 3 and st["completed"] == 1 and st["refunded"] == 1 and st["spent"] == 600.0 and st["keys"] == 3
    assert st["avg_stars"] == 5.0 and st["confirm_rate"] == 50.0
    texts = " | ".join(r["text"] for r in c["risk"]["reasons"])
    assert "возвратов: 1" in texts and "общий с другими" in texts and "не подтвердил" in texts and "антифроду" in texts
    assert c["risk"]["score"] == 20 + 20 + 35 + 10 and c["risk"]["level"] == "high"
    assert c["shared_with"] == [{"funpay_id": 8, "username": "twin"}]
    assert [o["order_id"] for o in c["orders"]] == ["A1", "A2", "A3"] and c["reviews"][0]["stars"] == 5
    assert crm.customers(risk="high")["total"] == 1 and crm.customers(tag="опт")["rows"][0]["funpay_id"] == 7
    assert crm.customers(q="765")["total"] == 2
    center.events.close()


def test_writes_go_through_bot_apis(tmp_path):
    seed_crm(tmp_path)
    got = []

    async def lis_item(request):
        got.append(("lis", request.headers.get("Authorization"), await request.json()))
        return web.json_response({"ok": True})

    async def mann_order(request):
        got.append(("order", request.match_info["oid"], await request.json()))
        return web.json_response({"ok": True})

    async def mann_customer(request):
        if request.match_info["cid"] == "404":
            return web.json_response({"ok": False, "error": "покупатель не найден"}, status=404)
        got.append(("customer", request.cookies.get("mb_token"), await request.json()))
        return web.json_response({"ok": True})

    async def go():
        center = Center(tmp_path)
        token = center.cfg.ensure_token()
        bots = web.Application()
        bots.add_routes([web.post("/api/item", lis_item), web.post("/api/orders/{oid}", mann_order),
                         web.post("/api/customers/{cid}", mann_customer)])
        async with TestServer(bots) as srv:
            async with TestClient(TestServer(make_app(center))) as c:
                await c.get(f"/login?token={token}", allow_redirects=False)
                r = await c.post("/api/crm/deals/lisbot/1", json={"notes": "n"})
                assert r.status == 409 and "не запущен" in (await r.json())["message"]
                for w in center.workers.values():
                    w.port, w.state = srv.port, "running"
                r = await (await c.post("/api/crm/deals/lisbot/1", json={"notes": "n", "tags": "a,b"})).json()
                assert r["ok"] and got[-1][0] == "lis" and got[-1][2] == {"id": 1, "notes": "n", "tags": "a,b"}
                assert got[-1][1].startswith("Bearer")
                assert (await c.post("/api/crm/deals/mannbot/A1", json={"tags": "x"})).status == 400
                await c.post("/api/crm/deals/mannbot/A1", json={"notes": "VIP"})
                assert got[-1][:2] == ("order", "A1") and got[-1][2]["notes"] == "VIP"
                r = await (await c.post("/api/crm/customers/7", json={"tags": "VIP", "blacklisted": True})).json()
                assert r["ok"] and got[-1][2] == {"tags": "VIP", "blacklisted": True}
                r = await c.post("/api/crm/customers/404", json={"notes": "x"})
                assert r.status == 404
                # чтение через API центра
                d = await (await c.get("/api/crm/deals?bot=mannbot&q=twin")).json()
                assert d["total"] == 1
                assert (await (await c.get("/api/crm/customers/7")).json())["username"] == "buyer"
                assert (await c.get("/api/crm/customers/999")).status == 404
                assert (await c.get("/api/crm/deals/lisbot/999")).status == 404
                csv = (await (await c.get("/api/crm/deals.csv?bot=lisbot")).read()).decode("utf-8-sig")
                assert csv.count("\n") == 5 and "редкий флоат" in csv
        kinds = [e["kind"] for e in center.events.recent(20)]
        assert kinds.count("crm.edit") == 2 and "crm.customer" in kinds
        center.events.close()
    asyncio.run(go())


def test_empty(tmp_path):
    center = Center(tmp_path)
    assert center.crm.deals()["total"] == 0 and center.crm.customers()["total"] == 0
    center.events.close()
