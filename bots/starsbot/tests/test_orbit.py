"""Орбита: позиция, снятая в Центре, выключает лоты StarsBot на ходу и не возвращается сверкой остатков."""
import asyncio
import json
import time

from test_engine import boot, make

from starsbot import supply_api
from starsbot.orbit_gate import OrbitGate


def write(path, version, closed):
    path.write_text(json.dumps({"version": version, "closed": {"starsbot": closed}}), encoding="utf-8")
    t = time.time() + version            # разное время изменения, даже если запись в ту же секунду
    import os
    os.utime(path, (t, t))


def test_close_and_reopen_stars(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    orbit = tmp_path / "shared" / "orbit.json"
    orbit.parent.mkdir()
    e.orbit.gate = OrbitGate("starsbot", orbit, ttl=0)
    e.orbit.tracker.gate = e.orbit.gate
    e.cost_per_star_ton = lambda: 0.0115          # цена звезды известна — сверка остатков работает

    async def run():
        await boot(e)
        stars_lots = [str(l["offer_id"]) for l in e.lots if l.get("offer_id")]
        await e.wallet_watch()
        before = {o: fp._lot(o)["active"] for o in stars_lots}
        assert "on" in before.values()
        write(orbit, 1, ["stars", "supply:stars", "premium"])
        await e.orbit.tick()
        assert all(fp._lot(o)["active"] == "" for o in stars_lots)
        await e.wallet_watch()                   # сверка остатков не возвращает снятые лоты
        assert all(fp._lot(o)["active"] == "" for o in stars_lots)
        q = supply_api.quote(e, "stars", 100)
        assert not q["available"] and "Орбиты" in q["reason"]
        ack = json.loads((orbit.parent / "orbit-ack" / "starsbot.json").read_text(encoding="utf-8"))
        assert ack["version"] == 1 and ack["applied"]["stars"] == "ok"
        write(orbit, 2, [])
        await e.orbit.tick()
        await e.wallet_watch()                   # вернула сверка остатков — как было до закрытия
        assert {o: fp._lot(o)["active"] for o in stars_lots} == before
        ack = json.loads((orbit.parent / "orbit-ack" / "starsbot.json").read_text(encoding="utf-8"))
        assert ack["version"] == 2 and ack["applied"] == {}
    asyncio.run(run())


def test_no_file_means_everything_open(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    e.orbit.gate = OrbitGate("starsbot", tmp_path / "nope.json", ttl=0)
    assert not e.orbit_closed("stars")
    assert OrbitGate("starsbot", "").closed() == set()
