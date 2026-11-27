"""Общий секрет поставщиков и схема «кто кого снабжает» на главной Центра."""
import asyncio
import json
import sys
from pathlib import Path

import yaml
from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.supplylinks import ENV_TOKEN, ENV_URLS, VAULT_KEY
from center.web import make_app

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "bots" / "starsbot") not in sys.path:
    sys.path.insert(0, str(ROOT / "bots" / "starsbot"))


def write_cfg(c, bot, data):
    path = c.workers[bot]._cfg_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_secret_is_adopted_from_shop_then_kept_in_vault(tmp_path):
    c = Center(tmp_path)
    write_cfg(c, "shopbot", {"suppliers": {"stars": {"token": "legacy-token-1234567890"}}})
    assert c.supply.token == "legacy-token-1234567890" and c.supply.token_source == "adopted"
    assert c.vault.get(VAULT_KEY) == "legacy-token-1234567890"
    c2 = Center(tmp_path)
    assert c2.supply.token == "legacy-token-1234567890" and c2.supply.token_source == "vault"
    msg = c2.supply.regenerate()
    assert "Перезапустите" in msg and c2.supply.token != "legacy-token-1234567890" and len(c2.supply.token) >= 16


def test_new_secret_and_bot_env(tmp_path):
    c = Center(tmp_path)
    tok = c.supply.token
    assert len(tok) >= 16 and c.supply.token_source == "new"
    env = c.workers["shopbot"].extra_env()
    assert env[ENV_TOKEN] == tok
    urls = json.loads(env[ENV_URLS])
    assert urls["stars"] == c.workers["starsbot"].base_url and urls["gifts"] == c.workers["tggift"].base_url
    assert urls["steam_keys"] == c.workers["mannbot"].base_url
    for bot in ("starsbot", "tggift", "mannbot"):
        assert c.workers[bot].extra_env()[ENV_TOKEN] == tok and ENV_URLS not in c.workers[bot].extra_env()
    assert ENV_TOKEN not in c.workers["lisbot"].extra_env()


def test_graph_shows_only_enabled_channels(tmp_path):
    c = Center(tmp_path)
    write_cfg(c, "starsbot", {"funpay": {"enabled": False}, "ggsel": {"enabled": True}})
    write_cfg(c, "mannbot", {"funpay": {"enabled": False}})
    write_cfg(c, "shopbot", {"suppliers": {"steam_keys": {"kind": "manual"}}})
    g = asyncio.run(c.supply.graph())
    sells = {(l["from"], l["to"]) for l in g["links"] if l["kind"] == "sell"}
    assert ("starsbot", "ggsel") in sells and ("starsbot", "funpay") not in sells and ("mannbot", "funpay") not in sells
    assert ("tggift", "funpay") in sells and ("starsbot", "ozon") not in sells
    assert any("StarsBot" in u for u in g["unused"]) and any("MannBot" in u for u in g["unused"])
    sup = {l["from"]: l for l in g["links"] if l["kind"] == "supply" and l["to"] == "shopbot"}
    assert sup["starsbot"]["what"] == "⭐ звёзды, 💎 Premium" and sup["starsbot"]["status"] == "idle"
    assert sup["manual"]["status"] == "manual" and "mannbot" not in sup         # ключи — вручную
    assert all(l["status"] in ("idle", "manual") for l in g["links"])           # никто не запущен


def test_graph_pings_supplier_with_shared_secret(tmp_path, monkeypatch):
    from starsbot.app.launcher import DEMO_OVERRIDES
    from starsbot.app.supervisor import Supervisor
    from starsbot.web.server import make_app as stars_app

    c = Center(tmp_path)
    monkeypatch.setenv(ENV_TOKEN, c.supply.token)          # так Центр запускает бота
    sup = Supervisor(tmp_path / "sb", demo=True, overrides=DEMO_OVERRIDES)

    async def go():
        server = TestServer(stars_app(sup))
        await server.start_server()
        try:
            w = c.workers["starsbot"]
            w.port, w.state = server.port, "running"
            g1 = await c.supply.graph()
            c.workers["shopbot"].state = "running"
            c.supply._pings.clear()
            g2 = await c.supply.graph()
            monkeypatch.setenv(ENV_TOKEN, "other-secret-1234567890")    # бот запущен со старым секретом
            c.supply._pings.clear()
            g3 = await c.supply.graph()
            return g1, g2, g3
        finally:
            await server.close()
            await sup.stop()
    g1, g2, g3 = asyncio.run(go())

    def stars(g):
        return next(l for l in g["links"] if l["from"] == "starsbot" and l["to"] == "shopbot")
    assert stars(g1)["status"] == "idle" and "магазин не запущен" in stars(g1)["text"]
    assert stars(g2)["status"] == "ok"
    assert stars(g3)["status"] == "bad" and "перезапустите" in stars(g3)["text"]


def test_graph_api_and_main_page(tmp_path):
    c = Center(tmp_path)
    token = c.cfg.ensure_token()

    async def go():
        cl = TestClient(TestServer(make_app(c)))
        await cl.start_server()
        try:
            assert (await cl.get("/api/supply/graph")).status == 401
            await cl.get(f"/login?token={token}")
            g = await (await cl.get("/api/supply/graph")).json()
            r = await (await cl.post("/api/supply/secret", json={})).json()
            page = await (await cl.get("/")).text()
            js = await (await cl.get("/static/supply.js")).text()
            return g, r, page, js
        finally:
            await cl.close()
    g, r, page, js = asyncio.run(go())
    assert {"nodes", "links", "unused", "secret"} <= set(g) and r["ok"]
    assert 'id="supply"' in page and "/static/supply.js" in page and "refreshSupply" in js
