import asyncio
import copy
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from starsbot.app.launcher import DEMO_OVERRIDES
from starsbot.app.supervisor import MemoryLogHandler, Supervisor
from starsbot.demo import run_scenario
from starsbot.treasury.bybit import BybitClient, BybitError, round_down, sign
from starsbot.treasury.fake import run_treasury_demo
from starsbot.ton.wallet import from_nano, to_nano
from starsbot.web.server import make_app

TOKENS = {"token": "admin-token-12345"}


def _sup(tmp_path, **treasury):
    over = copy.deepcopy(DEMO_OVERRIDES)
    over["treasury"] = {**over["treasury"], **treasury}
    sup = Supervisor(tmp_path, demo=True, overrides=over)
    sup.save({"dashboard": TOKENS}, rebuild=False)
    return sup


def _ticks(sup, n):
    async def go():
        bybit, hub = sup.demo_treasury
        for _ in range(n):
            await sup.engine.treasury.tick()
            bybit.tick()
            hub.tick()
            sup.engine.wallet.tick()
    asyncio.run(go())


def test_sign_and_round_down():
    # пример подписи из документации Bybit V5 (HMAC-SHA256 от timestamp+key+recv_window+payload)
    assert sign("secret", "1658384314791", "key", "5000", "category=spot") == \
        __import__("hmac").new(b"secret", b"1658384314791key5000category=spot", "sha256").hexdigest()
    assert round_down(12.3456, "0.01") == "12.34"
    assert round_down(0.00999, "0.01") == "0"
    assert round_down(1.23456, "0.0001") == "1.2345"


def test_bybit_client_signs_requests_and_refuses_foreign_address():
    seen = []

    async def handler(request: web.Request):
        body = await request.text()
        seen.append((request.path, dict(request.headers), request.query_string, body))
        if request.path == "/v5/market/tickers":
            return web.json_response({"retCode": 0, "result": {"list": [{"bid1Price": "1.20", "ask1Price": "1.21",
                                                                          "lastPrice": "1.205"}]}})
        if request.path == "/v5/order/create":
            return web.json_response({"retCode": 0, "result": {"orderId": "777"}})
        return web.json_response({"retCode": 10001, "retMsg": "bad"})

    async def go():
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        c = BybitClient("KEY", "SECRET", base_url=str(server.make_url("")), allowed_addresses=["UQmine"])
        t = await c.ticker()
        assert (t.bid, t.ask, t.last) == (1.2, 1.21, 1.205)
        assert "X-BAPI-SIGN" not in seen[-1][1]                    # публичный запрос без подписи
        oid = await c.buy_ton("10.5", "1.22")
        assert oid == "777"
        path, headers, _, body = seen[-1]
        assert json.loads(body)["timeInForce"] == "IOC" and json.loads(body)["side"] == "Buy"
        assert headers["X-BAPI-SIGN"] == sign("SECRET", headers["X-BAPI-TIMESTAMP"], "KEY",
                                              headers["X-BAPI-RECV-WINDOW"], body)
        try:
            await c.withdraw("TON", "TON", "UQsomeone_else", "10")
            raise AssertionError("вывод на чужой адрес должен быть запрещён")
        except BybitError as e:
            assert e.code == "address"
        try:
            await c.coin_chain()
            raise AssertionError
        except BybitError as e:
            assert e.code == 10001
        await c.close()
        await server.close()
    asyncio.run(go())


def test_chain_bybit_to_hub_to_wallet_keeps_books_and_advises_p2p(tmp_path):
    sup = _sup(tmp_path)
    tr = sup.engine.treasury
    w = sup.engine.wallet
    steps = asyncio.run(run_treasury_demo(sup))
    kinds = [m["kind"] for m in reversed(tr.moves()) if m["state"] == "done"]
    assert kinds[:5] == ["xfer", "buy", "buy", "xfer", "withdraw"] and "hub" in kinds, (kinds, steps)
    assert from_nano(w.balance) > 400                               # кошелёк бота пополнен с 60 TON
    # пополнение записано в журнал по себестоимости, сверка сходится
    top = sup.db.one("SELECT SUM(ton) t, SUM(rub) r FROM cashflow WHERE note LIKE 'казначейство:%'")
    assert top["t"] > 400 and 100 < top["r"] / top["t"] < 110
    assert not sup.econ.reconcile().get("alarm")
    # запаса меньше 1.5 дня → совет купить USDT на P2P, рубли — с Ozon и GGSEL (вывод дешевле маржи), не с FunPay
    adv = tr.advices()
    p2p = [a for a in adv if a["kind"] == "p2p"]
    assert len(p2p) == 1 and "P2P" in p2p[0]["text"] and "Ozon" in p2p[0]["text"]
    wd = sorted(a["source"] for a in adv if a["kind"] == "withdraw")
    assert wd == ["ggsel", "ozon"]
    # повторный tick не дублирует совет и не просит вывод на карту раньше срока
    _ticks(sup, 2)
    assert len([a for a in tr.advices() if a["kind"] == "p2p"]) == 1
    assert tr.card_ok()[0] is False
    # отмечаем покупку USDT — курс считается по факту
    assert "₽/USDT" in tr.record_p2p(100000, 1150)
    assert abs(tr.usdt_rub() - 86.957) < 0.01


