import asyncio
import time

from aiohttp import web
from aiohttp.test_utils import TestServer

from center.steamcheck import auth_code, check

SECRET = "ZGVtby1kZW1vLWRlbW8tZGVtbzE="


def test_auth_code_matches_steam_totp():
    # значения из steam-totp (Node), которым пользуется LisBot
    assert auth_code(SECRET, 0) == "JRVC6"
    assert auth_code(SECRET, 1700000000) == "CQ26X"
    assert auth_code(SECRET, 1793300000) == "YYFQQ"


async def _run(secrets, *, offset=0, resolve=lambda h: "1.2.3.4"):
    async def qt(_request):
        return web.json_response({"response": {"server_time": str(int(time.time()) + offset)}})
    app = web.Application()
    app.router.add_post("/qt", qt)
    srv = TestServer(app)
    await srv.start_server()
    try:
        return await check(secrets, time_url=str(srv.make_url("/qt")), resolve=resolve)
    finally:
        await srv.close()


def test_all_good_shows_current_code():
    r = asyncio.run(_run({"STEAM_SHARED_SECRET": SECRET, "STEAM_IDENTITY_SECRET": SECRET}))
    assert r["ok"] and len(r["code"]) == 5 and r["code"] in " ".join(r["lines"])
    assert any("Часы совпадают" in x for x in r["lines"])


def test_dns_failure_clock_drift_and_bad_secret():
    def no_dns(host):
        raise OSError("getaddrinfo ENOENT")
    before = auth_code(SECRET, time.time() + 95)
    r = asyncio.run(_run({"STEAM_SHARED_SECRET": SECRET}, offset=95, resolve=no_dns))
    assert not r["ok"]
    assert any("DNS" in x and "api.steampowered.com" in x for x in r["lines"])
    assert any("+95" in x for x in r["lines"])
    assert r["code"] in (before, auth_code(SECRET, time.time() + 95))   # код с поправкой на часы Steam

    r = asyncio.run(_run({"STEAM_SHARED_SECRET": '"abc"'}))
    assert not r["ok"] and not r["code"] and any("shared_secret не похож" in x for x in r["lines"])
