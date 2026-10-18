"""Веб-интерфейс приложения: мастер настройки, управление ботом, мини-CRM, аналитика, алерты, журнал."""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import time
from pathlib import Path

import jinja2
from aiohttp import web
from yarl import URL

from mannbot.analytics.analytics import fmt_dur
from mannbot.app import schema
from mannbot.app.supervisor import parse_mafile
from mannbot.core import messages as M
from mannbot.core.rtba import price_floor
from mannbot.core.models import OrderStatus as S, TradeOfferState
from mannbot.web import charts

# правила RTBA, которые центр может менять через /api/rtba/config (ключ -> тип)
RTBA_EDITABLE = {"enabled": "bool", "strategy": "strategy", "target_position": "int", "undercut_abs": "float",
                 "undercut_pct": "float", "cost_per_key": "float", "min_margin_pct": "float", "floor_price": "float",
                 "ceiling_price": "float", "funpay_fee_pct": "float", "min_change": "float",
                 "max_updates_per_hour": "int", "scarcity_premium_pct": "float", "raise_gap_pct": "float"}
TPL_DIR = Path(__file__).parent / "templates"
STATUS_COLORS = {"new": "gray", "awaiting_link": "blue", "pending_approval": "amber", "ready_to_send": "blue",
                 "offer_sent": "blue", "escrow": "amber", "delivered": "green", "completed": "green",
                 "refunded": "red", "manual": "amber", "blocked": "red"}
STATE_TEXT = {"stopped": ("Остановлен", "gray"), "starting": ("Запускается…", "amber"),
              "running": ("Работает", "green"), "error": ("Ошибка", "red")}


def _ts(v) -> str:
    if not v:
        return "—"
    return dt.datetime.fromtimestamp(float(v)).strftime("%d.%m %H:%M:%S")


def _deep_merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _deep_merge(a[k], v)
        else:
            a[k] = v
    return a


