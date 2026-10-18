import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.web import make_app

BUYER = "76561198000000001"


def test_registry_matches_only_exact_partner_and_assets(tmp_path):
    c = Center(tmp_path)
    s = c.steamshare
    iid = s.intent("mannbot", "ORD1", BUYER, 440, ["11", "12"])
    give = [{"appid": 440, "assetid": "12"}, {"appid": 440, "assetid": "11"}]
    # обмен уже создан, номер ещё не записан: узнаём по получателю и ключам, номер привязывается
    assert s.check("5001", BUYER, give, 0) == {"own": True, "bot": "mannbot", "ref": "ORD1"}
    assert s.check("5001", BUYER, give, 0)["own"]
    assert not s.bind("mannbot", iid, "5002")                        # номер уже другой — не переписываем
    # те же ключи в другом обмене, другой получатель, лишний ключ, просьба отдать нам — чужое
    assert s.check("5003", BUYER, give, 0)["reason"] == "not_registered"
    assert not s.check("5001", "76561198000000002", give, 0)["own"]
    assert not s.check("5001", BUYER, give + [{"appid": 440, "assetid": "13"}], 0)["own"]
    assert not s.check("5001", BUYER, give[:1], 0)["own"]
    assert s.check("5001", BUYER, give, 1)["reason"] == "items"
    assert not s.check("5001", BUYER, [{"appid": 730, "assetid": "11"}, {"appid": 730, "assetid": "12"}], 0)["own"]
    assert not s.check("5001", BUYER, [], 0)["own"]
    c.events.close()


def test_intent_with_bound_offer_and_expiry(tmp_path):
    c = Center(tmp_path)
    s = c.steamshare
    iid = s.intent("mannbot", "ORD2", BUYER, 440, ["21"])
    assert s.bind("mannbot", iid, "6001")
    assert not s.check("6002", BUYER, [{"appid": 440, "assetid": "21"}], 0)["own"]
    assert s.check("6001", BUYER, [{"appid": 440, "assetid": "21"}], 0)["own"]
    old = s.intent("mannbot", "ORD3", BUYER, 440, ["31"])
    s.db.execute("UPDATE outgoing SET ts=? WHERE id=?", (time.time() - 3600, old))
    assert not s.check("6003", BUYER, [{"appid": 440, "assetid": "31"}], 0)["own"]   # намерение протухло
    for bad in ([], ["x"], ["1", "1"]):
        try:
            s.intent("mannbot", "ORD", BUYER, 440, bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    try:
        s.intent("mannbot", "ORD", "123", 440, ["1"])
        raise AssertionError("partner")
    except ValueError:
        pass
    c.events.close()


def test_http_api_uses_own_bot_token_and_env(tmp_path):
    async def go():
        center = Center(tmp_path)
        env = center.workers["mannbot"].extra_env()
        tok = env["CENTER_STEAM_TOKEN"]
        assert env["CENTER_STEAM_SHARED"] == "1" and env["CENTER_STEAM_URL"].endswith(":8800/api/steam-share")
        assert center.workers["lisbot"].extra_env()["CENTER_STEAM_TOKEN"] == tok
        h = {"Authorization": f"Bearer {tok}", "X-Bot": "mannbot"}
        async with TestClient(TestServer(make_app(center))) as c:
            body = {"ref": "ORD9", "partner": BUYER, "appid": 440, "assets": ["91"]}
            assert (await c.post("/api/steam-share/intent", json=body)).status == 401
            panel = {"Authorization": f"Bearer {center.cfg.ensure_token()}", "X-Bot": "mannbot"}
            assert (await c.post("/api/steam-share/intent", json=body, headers=panel)).status == 401
            assert (await c.post("/api/steam-share/intent", json=body, headers={**h, "X-Bot": "x"})).status == 400
            r = await (await c.post("/api/steam-share/intent", json=body, headers=h)).json()
            assert r["ok"] and r["shared_account"]
            assert (await (await c.post("/api/steam-share/bind", json={"id": r["id"], "offer_id": "777"}, headers=h)).json())["ok"]
            chk = {"offer_id": "777", "partner": BUYER, "give": [{"appid": 440, "assetid": "91"}], "receive_count": 0}
            d = await (await c.post("/api/steam-share/check", json=chk, headers={**h, "X-Bot": "lisbot"})).json()
            assert d["own"] and d["ref"] == "ORD9"
            bad = await c.post("/api/steam-share/intent", json={**body, "assets": []}, headers=h)
            assert bad.status == 400
            assert (await c.get("/api/steam-status")).status == 401          # статус — только для панели
        center.events.close()
        center.steamshare.close()
    asyncio.run(go())


def test_two_accounts_mode(tmp_path):
    (tmp_path / "center.yaml").write_text("steam:\n  shared_account: false\n", encoding="utf-8")
    c = Center(tmp_path)
    assert c.workers["mannbot"].extra_env()["CENTER_STEAM_SHARED"] == "0"
    assert not c.cfg.problems
    c.events.close()
