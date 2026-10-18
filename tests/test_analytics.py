import asyncio
import io
import json
import sqlite3
import time
import zipfile

import yaml
from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.web import make_app

NOW = time.time()
DAY = 86400


def seed(home, *, cost_per_key=150.0):
    """Маленькие базы обоих ботов: 3 продажи LisBot (USD) и 3 заказа MannBot (₽)."""
    lis = home / "lisbot" / "data"
    lis.mkdir(parents=True)
    con = sqlite3.connect(lis / "bot.sqlite")
    con.executescript("""
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER);
        CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, mode TEXT, status TEXT, dry_run INTEGER DEFAULT 0,
          buy_price_usd REAL, expected_net_usd REAL, expected_profit_usd REAL, received_at INTEGER, listed_at INTEGER,
          sold_at INTEGER, sold_usd REAL, profit_usd REAL, partner_steamid TEXT, created_at INTEGER, updated_at INTEGER);
    """)
    ms = lambda t: int(t * 1000)  # noqa: E731
    rows = [  # name, status, buy, expected, sold, profit, created, sold_at, dry
        ("AK-47 | Redline", "sold", 10.0, 12.0, 12.0, 2.0, NOW - 10 * DAY, NOW - 2 * DAY, 0),
        ("AK-47 | Redline", "sold", 10.0, 12.0, 11.0, 1.0, NOW - 9 * DAY, NOW - 1 * DAY, 0),
        ("AWP | Asiimov", "sold", 50.0, 55.0, 49.0, -1.0, NOW - 40 * DAY, NOW - 35 * DAY, 0),
        ("M4A4 | Howl", "listed", 100.0, 110.0, None, None, NOW - 3 * DAY, None, 0),
        ("Dry", "sold", 5.0, 6.0, 6.0, 1.0, NOW - 3 * DAY, NOW - 1 * DAY, 1),
    ]
    for n, st, buy, exp, sold, pr, cr, sa, dry in rows:
        con.execute("INSERT INTO items(name,mode,status,dry_run,buy_price_usd,expected_net_usd,received_at,sold_at,sold_usd,"
                    "profit_usd,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (n, "rtba", st, dry, buy, exp, ms(cr + DAY), ms(sa) if sa else None, sold, pr, ms(cr), ms(cr)))
    con.execute("INSERT INTO kv VALUES('fx', ?, 0)", (json.dumps({"rate": 90.0, "ts": ms(NOW)}),))
    con.execute("INSERT INTO kv VALUES('balance.last', ?, 0)", (json.dumps({"balance": 200.0, "ts": ms(NOW)}),))
    con.commit()
    con.close()

    mann = home / "mannbot"
    (mann / "data").mkdir(parents=True)
    (mann / "config.yaml").write_text(yaml.safe_dump({"rtba": {"cost_per_key": cost_per_key, "funpay_fee_pct": 10}}), encoding="utf-8")
    con = sqlite3.connect(mann / "data" / "mannbot.sqlite3")
    con.executescript("""
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE orders (order_id TEXT PRIMARY KEY, buyer_username TEXT, description TEXT, quantity INTEGER,
          keys_qty INTEGER, price REAL, currency TEXT, status TEXT, created_at REAL, paid_at REAL, link_received_at REAL,
          offer_sent_at REAL, delivered_at REAL, completed_at REAL);
    """)
    for oid, st, keys, price, t, delivered in [("A1", "completed", 2, 400.0, NOW - DAY, True),
                                               ("A2", "delivered", 1, 200.0, NOW - 2 * DAY, True),
                                               ("A3", "refunded", 1, 200.0, NOW - 3 * DAY, False),
                                               ("A4", "awaiting_link", 1, 200.0, NOW - 600, False)]:
        con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, "buyer", "Mann Co. Supply Crate Key", keys, keys, price, "₽", st, t, t,
                     t + 60 if delivered else None, t + 120 if delivered else None, t + 600 if delivered else None,
                     t + 3600 if st == "completed" else None))
    con.execute("INSERT INTO kv VALUES('last_stock', '10')")
    con.commit()
    con.close()


