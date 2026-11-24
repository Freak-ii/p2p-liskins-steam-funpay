"""Курсы магазина после переименования Toncoin → Gram: новая пара и запасной источник USDT/RUB."""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

from shopbot import rates as rm


class Cfg:
    def get(self, key):
        return {"rates.source": "bybit", "rates.usdt_rub": 86.8, "rates.ton_usdt": 1.2,
                "rates.max_age_minutes": 10}.get(key)


class Db:
    def kv_get(self, key):
        return None

    def kv_set(self, key, value):
        self.saved = value


def test_gram_pair_and_rapira_when_p2p_is_empty_or_broken(monkeypatch):
    async def go():
        p2p_status = [200]

        async def handler(request):
            if request.path == "/ticker":
                ok = request.query["symbol"] == "GRAMUSDT"
                return web.json_response({"retCode": 0 if ok else 10001,
                                          "result": {"list": [{"lastPrice": "0.952"}]} if ok else {}})
            if request.path == "/p2p":
                return web.json_response({"result": {"items": []}}, status=p2p_status[0]) if p2p_status[0] == 200 \
                    else web.Response(status=502, text="<html>")
            return web.json_response({"data": [{"symbol": "USDT/RUB", "askPrice": 86.99}]})
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        s = TestServer(app)
        await s.start_server()
        for name, path in (("BYBIT_TICKER", "/ticker"), ("BYBIT_P2P", "/p2p"), ("RAPIRA", "/rapira")):
            monkeypatch.setattr(rm, name, str(s.make_url(path)))
        r = rm.Rates(Cfg(), Db())
        assert await r.refresh() and r.fresh and r.source == "rapira"
        assert r.ton_usdt == 0.952 and r.usdt_rub == 86.99
        p2p_status[0] = 502
        r.ts = 0
        assert await r.refresh() and r.fresh
        await s.close()
    asyncio.run(go())
