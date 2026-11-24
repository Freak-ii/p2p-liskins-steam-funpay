"""Маршруты раздела «Агенты»: страница /agents и API /api/agents/…; подключаются в center/web.py.

Общие для всех агентов:
    GET  /api/agents                         — карточки всех агентов (status())
    POST /api/agents/{id}/{start|stop|run}   — запустить цикл, остановить, сделать один проход сейчас
    GET  /api/agents/{id}/items?status=      — элементы агента (черновики, находки)
Агент канала:
    GET/POST /api/agents/channel/settings
    POST /api/agents/channel/items           — свой пост {text, title}
    GET  /api/agents/channel/sources?days=   — партнёры: сводка ShopBot по меткам src_
    POST /api/agents/channel/links           — ссылки с меткой для партнёра {tag}
    POST /api/agents/channel/items/{iid}/{edit|approve|reject|publish}
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from aiohttp import web

STATIC = Path(__file__).resolve().parent.parent / "static"


def routes(center) -> list:
    ag = center.agents

    def agent(request: web.Request):
        a = ag.get(request.match_info["aid"])
        if a is None:
            raise web.HTTPNotFound()
        return a

    def fail(e: Exception, status: int = 400) -> web.Response:
        return web.json_response({"ok": False, "message": str(e)}, status=status)

    async def page(request: web.Request):
        return web.FileResponse(STATIC / "agents.html", headers={"Cache-Control": "no-store"})

    async def overview(request: web.Request):
        return web.json_response({"agents": ag.overview()})

    async def action(request: web.Request):
        a = agent(request)
        verb = request.match_info["verb"]
        msg = await (a.start() if verb == "start" else a.stop() if verb == "stop" else a.run_now())
        return web.json_response({"ok": True, "message": msg, "agent": a.status()})

    async def items(request: web.Request):
        a = agent(request)
        st = request.query.get("status") or None
        try:
            limit = max(1, min(int(request.query.get("limit", 100)), 500))
        except ValueError:
            limit = 100
        return web.json_response({"items": a.items(st, limit)})

    ch = ag.get("channel")

    async def ch_settings(request: web.Request):
        if request.method == "POST":
            try:
                ch.save_settings(await request.json())
            except (ValueError, TypeError) as e:
                return fail(e)
            center.events.add("agent:channel", "info", "settings", "Настройки агента канала изменены из панели центра")
            return web.json_response({"ok": True, "message": "Настройки агента сохранены", "settings": ch.settings()})
        return web.json_response(ch.settings())

    async def ch_new(request: web.Request):
        d = await request.json()
        text = str(d.get("text") or "").strip()
        if not text:
            return fail(ValueError("пустой текст"))
        iid = ch.manual(text, str(d.get("title") or ""))
        return web.json_response({"ok": True, "message": "Черновик добавлен", "item": ch.item(iid)})

    async def ch_item(request: web.Request):
        iid, verb = int(request.match_info["iid"]), request.match_info["verb"]
        d = await request.json() if request.can_read_body else {}
        try:
            if verb == "edit":
                it = ch.edit(iid, d.get("text") if isinstance(d.get("text"), str) else None,
                             d.get("title") if isinstance(d.get("title"), str) else None)
                msg = "сохранено" + (" — есть замечания фильтра" if it.get("issues") else "")
            elif verb == "approve":
                at = None
                if d.get("at"):
                    try:
                        at = dt.datetime.fromisoformat(str(d["at"])).timestamp()
                    except ValueError as e:
                        raise ValueError("время публикации в формате ГГГГ-ММ-ДДTЧЧ:ММ") from e
                msg = await ch.approve(iid, at)
            elif verb == "reject":
                msg = ch.reject(iid)
            else:
                msg = await ch.publish(iid)
        except LookupError as e:
            return fail(e, 404)
        except ValueError as e:
            return fail(e, 409)
        return web.json_response({"ok": True, "message": msg, "item": ch.item(iid)})

    async def ch_sources(request: web.Request):
        try:
            days = max(1, min(int(request.query.get("days", 30)), 365))
        except ValueError:
            days = 30
        return web.json_response(await ch.sources(days))

    async def ch_links(request: web.Request):
        try:
            return web.json_response({"ok": True, **ch.partner_links(str((await request.json()).get("tag") or ""))})
        except ValueError as e:
            return fail(e)

    out = [
        web.get("/agents", page), web.get("/api/agents", overview),
        web.post(r"/api/agents/{aid:[a-z0-9_]+}/{verb:start|stop|run}", action),
        web.get(r"/api/agents/{aid:[a-z0-9_]+}/items", items),
    ]
    if ch is not None:
        out += [web.get("/api/agents/channel/settings", ch_settings), web.post("/api/agents/channel/settings", ch_settings),
                web.post("/api/agents/channel/items", ch_new),
                web.get("/api/agents/channel/sources", ch_sources), web.post("/api/agents/channel/links", ch_links),
                web.post(r"/api/agents/channel/items/{iid:\d+}/{verb:edit|approve|reject|publish}", ch_item)]
    for a in ag.agents.values():
        out += a.routes()
    return out
