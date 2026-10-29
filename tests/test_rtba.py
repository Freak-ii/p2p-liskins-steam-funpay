import asyncio
import json
import shutil
import sqlite3
import time

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.web import make_app
from tests.test_analytics import seed

NOW = time.time()
H = 3600
NAME = "AK-47 | Redline"


def seed_rtba(home):
    """К базам из seed(): история пола LIS, оценка, вотчлист, события RTBA-покупок; снимки рынка FunPay."""
    seed(home)
    con = sqlite3.connect(home / "lisbot" / "data" / "bot.sqlite")
    con.executescript("""
        CREATE TABLE lis_floor (id INTEGER PRIMARY KEY, name TEXT, ts INTEGER, floor_usd REAL, listings INTEGER);
        CREATE TABLE valuations (name TEXT PRIMARY KEY, ts INTEGER, ok INTEGER, reason TEXT, daily_volume REAL,
          net_usd REAL, fx REAL, max_buy_usd REAL);
        CREATE TABLE watchlist (name TEXT PRIMARY KEY, enabled INTEGER, source TEXT, max_held INTEGER, max_per_day INTEGER,
          min_roi REAL, max_price_usd REAL, notes TEXT, created_at INTEGER);
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts INTEGER, type TEXT, item_id INTEGER, data TEXT);
    """)
    for i, p in enumerate([10, 10.1, 9.9, 10, 8, 8, 10, 14, 10]):
        con.execute("INSERT INTO lis_floor(name,ts,floor_usd,listings) VALUES(?,?,?,5)", (NAME, int((NOW - (10 - i) * H) * 1000), p))
    con.execute("INSERT INTO valuations VALUES(?,?,1,'',500,12,90,10.7)", (NAME, int(NOW * 1000)))
    con.execute("INSERT INTO watchlist VALUES(?,1,'config',NULL,NULL,NULL,NULL,NULL,0)", (NAME,))
    for item_id, lat in ((1, 140), (2, 260)):
        con.execute("INSERT INTO events(ts,type,item_id,data) VALUES(?,?,?,?)",
                    (int((NOW - H) * 1000), "buy.ok", item_id, json.dumps({"latencyMs": lat, "mode": "rtba"})))
    con.commit()
    con.close()

    con = sqlite3.connect(home / "mannbot" / "data" / "mannbot.sqlite3")
    con.executescript("""
        CREATE TABLE price_history (id INTEGER PRIMARY KEY, ts REAL, offer_id TEXT, old_price REAL, new_price REAL,
          best_competitor REAL, competitors INTEGER, position INTEGER, reason TEXT, applied INTEGER);
        CREATE TABLE competitor_snapshots (id INTEGER PRIMARY KEY, ts REAL, data TEXT);
    """)
    for i in range(48):                          # двое суток рынка: лидер то 200, то 180 ₽
        ts = NOW - (48 - i) * H
        lead = 200 if (i // 6) % 2 == 0 else 180
        lots = [{"offer_id": "1", "seller": "me", "price": lead + 5, "amount": 50},
                {"offer_id": "2", "seller": "a", "price": lead, "amount": 30},
                {"offer_id": "3", "seller": "b", "price": lead + 10, "amount": 20}]
        con.execute("INSERT INTO competitor_snapshots(ts,data) VALUES(?,?)", (ts, json.dumps(lots)))
        con.execute("INSERT INTO price_history(ts,offer_id,old_price,new_price,best_competitor,competitors,position,reason,"
                    "applied) VALUES(?,?,?,?,?,?,?,?,?)", (ts, "1", lead + 5, lead - 0.5, lead, 2, 1, "лидер рынка", i % 6 == 0))
    con.commit()
    con.close()
    cfg = yaml.safe_load((home / "mannbot" / "config.yaml").read_text(encoding="utf-8"))
    cfg["funpay"] = {"lots": [{"offer_id": 1, "node_id": 1808}]}
    (home / "mannbot" / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def test_status_from_bot_databases(tmp_path):
    seed_rtba(tmp_path)

    async def go():
        center = Center(tmp_path)
        s = await center.rtba.status(7)
        lb, mb = s["lisbot"], s["mannbot"]
        assert not lb["live"] and lb["latency"]["n"] == 2 and lb["latency"]["p50"] == 260
        assert {b["name"] for b in lb["buys"]} == {NAME}
        assert not mb["live"] and len(mb["history"]) == 48 and mb["competitors"][0]["seller"] == "me"
        assert mb["floor"] == round(150 * 1.05 / 0.9, 2)           # себестоимость + маржа 5% с учётом комиссии 10%
        center.events.close()
    asyncio.run(go())


def test_budget_plan_is_only_applied_by_command(tmp_path):
    seed_rtba(tmp_path)

    async def go():
        center = Center(tmp_path)
        center.cfg.save({"analytics": {"usd_rub": 100}})
        assert not center.rtba.save_plan({"total_usd": 500, "plan": {"mannbot.min_margin_pct": 12,
                                                                       "lisbot.min_roi": 0.1, "mannbot.strategy": "median"}})
        assert center.rtba.save_plan({"plan": {"lisbot.min_roi": 5}})               # вне допустимого
        assert center.rtba.save_plan({"plan": {"dryRun": False}})                    # центр этим не управляет
        p = await center.rtba.plan()
        rows = {r["key"]: r for r in p["rows"]}
        # склад MannBot: 10 ключей × 150 ₽ = 1500 ₽ = 15 $ → LisBot может держать 500 − 15 = 485 $
        assert rows["lisbot.max_exposure_usd"]["planned"] == 485 and rows["lisbot.max_exposure_usd"]["differs"]
        assert rows["lisbot.min_roi"]["current"] == 0.08 and rows["mannbot.min_margin_pct"]["current"] == 5.0
        assert rows["mannbot.strategy"]["planned"] == "median"
        assert not (tmp_path / "lisbot" / "config.json").exists()                  # план сам ничего не меняет
        assert yaml.safe_load((tmp_path / "mannbot" / "config.yaml").read_text(encoding="utf-8"))["rtba"].get("min_margin_pct") is None

        res = await center.rtba.apply()
        assert res["ok"], res
        lis = json.loads((tmp_path / "lisbot" / "config.json").read_text(encoding="utf-8"))
        assert lis["strategy"] == {"maxOpenExposureUsd": 485.0, "minRoi": 0.1} and "dryRun" not in lis
        mann = yaml.safe_load((tmp_path / "mannbot" / "config.yaml").read_text(encoding="utf-8"))
        assert mann["rtba"]["min_margin_pct"] == 12 and mann["rtba"]["strategy"] == "median"
        assert "general" not in mann                                                 # dry_run не тронут
        assert not [r for r in (await center.rtba.plan())["rows"] if r["differs"]]
        # пункт можно убрать из плана
        assert not center.rtba.save_plan({"plan": {"lisbot.min_roi": None}})
        assert "lisbot.min_roi" not in yaml.safe_load((tmp_path / "center.yaml").read_text(encoding="utf-8"))["budget"]["plan"]
        assert any(e["kind"] == "budget.apply" for e in center.events.recent(20))
        # «Вернуть как было»: значения до применения уходят ботам, план не меняется, второй раз отменять нечего
        last = (await center.rtba.plan())["last_apply"]
        assert {r["key"]: r["previous"] for r in last["rows"]}["mannbot.min_margin_pct"] == 5.0
        undo = await center.rtba.undo()
        assert undo["ok"], undo
        lis = json.loads((tmp_path / "lisbot" / "config.json").read_text(encoding="utf-8"))
        assert lis["strategy"]["minRoi"] == 0.08
        mann = yaml.safe_load((tmp_path / "mannbot" / "config.yaml").read_text(encoding="utf-8"))
        assert mann["rtba"]["min_margin_pct"] == 5.0
        assert not (await center.rtba.undo())["ok"] and center.rtba.last_apply() is None
        assert any(e["kind"] == "budget.undo" for e in center.events.recent(20))
        center.events.close()
    asyncio.run(go())


def test_apply_to_running_bots_via_their_api(tmp_path):
    seed_rtba(tmp_path)
    got = {}

    async def lis_settings(request):
        got["lis"] = await request.json()
        return web.json_response({"ok": True})

    async def mann_cfg(request):
        got["mann"] = await request.json()
        return web.json_response({"ok": True, "applied": got["mann"]["changes"], "live": True})

    async def mann_rtba(request):
        return web.json_response({"config": {"min_margin_pct": 5.0, "strategy": "top1"}, "history": []})

    async def lis_get(request):
        return web.json_response({"fields": [{"key": "strategy.minRoi", "value": 0.08}]})

    async def go():
        center = Center(tmp_path)
        center.cfg.save({"analytics": {"usd_rub": 100}})
        servers = []
        for w, routes in ((center.workers["lisbot"], [web.post("/api/settings", lis_settings), web.get("/api/settings", lis_get)]),
                          (center.workers["mannbot"], [web.post("/api/rtba/config", mann_cfg), web.get("/api/rtba", mann_rtba)])):
            app = web.Application()
            app.add_routes(routes)
            srv = TestServer(app)
            await srv.start_server()
            servers.append(srv)
            w.port, w.state = srv.port, "running"
        center.rtba.save_plan({"plan": {"lisbot.min_roi": 0.12, "mannbot.min_margin_pct": 9}})
        res = await center.rtba.apply()
        assert res["ok"] and "применено сразу" in res["message"]
        assert got["lis"] == {"changes": {"strategy.minRoi": 0.12}}
        assert got["mann"] == {"changes": {"min_margin_pct": 9.0}}
        assert not (tmp_path / "lisbot" / "config.json").exists()
        for srv in servers:
            await srv.close()
        center.events.close()
    asyncio.run(go())


def test_backtest_mannbot_rules_on_market_history(tmp_path):
    seed_rtba(tmp_path)
    center = Center(tmp_path)
    r = center.rtba.backtest_mannbot([{"label": "текущие", "changes": {}},
                                      {"label": "маржа 40%", "changes": {"min_margin_pct": 40}}], 30)
    assert r["snapshots"] == 48 and r["orders"] == 2
    cur, high = r["variants"]
    assert cur["avg_position"] == 1.0 and cur["updates"] > 0 and cur["est_revenue"] > 0
    # высокая маржа держит цену на полу выше рынка: дороже, но чаще не первые
    assert high["floor"] > cur["floor"] and high["avg_price"] > cur["avg_price"]
    assert high["share_first"] < cur["share_first"] and high["floor_hits"] > 0
    center.events.close()


@pytest.mark.skipif(not shutil.which("node"), reason="нужен Node.js")
def test_backtest_lisbot_through_node(tmp_path):
    seed_rtba(tmp_path)

    async def go():
        center = Center(tmp_path)
        r = await center.rtba.backtest_lisbot([{"label": "ROI 10%", "changes": {"strategy.minRoi": 0.1}},
                                               {"label": "ROI 60%", "changes": {"strategy.minRoi": 0.6}}], 7)
        assert "error" not in r, r
        soft, strict = r["variants"]
        assert r["samples"] == 9 and soft["buys"] >= 1 and strict["buys"] == 0
        center.events.close()
    asyncio.run(go())


def test_rtba_web_api(tmp_path):
    seed_rtba(tmp_path)

    async def go():
        center = Center(tmp_path)
        center.cfg.save({"analytics": {"usd_rub": 100}})
        token = center.cfg.ensure_token()
        async with TestClient(TestServer(make_app(center))) as c:
            assert (await c.get("/api/rtba")).status == 401
            await c.get(f"/login?token={token}", allow_redirects=False)
            s = await (await c.get("/api/rtba?days=7")).json()
            assert s["lisbot"]["latency"]["n"] == 2
            r = await c.post("/api/rtba/budget", json={"plan": {"mannbot.min_margin_pct": 500}})
            assert r.status == 400
            p = await (await c.post("/api/rtba/budget", json={"total_usd": 100})).json()
            assert p["total_usd"] == 100
            r = await (await c.post("/api/rtba/backtest", json={"bot": "mannbot", "variants": []})).json()
            assert r["variants"][0]["label"] == "текущие правила"
            r = await c.post("/api/rtba/apply", json={})
            assert (await r.json())["ok"]
        center.events.close()
    asyncio.run(go())