def test_unknown_withdraw_blocks_automation_until_resolved(tmp_path):
    sup = _sup(tmp_path, hub={"enabled": False})
    tr = sup.engine.treasury
    bybit, _ = sup.demo_treasury
    bybit.bal.unified["TON"] = 0
    bybit.bal.fund["TON"] = 1500.0
    bybit.fail_next_withdraw = "network"
    sup.engine.wallet.balance = to_nano(10)
    sup.econ.add_cashflow("adjust", 0, 0, "")
    _ticks(sup, 1)
    m = tr.moves()[0]
    assert m["kind"] == "withdraw" and m["state"] == "unknown"
    _ticks(sup, 3)
    assert len(tr.moves()) == 1                                      # ничего не повторили
    assert any("неизвестен" in t for _, t in sup.notifier.sent)
    assert "несостоявшимся" in tr.resolve(int(m["id"]), False)
    _ticks(sup, 1)
    assert tr.moves()[0]["kind"] == "withdraw" and tr.moves()[0]["state"] == "sent"


def test_manual_mode_proposes_and_dry_run_moves_nothing(tmp_path):
    sup = _sup(tmp_path, auto=False, hub={"enabled": False})
    tr = sup.engine.treasury
    sup.engine.wallet.balance = to_nano(10)
    _ticks(sup, 1)
    m = tr.moves()[0]
    assert m["state"] == "proposed" and m["kind"] == "xfer"
    assert any(b for _, t in sup.notifier.sent if "Выполнить?" in t for b in [t])
    _ticks(sup, 1)
    assert len(tr.moves()) == 1                                      # ждёт кнопку, нового не предлагает
    out = asyncio.run(sup.engine.handle_command("treasury_ok", [str(m["id"])]))
    assert "xfer" in out and tr.moves()[0]["state"] == "done"

    sup2 = _sup(tmp_path / "dry", hub={"enabled": False})
    sup2.engine.treasury.cfg.general.demo = False                    # боевые клиенты не нужны: проверяем флаг
    sup2.engine.treasury.cfg.general.dry_run = True
    sup2.engine.wallet.balance = to_nano(10)
    asyncio.run(sup2.engine.treasury.refresh())
    text = asyncio.run(sup2.engine.treasury.plan())
    assert text.startswith("[тестовый режим]") and not sup2.engine.treasury.moves()


def test_router_waits_for_big_batch_and_cooldown(tmp_path):
    sup = _sup(tmp_path, hub={"enabled": False}, bybit={"enabled": False})
    tr = sup.engine.treasury
    tr.set_source_balance("ozon", 9000)
    _ticks(sup, 1)
    assert not [a for a in tr.advices() if a["kind"] == "withdraw"]     # мелочь не выводим
    tr.set_source_balance("ozon", 64300)
    sup.engine.wallet.balance = to_nano(5000)                           # запаса много → только вывод прибыли
    sup.econ.add_cashflow("adjust", 0, 5000, "")
    _ticks(sup, 1)
    wd = [a for a in tr.advices() if a["kind"] == "withdraw"]
    assert len(wd) == 1 and wd[0]["source"] == "ozon" and wd[0]["amount_rub"] == 64000
    tr.set_source_balance("ggsel", 50000)
    _ticks(sup, 2)
    assert len([a for a in tr.advices() if a["kind"] == "withdraw"]) == 1   # не чаще раза в 24 ч
    assert "Отмечено" in asyncio.run(sup.engine.handle_command("treasury_done", [str(wd[0]["id"])]))
    assert (sup.db.kv_get("treasury_src:ozon") or {})["rub"] == 300


def test_treasury_page(tmp_path):
    sup = _sup(tmp_path)

    async def go():
        await run_scenario(sup.engine, sup.engine.fp, sup.engine.wallet, verbose=False)
        await run_treasury_demo(sup)
        client = TestClient(TestServer(make_app(sup, MemoryLogHandler())))
        await client.start_server()
        await client.get(f"/login?token={TOKENS['token']}", allow_redirects=False)
        r = await client.get("/treasury")
        html = await r.text()
        assert r.status == 200 and "Где деньги" in html and "Покупка TON на Bybit" in html and "P2P" in html
        r = await client.post("/treasury", data={"action": "p2p", "rub": "50000", "usdt": "575"})
        assert r.status == 200 and "86.96" in await r.text()
        r = await client.post("/treasury", data={"action": "save", "enabled": "on", "auto": "on", "usdt_rub": "87",
                                                 "min_batch_rub": "30000", "fee_funpay": "6", "on_funpay": "on",
                                                 "on_ozon": "on", "bybit_key": "", "bybit_secret": ""})
        assert r.status == 200
        assert sup.cfg.treasury.router.min_batch_rub == 30000
        srcs = {s["key"]: s for s in sup.engine.treasury.sources(include_off=True)}
        assert srcs["ozon"].get("enabled") is True and srcs["wb"].get("enabled") is False
        await client.close()
    asyncio.run(go())
