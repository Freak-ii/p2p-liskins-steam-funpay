"""Орбита: ключи Mann сняты в Пульте центра — лот FunPay выключается на ходу, магазин ключей не получает."""
import asyncio
import json
import os
import time

from test_engine import make

from mannbot import supply_api
from mannbot.orbit_gate import OrbitGate, OrbitTracker


def write(path, version, closed):
    path.write_text(json.dumps({"version": version, "closed": {"mannbot": closed}}), encoding="utf-8")
    t = time.time() + version
    os.utime(path, (t, t))


def test_keys_closed_and_reopened(tmp_path):
    db, notifier, fp, steam, engine, analytics = make()
    orbit = tmp_path / "orbit.json"
    engine.orbit = OrbitGate("mannbot", orbit, ttl=0)
    engine.orbit_tracker = OrbitTracker(engine.orbit)

    async def go():
        await engine.stock_sync()
        assert fp.lot["active"] == "on"
        write(orbit, 1, ["keys", "supply:steam_keys"])
        engine.set_paused(True)                      # на паузе лот всё равно снимается
        await engine.orbit_tick()
        assert fp.lot["active"] == ""
        engine.set_paused(False)
        await engine.stock_sync()                    # сверка склада не возвращает снятый лот
        assert fp.lot["active"] == ""
        q = await supply_api.quote(engine, "running", "steam_keys", 1)
        assert not q["available"] and "Орбиты" in q["reason"]
        ack = json.loads((tmp_path / "orbit-ack" / "mannbot.json").read_text(encoding="utf-8"))
        assert ack["version"] == 1 and ack["applied"]["keys"] == "ok"
        write(orbit, 2, [])
        await engine.orbit_tick()
        assert fp.lot["active"] == "on"
    asyncio.run(go())
