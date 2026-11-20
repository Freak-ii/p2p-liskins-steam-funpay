import asyncio
import json

import aiohttp
import yaml
from aiohttp.test_utils import TestClient, TestServer

from mannbot.app.launcher import DEMO_OVERRIDES
from mannbot.app.supervisor import MemoryLogHandler, Supervisor, parse_mafile
from mannbot.core import messages as M
from mannbot.demo import run_scenario
from mannbot.web.server import make_app


async def _client(sup, mem=None):
    client = TestClient(TestServer(make_app(sup, mem)))
    await client.start_server()
    r = await client.get(f"/login?token={sup.ensure_token()}", allow_redirects=False)
    assert r.status == 302
    return client


def test_demo_app_pages_and_controls(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=DEMO_OVERRIDES)

    async def go():
        sup.engine.cfg.deal.message_rate_limit_seconds = 0
        res = await run_scenario(sup.engine, sup.engine.fp, sup.engine.steam, verbose=False)
        anon = TestClient(TestServer(make_app(sup)))
        await anon.start_server()
        assert (await anon.get("/", allow_redirects=False)).status == 302     # без входа
        assert (await anon.get("/api/ping")).status == 200
        await anon.close()
        client = await _client(sup, MemoryLogHandler())
        try:
            for path in ["/", "/?days=30", "/orders", "/orders?status=delivered", f"/orders/{res['orders'][0]}",
                         "/customers", "/pricing", "/alerts", "/settings", "/setup", "/messages", "/messages?lang=en",
                         "/logs", "/api/logs", "/api/status"]:
                r = await client.get(path)
                assert r.status == 200, path
            r = await client.post(f"/orders/{res['orders'][4]}", data={"action": "note", "text": "VIP"})
            assert r.status == 200 and sup.db.get_order(res["orders"][4])["notes"] == "VIP"
            await client.post("/control", data={"action": "start"})
            await asyncio.sleep(0.5)
            assert sup.state == "running"
            await client.post("/control", data={"action": "stop"})
            assert sup.state == "stopped"
        finally:
            await client.close()
    asyncio.run(go())


def test_setup_settings_messages_flow(tmp_path):
    sup = Supervisor(tmp_path)
    assert not sup.configured

    async def go():
        client = await _client(sup)
        try:
            r = await client.get("/", allow_redirects=False)
            assert r.headers["Location"] == "/setup"                          # первый запуск → мастер
            mafile = json.dumps({"shared_secret": "c2VjcmV0", "identity_secret": "aWRlbnRpdHk=",
                                 "account_name": "mybot", "Session": {"SteamID": 76561198000000001}})
            fd = aiohttp.FormData()
            fd.add_field("file", mafile.encode(), filename="x.maFile")
            res = await (await client.post("/setup/mafile", data=fd)).json()
            assert res["ok"] and res["fields"]["username"] == "mybot"
            await client.post("/setup", data={"golden_key": "gk123", "lot_offer_0": "555", "lot_node_0": "1808",
                                              "lot_game_0": "435", "lot_kpu_0": "1", "username": "mybot",
                                              "password": "pw", "steam_id": "76561198000000001",
                                              "dry_run": "on", "language": "ru", "cost_per_key": "150"})
            assert sup.configured, sup.problems
            raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
            assert raw["funpay"]["golden_key"] == "gk123" and raw["steam"]["shared_secret"] == "c2VjcmV0"
            assert raw["funpay"]["lots"][0]["offer_id"] == 555
            # пустое секретное поле в настройках не стирает значение
            form = {"funpay__golden_key": "", "rtba__cost_per_key": "160", "risk__blacklist_steam_ids": "1\n2",
                    "general__dry_run": "on", "deal__trade_hold_policy": "manual"}
            await client.post("/settings", data=form)
            assert sup.cfg.funpay.golden_key == "gk123" and sup.cfg.rtba.cost_per_key == 160
            assert sup.cfg.risk.blacklist_steam_ids == ["1", "2"] and sup.cfg.deal.trade_hold_policy == "manual"
            # редактор сообщений: валидный текст сохраняется, битый — нет
            await client.post("/messages", data={"lang": "ru", "success": "Готово #{order_id}, спасибо, {buyer}!",
                                                 "reminder": "Пришлите ссылку {oops}"})
            assert M.render("ru", "success", order_id="X1", buyer="B") == "Готово #X1, спасибо, B!"
            assert "reminder" not in M.OVERRIDES.get("ru", {})
            prev = await (await client.post("/api/preview", json={"text": "#{order_id}"})).json()
            assert prev["ok"] and prev["text"] == "#ABCD1234"
        finally:
            await client.close()
            M.set_overrides({})
    asyncio.run(go())


def test_parse_mafile_errors():
    try:
        parse_mafile('{"shared_secret": ""}')
        raise AssertionError
    except ValueError:
        pass


