"""Общая веб-панель центра (aiohttp): сводка по всем ботам, управление процессами и торговлей,
журналы, общая лента тревог, настройка LisBot. Полные панели ботов открываются по ссылке
«Открыть панель» с автоматическим входом.
"""
from __future__ import annotations

import asyncio
import hmac
from pathlib import Path

from aiohttp import web

from center.app import Center
from center.bots import LIS_SECRET_KEYS, LisBotWorker

STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "center_token"


def make_app(center: Center) -> web.Application:
    token = center.cfg.ensure_token

    def authed(request: web.Request) -> bool:
        t = token()
        given = request.cookies.get(COOKIE, "") or request.headers.get("Authorization", "").removeprefix("Bearer ")
        return bool(t) and hmac.compare_digest(given, t)

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path in ("/login", "/api/ping") or request.path.startswith("/static/"):
            return await handler(request)
        if not authed(request):
            if request.path.startswith("/api/"):
                return web.json_response({"error": "нужна авторизация"}, status=401)
            raise web.HTTPFound("/login")
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin.split("//")[-1] != request.host:
                raise web.HTTPForbidden(text="bad origin")
        resp = await handler(request)
        return resp

    async def login(request: web.Request):
        given = request.query.get("token", "")
        if request.method == "POST":
            given = str((await request.post()).get("token", ""))
        if given:
            if hmac.compare_digest(given, token()):
                resp = web.HTTPFound("/")
                resp.set_cookie(COOKIE, token(), httponly=True, samesite="Strict", max_age=90 * 86400)
                raise resp
            await asyncio.sleep(1)
        return web.FileResponse(STATIC / "login.html")

    async def index(request: web.Request):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    async def ping(request: web.Request):
        return web.json_response({"app": "center", "ok": True})

    async def overview(request: web.Request):
        return web.json_response(center.overview())

    def worker(request: web.Request):
        name = request.match_info["name"]
        if name not in center.workers:
            raise web.HTTPNotFound()
        return center.workers[name]

    async def worker_action(request: web.Request):
        w = worker(request)
        action = request.match_info["action"]
        if action == "start":
            msg = await w.start("панель центра")
        elif action == "stop":
            msg = await w.stop("панель центра")
        elif action == "restart":
            msg = await w.restart("панель центра")
        elif action == "install" and isinstance(w, LisBotWorker):
            msg = await w.install_deps()
        elif action in w.actions:
            if w.state != "running":
                return web.json_response({"ok": False, "message": f"{w.title} не запущен"}, status=409)
            try:
                msg = await w.control(action)
            except Exception as e:  # noqa: BLE001
                return web.json_response({"ok": False, "message": f"бот не ответил: {e}"}, status=502)
            asyncio.get_running_loop().call_later(1, lambda: asyncio.ensure_future(center.refresh(w.name)))
        else:
            return web.json_response({"ok": False, "message": "неизвестное действие"}, status=400)
        center.events.add(w.name, "info", "action", f"{w.title}: {action} из панели центра — {msg}")
        return web.json_response({"ok": True, "message": msg})

    async def worker_logs(request: web.Request):
        w = worker(request)
        return web.json_response(w.logs(int(request.query.get("after", 0)), int(request.query.get("limit", 500))))

    async def open_panel(request: web.Request):
        w = worker(request)
        if w.state != "running":
            raise web.HTTPFound("/?msg=" + f"{w.title} не запущен")
        raise web.HTTPFound(w.panel_url())

    async def alerts(request: web.Request):
        return web.json_response(center.alerts(int(request.query.get("limit", 100))))

    async def events(request: web.Request):
        return web.json_response(center.events.recent(int(request.query.get("limit", 200)), request.query.get("source")))

    async def lis_secrets(request: web.Request):
        w: LisBotWorker = center.workers["lisbot"]  # type: ignore[assignment]
        if request.method == "POST":
            data = await request.json()
            w.save_secrets({k: data.get(k) for k in LIS_SECRET_KEYS})
            center.events.add("lisbot", "info", "settings", "Подключения LisBot изменены из панели центра")
            msg = "Сохранено"
            if data.get("restart"):
                msg += ". " + await w.restart("новые подключения")
            return web.json_response({"ok": True, "message": msg})
        return web.json_response(w.masked_secrets())

    async def quit_center(request: web.Request):
        asyncio.get_running_loop().call_later(0.5, center.quit_event.set)
        return web.json_response({"ok": True, "message": "Центр завершает работу"})

    app = web.Application(middlewares=[auth])
    app.add_routes([
        web.get("/login", login), web.post("/login", login), web.get("/api/ping", ping), web.get("/", index),
        web.get("/api/overview", overview), web.post("/api/workers/{name}/{action}", worker_action),
        web.get("/api/workers/{name}/logs", worker_logs), web.get("/open/{name}", open_panel),
        web.get("/api/alerts", alerts), web.get("/api/events", events),
        web.get("/api/lisbot/secrets", lis_secrets), web.post("/api/lisbot/secrets", lis_secrets),
        web.post("/api/quit", quit_center),
    ])
    app.router.add_static("/static/", STATIC)
    return app


async def start_web(center: Center, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(center), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
