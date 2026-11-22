import asyncio

import yaml
from aiohttp.test_utils import TestClient, TestServer
from pytoniq_core.crypto.keys import mnemonic_new

from starsbot.app.launcher import DEMO_OVERRIDES
from starsbot.app.supervisor import MemoryLogHandler, Supervisor
from starsbot.core import messages as M
from starsbot.demo import run_scenario
from starsbot.web.server import make_app

TOKENS = {"token": "admin-token-12345", "operator_token": "operator-tok-123", "viewer_token": "viewer-token-123"}


async def _client(sup, mem=None, token=None):
    client = TestClient(TestServer(make_app(sup, mem)))
    await client.start_server()
    r = await client.get(f"/login?token={token or sup.ensure_token()}", allow_redirects=False)
    assert r.status == 302
    return client


def test_demo_pages_controls_and_money(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=DEMO_OVERRIDES)
    sup.save({"dashboard": TOKENS}, rebuild=False)

    async def go():
        res = await run_scenario(sup.engine, sup.engine.fp, sup.engine.wallet, verbose=False)
        anon = TestClient(TestServer(make_app(sup)))
        await anon.start_server()
        assert (await anon.get("/", allow_redirects=False)).status == 302     # без входа
        assert (await anon.get("/api/status")).status == 401
        assert (await (await anon.get("/api/ping")).json())["app"] == "starsbot"
        await anon.close()
        client = await _client(sup, MemoryLogHandler(), TOKENS["token"])
        try:
            oid = res["orders"][1]
            for path in ["/", "/?days=30", "/orders", "/orders?q=bob_real", f"/orders/{oid}", "/customers",
                         "/customers?seg=repeat", "/customers?seg=vip&q=Greg", "/customers/11",
                         "/pricing", "/pricing?stars=250", "/money", "/money?days=7&p100=150", "/analytics",
                         "/alerts", "/settings", "/setup", "/messages", "/messages?lang=en", "/logs", "/api/logs",
                         "/api/status"]:
                r = await client.get(path)
                assert r.status == 200, path
            assert "bob_real" in await (await client.get("/orders?q=bob_real")).text()
            hp = await (await client.get("/help")).text()
            assert "<h3>1. Подготовка" in hp and "type='checkbox'" in hp and "<table>" in hp and "<code>v5r1</code>" in hp
            r = await client.post(f"/orders/{oid}", data={"action": "note", "text": "VIP"})
            assert r.status == 200 and sup.db.get_order(oid)["notes"] == "VIP"
            calc = await (await client.post("/api/calc", json={"prices": {"100": 200}})).json()
            row = next(x for x in calc["rows"] if x["stars"] == 100)
            assert row["price"] == 200 and row["profit"] > 0 and row["net_rub"] < 200
            before = sup.econ.topup_rate()
            await client.post("/money", data={"action": "cashflow", "kind": "topup", "rub": "2000", "ton": "10"})
            assert sup.econ.topup_rate() != before
            # SSE: событие БД долетает до панели
            r = await client.get("/api/stream")
            await r.content.readline()
            sup.db.add_event(oid, "ping_event")
            got = b""
            for _ in range(10):
                got += await asyncio.wait_for(r.content.readline(), 3)
                if b"ping_event" in got:
                    break
            assert b"ping_event" in got
            r.close()
            await client.post("/control", data={"action": "start"})
            await asyncio.sleep(0.5)
            assert sup.state == "running"
            await client.post("/control", data={"action": "stop"})
            assert sup.state == "stopped"
        finally:
            await client.close()
    asyncio.run(go())


