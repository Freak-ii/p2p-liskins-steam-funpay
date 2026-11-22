import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

from center import tgcheck

TOKEN = "7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def test_static_problems():
    assert tgcheck.static_problems(TOKEN, "123456") == []
    assert "id самого бота" in tgcheck.static_problems(TOKEN, "7123456789")[0]
    assert "числом" in tgcheck.static_problems(TOKEN, "@frayz")[0]
    assert "токен" in tgcheck.static_problems("abc", "1")[0]
    assert tgcheck.static_problems(TOKEN, [123, -100500]) == []


def test_check_against_fake_api():
    async def go():
        sent = []
        hook = {"url": ""}

        async def handler(request):
            method = request.match_info["method"]
            if request.match_info["token"] != "bot" + TOKEN:
                return web.json_response({"ok": False, "description": "Unauthorized"}, status=401)
            if method == "getMe":
                return web.json_response({"ok": True, "result": {"username": "frayz_lis_bot"}})
            if method == "getWebhookInfo":
                return web.json_response({"ok": True, "result": hook})
            body = await request.json()
            if body["chat_id"] == "111":
                return web.json_response({"ok": False, "description": "Bad Request: chat not found"}, status=400)
            sent.append(body)
            return web.json_response({"ok": True, "result": {}})

        app = web.Application()
        app.router.add_post("/{token}/{method}", handler)
        srv = TestServer(app)
        await srv.start_server()
        api = f"http://127.0.0.1:{srv.port}"
        r = await tgcheck.check(TOKEN, "42", "LisBot", api=api)
        assert r["ok"] and "@frayz_lis_bot" in r["message"] and sent[-1]["chat_id"] == "42"
        r = await tgcheck.check(TOKEN, "111", "LisBot", api=api)
        assert not r["ok"] and "Start" in r["message"]
        r = await tgcheck.check(TOKEN[:-1] + "x", "42", api=api)
        assert not r["ok"] and "токен" in r["message"]
        hook["url"] = "https://example.com/hook"
        r = await tgcheck.check(TOKEN, "42", api=api)
        assert not r["ok"] and "вебхук" in r["message"]
        await srv.close()
    asyncio.run(go())