def test_pnl_daily_details(tmp_path):
    seed(tmp_path)

    async def go():
        center = Center(tmp_path)
        a = center.analytics
        p = await a.pnl(30)
        assert p["rate"]["rate"] == 90.0 and p["rate"]["source"] == "LisBot (Steam)"
        lb, mb = p["bots"]["lisbot"], p["bots"]["mannbot"]
        assert lb["deals"] == 2 and lb["USD"]["profit"] == 3.0 and lb["USD"]["revenue"] == 23.0   # без dry-run и старше 30 дн
        assert lb["USD"]["roi"] == 15.0 and lb["RUB"]["profit"] == 270.0
        assert lb["USD"]["capital"] == 300.0                                   # баланс 200 + товар на ТП 100
        # MannBot: 600 ₽ выручки, комиссия 60, себестоимость 3 ключа × 150 = 450 → прибыль 90
        assert mb["deals"] == 2 and mb["RUB"]["revenue"] == 600.0 and mb["RUB"]["profit"] == 90.0
        assert mb["RUB"]["capital"] == 1500.0 and mb["RUB"]["turnover"] == 0.3
        assert p["total"]["RUB"]["profit"] == 360.0 and not p["warnings"]
        assert (await a.pnl(None))["bots"]["lisbot"]["deals"] == 3

        d = await a.daily(7, "RUB")
        assert len(d["series"]) == 7 and round(sum(r["profit"] for r in d["series"]), 2) == 360.0

        ld = a.lisbot_details()
        red = next(i for i in ld["items"] if i["name"] == "AK-47 | Redline")
        assert red["sold"] == 2 and red["profit"] == 3.0 and red["roi"] == 15.0
        assert ld["forecast"]["n"] == 3 and ld["hold"]["median_days"] is not None
        types = {t["type"]: t for t in ld["by_type"]}
        assert red["type"] == "Винтовки" and "sold_spent" not in red
        assert types["Винтовки"]["names"] == 2 and types["Винтовки"]["bought"] == 3 and types["Винтовки"]["profit"] == 3.0
        assert types["Винтовки"]["roi"] == 15.0 and types["Снайперские"]["roi"] == -2.0

        md = a.mannbot_details(30)
        assert md["orders"] == 4 and md["funnel"][3]["n"] == 2 and md["confirm_rate"] == 50.0
        assert sum(md["by_hour"]) == 4 and sum(md["by_weekday"]) == 4 and len(md["by_hour"]) == 24
        assert md["times"]["total"] == 600 and md["keys_sold"] == 3
        center.events.close()
    asyncio.run(go())


def test_mannbot_without_cost_warns(tmp_path):
    seed(tmp_path, cost_per_key=0)

    async def go():
        center = Center(tmp_path)
        p = await center.analytics.pnl(30)
        assert p["bots"]["mannbot"]["RUB"]["profit"] is None and p["warnings"]
        assert p["total"]["RUB"]["profit"] == 270.0                            # только LisBot
        center.events.close()
    asyncio.run(go())


def test_empty_center(tmp_path):
    async def go():
        center = Center(tmp_path)
        center.cfg.data["analytics"]["usd_rub"] = 100
        p = await center.analytics.pnl(30)
        assert p["rate"]["rate"] == 100 and p["total"]["deals"] == 0
        assert center.analytics.lisbot_details()["items"] == [] and center.analytics.mannbot_details()["orders"] == 0
        center.events.close()
    asyncio.run(go())


def test_api_and_export(tmp_path):
    seed(tmp_path)

    async def go():
        center = Center(tmp_path)
        token = center.cfg.ensure_token()
        async with TestClient(TestServer(make_app(center))) as c:
            assert (await c.get("/api/analytics/pnl")).status == 401
            await c.get(f"/login?token={token}", allow_redirects=False)
            p = await (await c.get("/api/analytics/pnl?days=all")).json()
            assert p["total"]["deals"] == 5
            d = await (await c.get("/api/analytics/daily?days=30&currency=USD")).json()
            assert d["currency"] == "USD" and len(d["series"]) == 30
            assert (await (await c.get("/api/analytics/bot/mannbot")).json())["orders"] == 4
            r = await c.get("/api/analytics/export.csv")
            text = (await r.read()).decode("utf-8-sig")
            assert r.headers["Content-Disposition"].startswith("attachment") and text.count("\n") == 6
            r = await c.get("/api/analytics/export.xlsx")
            z = zipfile.ZipFile(io.BytesIO(await r.read()))
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
            assert "AWP | Asiimov" in sheet and "Прибыль, ₽" in sheet
        center.events.close()
    asyncio.run(go())
