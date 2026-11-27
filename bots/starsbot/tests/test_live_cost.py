"""Себестоимость по живой цене закупки: GRAM/USDT × P2P USDT, звезда — фиксированные 0.015 $."""
import asyncio
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestServer

from starsbot import rates as rates_mod
from starsbot.config import load_config
from starsbot.core.rtba import price_floor
from starsbot.crm.db import Database
from starsbot.economics import Economics
from starsbot.rates import RateProvider, StaticRate
from starsbot.treasury.bybit import BybitClient, BybitError


class Eng:
    def __init__(self, old=0.01476):
        self.old, self.econ = old, None

    def cost_per_star_ton(self):
        return (self.econ.star_ton_live() if self.econ else None) or self.old


def p2p(gram, usdt):
    r = StaticRate(round(gram * usdt, 4))
    r.source, r.gram_usdt, r.usdt_rub, r.updated_at = "p2p", gram, usdt, time.time()
    return r


def make(tmp_path, r, **econ):
    cfg = load_config(None, {"economics": {"withdraw_fee_pct": 3, **econ}})
    eng = Eng()
    ec = Economics(cfg, Database(tmp_path / "t.sqlite3"), r, eng)
    eng.econ = ec
    return cfg, ec


def test_cost_follows_dollar_price_when_gram_falls(tmp_path):
    """GRAM упал: рублей за GRAM меньше, GRAM за звезду больше — звезда в рублях стоит столько же."""
    a = make(tmp_path, p2p(1.016, 86.85))[1].cost_per_star_rub()
    b = make(tmp_path, p2p(0.952, 86.85))[1].cost_per_star_rub()
    assert abs(a - 0.015 * 86.85 * 1.001) < 0.001 and abs(a - b) < 0.001   # + 0.1% спот
    # по старой цене звезды в GRAM себестоимость была бы занижена больше чем на 6%
    assert 0.01476 * 0.952 * 86.85 < b * 0.94


def test_live_rate_beats_old_topups_but_not_manual_rate(tmp_path):
    cfg, ec = make(tmp_path, p2p(0.952, 86.85))
    ec.add_cashflow("topup", 17900, 199.5)
    assert ec.effective_rate() == (82.76, "P2P USDT × биржа")   # + 0.1% спот Bybit
    cfg.economics["cost_basis"] = "topups"
    assert ec.effective_rate() == (89.72, "ваши пополнения")
    cfg.economics["cost_basis"] = "live"
    cfg.economics["cost_rate_rub"] = 89
    assert ec.effective_rate()[0] == 89 and "дороже" in ec.rate_warning()
    cfg.economics["cost_rate_rub"] = 75
    assert "в минус" in ec.rate_warning()
    cfg.economics["cost_rate_rub"] = 83
    assert ec.rate_warning() == ""


def test_stale_p2p_falls_back(tmp_path):
    r = p2p(0.952, 86.85)
    r.updated_at = time.time() - 4 * 3600
    _, ec = make(tmp_path, r)
    # рублёвый курс устарел, но цена GRAM в долларах за 4 часа ещё годится для цены звезды
    assert not ec.live() and abs(ec.star_ton_live() - 0.015 / 0.952) < 1e-9
    r.updated_at = time.time() - 30 * 3600
    assert ec.star_ton_live() is None and ec.ton_per_star() == 0.01476


def test_forex_fallback_is_marked_up(tmp_path):
    """CoinGecko — форекс-курс: без спреда бот считал бы звезду на 5–9% дешевле настоящей закупки."""
    r = StaticRate(83.4)
    r.source = "coingecko"
    cfg, ec = make(tmp_path, r)
    assert ec.effective_rate() == (round(83.4 * 1.06, 2), "биржевой курс + спред")
    assert ec.spread_warning() == ""
    cfg.economics["exchange_spread_pct"] = 0
    assert "занижена" in ec.spread_warning() and "занижена" in ec.rate_warning()
    ec.add_cashflow("topup", 17900, 199.5)
    assert ec.spread_warning() == ""


def test_floor_pays_withdrawal_and_three_percent(tmp_path):
    cfg, ec = make(tmp_path, p2p(0.952, 86.85))
    cfg.rtba["min_margin_pct"] = 3
    floor = price_floor(cfg.rtba, 500, ec.cost_per_star_rub(), ec.fees_pct())
    profit = floor * 0.97 - 500 * 0.015 * 86.85
    assert abs(profit / (500 * 0.015 * 86.85) - 0.03) < 0.001


