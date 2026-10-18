import asyncio
import sqlite3
import time

from aiohttp import web
from aiohttp.test_utils import TestServer

from center.app import Center
from center.demo import lisbot_schema
from center.workers import RUNNING

NOW = time.time()
DAY = 86400


def _seed(home):
    d = home / "lisbot" / "data"
    d.mkdir(parents=True)
    con = sqlite3.connect(d / "bot.sqlite")
    con.executescript(lisbot_schema())
    for name, enabled in (("Loser", 1), ("Winner", 1), ("Off", 0)):
        con.execute("INSERT INTO watchlist(name, enabled, max_held, created_at) VALUES(?,?,2,0)", (name, enabled))
    sales = [("Loser", -2.0)] * 3 + [("Winner", 1.0)] * 3 + [("Off", -5.0)] * 3 + [("Loser", -9.0)]
    for i, (name, p) in enumerate(sales):
        t = (NOW - (40 if i == 9 else i + 1) * DAY) * 1000        # последняя убыточная — старше 30 дней
        con.execute("INSERT INTO items(name, status, buy_price_usd, sold_usd, profit_usd, sold_at, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", (name, "sold", 10, 10 + p, p, int(t), int(t - DAY * 1000), int(t)))
    con.commit()
    con.close()


def test_losing_item_proposed_and_disabled_via_lisbot_api(tmp_path):
    _seed(tmp_path)

    async def go():
        posted = []

        async def wl(request):
            return web.json_response([{"name": "Loser", "enabled": 1, "max_held": 2, "max_per_day": None, "min_roi": 0.1,
                                       "max_price_usd": None, "notes": None}])

        async def wl_post(request):
            posted.append(await request.json())
            return web.json_response({"ok": True})
        app = web.Application()
        app.add_routes([web.get("/api/watchlist", wl), web.post("/api/watchlist", wl_post)])
        srv = TestServer(app)
        await srv.start_server()
        c = Center(tmp_path)
        co = c.coordinator
        found = [a for a in co.detect() if a["tool"] == "disable_item"]
        assert [a["args"]["name"] for a in found] == ["Loser"] and "−$6.00" in found[0]["reason"]
        await co.check()
        p = co.decisions(kind="proposal", status="proposed")[0]
        assert p["tool"] == "disable_item" and p["title"] == "Перестать закупать «Loser»"
        w = c.workers["lisbot"]
        w.state, w.port = RUNNING, srv.port
        ok, msg = await co.decide(p["id"], True)
        assert ok and msg == "закупки предмета выключены"
        assert posted[0]["name"] == "Loser" and posted[0]["enabled"] is False
        assert posted[0]["max_held"] == 2 and posted[0]["min_roi"] == 0.1 and "координатором" in posted[0]["notes"]
        await srv.close()
        c.events.close()
    asyncio.run(go())