def test_roles(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=DEMO_OVERRIDES)
    sup.save({"dashboard": TOKENS}, rebuild=False)

    async def go():
        res = await run_scenario(sup.engine, sup.engine.fp, sup.engine.wallet, verbose=False)
        oid = res["orders"][0]
        viewer = await _client(sup, token=TOKENS["viewer_token"])
        op = await _client(sup, token=TOKENS["operator_token"])
        try:
            assert (await viewer.get("/")).status == 200
            assert (await viewer.get("/settings")).status == 403
            assert (await viewer.post(f"/orders/{oid}", data={"action": "note", "text": "x"})).status == 403
            assert (await viewer.post("/control", data={"action": "pause"})).status == 403
            assert (await op.post(f"/orders/{oid}", data={"action": "note", "text": "op"})).status == 200
            assert (await op.post(f"/orders/{oid}", data={"action": "refund"})).status == 403
            assert (await op.post("/setup", data={})).status == 403
            assert (await op.post("/control", data={"action": "dry_off"})).status == 403
            assert (await op.post("/money", data={"action": "delete", "id": "1"})).status == 403
            assert (await viewer.get("/export/orders.csv")).status == 403
            r = await op.get("/export/orders.csv")
            body = (await r.read()).decode("utf-8-sig")
            assert r.status == 200 and "attachment" in r.headers["Content-Disposition"]
            lines = body.strip().splitlines()
            assert lines[0].startswith("order_id;") and len(lines) == 1 + len(res["orders"])
            done = next(l for l in lines if l.startswith(oid + ";")).split(";")
            assert done[14]                                        # прибыль посчитана для выданного заказа
            sup.econ.add_cashflow("topup", 1000, 3.5, "тест")
            cf = (await (await op.get("/export/cashflow.csv")).read()).decode("utf-8-sig")
            assert "Пополнение кошелька TON;1000" in cf and "3,5" in cf
            assert (await op.get("/export/secrets.csv")).status == 404
            await op.post("/control", data={"action": "pause"})
            assert sup.engine.paused
        finally:
            await viewer.close()
            await op.close()
        roles = {r["role"] for r in sup.db.q("SELECT role FROM audit")}
        assert {"viewer", "operator"} <= roles
    asyncio.run(go())


def test_setup_settings_messages_flow(tmp_path):
    sup = Supervisor(tmp_path)
    assert not sup.configured
    words = " ".join(mnemonic_new())

    async def go():
        client = await _client(sup)
        try:
            r = await client.get("/", allow_redirects=False)
            assert r.headers["Location"] == "/setup"                          # первый запуск → мастер
            await client.post("/setup", data={"golden_key": "gk123", "lot_50": "555", "lot_100": "556",
                                              "fragment_cookies": "stel_ssid=a; stel_token=b; stel_ton_token=c",
                                              "mnemonic": words, "ton_address": "UQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqEBI",
                                              "ton_version": "v5r1", "dry_run": "on", "language": "ru"})
            assert sup.configured, sup.problems
            raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
            assert raw["funpay"]["golden_key"] == "gk123" and raw["ton"]["mnemonic"] == words
            lots = {l["stars"]: l["offer_id"] for l in raw["funpay"]["lots"]}
            assert lots[50] == 555 and lots[100] == 556 and lots[75] == 0
            # секреты не попадают в HTML
            for page in ("/setup", "/settings"):
                html = await (await client.get(page)).text()
                assert "gk123" not in html and words.split()[0] + " " + words.split()[1] not in html
            # пустое секретное поле в настройках не стирает значение
            form = {"funpay__golden_key": "", "ton__mnemonic": "", "economics__withdraw_fee_pct": "4.5",
                    "risk__blacklist_usernames": "scam_one\nscam_two", "general__dry_run": "on"}
            await client.post("/settings", data=form)
            assert sup.cfg.funpay.golden_key == "gk123" and sup.cfg.ton.mnemonic == words
            assert sup.cfg.economics.withdraw_fee_pct == 4.5
            assert sup.cfg.risk.blacklist_usernames == ["scam_one", "scam_two"]
            # редактор сообщений: валидный текст сохраняется, со сторонней ссылкой — нет
            await client.post("/messages", data={"lang": "ru",
                                                 "delivered": "Готово! {stars} ⭐ для @{username}, заказ #{order_id}",
                                                 "reminder": "Пишите на https://evil.example"})
            assert M.render("ru", "delivered", stars=50, username="x", order_id="X1") == \
                "Готово! 50 ⭐ для @x, заказ #X1"
            assert "reminder" not in M.OVERRIDES.get("ru", {})
            prev = await (await client.post("/api/preview", json={"text": "#{order_id} → @{username}"})).json()
            assert prev["ok"] and prev["text"] == "#ABCD1234 → @durov"
        finally:
            await client.close()
            M.set_overrides({})
    asyncio.run(go())