async def _serve(handler):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    s = TestServer(app)
    await s.start_server()
    return s


def test_rate_provider_multiplies_gram_by_usdt_and_survives_outage(tmp_path, monkeypatch):
    async def go():
        down = set()

        async def handler(request):
            if request.path in down:
                return web.json_response({"retCode": 10001, "retMsg": "Not supported symbols", "result": {}})
            if request.path == "/bybit":
                return web.json_response({"retCode": 0, "result": {"list": [{"ask1Price": "0.952", "lastPrice": "0.95"}]}})
            if request.path == "/mexc":
                return web.json_response({"symbol": "GRAMUSDT", "price": "0.953"})
            if request.path == "/rapira":
                return web.json_response({"data": [{"symbol": "USDT/RUB", "askPrice": 86.99}]})
            return web.json_response({"the-open-network": {"rub": 77.84}})
        s = await _serve(handler)
        u = lambda p: str(s.make_url(p))
        monkeypatch.setattr(rates_mod, "GRAM_TICKERS", (("bybit", u("/bybit")), ("mexc", u("/mexc"))))
        monkeypatch.setattr(rates_mod, "RAPIRA", u("/rapira"))
        monkeypatch.setattr(rates_mod, "SOURCES", (("coingecko", u("/cg"), rates_mod._from_coingecko),))
        cfg = load_config(None, {}).rates
        r = RateProvider(cfg, Database(tmp_path / "r.sqlite3"))
        assert abs(await r.refresh(force=True) - 0.952 * 86.99) < 1e-3 and r.is_p2p
        down.add("/bybit")                                 # пара снята — берём вторую биржу
        assert abs(await r.refresh(force=True) - 0.953 * 86.99) < 1e-3
        cfg["usdt_rub"] = 85.0                             # своя цена USDT важнее биржевой
        assert abs(await r.refresh(force=True) - 0.953 * 85) < 1e-3
        down.add("/mexc")
        assert await r.refresh(force=True) == 77.84 and r.source == "coingecko" and not r.is_p2p
        await s.close()
    asyncio.run(go())


def test_bybit_client_follows_rename_to_gram():
    seen = []

    async def handler(request):
        body = await request.text()
        seen.append((request.path, request.query_string, body))
        if request.path == "/v5/market/tickers":
            if request.query.get("symbol") == "TONUSDT":
                return web.json_response({"retCode": 10001, "retMsg": "Not supported symbols", "result": {}})
            return web.json_response({"retCode": 0, "result": {"list": [{"bid1Price": "0.951", "ask1Price": "0.952"}]}})
        if request.path == "/v5/account/wallet-balance":
            return web.json_response({"retCode": 0, "result": {"list": [{"coin": [
                {"coin": "GRAM", "walletBalance": "7", "locked": "1"}]}]}})
        if request.path.endswith("query-account-coins-balance"):
            return web.json_response({"retCode": 0, "result": {"balance": [{"coin": "GRAM", "transferBalance": "5"}]}})
        return web.json_response({"retCode": 0, "result": {"orderId": "1", "id": "2"}})

    async def go():
        s = await _serve(handler)
        c = BybitClient("K", "S", base_url=str(s.make_url("")), allowed_addresses=["UQmine"])
        assert (await c.ticker()).ask == 0.952
        b = await c.balances()
        assert b.get("UNIFIED", "TON") == 6 and b.get("FUND", "TON") == 5   # внутри бота монета по-прежнему TON
        assert any("GRAM" in q for p, q, _ in seen if p == "/v5/account/wallet-balance")
        await c.buy_ton("10", "0.96")
        assert json.loads(seen[-1][2])["symbol"] == "GRAMUSDT"
        await c.withdraw("TON", "TON", "UQmine", "10")
        assert json.loads(seen[-1][2])["coin"] == "GRAM"
        try:
            await c.withdraw("TON", "TON", "UQother", "10")
            raise AssertionError
        except BybitError as e:
            assert e.code == "address"
        await c.close()
        await s.close()
    asyncio.run(go())
