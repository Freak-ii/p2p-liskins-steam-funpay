"""Стыки модулей центра: демо-режим, отказы ботов, порты, курс без сети."""
import asyncio
import os
import sqlite3
import shutil
import subprocess
import sys
import threading
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from center.app import Center
from center.bots import BotWorker, LisBotWorker, MannBotWorker
from center.demo import seed_lisbot
from center.events import EventStore
from center.workers import CRASHED


def test_demo_center_shows_lisbot_history(tmp_path):
    async def go():
        c = Center(tmp_path, demo=True)
        db = c.analytics.lis_db
        assert db.exists()
        # схема — та же, что создаёт сам LisBot (src/db.js)
        cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(items)")}
        assert {"lis_item_id", "list_price_cents", "tags"} <= cols
        p = await c.analytics.pnl(30)
        assert p["bots"]["lisbot"]["deals"] > 5 and p["rate"]["source"] == "LisBot (Steam)"
        deals = c.crm.deals(q="", bot="lisbot", group="", days=None, dry=False, sort="created_at", desc=True,
                            limit=100, offset=0)
        assert deals["groups"]["open"] == 3 and deals["groups"]["done"] > 5
        assert c.coordinator.detect() == []            # демо-история не вызывает аномалий и автопаузы
        assert c.workers["lisbot"].alerts(5)
        c.events.close()
    asyncio.run(go())


def test_demo_seed_refreshes_only_demo_db(tmp_path):
    d = tmp_path / "data"
    assert seed_lisbot(d, now=time.time() - 30 * 86400)
    assert seed_lisbot(d)                               # старая демо-база пересоздаётся со свежими датами
    real = tmp_path / "real"
    real.mkdir()
    sqlite3.connect(real / "bot.sqlite").execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)").connection.commit()
    assert not seed_lisbot(real)                        # база настоящего LisBot не трогается


def test_demo_mannbot_costs_from_demo_overrides(tmp_path):
    c = Center(tmp_path, demo=True)
    assert c.analytics.mann_costs()[0] == 150          # себестоимость из DEMO_OVERRIDES лаунчера MannBot
    assert c.analytics.mann_config() == {}             # в config.yaml при этом ничего не дописано
    c.events.close()
    c2 = Center(tmp_path / "live")
    assert c2.analytics.mann_costs() == (0.0, 0.0)
    c2.events.close()


async def _bot_server(make, path, method="POST"):
    async def handler(request):
        resp = make(request)
        if isinstance(resp, web.HTTPException):
            raise resp
        return resp
    app = web.Application()
    app.router.add_route(method, path, handler)
    srv = TestServer(app, host="127.0.0.1")
    await srv.start_server()
    return srv


def test_bot_refusal_is_an_error_not_success(tmp_path):
    """Отказ бота должен дойти до координатора и панели как ошибка, а не как «готово»."""
    async def go():
        ev = EventStore(tmp_path / "e.sqlite3")
        lis = LisBotWorker(tmp_path / "lisbot", ev, port=0)
        lis.token = lambda: "t"
        srv = await _bot_server(lambda r: web.json_response({"error": "неизвестное действие"}, status=400), "/api/control")
        lis.port = srv.port
        with pytest.raises(RuntimeError, match="LisBot отказал"):
            await lis.control("buyPause")
        await srv.close()

        mann = MannBotWorker(tmp_path / "mannbot", ev, port=0)
        srv = await _bot_server(lambda r: web.HTTPFound("/login"), "/control")   # MannBot без входа
        mann.port = srv.port
        with pytest.raises(RuntimeError, match="нужен вход"):
            await mann.control("pause")
        await srv.close()
        srv = await _bot_server(lambda r: web.HTTPFound("/?msg=%E2%8F%B8+ok"), "/control")
        mann.port = srv.port
        assert await mann.control("pause") == "⏸ ok"
        await srv.close()
        ev.close()
    asyncio.run(go())


def test_fallback_port_skips_other_bots(tmp_path):
    import socket
    ev = EventStore(tmp_path / "e.sqlite3")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        p = busy.getsockname()[1]
        w = MannBotWorker(tmp_path / "mannbot", ev, port=p)
        BotWorker.reserved_ports.add(p + 1)              # следующий порт принадлежит другому боту центра
        try:
            w.prepare()
            assert w.port not in (p, p + 1)
        finally:
            BotWorker.reserved_ports.discard(p + 1)
    ev.close()


