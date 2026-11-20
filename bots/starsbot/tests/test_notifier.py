import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

from starsbot.config import load_config
from starsbot.crm.db import Database
from starsbot.notify.notifier import Notifier


def test_telegram_buttons_and_callbacks_only_from_admins():
    cfg = load_config(None, {"notifications": {"telegram": {"enabled": True, "bot_token": "T", "admin_chat_ids": [42]}}})
    sent, updates = [], [
        {"update_id": 1, "callback_query": {"id": "a", "data": "cmd:approve AB12", "message": {"chat": {"id": 42}}}},
        {"update_id": 2, "callback_query": {"id": "b", "data": "cmd:refund AB12", "message": {"chat": {"id": 42}}}},
        {"update_id": 3, "callback_query": {"id": "c", "data": "cmd:approve AB12", "message": {"chat": {"id": 7}}}},
        {"update_id": 4, "message": {"chat": {"id": 42}, "text": "/status"}},
    ]

    async def get_updates(request):
        batch, updates[:] = list(updates), []
        if not batch:
            await asyncio.sleep(0.2)
        return web.json_response({"ok": True, "result": batch})

    async def send(request):
        sent.append((request.match_info["m"], await request.json()))
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/botT/getUpdates", get_updates)
    app.router.add_post("/botT/{m}", send)

    async def go():
        server = TestServer(app)
        await server.start_server()
        n = Notifier(cfg.notifications, Database())
        n.api = str(server.make_url("")).rstrip("/")
        calls = []

        async def handler(cmd, args):
            calls.append((cmd, args))
            return "ok"
        n.command_handler = handler
        await n.notify("WARNING", "risk_review", "проверьте", order_id="AB12",
                       buttons=[("✔ Выдать", "approve AB12"), ("✋", "reject AB12")])
        task = asyncio.create_task(n.poll_commands())
        await asyncio.sleep(0.6)
        task.cancel()
        await n.close()
        await server.close()
        return calls
    calls = asyncio.run(go())
    # кнопка approve от админа выполнена; refund кнопкой нельзя; чужой чат игнорируется; команда текстом работает
    assert calls == [("approve", ["AB12"]), ("status", [])]
    first = sent[0][1]
    assert first["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "cmd:approve AB12"
    assert sum(1 for m, _ in sent if m == "answerCallbackQuery") == 3