def test_pages_before_first_wallet_check(tmp_path):
    """Реальный режим, кошелёк ещё ни разу не проверялся, FunPay недоступен — страницы и кнопки без 500."""
    sup = Supervisor(tmp_path, demo=False)
    sup.save({"dashboard": TOKENS}, rebuild=True)
    assert not sup.db.kv_get("wallet")

    async def go():
        client = await _client(sup, token=TOKENS["token"])
        try:
            for path in ["/", "/money", "/crypto", "/gifts", "/funpay", "/pricing", "/analytics"]:
                r = await client.get(path)
                assert r.status == 200, path

            async def boom(*a, **k):
                raise RuntimeError("funpay недоступен")
            sup.engine.fp.login = boom
            sup.engine.lot_manager.create_missing = boom
            r = await client.post("/funpay", data={"action": "create"}, allow_redirects=False)
            assert r.status == 302 and "msg=" in r.headers["Location"]
        finally:
            await client.close()

    asyncio.run(go())


def test_mdlite_escapes_and_nests():
    from starsbot.web.mdlite import render
    h = render("# T\n- [x] a <b>\n  - **b** [x](https://evil) [ok](/money)\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "&lt;b&gt;" in h and "checked" in h and h.count("<ul>") == 2 and h.count("</ul>") == 2
    assert "evil" in h and 'href="https' not in h and 'href="/money"' in h and "<td>2</td>" in h
def test_marketplace_pages(tmp_path):
    from starsbot.avito.fake import DEMO_PRODUCTS, run_avito_demo
    from starsbot.wb.channel import run_wb_demo
    over = {**DEMO_OVERRIDES, "avito": {"enabled": True, "products": DEMO_PRODUCTS},
            "wb": {"enabled": True, "products": [{"stars": 100, "nm_id": "WB100", "price": 190}]}}
    sup = Supervisor(tmp_path, demo=True, overrides=over)
    sup.save({"dashboard": TOKENS}, rebuild=False)

    async def go():
        sup.engine.cfg.deal.message_rate_limit_seconds = 0
        sup.engine.fragment_ok = True
        await sup.engine.wallet_watch()
        await run_avito_demo(sup.engine, sup.engine.wallet)
        await run_wb_demo(sup.engine, sup.engine.wallet)
        client = await _client(sup, MemoryLogHandler(), TOKENS["token"])
        try:
            for path in ["/avito", "/wb", "/ozon", "/settings"]:
                r = await client.get(path)
                assert r.status == 200, (path, await r.text())
            html = await (await client.get("/avito")).text()
            assert "Анна" in html and "Пол маржи" in html
            r = await client.post("/wb", data={"action": "issue", "stars": "100", "count": "4"})
            assert r.status == 200
            csv_text = await (await client.get("/wb/codes.csv")).text()
            assert csv_text.count("TGW-") >= 4
            r = await client.post("/avito", data={"action": "products", "id_100": "https://www.avito.ru/moskva/x_1234567",
                                                  "price_100": "200"})
            assert r.status == 200
            p = next(p for p in sup.raw["avito"]["products"] if p["stars"] == 100)    # в демо конфиг перекрыт
            assert p["item_id"] == 1234567 and p["price"] == 200
            r = await client.post("/avito", data={"action": "pricing", "price_mode": "margin", "min_margin_pct": "3",
                                                  "target_margin_pct": "9", "price_sync": "1"})
            assert r.status == 200 and sup.raw["avito"]["price_mode"] == "margin"
        finally:
            await client.close()
    asyncio.run(go())