def test_port_conflicts_stop_restart_loop(tmp_path):
    ev = EventStore(tmp_path / "e.sqlite3")
    mann = MannBotWorker(tmp_path / "mannbot", ev, port=8801)
    assert mann.classify_exit(1, "Порт 8801 занят другой программой")[0] == CRASHED
    assert mann.classify_exit(0, "MannBot уже запущен на порту 8801")[0] == CRASHED
    lis = LisBotWorker(tmp_path / "lisbot", ev, port=8802)
    assert lis.classify_exit(1, "Error: listen EADDRINUSE: address already in use")[0] == CRASHED
    ev.close()


def test_rate_without_network_is_not_retried_every_request(tmp_path, monkeypatch):
    import center.analytics as an
    calls = []

    class Boom:
        def __init__(self, *a, **kw):
            calls.append(1)
            raise OSError("нет сети")
    monkeypatch.setattr(an.aiohttp, "ClientSession", Boom)

    async def go():
        c = Center(tmp_path)
        r1 = await c.analytics.usd_rub()
        r2 = await c.analytics.usd_rub()
        assert r1["source"].startswith("запасной") and r2 == r1 and len(calls) == 1
        c.events.close()
    asyncio.run(go())


def test_lisbot_identifies_only_its_own_instance(tmp_path):
    """Подхватывается только LisBot с нашим токеном и нашей папкой данных, а не любой бот на порту."""
    async def go():
        ev = EventStore(tmp_path / "e.sqlite3")
        lis = LisBotWorker(tmp_path / "lisbot", ev, port=0)
        lis.token = lambda: "t"
        meta = {"dataDir": str(lis.data_dir), "pid": 4242}
        srv = await _bot_server(lambda r: web.json_response(meta) if r.headers.get("Authorization") == "Bearer t"
                                else web.json_response({"error": "unauthorized"}, status=401), "/api/meta", "GET")
        url = f"http://127.0.0.1:{srv.port}"
        assert await lis.identify(url) == 4242
        meta["dataDir"] = str(tmp_path / "other")               # другой экземпляр LisBot со своей базой
        assert await lis.identify(url) is None
        lis.token = lambda: "чужой"
        assert await lis.identify(url) is None
        await srv.close()
        ev.close()
    asyncio.run(go())


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows бот завершается вместе с центром (Job Object)")
def test_center_adopts_mannbot_left_after_crash(tmp_path):
    """Настоящий MannBot (демо) работает без центра — новый центр подхватывает его, а не запускает второй."""
    async def go():
        ev = EventStore(tmp_path / "e.sqlite3")
        port = _free_port()
        w = MannBotWorker(tmp_path / "mannbot", ev, port=port, demo=True)
        w.adopt_poll_seconds = 0.2
        orphan = subprocess.Popen(w.command(), cwd=str(w.cwd()), env={**os.environ, **w.env()},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=orphan.wait, daemon=True).start()
        try:
            for _ in range(150):
                if w.token() and await w.find_running() is not None:
                    break
                await asyncio.sleep(0.1)
            await w.start("автозапуск центра")
            for _ in range(100):
                if w.state == "running":
                    break
                await asyncio.sleep(0.1)
            assert w.adopted and w.pid == orphan.pid and w.port == port
            assert (await w.summary())["demo"]
            await w.stop()
            for _ in range(100):
                if orphan.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            assert orphan.poll() is not None and w.soft_stopped    # мягкая остановка через API MannBot
        finally:
            if orphan.poll() is None:
                orphan.kill()
            ev.close()
    asyncio.run(go())


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_LISBOT_READY = (LisBotWorker.src / "node_modules" / "better-sqlite3").is_dir() and bool(shutil.which("node"))


@pytest.mark.skipif(not _LISBOT_READY, reason="нет Node.js или node_modules LisBot (npm ci в bots/lisbot)")
def test_demo_lisbot_trades_on_emulator(tmp_path):
    """Демо центра: LisBot запускается без ключей на эмуляции LIS-SKINS и Steam, отвечает и сам покупает."""
    async def go():
        c = Center(tmp_path, demo=True)
        w = c.workers["lisbot"]
        w.port = _free_port()
        assert "--demo" in w.command()
        await w.start("тест")
        try:
            for _ in range(300):
                if w.state == "running":
                    break
                await asyncio.sleep(0.1)
            assert w.state == "running", (w.state, w.detail, [ln["text"] for ln in w.logs()][-20:])
            s = await w.summary()
            assert s["demo"] and s["dry_run"] is False and s["bot_state"] == "running"
            async with w._session() as sess:
                async with sess.get(f"{w.base_url}/api/meta") as r:
                    meta = await r.json()
            assert meta["demo"] and meta["pid"] == w.pid
        finally:
            await w.stop("тест")
            c.events.close()
        assert w.soft_stopped
    asyncio.run(go())
