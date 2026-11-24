import asyncio
import json
import sqlite3

from center.agents.trends import Point, Store, TrendsEngine, parse_gift_floor
from center.agents.trends_demo import demo_fetch

DAY = 86400.0


def test_parse_fragment_floor():
    page = ('<a href="/gift/lootbag-12" class="tm-grid-item"><div class="tm-grid-item-value">51.5</div>For sale</a>'
            '<a href="/gift/lootbag-13" class="tm-grid-item"><div class="tm-grid-item-value">1,200</div>Sold</a>'
            '<a href="/gift/lootbag-14" class="tm-grid-item"><div class="tm-grid-item-value">48</div>For sale</a>')
    assert parse_gift_floor(page) == (48.0, 2)


def test_changes_movers_and_recommendations(tmp_path):
    now = [1_800_000_000.0]
    eng = TrendsEngine(tmp_path, fetch=demo_fetch(lambda: now[0]), store=Store(":memory:"), clock=lambda: now[0])
    # неделю назад подарок стоил на 20% дешевле, звёзды на FunPay вчера — на 5% дороже
    eng.store.add(now[0] - 7 * DAY, [Point("gifts", "lootbag", 35.0)])
    eng.store.add(now[0] - DAY, [Point("stars", "100", 160.0)])
    sb = tmp_path / "starsbot" / "data"
    sb.mkdir(parents=True)
    con = sqlite3.connect(sb / "starsbot.sqlite3")
    con.execute("CREATE TABLE competitor_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, stars INTEGER, data TEXT)")
    con.execute("INSERT INTO competitor_snapshots(ts,stars,data) VALUES(?,?,?)",
                (now[0] - 60, 100, json.dumps([{"price": 150}, {"price": 151}, {"price": 152}, {"price": 160}])))
    con.commit()
    con.close()
    rep = asyncio.run(eng.run_once())
    assert rep["counts"]["skins"] == 4 and rep["counts"]["stars"] == 1 and rep["counts"]["gifts"] >= 8
    kinds = {(r["source"], r["key"]) for r in rep["recs"]}
    assert ("stars", "100") in kinds                      # 152 против 160 вчера: −5% за сутки
    lb = next(r for r in rep["rows"] if r["key"] == "lootbag")
    assert lb["d7"] is not None and lb["d7"] > 10
    assert ("gifts", "lootbag") in kinds
    assert eng.report()["ts"] == now[0]


def test_agent_registered_and_not_autostarted():
    from center.agents import REGISTRY
    import center.agents.trends  # noqa: F401
    assert "trends" in REGISTRY
    from center.config import DEFAULTS
    assert DEFAULTS["agents"]["trends"]["autostart"] is False
