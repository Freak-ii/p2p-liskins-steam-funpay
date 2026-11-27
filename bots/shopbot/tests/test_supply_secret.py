"""Общий секрет поставщиков, адреса поставщиков от Центра и кнопки главного меню."""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

from shopbot import texts as T
from shopbot.__main__ import App
from shopbot.config import Config
from shopbot.suppliers import build_suppliers, supply_token, supply_url
from shopbot.web.server import make_app


def test_center_secret_and_urls_win_over_defaults(tmp_path, monkeypatch):
    cfg = Config(tmp_path)
    assert supply_token(cfg, "stars") == ("", "")
    assert supply_url(cfg, "stars") == "http://127.0.0.1:8803"
    monkeypatch.setenv("ORBITA_SUPPLY_TOKEN", "center-secret-1234567890")
    monkeypatch.setenv("ORBITA_SUPPLY_URLS", '{"stars": "http://127.0.0.1:8804", "steam_keys": "http://127.0.0.1:8801"}')
    assert supply_token(cfg, "gifts") == ("center-secret-1234567890", "center")
    assert supply_url(cfg, "stars") == "http://127.0.0.1:8804"          # порт StarsBot сменился — магазин знает
    sup = build_suppliers(cfg, demo=False)
    assert sup["stars"].url == "http://127.0.0.1:8804" and sup["premium"].token == "center-secret-1234567890"
    cfg.save({"suppliers": {"stars": {"url": "http://10.0.0.5:9000"}}})
    assert supply_url(cfg, "stars") == "http://10.0.0.5:9000"           # свой адрес из config.yaml сильнее


def test_one_secret_field_fills_all_suppliers(tmp_path):
    app = App(tmp_path, demo=True)
    app.cfg.save({"dashboard": {"token": "admin-token-12345"}})

    async def go():
        await app.start()
        c = TestClient(TestServer(make_app(app)))
        await c.start_server()
        try:
            await c.get("/login?token=admin-token-12345")
            html = await (await c.get("/settings")).text()
            await c.post("/settings", data={"suppliers.token": "short"})
            short = app.cfg.get("suppliers.stars.token")
            await c.post("/settings", data={"suppliers.token": "shared-secret-1234567890"})
            after = await (await c.get("/settings")).text()
            return html, short, after
        finally:
            await c.close()
    html, short, after = asyncio.run(go())
    assert "Общий секрет поставщиков" in html and "Токен TG Gift" not in html
    assert not short
    assert all(app.cfg.get(f"suppliers.{n}.token") == "shared-secret-1234567890"
               for n in ("stars", "premium", "gifts", "steam_keys"))
    assert 'placeholder="задан"' in after


def test_menu_has_steam_topup_and_tf2_keys():
    rows = T.menu_buttons(set(), steam_topup=True)
    flat = [b for r in rows for b in r]
    assert ("🔑 Ключи TF2", "buy:steam_keys") in flat and ("💳 Пополнить Steam", "sv:steam_topup") in flat
    assert ("💳 Пополнить Steam", "sv:steam_topup") not in [b for r in T.menu_buttons(set()) for b in r]
    assert not any(t == "🎮 Steam" for t, _ in flat)


def test_main_menu_button_follows_rtba(tmp_path):
    from tests.test_shop import make
    shop, bot = make(tmp_path)
    import asyncio as _a
    shop.rtba.set_mode("svc.steam_topup", "on")
    _a.run(shop.rtba.tick())
    assert bot.steam_topup_open() is (shop.services is not None and shop.services.catalog.item("steam_topup") is not None)
    shop.rtba.set_mode("svc.steam_topup", "off")
    _a.run(shop.rtba.tick())
    assert bot.steam_topup_open() is False
