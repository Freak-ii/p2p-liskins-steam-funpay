"""Орбита: товар, снятый в Пульте центра, пропадает из меню бота, мини-приложения и постов канала."""
import asyncio
import json
import os
import time

from test_shop import make

from shopbot.miniapp import page_query
from shopbot.orbit_gate import OrbitGate, OrbitTracker
from shopbot import texts as T


def write(path, version, closed):
    path.write_text(json.dumps({"version": version, "closed": {"shopbot": closed}}), encoding="utf-8")
    t = time.time() + version
    os.utime(path, (t, t))


def test_closed_product_disappears_everywhere(tmp_path):
    shop, bot = make(tmp_path)
    orbit = tmp_path / "orbit.json"
    shop.orbit.gate = OrbitGate("shopbot", orbit, ttl=0)
    shop.orbit.tracker = OrbitTracker(shop.orbit.gate)

    async def go():
        await shop.rtba.tick()
        assert shop.rtba.is_open("buy.steam_keys")
        before = {p["id"]: p["text"] for p in await shop.channel.build()}
        assert "Ключи для пополнения Steam" in before["showcase"]
        write(orbit, 1, ["buy.steam_keys", "post.steam_keys", "buy.premium", "post.premium"])
        await shop.orbit.tick()
        assert not shop.rtba.is_open("buy.steam_keys") and not shop.rtba.is_open("buy.premium")
        shop.rtba.set_mode("buy.steam_keys", "on")          # «on» владельца не обходит Орбиту
        await shop.rtba.tick()
        assert not shop.rtba.is_open("buy.steam_keys")
        await bot.menu(1)
        msg = bot.outbox[-1]
        assert "🎮 <b>Steam</b>" not in msg["text"] and "Premium" not in msg["text"]
        assert "buy:steam_keys" not in json.dumps(msg["buttons"]) and "buy:stars" in json.dumps(msg["buttons"])
        posts = {p["id"]: p for p in await shop.channel.build()}
        assert "Steam" not in posts["showcase"]["text"] and "Premium" not in posts["showcase"]["text"]
        assert "steam_keys" not in json.dumps(posts["showcase"]["buttons"])
        assert "ключи Steam" not in posts["open"]["text"] and "Premium" not in posts["open"]["text"]
        ack = json.loads((tmp_path / "orbit-ack" / "shopbot.json").read_text(encoding="utf-8"))
        assert ack["version"] == 1 and ack["applied"]["buy.steam_keys"] == "ok"
        write(orbit, 2, [])
        await shop.orbit.tick()
        assert shop.rtba.is_open("buy.premium")
    asyncio.run(go())


def test_texts_helpers():
    assert T.goods_text(frozenset()) == "Звёзды, подарки, Premium и ключи Steam"
    assert T.goods_text({"premium", "steam_keys"}) == "Звёзды и подарки"
    assert "Звёзды и подарки для Telegram" in T.welcome("X", {"premium", "steam_keys"})
    assert "hide=premium%2Csteam_keys" in page_query({"general.shop_name": ""} and _Cfg(), "b", {"steam_keys", "premium"})


class _Cfg:
    def get(self, key, default=None):
        return ""