def make_app(sup, log_handler=None) -> web.Application:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TPL_DIR), autoescape=True)
    env.filters.update(ts=_ts, dur=fmt_dur, st=lambda s: M.status_text("ru", s),
                       jsonload=lambda s: json.loads(s or "{}"), tojson=lambda o: json.dumps(o, ensure_ascii=False))
    env.filters["money"] = lambda v: "—" if v is None else f"{float(v):,.2f}".replace(",", " ").replace(".00", "")
    env.filters["md"] = lambda d: str(d)[5:]
    env.filters["hm"] = lambda v: dt.datetime.fromtimestamp(float(v)).strftime("%d.%m %H:%M")
    env.globals.update(colors=STATUS_COLORS, statuses=[s.value for s in S], charts=charts,
                       offer_states={t.value: t.name for t in TradeOfferState}, state_text=STATE_TEXT)

    def token() -> str:
        return str(sup.cfg.dashboard.token or "")

    def render(name: str, request: web.Request | None = None, **ctx) -> web.Response:
        db = sup.db
        ctx.setdefault("app", sup.status())
        ctx.setdefault("paused", sup.engine.paused)
        ctx.setdefault("dry_run", sup.cfg.general.dry_run)
        ctx.setdefault("alerts_open", db.scalar("SELECT COUNT(*) FROM alerts WHERE acknowledged=0 AND level!='INFO'"))
        ctx.setdefault("flash", request.query.get("msg", "") if request is not None else "")
        ctx.setdefault("path", request.path if request is not None else "")
        return web.Response(text=env.get_template(name).render(**ctx), content_type="text/html")

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path in ("/login", "/api/ping"):
            return await handler(request)
        if not token() or not hmac.compare_digest(request.cookies.get("mb_token", ""), token()):
            raise web.HTTPFound("/login")
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin.split("//")[-1] != request.host:
                raise web.HTTPForbidden(text="bad origin")
        return await handler(request)

    def _logged_in(target: str = "/") -> web.HTTPFound:
        resp = web.HTTPFound(target)
        resp.set_cookie("mb_token", token(), httponly=True, samesite="Strict", max_age=90 * 86400)
        return resp

    async def login(request: web.Request):
        given = request.query.get("token", "")
        if request.method == "POST":
            given = str((await request.post()).get("token", ""))
        if given:
            if token() and hmac.compare_digest(given, token()):
                raise _logged_in("/" if sup.configured else "/setup")
            await asyncio.sleep(1)
        return render("login.html", request, alerts_open=0)

    async def ping(request: web.Request):
        return web.json_response({"app": "mannbot", "ok": True})

    def back(request: web.Request, msg: str, default: str = "/") -> web.HTTPFound:
        ref = request.headers.get("Referer")
        path = URL(ref).path if ref else default
        return web.HTTPFound(URL(path).with_query(msg=msg))

    # ------------------------------------------------------------------ дашборд и CRM
    async def dashboard(request: web.Request):
        if not sup.configured and not sup.status()["demo"] and not sup.db.scalar("SELECT COUNT(*) FROM orders"):
            raise web.HTTPFound("/setup")
        a, db = sup.analytics, sup.db
        days = float(request.query.get("days", 7))
        return render("dashboard.html", request, s=a.summary(days), days=days, funnel=a.funnel(days),
                      series=a.daily_series(30), heat=a.hourly_heatmap(30), fails=a.failure_reasons(30),
                      recs=db.q("SELECT * FROM recommendations WHERE active=1 ORDER BY severity, ts DESC"),
                      active=db.active_orders()[:15], status=sup.engine.status_text())

    async def orders(request: web.Request):
        st = request.query.get("status", "")
        qtext = request.query.get("q", "").strip()
        sql, params = "SELECT * FROM orders WHERE 1=1", []
        if st:
            sql += " AND status=?"
            params.append(st)
        if qtext:
            sql += " AND (order_id LIKE ? OR buyer_username LIKE ? OR steam_id64 LIKE ?)"
            params += [f"%{qtext}%"] * 3
        return render("orders.html", request, rows=sup.db.q(sql + " ORDER BY created_at DESC LIMIT 300", params),
                      st=st, q=qtext)

    async def order(request: web.Request):
        db = sup.db
        oid = request.match_info["oid"]
        o = db.get_order(oid)
        if not o:
            raise web.HTTPNotFound()
        return render("order.html", request, o=o, events=db.events(oid),
                      msgs=db.q("SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 80", (o["chat_id"],)),
                      offers=db.q("SELECT * FROM offers WHERE order_id=? ORDER BY created_at", (oid,)),
                      customer=db.customer(o["buyer_id"]), risk=json.loads(o["risk_reasons"] or "[]"))

    async def order_action(request: web.Request):
        engine, db = sup.engine, sup.db
        oid = request.match_info["oid"]
        data = await request.post()
        act = data.get("action")
        needs_bot = act in ("approve", "resend", "refund", "delivered", "message")
        if needs_bot and sup.state != "running" and not sup.status()["demo"]:
            raise web.HTTPFound(f"/orders/{oid}?msg=Сначала запустите бота")
        if act == "approve":
            msg = await engine.admin_approve(oid)
        elif act == "reject":
            msg = await engine.admin_reject(oid)
        elif act == "resend":
            msg = await engine.admin_resend(oid)
        elif act == "refund":
            msg = await engine.admin_refund(oid, "dashboard")
        elif act == "delivered":
            msg = await engine.admin_mark_delivered(oid)
        elif act == "message" and data.get("text"):
            msg = await engine.admin_message(oid, str(data["text"])[:1500])
        elif act == "note":
            db.update_order(oid, notes=str(data.get("text", ""))[:2000])
            msg = "заметка сохранена"
        else:
            msg = "неизвестное действие"
        db.add_event(oid, "dashboard_action", action=act, result=msg)
        raise web.HTTPFound(f"/orders/{oid}?msg={msg}")

    async def customers(request: web.Request):
        return render("customers.html", request, rows=sup.db.q("SELECT * FROM customers ORDER BY last_seen DESC LIMIT 300"))

    async def customer_action(request: web.Request):
        data = await request.post()
        sup.engine.admin_blacklist(int(request.match_info["cid"]), data.get("action") == "blacklist",
                                   str(data.get("note", "")))
        raise web.HTTPFound("/customers")

    # JSON для CRM центра: заметки к заказу, заметки/теги/чёрный список покупателя
    async def api_order(request: web.Request):
        oid = request.match_info["oid"]
        if not sup.db.get_order(oid):
            return web.json_response({"ok": False, "error": "заказ не найден"}, status=404)
        data = await request.json()
        if not isinstance(data.get("notes"), str):
            return web.json_response({"ok": False, "error": "нужно поле notes"}, status=400)
        sup.db.update_order(oid, notes=data["notes"][:2000])
        sup.db.add_event(oid, "dashboard_action", action="note", result="заметка сохранена", by=data.get("by", "api"))
        return web.json_response({"ok": True})

    async def api_customer(request: web.Request):
        cid = int(request.match_info["cid"])
        data = await request.json()
        text = lambda k, n: data[k][:n] if isinstance(data.get(k), str) else None  # noqa: E731
        if not sup.db.update_customer(cid, notes=text("notes", 2000), tags=text("tags", 200)):
            return web.json_response({"ok": False, "error": "покупатель не найден"}, status=404)
        if isinstance(data.get("blacklisted"), bool):
            sup.engine.admin_blacklist(cid, data["blacklisted"])
        return web.json_response({"ok": True})

    async def pricing(request: web.Request):
        db, a = sup.db, sup.analytics
        snap = db.one("SELECT * FROM competitor_snapshots ORDER BY id DESC LIMIT 1")
        return render("pricing.html", request, series=a.price_series(300), stock=a.stock_series(300),
                      history=db.q("SELECT * FROM price_history ORDER BY id DESC LIMIT 50"),
                      comps=json.loads(snap["data"]) if snap else [], snap_ts=snap["ts"] if snap else None,
                      rtba=sup.cfg.rtba, markup=sup.engine.rtba.markup)

    async def alerts(request: web.Request):
        return render("alerts.html", request, rows=sup.db.q("SELECT * FROM alerts ORDER BY id DESC LIMIT 300"))

    async def alerts_ack(request: web.Request):
        sup.db.exec("UPDATE alerts SET acknowledged=1")
        raise web.HTTPFound("/alerts")

    # ------------------------------------------------------------------ управление ботом
    async def control(request: web.Request):
        act = (await request.post()).get("action")
        if act == "start":
            msg = await sup.start()
        elif act == "stop":
            msg = await sup.stop()
        elif act == "restart":
            msg = await sup.restart()
        elif act in ("pause", "resume"):
            msg = await sup.engine.handle_command(act, [])
        elif act == "dry_off":
            sup.save({"general": {"dry_run": False}})
            msg = "Боевой режим включён" + (" — перезапустите бота" if sup.restart_required else "")
        elif act == "dry_on":
            sup.save({"general": {"dry_run": True}})
            msg = "Тестовый режим включён" + (" — перезапустите бота" if sup.restart_required else "")
        elif act == "quit" and sup.quit_event is not None:
            asyncio.get_running_loop().call_later(0.5, sup.quit_event.set)
            return web.Response(text="<html><body style='font:16px system-ui;padding:40px'>MannBot закрыт. "
                                     "Это окно можно закрыть.</body></html>", content_type="text/html")
        else:
            msg = "?"
        raise back(request, msg)

    async def api_status(request: web.Request):
        return web.json_response({**sup.status(), "summary": sup.analytics.summary(1)})

    # ------------------------------------------------------------------ RTBA для центра (JSON)
    async def api_rtba(request: web.Request):
        db, rt = sup.db, sup.engine.rtba
        snap = db.one("SELECT * FROM competitor_snapshots ORDER BY id DESC LIMIT 1")
        hist = db.q("SELECT * FROM price_history ORDER BY id DESC LIMIT ?", (int(request.query.get("limit", 100)),))
        since = time.time() - 86400
        return web.json_response({
            "config": {k: sup.cfg.rtba.get(k) for k in RTBA_EDITABLE} | {"enabled": bool(sup.cfg.rtba.enabled)},
            "markup": rt.markup, "floor": price_floor(sup.cfg.rtba), "updates_last_hour": len(rt.updates),
            "state": sup.state, "restart_required": sup.restart_required, "dry_run": bool(sup.cfg.general.dry_run),
            "stock": db.kv_get("last_stock"), "lots": [l.get("offer_id") for l in sup.cfg.funpay.lots or []],
            "history": [dict(r) for r in hist],
            "day": dict(db.one("SELECT COUNT(*) checks, COALESCE(SUM(applied),0) applied FROM price_history WHERE ts>=?",
                               (since,)) or {}),
            "competitors": json.loads(snap["data"]) if snap else [], "competitors_ts": snap["ts"] if snap else None,
        })

    async def api_rtba_config(request: web.Request):
        """Изменение правил RTBA из центра. Работающему боту правила применяются сразу, без перезапуска."""
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"ok": False, "error": "нужен JSON"}, status=400)
        clean, errors = {}, []
        for k, v in (data.get("changes") or {}).items():
            kind = RTBA_EDITABLE.get(k)
            if kind is None:
                errors.append(f"{k}: нельзя менять")
            elif kind == "strategy":
                if v not in ("top1", "topN", "median"):
                    errors.append(f"{k}: top1, topN или median")
                else:
                    clean[k] = v
            elif kind == "bool":
                clean[k] = bool(v)
            else:
                try:
                    num = int(v) if kind == "int" else float(v)
                except (TypeError, ValueError):
                    errors.append(f"{k}: не число")
                    continue
                if num < 0 or (k.endswith("_pct") and num > 100):
                    errors.append(f"{k}: вне допустимого диапазона")
                else:
                    clean[k] = num
        if errors:
            return web.json_response({"ok": False, "error": "; ".join(errors)}, status=400)
        if not clean:
            return web.json_response({"ok": True, "applied": {}})
        running = sup.state == "running"
        sup.save({"rtba": clean}, rebuild=not running)
        if running:
            sup.engine.rtba.cfg.update(clean)      # тот же объект читают RTBAEngine и аналитика бота
        db_note = ", ".join(f"{k}={v}" for k, v in clean.items())
        sup.db.add_event("-", "rtba_config", source="center", changes=db_note)
        return web.json_response({"ok": True, "applied": clean, "live": running})

    # ------------------------------------------------------------------ мастер настройки
    async def setup(request: web.Request):
        cfg = sup.cfg
        if request.method == "POST":
            f = await request.post()
            lots = []
            for i in range(5):
                oid = str(f.get(f"lot_offer_{i}", "")).strip()
                if oid.isdigit():
                    lots.append({"offer_id": int(oid), "node_id": int(f.get(f"lot_node_{i}") or 1808),
                                 "game_id": int(f.get(f"lot_game_{i}") or 435),
                                 "keys_per_unit": int(f.get(f"lot_kpu_{i}") or 1)})
            chat_ids = [int(x) for x in str(f.get("tg_chat_ids", "")).replace(",", " ").split() if x.lstrip("-").isdigit()]
            upd = {
                "general": {"dry_run": f.get("dry_run") == "on", "autostart": f.get("autostart") == "on",
                            "language": f.get("language", "ru")},
                "funpay": {"golden_key": str(f.get("golden_key", "")).strip(), "lots": lots},
                "steam": {k: str(f.get(k, "")).strip() for k in
                          ("username", "password", "shared_secret", "identity_secret", "steam_id",
                           "login_secure_cookie")},
                "notifications": {"telegram": {"enabled": bool(f.get("tg_token") or sup.secret_is_set(
                    "notifications.telegram", "bot_token")) and bool(chat_ids),
                    "bot_token": str(f.get("tg_token", "")).strip(), "admin_chat_ids": chat_ids}},
            }
            if str(f.get("cost_per_key", "")).strip():
                upd["rtba"] = {"cost_per_key": float(str(f["cost_per_key"]).replace(",", "."))}
            sup.save(upd)
            if sup.configured:
                raise web.HTTPFound("/?msg=Настройки сохранены. Нажмите «Запустить бота»")
            raise web.HTTPFound("/setup?msg=Сохранено. Осталось: " + "; ".join(sup.problems))
        lots = list(cfg.funpay.lots or [])
        lots = [l for l in lots if l.get("offer_id")] + [{}] * max(1, 3 - len([l for l in lots if l.get("offer_id")]))
        return render("setup.html", request, cfg=cfg, lots=lots, problems=sup.problems,
                      secret=lambda s, k: sup.secret_is_set(s, k))

    async def setup_mafile(request: web.Request):
        reader = await request.multipart()
        part = await reader.next()
        if part is None:
            return web.json_response({"ok": False, "text": "файл не получен"})
        content = await part.read(decode=False)
        try:
            fields = parse_mafile(content)
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "text": f"maFile не распознан: {e}"})
        sup.save({"steam": fields})
        return web.json_response({"ok": True, "text": f"maFile загружен: {fields.get('username', '')} "
                                                      f"{fields.get('steam_id', '')}", "fields": {
            "username": fields.get("username", ""), "steam_id": fields.get("steam_id", "")}})

    async def api_test(request: web.Request):
        what = request.match_info["what"]
        data = await request.json() if request.can_read_body else {}
        if what == "funpay":
            res = await sup.run_test(sup.test_funpay(data.get("golden_key") or None), 25)
        elif what == "steam":
            res = await sup.run_test(sup.test_steam({k: data.get(k) for k in ("username", "password", "shared_secret",
                                                                  "identity_secret", "steam_id",
                                                                  "login_secure_cookie")}))
        elif what == "telegram":
            ids = [int(x) for x in str(data.get("chat_ids", "")).replace(",", " ").split() if x.lstrip("-").isdigit()]
            res = await sup.run_test(sup.test_telegram(data.get("token") or None, ids or None), 20)
        else:
            res = {"ok": False, "text": "?"}
        return web.json_response(res)

    # ------------------------------------------------------------------ настройки
    async def settings(request: web.Request):
        if request.method == "POST":
            f = await request.post()
            upd: dict = {}
            for section, fields in schema.SETTINGS:
                _deep_merge(upd, schema.parse_form(f, section, fields))
            sup.save(upd)
            raise web.HTTPFound("/settings?msg=" + ("Сохранено. Перезапустите бота, чтобы применить"
                                                    if sup.restart_required else "Сохранено"))
        return render("settings.html", request, sections=schema.SETTINGS, titles=schema.SECTION_TITLES,
                      val=lambda s, k: schema.get_value(sup.cfg, s, k),
                      secret=lambda s, k: sup.secret_is_set(s, k), problems=sup.problems,
                      config_path=str(sup.config_path))

    # ------------------------------------------------------------------ JSON-настройки для центра
    def _coerce(typ: str, key: str, v):
        if typ == "bool":
            return bool(v)
        if typ == "int":
            return int(float(v or 0))
        if typ == "float":
            return float(str(v or 0).replace(",", "."))
        if typ == "list":
            items = v if isinstance(v, list) else str(v or "").replace(",", "\n").splitlines()
            items = [str(x).strip() for x in items if str(x).strip()]
            return [int(x) if key == "admin_chat_ids" and x.lstrip("-").isdigit() else x for x in items]
        if typ.startswith("choice:") and str(v) not in typ[7:].split("|"):
            raise ValueError(f"{key}: допустимо {typ[7:]}")
        return str(v if v is not None else "").strip()

    async def api_settings(request: web.Request):
        """Те же поля, что на странице «Настройки», в JSON: центр показывает и меняет их у себя.
        Секреты не отдаются — только признак, задан ли секрет. Пустой секрет при сохранении = «не менять»."""
        if request.method == "POST":
            data = await request.json()
            values = data.get("values") or {}
            upd: dict = {}
            errors = []
            for section, fields in schema.SETTINGS:
                got = values.get(section) or {}
                for key, _label, typ, _help in fields:
                    if key not in got:
                        continue
                    try:
                        val = _coerce(typ, key, got[key])
                    except (TypeError, ValueError) as e:
                        errors.append(f"{section}.{key}: {e}")
                        continue
                    node = upd
                    for part in section.split("."):
                        node = node.setdefault(part, {})
                    node[key] = val
            if isinstance(data.get("lots"), list):
                lots = []
                for l in data["lots"]:
                    try:
                        if str(l.get("offer_id", "")).strip().isdigit():
                            lots.append({"offer_id": int(l["offer_id"]), "node_id": int(l.get("node_id") or 1808),
                                         "game_id": int(l.get("game_id") or 435),
                                         "keys_per_unit": int(l.get("keys_per_unit") or 1)})
                    except (TypeError, ValueError):
                        errors.append(f"лот {l.get('offer_id')}: неверные числа")
                upd.setdefault("funpay", {})["lots"] = lots
            if errors:
                return web.json_response({"ok": False, "error": "; ".join(errors)}, status=400)
            sup.save(upd)
            return web.json_response({"ok": True, "restart_required": sup.restart_required,
                                      "configured": sup.configured, "problems": sup.problems})
        sections = []
        for section, fields in schema.SETTINGS:
            out = []
            for key, label, typ, hlp in fields:
                f = {"key": key, "label": label, "type": typ, "help": hlp}
                if typ == "secret":
                    f["set"] = sup.secret_is_set(section, key)
                else:
                    f["value"] = schema.get_value(sup.cfg, section, key)
                out.append(f)
            sections.append({"id": section, "title": schema.SECTION_TITLES.get(section, section), "fields": out})
        lots = [{k: l.get(k) for k in ("offer_id", "node_id", "game_id", "keys_per_unit")}
                for l in (sup.cfg.funpay.lots or []) if l.get("offer_id")]
        return web.json_response({"sections": sections, "lots": lots, "problems": sup.problems,
                                  "configured": sup.configured, "restart_required": sup.restart_required,
                                  "state": sup.state})

    # ------------------------------------------------------------------ сообщения
    async def messages_page(request: web.Request):
        lang = request.query.get("lang", "ru") if request.query.get("lang") in ("ru", "en") else "ru"
        errors: dict[str, str] = {}
        if request.method == "POST":
            f = await request.post()
            lang = f.get("lang", "ru")
            current = {l: dict(v) for l, v in (sup.cfg.get("messages") or {}).items()}
            mine = current.setdefault(lang, {})
            for key in M.TEMPLATES[lang]:
                text = str(f.get(key, "")).replace("\r\n", "\n")
                if f.get(f"reset_{key}") or not text.strip() or text == M.TEMPLATES[lang][key]:
                    mine.pop(key, None)
                    continue
                err = M.validate_template(text)
                if err:
                    errors[key] = err
                else:
                    mine[key] = text
            sup.save({"messages": current}, rebuild=False)
            if not errors:
                raise web.HTTPFound(f"/messages?lang={lang}&msg=Тексты сохранены и уже действуют")
        items = [(k, schema.MESSAGE_TITLES.get(k, k), M.template(lang, k), k in M.OVERRIDES.get(lang, {}))
                 for k in M.TEMPLATES[lang]]
        return render("messages.html", request, items=items, lang=lang, errors=errors,
                      vars=sorted(M.TEMPLATE_VARS))

    async def preview_message(request: web.Request):
        data = await request.json()
        err = M.validate_template(data.get("text", ""))
        if err:
            return web.json_response({"ok": False, "text": err})
        return web.json_response({"ok": True, "text": data["text"].format(**M.TEMPLATE_VARS)})

    # ------------------------------------------------------------------ журнал
    async def logs(request: web.Request):
        return render("logs.html", request)

    async def api_logs(request: web.Request):
        after = int(request.query.get("after", 0))
        lines = [l for l in (log_handler.lines if log_handler else []) if l["n"] > after]
        return web.json_response({"lines": lines[-500:], "state": sup.status()})

    app = web.Application(middlewares=[auth], client_max_size=2 * 1024 * 1024)
    app.add_routes([
        web.get("/login", login), web.post("/login", login), web.get("/api/ping", ping),
        web.get("/", dashboard), web.get("/orders", orders), web.get("/orders/{oid}", order),
        web.post("/orders/{oid}", order_action), web.get("/customers", customers),
        web.post("/customers/{cid}", customer_action), web.post("/api/orders/{oid}", api_order),
        web.post("/api/customers/{cid:\\d+}", api_customer), web.get("/pricing", pricing), web.get("/alerts", alerts),
        web.post("/alerts/ack", alerts_ack), web.post("/control", control), web.get("/api/status", api_status),
        web.get("/api/summary", api_status), web.get("/api/settings", api_settings),
        web.post("/api/settings", api_settings),
        web.get("/api/summary", api_status), web.get("/api/rtba", api_rtba), web.post("/api/rtba/config", api_rtba_config),
        web.get("/setup", setup), web.post("/setup", setup), web.post("/setup/mafile", setup_mafile),
        web.post("/api/test/{what}", api_test), web.get("/settings", settings), web.post("/settings", settings),
        web.get("/messages", messages_page), web.post("/messages", messages_page),
        web.post("/api/preview", preview_message), web.get("/logs", logs), web.get("/api/logs", api_logs),
    ])
    return app


async def start_web(sup, host: str, port: int, log_handler=None) -> web.AppRunner:
    runner = web.AppRunner(make_app(sup, log_handler), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