def test_rtba_json_api_for_center(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=DEMO_OVERRIDES)

    async def go():
        sup.engine.cfg.deal.message_rate_limit_seconds = 0
        await run_scenario(sup.engine, sup.engine.fp, sup.engine.steam, verbose=False)
        sup.db.exec("INSERT INTO price_history(ts,offer_id,old_price,new_price,best_competitor,competitors,position,"
                    "reason,applied) VALUES(strftime('%s','now'),'1',150,149.5,150,3,1,'лидер рынка 150.00',1)")
        client = await _client(sup)
        try:
            d = await (await client.get("/api/rtba")).json()
            assert d["history"][0]["new_price"] == 149.5 and d["day"]["applied"] >= 1
            assert "min_margin_pct" in d["config"] and d["floor"] >= 0
            bad = await client.post("/api/rtba/config", json={"changes": {"dry_run": False}})
            assert bad.status == 400                                   # боевой режим через RTBA не включить
            r = await client.post("/api/rtba/config", json={"changes": {"min_margin_pct": 12, "strategy": "median"}})
            assert (await r.json())["ok"]
            assert sup.cfg.rtba.min_margin_pct == 12 and sup.engine.rtba.cfg.strategy == "median"
            saved = yaml.safe_load(sup.config_path.read_text(encoding="utf-8"))
            assert saved["rtba"]["min_margin_pct"] == 12
            # работающему боту правила применяются сразу
            await client.post("/control", data={"action": "start"})
            await asyncio.sleep(0.5)
            r = await client.post("/api/rtba/config", json={"changes": {"min_margin_pct": 7.5}})
            assert (await r.json())["live"] and sup.engine.rtba.cfg.min_margin_pct == 7.5 and not sup.restart_required
            await client.post("/control", data={"action": "stop"})
        finally:
            await client.close()
    asyncio.run(go())


def test_api_settings_json(tmp_path):
    sup = Supervisor(tmp_path)

    async def go():
        client = await _client(sup)
        try:
            d = await (await client.get("/api/settings")).json()
            fp = next(s for s in d["sections"] if s["id"] == "funpay")
            gk = next(f for f in fp["fields"] if f["key"] == "golden_key")
            assert gk == {"key": "golden_key", "label": gk["label"], "type": "secret", "help": gk["help"], "set": False}
            r = await client.post("/api/settings", json={
                "values": {"funpay": {"golden_key": "GK", "poll_interval": "9"}, "rtba": {"strategy": "median"},
                           "risk": {"blacklist_funpay_users": "a\nb"}},
                "lots": [{"offer_id": "777", "keys_per_unit": 2}, {"offer_id": ""}]})
            assert (await r.json())["ok"]
            assert sup.cfg.funpay.golden_key == "GK" and sup.cfg.funpay.poll_interval == 9
            assert sup.cfg.rtba.strategy == "median" and sup.cfg.risk.blacklist_funpay_users == ["a", "b"]
            assert sup.cfg.funpay.lots == [{"offer_id": 777, "node_id": 1808, "game_id": 435, "keys_per_unit": 2}]
            await client.post("/api/settings", json={"values": {"funpay": {"golden_key": ""}}})
            assert sup.cfg.funpay.golden_key == "GK"                       # пустой секрет не затирает
            r = await client.post("/api/settings", json={"values": {"rtba": {"strategy": "bogus"}}})
            assert r.status == 400
            d = await (await client.get("/api/settings")).json()
            assert "GK" not in json.dumps(d)                                # секреты наружу не отдаются
        finally:
            await client.close()
    asyncio.run(go())


def test_crm_json_api(tmp_path):
    """JSON-эндпоинты для CRM центра: заметка заказа, заметки/теги/чёрный список покупателя."""
    sup = Supervisor(tmp_path, demo=True, overrides=DEMO_OVERRIDES)

    async def go():
        sup.engine.cfg.deal.message_rate_limit_seconds = 0
        res = await run_scenario(sup.engine, sup.engine.fp, sup.engine.steam, verbose=False)
        oid = res["orders"][0]
        cid = sup.db.get_order(oid)["buyer_id"]
        anon = TestClient(TestServer(make_app(sup)))
        await anon.start_server()
        assert (await anon.post(f"/api/orders/{oid}", json={"notes": "x"}, allow_redirects=False)).status == 302
        await anon.close()
        client = await _client(sup)
        try:
            r = await client.post(f"/api/orders/{oid}", json={"notes": "из центра"})
            assert (await r.json())["ok"] and sup.db.get_order(oid)["notes"] == "из центра"
            assert (await client.post("/api/orders/nope", json={"notes": "x"})).status == 404
            assert (await client.post(f"/api/orders/{oid}", json={})).status == 400
            r = await client.post(f"/api/customers/{cid}", json={"notes": "VIP", "tags": "опт", "blacklisted": True})
            assert (await r.json())["ok"]
            c = sup.db.customer(cid)
            assert c["notes"] == "VIP" and c["tags"] == "опт" and c["blacklisted"] == 1
            await client.post(f"/api/customers/{cid}", json={"blacklisted": False})
            c = sup.db.customer(cid)
            assert c["blacklisted"] == 0 and c["notes"] == "VIP"
            assert (await client.post("/api/customers/424242", json={"notes": "x"})).status == 404
        finally:
            await client.close()
    asyncio.run(go())


def test_api_alerts_ack(tmp_path):
    sup = Supervisor(tmp_path)
    a1, a2 = sup.db.add_alert("CRITICAL", "x", "one"), sup.db.add_alert("WARN", "y", "two")

    async def go():
        client = await _client(sup)
        try:
            r = await client.post("/api/alerts/ack", json={"id": a1})
            assert r.status == 200 and (await r.json())["ok"]
            acked = {r["id"]: r["acknowledged"] for r in sup.db.q("SELECT id, acknowledged FROM alerts")}
            assert acked[a1] == 1 and acked[a2] == 0
            await client.post("/api/alerts/ack", json={"all": True})
            assert sup.db.scalar("SELECT COUNT(*) FROM alerts WHERE acknowledged=0") == 0
        finally:
            await client.close()
    asyncio.run(go())
