"""Общая веб-панель центра (aiohttp): сводка по всем ботам, управление процессами и торговлей,
журналы, общая лента тревог, настройка LisBot. Полные панели ботов открываются по ссылке
«Открыть панель» с автоматическим входом.
"""
from __future__ import annotations

import asyncio
import hmac
import time
from pathlib import Path

from aiohttp import web

from center.app import Center
from center.bots import LIS_SECRET_KEYS, LisBotWorker, MannBotWorker, parse_mafile
from center.crm import CrmError
from center.backup import routes as backup_routes
from center.coordinator import routes as coordinator_routes
from center.inventory import routes as inventory_routes
from center.readiness import routes as readiness_routes
from center.updates import routes as update_routes
from center.monitor import routes as monitor_routes

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

    async def rtba_page(request: web.Request):
        return web.FileResponse(STATIC / "rtba.html", headers={"Cache-Control": "no-store"})

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
            except RuntimeError as e:                    # бот ответил отказом
                return web.json_response({"ok": False, "message": str(e)}, status=502)
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
        url = w.panel_url()
        if w.bind_host not in ("127.0.0.1", "localhost"):        # Docker/VPS: тот же адрес, что у панели центра
            url = url.replace("127.0.0.1", request.url.host or "127.0.0.1", 1)
        raise web.HTTPFound(url)

    async def alerts(request: web.Request):
        return web.json_response(center.alerts(int(request.query.get("limit", 100))))

    async def events(request: web.Request):
        q = request.query
        try:
            limit = max(1, min(int(q.get("limit", 200)), 1000))
        except ValueError:
            limit = 200
        return web.json_response(center.events.recent(limit, q.get("source") or None, important=q.get("level") == "important",
                                                      q=q.get("q", "").strip()[:100]))

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

    async def mann_settings(request: web.Request):
        w: MannBotWorker = center.workers["mannbot"]  # type: ignore[assignment]
        if w.state != "running":
            return web.json_response({"ok": False, "error": "MannBot не запущен: запустите процесс, настройки "
                                      "меняются через него (торговля при этом не начнётся)"}, status=409)
        try:
            if request.method == "GET":
                return web.json_response(await w.get_settings())
            data = await request.json()
            values = data.get("values") or {}
            if data.get("mafile"):
                try:
                    values.setdefault("steam", {}).update(parse_mafile(data["mafile"]))
                except ValueError as e:
                    return web.json_response({"ok": False, "error": f"maFile не распознан: {e}"}, status=400)
            res, need_restart = await w.save_settings(values, data.get("lots"))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"MannBot не ответил: {e}"}, status=502)
        if not res.get("ok"):
            return web.json_response(res, status=400)
        center.events.add("mannbot", "info", "settings", "Настройки MannBot изменены из панели центра")
        msg = "Сохранено"
        if need_restart:
            msg += ". " + await w.restart("новые настройки")
        return web.json_response({"ok": True, "message": msg, "problems": res.get("problems", [])})
    def days_q(request: web.Request, default: float | None = 30) -> float | None:
        v = request.query.get("days", "")
        if v in ("all", "0"):
            return None
        try:
            return max(1.0, min(float(v), 3650.0)) if v else default
        except ValueError:
            return default

    async def an_pnl(request: web.Request):
        return web.json_response(await center.analytics.pnl(days_q(request)))

    async def an_daily(request: web.Request):
        cur = "USD" if request.query.get("currency", "").upper() == "USD" else "RUB"
        return web.json_response(await center.analytics.daily(int(days_q(request) or 90), cur))

    async def an_bot(request: web.Request):
        a = center.analytics
        if request.match_info["name"] == "lisbot":
            return web.json_response(await asyncio.to_thread(a.lisbot_details, days_q(request, None)))
        return web.json_response(await asyncio.to_thread(a.mannbot_details, days_q(request)))

    async def an_export(request: web.Request):
        a = center.analytics
        rows = await a.export_rows(days_q(request, None))
        fmt = request.match_info["fmt"]
        body, ctype = (await asyncio.to_thread(a.to_xlsx, rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") \
            if fmt == "xlsx" else (await asyncio.to_thread(a.to_csv, rows), "text/csv; charset=utf-8")
        name = f"center-deals-{time.strftime('%Y%m%d')}.{fmt}"
        return web.Response(body=body, content_type=ctype.split(";")[0], charset="utf-8" if fmt == "csv" else None,
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ CRM
    def crm_filters(request: web.Request) -> dict:
        q = request.query
        bot = q.get("bot", "")
        return {"q": q.get("q", "").strip()[:200], "bot": bot if bot in center.workers else "", "group": q.get("group", ""),
                "days": days_q(request, None), "dry": q.get("dry") == "1", "sort": q.get("sort", "created_at"),
                "desc": q.get("dir", "desc") != "asc"}

    def crm_int(request: web.Request, key: str, default: int) -> int:
        try:
            return max(0, int(request.query.get(key, default)))
        except ValueError:
            return default

    def crm_error(e: CrmError) -> web.Response:
        return web.json_response({"ok": False, "message": str(e)}, status=e.status)

    async def crm_deals(request: web.Request):
        return web.json_response(await asyncio.to_thread(
            lambda: center.crm.deals(**crm_filters(request), limit=crm_int(request, "limit", 100),
                                     offset=crm_int(request, "offset", 0))))

    async def crm_deal(request: web.Request):
        bot, ref = request.match_info["bot"], request.match_info["ref"]
        try:
            if request.method == "POST":
                data = await request.json()
                text = lambda k: data[k] if isinstance(data.get(k), str) else None  # noqa: E731
                return web.json_response({"ok": True, "message": await center.crm.update_deal(
                    bot, ref, notes=text("notes"), tags=text("tags"))})
            return web.json_response(await asyncio.to_thread(center.crm.deal, bot, ref))
        except CrmError as e:
            return crm_error(e)

    async def crm_export(request: web.Request):
        filters = crm_filters(request)
        body = await asyncio.to_thread(lambda: center.analytics.to_csv(center.crm.export_rows(**filters)))
        name = f"crm-deals-{time.strftime('%Y%m%d')}.csv"
        return web.Response(body=body, content_type="text/csv", charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def crm_customers_export(request: web.Request):
        q = request.query
        rows = await asyncio.to_thread(lambda: center.crm.customer_export_rows(
            q=q.get("q", "").strip()[:200], risk=q.get("risk", ""), tag=q.get("tag", ""), segment=q.get("segment", ""),
            sort=q.get("sort", "last_order")))
        name = f"crm-customers-{time.strftime('%Y%m%d')}.csv"
        return web.Response(body=center.analytics.to_csv(rows), content_type="text/csv", charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def crm_customers(request: web.Request):
        q = request.query
        return web.json_response(await asyncio.to_thread(
            lambda: center.crm.customers(q=q.get("q", "").strip()[:200], risk=q.get("risk", ""), tag=q.get("tag", ""),
                                         segment=q.get("segment", ""),
                                         sort=q.get("sort", "last_order"), limit=crm_int(request, "limit", 200),
                                         offset=crm_int(request, "offset", 0))))

    async def crm_cohorts(request: web.Request):
        return web.json_response(await asyncio.to_thread(center.crm.cohorts, crm_int(request, "months", 6)))

    async def crm_customer(request: web.Request):
        cid = int(request.match_info["cid"])
        try:
            if request.method == "POST":
                data = await request.json()
                text = lambda k: data[k] if isinstance(data.get(k), str) else None  # noqa: E731
                bl = data.get("blacklisted")
                return web.json_response({"ok": True, "message": await center.crm.update_customer(
                    cid, notes=text("notes"), tags=text("tags"), blacklisted=bl if isinstance(bl, bool) else None)})
            return web.json_response(await asyncio.to_thread(center.crm.customer, cid))
        except CrmError as e:
            return crm_error(e)
    async def rtba_status(request: web.Request):
        return web.json_response(await center.rtba.status(days_q(request, 7) or 7))

    async def rtba_budget(request: web.Request):
        if request.method == "POST":
            errors = center.rtba.save_plan(await request.json())
            if errors:
                return web.json_response({"ok": False, "message": "; ".join(errors)}, status=400)
        return web.json_response(await center.rtba.plan())

    async def rtba_apply(request: web.Request):
        data = await request.json() if request.can_read_body else {}
        res = await center.rtba.apply(data.get("keys"))
        for name in center.workers:
            asyncio.ensure_future(center.refresh(name))
        return web.json_response(res, status=200 if res["ok"] else 502)

    async def rtba_backtest(request: web.Request):
        data = await request.json()
        variants = [v for v in (data.get("variants") or []) if isinstance(v, dict)][:6]
        if data.get("bot") == "lisbot":
            res = await center.rtba.backtest_lisbot(variants, float(data.get("days") or 7))
        else:
            res = await asyncio.to_thread(center.rtba.backtest_mannbot, variants, float(data.get("days") or 30))
        return web.json_response(res)

    async def an_goal(request: web.Request):
        if request.method == "POST":
            try:
                center.analytics.save_goal((await request.json()).get("goal"))
            except (ValueError, TypeError) as e:
                return web.json_response({"ok": False, "message": str(e)}, status=400)
            center.events.add("center", "info", "settings", "Цель прибыли на месяц изменена из панели центра")
        return web.json_response(await center.analytics.goal())

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
        web.get("/api/mannbot/settings", mann_settings), web.post("/api/mannbot/settings", mann_settings),
        web.post("/api/quit", quit_center),
        web.get("/api/analytics/pnl", an_pnl), web.get("/api/analytics/daily", an_daily),
        web.get("/api/analytics/bot/{name:lisbot|mannbot}", an_bot),
        web.get("/api/analytics/goal", an_goal), web.post("/api/analytics/goal", an_goal),
        web.get("/api/analytics/export.{fmt:csv|xlsx}", an_export),
        web.get("/api/crm/deals", crm_deals), web.get("/api/crm/deals.csv", crm_export),
        web.get("/api/crm/deals/{bot:lisbot|mannbot}/{ref}", crm_deal), web.post("/api/crm/deals/{bot:lisbot|mannbot}/{ref}", crm_deal),
        web.get("/api/crm/customers", crm_customers),
        web.get("/api/crm/cohorts", crm_cohorts), web.get("/api/crm/customers.csv", crm_customers_export),
        web.get("/api/crm/customers/{cid:-?\\d+}", crm_customer), web.post("/api/crm/customers/{cid:-?\\d+}", crm_customer),
        web.get("/rtba", rtba_page), web.get("/api/rtba", rtba_status), web.get("/api/rtba/budget", rtba_budget), web.post("/api/rtba/budget", rtba_budget),
        web.post("/api/rtba/apply", rtba_apply), web.post("/api/rtba/backtest", rtba_backtest),
    ])
    app.add_routes(monitor_routes(center))
    app.add_routes(coordinator_routes(center))
    app.add_routes(backup_routes(center))
    app.add_routes(inventory_routes(center))
    app.add_routes(readiness_routes(center))
    app.add_routes(update_routes(center))
    app.router.add_static("/static/", STATIC)
    return app


async def start_web(center: Center, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(center), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
