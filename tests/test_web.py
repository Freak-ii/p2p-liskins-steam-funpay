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
            assert {b["name"] for b in d["bots"]} == {"lisbot", "mannbot", "starsbot", "shopbot", "tggift"}
            r = await c.post("/api/lisbot/secrets", json={"LIS_API_KEY": "abc", "STEAM_PASSWORD": "pw"})
            assert (await r.json())["ok"]
            s = await (await c.get("/api/lisbot/secrets")).json()
            assert s["LIS_API_KEY"] == "abc" and s["STEAM_PASSWORD"] == "" and s["STEAM_PASSWORD_SET"]
            r = await c.post("/api/workers/lisbot/pause")
            assert r.status == 409                                # бот не запущен — команда не уходит
            r = await c.get("/api/mannbot/settings")
            assert r.status == 409                                # настройки MannBot идут через запущенный процесс
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
            # по умолчанию центр не запускает ни одного бота сам
            assert not any(auto.values()) and not any(b["autostart_allowed"] for b in d["bots"])
            r = await c.post("/api/workers/mannbot/autostart_on")
            assert (await r.json())["ok"]
            d = await (await c.get("/api/overview")).json()
            assert not any(b["autostart"] for b in d["bots"])                    # без center.bots_autostart не действует
        assert Center(tmp_path).cfg.worker("mannbot")["autostart"] is True       # сохранено в center.yaml
        c2 = Center(tmp_path)
        c2.cfg.save({"center": {"bots_autostart": True}})
        assert c2.cfg.autostart("mannbot") and not c2.cfg.autostart("starsbot")
        c2.events.close()
        center.events.close()
    asyncio.run(go())


def test_center_start_never_starts_bots_by_itself(tmp_path):
    """Старый center.yaml с autostart: true у бота (как на ПК) не запускает ботов: только кнопка «Запустить»."""
    async def go():
        center = Center(tmp_path)
        center.cfg.save({"workers": {n: {"autostart": True} for n in center.workers}})
        started = []
        for w in center.workers.values():
            w.check_deps = lambda: None
            async def fake_start(reason="", _n=w.name):
                started.append(_n)
            w.start = fake_start
        for svc in (center.monitor, center.coordinator, center.backup, center.inventory, center.tg, center.updates):
            async def noop(*a, **k):
                return None
            svc.start = noop
        await center.start()
        assert started == []
        assert all("кнопкой" in w.detail for w in center.workers.values() if w.state == "stopped")
        center._poll_task.cancel()
        center.events.close()
    asyncio.run(go())
