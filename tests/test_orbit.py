"""Орбита: реестр позиций центра, решение для ботов (shared/orbit.json), круги и пересчёт при перетасовке."""
import asyncio
import json
from pathlib import Path

import yaml
from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.loops import Edge, circle_id, evaluate, find_circles
from center.web import make_app

ROOT = Path(__file__).resolve().parent.parent


def test_gate_copies_are_identical():
    src = (ROOT / "center" / "orbit_gate.py").read_text(encoding="utf-8")
    for bot in ("starsbot/starsbot", "mannbot/mannbot", "shopbot/shopbot"):
        assert (ROOT / "bots" / bot / "orbit_gate.py").read_text(encoding="utf-8") == src, bot


def test_circles_and_ids():
    edges = [Edge("fp_rub>card_rub", "fp_rub", "card_rub", "transfer", -3, "вывод"),
             Edge("card_rub>usdt", "card_rub", "usdt", "transfer", 0, "p2p"),
             Edge("usdt>ton", "usdt", "ton", "transfer", -0.3, "bybit"),
             Edge("stars@funpay", "ton", "fp_rub", "sale", 2.4, "звёзды", position="stars", channel="funpay"),
             Edge("lis", "usdt", "lis_usd", "transfer", 0, "lis"),
             Edge("skins_steam@steam", "lis_usd", "steam", "sale", 3, "скины", position="skins_steam", channel="steam")]
    cs = find_circles(edges)
    assert [c.id for c in cs] == ["fp_rub>card_rub>usdt>ton[stars@funpay]>fp_rub"]
    assert abs(cs[0].pct - ((0.97 * 0.997 * 1.024) - 1) * 100) < 1e-9
    ev = evaluate(edges)
    assert ev["no_circle"] == ["skins_steam@steam"]          # Steam без выхода — круга нет
    assert ev["stranded"][0]["node"] in ("lis_usd", "steam")
    assert ev["losing"] == ["stars@funpay"]                   # −0,97% < 0
    cid, nodes, _ = circle_id([edges[3], edges[0], edges[1], edges[2]])
    assert cid.startswith("fp_rub>") and nodes[0] == nodes[-1] == "fp_rub"


def test_close_position_everywhere_and_parking(tmp_path):
    c = Center(tmp_path)
    o = c.orbit
    d = o.publish()
    assert d["closed"] == {} and json.loads(o.path.read_text(encoding="utf-8"))["version"] == 1
    assert o.publish()["version"] == 1                        # ничего не менялось — версия та же
    o.set_position("mann_keys", False, reason="минус")
    data = json.loads(o.path.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert set(data["closed"]["mannbot"]) == {"keys", "supply:steam_keys"}
    assert set(data["closed"]["shopbot"]) == {"buy.steam_keys", "post.steam_keys"}
    assert data["closed"]["lisbot"] == ["buy.steam"]          # без ключей Steam-кошельку некуда отдавать деньги
    pos = {p["id"]: p for p in o.view()["positions"]}
    assert pos["mann_keys"]["state"] == "closed" and pos["skins_steam"]["state"] == "parked"
    # закрыть один канал: звёзды только на GGSEL
    o.set_position("stars", False, channel="ggsel")
    data = json.loads(o.path.read_text(encoding="utf-8"))
    assert "ggsel:stars" in data["closed"]["starsbot"] and "stars" not in data["closed"]["starsbot"]
    # поставщик подарков нужен и другим ячейкам — закрытие одной позиции его не трогает
    o.set_position("stars", True)
    o.set_position("mann_keys", True)
    assert json.loads(o.path.read_text(encoding="utf-8"))["closed"] == {}
    # переводы и автопарковка выключаются
    c.cfg.save({"orbit": {"auto_park": False}})
    o.set_position("mann_keys", False)
    assert "lisbot" not in json.loads(o.path.read_text(encoding="utf-8"))["closed"]
    assert o.bot_env("starsbot") == {"CENTER_ORBIT": str(o.path), "CENTER_ORBIT_BOT": "starsbot"}
    assert yaml.safe_load((tmp_path / "center.yaml").read_text(encoding="utf-8"))["orbit"]["auto_park"] is False
    assert not c.cfg.problems


def test_channel_agent_respects_orbit(tmp_path):
    c = Center(tmp_path)
    ch = c.agents.get("channel")
    assert not any("Орбиты" in i for i in ch.check("Telegram Premium со скидкой"))
    c.orbit.set_position("premium", False)
    assert any("Орбиты" in i for i in ch.check("Telegram Premium со скидкой"))
    assert not any("Орбиты" in i for i in ch.check("Звёзды за пару минут"))


def test_remote_command_and_api(tmp_path):
    c = Center(tmp_path)

    async def go():
        assert "снял" in (await c.remote_command("orbit close premium дорого")).lower()
        assert "premium: closed" in await c.remote_command("orbit status")
        token = c.cfg.ensure_token()
        async with TestClient(TestServer(make_app(c))) as cl:
            h = {"Authorization": f"Bearer {token}"}
            r = await cl.get("/api/orbit", headers=h)
            v = await r.json()
            assert v["name"] == "Орбита" and v["circles_total"] > 0
            r = await cl.post("/api/orbit/position", json={"id": "premium", "open": True}, headers=h)
            assert (await r.json())["ok"]
            r = await cl.post("/api/orbit/settings", json={"margins": {"mann_keys@funpay": -30},
                                                         "transfers": {"fp_rub>usdt": {"enabled": True}}}, headers=h)
            v = (await r.json())["view"]
            cell = next(ch for p in v["positions"] if p["id"] == "mann_keys" for ch in p["channels"]
                        if ch["channel"] == "funpay")
            assert cell["margin"] == -30 and cell["margin_source"] == "настройка"
            assert next(t for t in v["transfers"] if t["id"] == "fp_rub>usdt")["enabled"]
            r = await cl.post("/api/orbit/position", json={"id": "nope", "open": False}, headers=h)
            assert r.status == 400
            assert (await cl.get("/orbit", headers=h)).status == 200
    asyncio.run(go())
