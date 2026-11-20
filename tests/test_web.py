import asyncio

from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.web import make_app


def test_auth_overview_and_secrets(tmp_path):
    async def go():
        center = Center(tmp_path)
        token = center.cfg.ensure_token()
        async with TestClient(TestServer(make_app(center))) as c:
            r = await c.get("/api/overview")
            assert r.status == 401
            r = await c.get("/", allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/login"
            assert (await c.get("/api/ping")).status == 200
            r = await c.get(f"/login?token={token}", allow_redirects=False)
            assert r.status == 302
            d = await (await c.get("/api/overview")).json()
            assert {b["name"] for b in d["bots"]} == {"lisbot", "mannbot", "starsbot"}
            r = await c.post("/api/lisbot/secrets", json={"LIS_API_KEY": "abc", "STEAM_PASSWORD": "pw"})
            assert (await r.json())["ok"]
            s = await (await c.get("/api/lisbot/secrets")).json()
            assert s["LIS_API_KEY"] == "abc" and s["STEAM_PASSWORD"] == "" and s["STEAM_PASSWORD_SET"]
            r = await c.post("/api/workers/lisbot/pause")
            assert r.status == 409                                # бот не запущен — команда не уходит
            r = await c.post("/api/workers/nope/start")
            assert r.status == 404
            r = await c.post("/api/workers/lisbot/start", headers={"Origin": "http://evil.example"})
            assert r.status == 403
        center.events.close()
    asyncio.run(go())


def test_autostart_toggle_and_defaults(tmp_path):
    async def go():
        center = Center(tmp_path)
        token = center.cfg.ensure_token()
        async with TestClient(TestServer(make_app(center))) as c:
            await c.get(f"/login?token={token}", allow_redirects=False)
            d = await (await c.get("/api/overview")).json()
            auto = {b["name"]: b["autostart"] for b in d["bots"]}
            assert auto == {"starsbot": True, "mannbot": False, "lisbot": False}
            r = await c.post("/api/workers/mannbot/autostart_on")
            assert (await r.json())["ok"]
            r = await c.post("/api/workers/starsbot/autostart_off")
            d = await (await c.get("/api/overview")).json()
            auto = {b["name"]: b["autostart"] for b in d["bots"]}
            assert auto == {"starsbot": False, "mannbot": True, "lisbot": False}
        assert Center(tmp_path).cfg.worker("mannbot")["autostart"] is True       # сохранено в center.yaml
        center.events.close()
    asyncio.run(go())
