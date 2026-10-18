import asyncio

import yaml
from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.web import make_app

LIS = {"LIS_API_KEY": "k", "STEAM_ACCOUNT_NAME": "Trader", "STEAM_PASSWORD": "p", "STEAM_SHARED_SECRET": "s",
       "STEAM_IDENTITY_SECRET": "i", "STEAM_TRADE_URL": "https://steamcommunity.com/tradeoffer/new/?partner=123&token=x",
       "TELEGRAM_BOT_TOKEN": "111:lis", "TELEGRAM_CHAT_ID": "5"}


def _mann(c, username, steam_id="", tg="222:mann", cost=150):
    w = c.workers["mannbot"]
    w._cfg_path.parent.mkdir(parents=True, exist_ok=True)
    w._cfg_path.write_text(yaml.safe_dump({"steam": {"username": username, "steam_id": steam_id},
                                           "rtba": {"cost_per_key": cost}}), encoding="utf-8")
    c.vault.update({"mannbot.funpay.golden_key": "g", "mannbot.steam.password": "p", "mannbot.steam.shared_secret": "s",
                    "mannbot.steam.identity_secret": "i", "mannbot.notifications.telegram.bot_token": tg})


def _levels(r):
    return {i["key"]: i["level"] for i in r["items"]}


def test_empty_setup_is_not_ready(tmp_path):
    c = Center(tmp_path)
    r = c.readiness.check()
    lv = _levels(r)
    assert not r["ready"] and lv["lis_api"] == "fail" and lv["mann_funpay"] == "fail" and lv["mann_confirm"] == "fail"
    assert "steam_separate" not in lv and lv["lis_mode"] == "info" and lv["tg_center"] == "warn"
    assert r["items"][0]["level"] == "fail"                                   # проблемы сверху
    c.events.close()


def test_ready_and_same_steam_account(tmp_path):
    c = Center(tmp_path)
    c.workers["lisbot"].save_secrets(LIS)
    _mann(c, "other")
    r = c.readiness.check()
    lv = _levels(r)
    assert r["ready"], [i for i in r["items"] if i["level"] == "fail"]
    assert lv["steam_separate"] == "ok" and "tg_separate" not in lv and lv["tg_center"] == "ok"   # центр шлёт через LisBot

    _mann(c, "TRADER")                                                       # тот же логин, другой регистр
    r = c.readiness.check()
    assert not r["ready"] and _levels(r)["steam_separate"] == "fail"
    _mann(c, "other", steam_id=str(76561197960265728 + 123))                # тот же аккаунт по SteamID из трейд-ссылки
    assert _levels(c.readiness.check())["steam_separate"] == "fail"
    assert any("одном аккаунте Steam" in x for x in c.readiness.lines())
    c.events.close()


def test_shared_telegram_bot(tmp_path):
    c = Center(tmp_path)
    c.workers["lisbot"].save_secrets(LIS)
    _mann(c, "other", tg="111:lis")
    r = c.readiness.check()
    t = next(i for i in r["items"] if i["key"] == "tg_separate")
    assert t["level"] == "fail" and "LisBot и MannBot" in t["title"]
    _mann(c, "other")
    c.vault.set("center.monitor.telegram_token", "222:mann")                # бот центра совпал с MannBot
    t = next(i for i in c.readiness.check()["items"] if i["key"] == "tg_separate")
    assert "MannBot и центр" in t["title"]
    c.cfg.save({"monitor": {"telegram": {"commands": False}}})               # центр команды не читает — не мешает
    assert "tg_separate" not in _levels(c.readiness.check())
    c.events.close()


def test_api_and_demo(tmp_path):
    async def go():
        c = Center(tmp_path)
        token = c.cfg.ensure_token()
        async with TestClient(TestServer(make_app(c))) as cl:
            await cl.get(f"/login?token={token}", allow_redirects=False)
            r = await (await cl.get("/api/readiness")).json()
            assert r["ready"] is False and r["counts"]["fail"] >= 3
        c.events.close()
        d = Center(tmp_path / "d", demo=True)
        token = d.cfg.ensure_token()
        async with TestClient(TestServer(make_app(d))) as cl:
            await cl.get(f"/login?token={token}", allow_redirects=False)
            assert (await (await cl.get("/api/readiness")).json())["demo"]
        d.events.close()
    asyncio.run(go())
