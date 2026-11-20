"""Веб-панель StarsBot: мастер настройки, управление ботом, мини-CRM, «Деньги», RTBA, аналитика, алерты, журнал.

Роли доступа (по токену входа):
  * admin    — dashboard.token: всё, включая настройки, секреты, боевой режим;
  * operator — dashboard.operator_token: заказы, клиенты, пауза/запуск, журнал денег;
  * viewer   — dashboard.viewer_token: только просмотр.
Все изменяющие действия пишутся в журнал аудита. Realtime — Server-Sent Events на /api/stream.
"""
from __future__ import annotations

import asyncio
import csv
import io
import datetime as dt
import hmac
import json
import logging
import time
from pathlib import Path

import jinja2
from aiohttp import web
from yarl import URL

from starsbot.analytics.analytics import fmt_dur
from starsbot.app import schema
from starsbot.config import DENOMINATIONS
from starsbot.core import messages as M
from starsbot.core.models import OrderStatus as S
from starsbot.web import charts

log = logging.getLogger("starsbot.web")

TPL_DIR = Path(__file__).parent / "templates"
STATUS_COLORS = {"new": "gray", "awaiting_username": "blue", "pending_approval": "amber", "ready": "blue",
                 "purchasing": "blue", "sent": "blue", "delivered": "green", "completed": "green",
                 "refunded": "red", "manual": "amber", "blocked": "red"}
STATE_TEXT = {"stopped": ("Остановлен", "gray"), "starting": ("Запускается…", "amber"),
              "running": ("Работает", "green"), "error": ("Ошибка", "red")}
ROLE_TITLES = {"admin": "администратор", "operator": "оператор", "viewer": "наблюдатель"}
ROLE_LEVEL = {"viewer": 0, "operator": 1, "admin": 2}
CASHFLOW_KINDS = {"topup": "Пополнение кошелька TON", "withdraw_funpay": "Вывод с FunPay",
                  "withdraw_ton": "Вывод TON с кошелька", "withdraw_ozon": "Выплата с Ozon", "expense": "Расход (прочее)", "adjust": "Корректировка TON"}
COOKIE = "sb_token"
HEALTH_NAMES = {"funpay_poll": "FunPay", "ton_watch": "TON-транзакции", "wallet": "Баланс",
                "fragment_session": "Fragment"}
# сегменты мини-CRM: (код, название, цвет, SQL-условие по таблице customers)
SEGMENTS = [("new", "новый", "gray", "orders_count<=1"),
            ("repeat", "постоянный", "green", "(completed_count>=2 OR orders_count>=3)"),
            ("vip", "VIP", "blue", "total_spent>=5000"),
            ("refunds", "были возвраты", "amber", "refunded_count>0"),
            ("blacklist", "ЧС", "red", "blacklisted=1")]
SEGMENT_SQL = {code: cond for code, _, _, cond in SEGMENTS}


def segments_of(c) -> list[tuple[str, str]]:
    out = []
    if c["blacklisted"]:
        out.append(("ЧС", "red"))
    if c["total_spent"] >= 5000:
        out.append(("VIP", "blue"))
    if c["completed_count"] >= 2 or c["orders_count"] >= 3:
        out.append(("постоянный", "green"))
    elif c["orders_count"] <= 1:
        out.append(("новый", "gray"))
    if c["refunded_count"]:
        out.append(("были возвраты", "amber"))
    return out
ROLE = web.RequestKey("role", str) if hasattr(web, "RequestKey") else "role"


def _ts(v) -> str:
    if not v:
        return "—"
    return dt.datetime.fromtimestamp(float(v)).strftime("%d.%m %H:%M:%S")


def _num(v, default: float | None = 0.0) -> float | None:
    s = str(v if v is not None else "").strip().replace(",", ".").replace(" ", "")
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


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
    def _fnum(v):
        # None, пустая строка и неопределённое поле (кошелёк ещё ни разу не проверялся) — «нет данных»
        if v is None or isinstance(v, jinja2.Undefined) or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    env.filters["money"] = lambda v: "—" if _fnum(v) is None else f"{_fnum(v):,.2f}".replace(",", " ").replace(".00", "")
    env.filters["ton"] = lambda v: "—" if _fnum(v) is None else f"{_fnum(v):.4f}".rstrip("0").rstrip(".")
    env.filters["md"] = lambda d: str(d)[5:]
    env.filters["hm"] = lambda v: dt.datetime.fromtimestamp(float(v)).strftime("%d.%m %H:%M")
    env.globals.update(colors=STATUS_COLORS, statuses=[s.value for s in S], charts=charts, state_text=STATE_TEXT,
                       role_titles=ROLE_TITLES, cashflow_kinds=CASHFLOW_KINDS,
                       segments_of=segments_of)

    def friendly(path: str):
        # ошибка FunPay/сети/Telegram в кнопке панели — сообщение на странице, а не 500
        def deco(fn):
            async def wrapper(request: web.Request):
                try:
                    return await fn(request)
                except web.HTTPException:
                    raise
                except Exception as e:                       # noqa: BLE001
                    log.exception("panel action %s failed", path)
                    raise web.HTTPFound(URL(path).with_query(msg=f"Ошибка: {e}"[:600]))
            return wrapper
        return deco

    def wallet_kv() -> dict:
        # до первой проверки кошелька в базе ничего нет — шаблоны получают поля со значением None
        return {"balance": None, "capacity": None, "committed": None, "ts": None, **(sup.db.kv_get("wallet") or {})}

    # ------------------------------------------------------------------ доступ
    def tokens() -> dict[str, str]:
        d = sup.cfg.dashboard
        out = {"admin": str(d.token or "")}
        for role, key in (("operator", "operator_token"), ("viewer", "viewer_token")):
            t = str(d.get(key) or "")
            if len(t) >= 12:
                out[role] = t
        return out

    def role_for(given: str) -> str | None:
        if not given:
            return None
        for role, t in tokens().items():
            if t and hmac.compare_digest(given, t):
                return role
        return None

    def need(request: web.Request, role: str) -> None:
        if ROLE_LEVEL[request[ROLE]] < ROLE_LEVEL[role]:
            raise web.HTTPForbidden(text=f"Недостаточно прав: нужна роль «{ROLE_TITLES[role]}»")

    def audit(request: web.Request, action: str, target: str = "", result: str = "") -> None:
        sup.db.audit(request[ROLE], action, target, str(result)[:300])

    def render(name: str, request: web.Request | None = None, **ctx) -> web.Response:
        db = sup.db
        ctx.setdefault("app", sup.status())
        ctx.setdefault("paused", sup.engine.paused)
        ctx.setdefault("dry_run", sup.cfg.general.dry_run)
        ctx.setdefault("alerts_open", db.scalar("SELECT COUNT(*) FROM alerts WHERE acknowledged=0 AND level!='INFO'"))
        ctx.setdefault("flash", request.query.get("msg", "") if request is not None else "")
        ctx.setdefault("path", request.path if request is not None else "")
        role = request.get(ROLE) if request is not None else None
        ctx.setdefault("role", role)
        ctx.setdefault("can_op", bool(role) and ROLE_LEVEL[role] >= 1)
        ctx.setdefault("is_admin", role == "admin")
        ctx.setdefault("ozon_on", "ozon" in sup.engine.channels)
        return web.Response(text=env.get_template(name).render(**ctx), content_type="text/html")

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path in ("/login", "/api/ping"):
            return await handler(request)
        role = role_for(request.cookies.get(COOKIE, ""))
        if not role:
            if request.path.startswith("/api/"):
                raise web.HTTPUnauthorized(text="login required")
            raise web.HTTPFound("/login")
        request[ROLE] = role
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin.split("//")[-1] != request.host:
                raise web.HTTPForbidden(text="bad origin")
            if request.path != "/logout":
                need(request, "operator" if request.path in OPERATOR_POST or
                     request.path.startswith(("/orders/", "/customers/")) else "admin")
        return await handler(request)

    async def login(request: web.Request):
        given = request.query.get("token", "")
        if request.method == "POST":
            given = str((await request.post()).get("token", ""))
        if given:
            role = role_for(given)
            if role:
                sup.db.audit(role, "login", "", "ok")
                target = "/setup" if role == "admin" and not sup.configured and not sup.status()["demo"] else "/"
                resp = web.HTTPFound(target)
                resp.set_cookie(COOKIE, given, httponly=True, samesite="Strict", max_age=90 * 86400)
                raise resp
            await asyncio.sleep(1)
        return render("login.html", request, alerts_open=0)

    async def logout(request: web.Request):
        resp = web.HTTPFound("/login")
        resp.del_cookie(COOKIE)
        raise resp

    async def ping(request: web.Request):
        return web.json_response({"app": "starsbot", "ok": True})

    def back(request: web.Request, msg: str, default: str = "/") -> web.HTTPFound:
        ref = request.headers.get("Referer")
        path = URL(ref).path if ref else default
        return web.HTTPFound(URL(path).with_query(msg=msg))

    # ------------------------------------------------------------------ дашборд и CRM
    async def dashboard(request: web.Request):
        if not sup.configured and not sup.status()["demo"] and not sup.db.scalar("SELECT COUNT(*) FROM orders") \
                and request[ROLE] == "admin":
            raise web.HTTPFound("/setup")
        a, db = sup.analytics, sup.db
        days = _num(request.query.get("days"), 7) or 7
        return render("dashboard.html", request, s=a.summary(days), days=days, funnel=a.funnel(days),
                      series=a.daily_series(30), heat=a.hourly_heatmap(30), fails=a.failure_reasons(30),
                      denoms=a.denominations(days), cap=sup.econ.wallet_capacity(),
                      recs=db.q("SELECT * FROM recommendations WHERE active=1 ORDER BY severity, ts DESC"),
                      active=db.active_orders()[:20], status=sup.engine.status_text(),
                      fragment=sup.engine.fragment_status, wallet=wallet_kv(),
                      rate=sup.econ.effective_rate(), safety=sup.cfg.safety, live_done=sup.engine.live_purchases(),
                      spent24=sup.engine.spent_ton_24h(), cookie_age=sup.engine.fragment_cookie_age_days(),
                      warn_days=float(sup.cfg.fragment.get("session_warn_days") or 0),
                      health=sup.engine.health() if sup.state == "running" else None, health_names=HEALTH_NAMES,
                      feed=db.q("SELECT * FROM order_events WHERE order_id NOT IN ('-', '') ORDER BY id DESC LIMIT 25"))

    async def orders(request: web.Request):
        st = request.query.get("status", "")
        channel = request.query.get("channel", "")
        qtext = request.query.get("q", "").strip()
        sql, params = "SELECT * FROM orders WHERE 1=1", []
        if st:
            sql += " AND status=?"
            params.append(st)
        if channel:
            sql += " AND COALESCE(channel,'funpay')=?"
            params.append(channel)
        if qtext:
            sql += " AND (order_id LIKE ? OR buyer_username LIKE ? OR tg_username LIKE ?)"
            params += [f"%{qtext.lstrip('@#')}%"] * 3
        return render("orders.html", request, rows=sup.db.q(sql + " ORDER BY created_at DESC LIMIT 300", params),
                      st=st, q=qtext, channel=channel)

    async def order(request: web.Request):
        db = sup.db
        oid = request.match_info["oid"]
        o = db.get_order(oid)
        if not o:
            raise web.HTTPNotFound()
        flow = sup.econ.order_flow(o) if o["ton_amount"] else None
        return render("order.html", request, o=o, events=db.events(oid), flow=flow,
                      msgs=db.q("SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 80", (o["chat_id"],)),
                      purchases=db.q("SELECT * FROM purchases WHERE order_id=? ORDER BY id", (oid,)),
                      customer=db.customer(o["buyer_id"]) if o["channel"] != "ozon" else None,
                      risk=json.loads(o["risk_reasons"] or "[]"),
                      ozon_code=db.one("SELECT * FROM ozon_codes WHERE order_id=?", (oid,))
                      if o["channel"] == "ozon" and "ozon" in sup.engine.channels else None)

    async def order_action(request: web.Request):
        engine, db = sup.engine, sup.db
        oid = request.match_info["oid"]
        data = await request.post()
        act = str(data.get("action", ""))
        needs_bot = act in ("approve", "set_username", "refund", "delivered", "message")
        if needs_bot and sup.state != "running" and not sup.status()["demo"]:
            raise web.HTTPFound(URL(f"/orders/{oid}").with_query(msg="Сначала запустите бота"))
        if act in ("refund", "delivered") and request[ROLE] != "admin" and not sup.cfg.dashboard.get(
                "operator_can_refund", False):
            need(request, "admin")
        if act == "approve":
            msg = await engine.admin_approve(oid)
        elif act == "set_username":
            msg = await engine.admin_set_username(oid, str(data.get("username", "")).strip())
        elif act == "reject":
            msg = await engine.admin_reject(oid)
        elif act == "refund":
            msg = await engine.admin_refund(oid, "dashboard")
        elif act == "delivered":
            msg = await engine.admin_mark_delivered(oid)
        elif act == "message" and str(data.get("text", "")).strip():
            msg = await engine.admin_message(oid, str(data["text"])[:1500])
        elif act == "note":
            db.update_order(oid, notes=str(data.get("text", ""))[:2000])
            msg = "заметка сохранена"
        else:
            msg = "неизвестное действие"
        db.add_event(oid, "dashboard_action", action=act, result=msg, role=request[ROLE])
        audit(request, f"order.{act}", oid, msg)
        raise web.HTTPFound(URL(f"/orders/{oid}").with_query(msg=msg))

    async def customers(request: web.Request):
        qtext = request.query.get("q", "").strip().lstrip("@")
        seg = request.query.get("seg", "")
        sql, params = "SELECT * FROM customers WHERE 1=1", []
        if qtext:
            sql += " AND (username LIKE ? OR tg_usernames LIKE ? OR notes LIKE ? OR tags LIKE ?)"
            params = [f"%{qtext}%"] * 4
        if seg in SEGMENT_SQL:
            sql += " AND " + SEGMENT_SQL[seg]
        return render("customers.html", request, q=qtext, seg=seg, segments=SEGMENTS,
                      rows=sup.db.q(sql + " ORDER BY last_seen DESC LIMIT 300", params))

    async def customer(request: web.Request):
        db = sup.db
        cid = int(request.match_info["cid"])
        c = db.customer(cid)
        if not c:
            raise web.HTTPNotFound()
        rows = db.q("SELECT * FROM orders WHERE buyer_id=? ORDER BY created_at DESC LIMIT 200", (cid,))
        recipients = db.q("SELECT tg_username, COUNT(*) n, SUM(stars) stars, MAX(created_at) last FROM orders "
                          "WHERE buyer_id=? AND tg_username IS NOT NULL GROUP BY tg_username ORDER BY n DESC", (cid,))
        profit = sum((sup.econ.order_flow(o).profit or 0) for o in rows if o["delivered_at"] and o["status"] != "refunded")
        return render("customer.html", request, c=c, orders=rows, recipients=recipients, profit=round(profit, 2),
                      segs=segments_of(c),
                      msgs=db.q("SELECT m.* FROM messages m JOIN orders o ON o.chat_id=m.chat_id WHERE o.buyer_id=? "
                                "GROUP BY m.id ORDER BY m.id DESC LIMIT 40", (cid,)))

    async def customer_action(request: web.Request):
        data = await request.post()
        cid = int(request.match_info["cid"])
        act = str(data.get("action", ""))
        if act in ("blacklist", "unblock"):
            sup.engine.admin_blacklist(cid, act == "blacklist", str(data.get("note", "")))
        elif act == "note":
            sup.db.set_customer_note(cid, str(data.get("note", ""))[:1000], str(data.get("tags", ""))[:200])
        audit(request, f"customer.{act}", str(cid))
        raise back(request, "Сохранено", f"/customers/{cid}")

    async def pricing(request: web.Request):
        db, a = sup.db, sup.analytics
        lots = [l for l in (sup.cfg.funpay.lots or []) if l.get("stars")]
        denoms = [int(l["stars"]) for l in lots] or list(DENOMINATIONS)
        stars = int(_num(request.query.get("stars"), denoms[0]) or denoms[0])
        snap = db.one("SELECT * FROM competitor_snapshots WHERE stars=? ORDER BY id DESC LIMIT 1", (stars,))
        last = {int(r["stars"]): r for r in db.q(
            "SELECT * FROM price_history WHERE id IN (SELECT MAX(id) FROM price_history GROUP BY stars)") if r["stars"]}
        cps = sup.econ.cost_per_star_rub()
        return render("pricing.html", request, stars=stars, denoms=denoms, lots=lots, last=last,
                      series=a.price_series(stars, 300), cost_per_star=cps, fee_pct=sup.econ.fees_pct(),
                      history=db.q("SELECT * FROM price_history WHERE stars=? ORDER BY id DESC LIMIT 60", (stars,)),
                      comps=json.loads(snap["data"]) if snap else [], snap_ts=snap["ts"] if snap else None,
                      rtba=sup.cfg.rtba, markup=sup.engine.rtba.markup)

    # ------------------------------------------------------------------ деньги
    async def money(request: web.Request):
        econ, db = sup.econ, sup.db
        days = _num(request.query.get("days"), 30) or 30
        custom = {}
        for d in DENOMINATIONS:
            v = _num(request.query.get(f"p{d}"), None)
            if v:
                custom[d] = v
        rate, rate_src = econ.effective_rate()
        ozon_calc = econ.calculator(channel="ozon") if "ozon" in sup.engine.channels else []
        return render("money.html", request, days=days, period=econ.period(days), calc=econ.calculator(custom),
                      ozon_calc=ozon_calc, oz=sup.cfg.get("ozon") or {},
                      custom=custom, rate=rate, rate_src=rate_src, market=getattr(sup.engine.rates, "value", None),
                      tps=econ.ton_per_star(), cps=econ.cost_per_star_rub(), cap=econ.wallet_capacity(),
                      rec=econ.reconcile(), wallet=wallet_kv(), e=sup.cfg.economics,
                      balance=sup.analytics.balance_series(300),
                      journal=db.q("SELECT * FROM cashflow ORDER BY id DESC LIMIT 100"))

    async def export(request: web.Request):
        """CSV для учёта: «;» и BOM, чтобы Excel сразу открыл кириллицу по столбцам."""
        need(request, "operator")
        what = request.match_info["what"]
        days = _num(request.query.get("days"), 0) or 0
        since = time.time() - days * 86400 if days else 0
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        fmt = lambda t: dt.datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d %H:%M:%S") if t else ""
        if what == "orders":
            w.writerow(["order_id", "created", "delivered", "buyer", "recipient", "stars", "price_rub", "status",
                        "funpay_fee", "withdraw_fee", "net_rub", "ton_total", "ton_rate", "cost_rub", "profit_rub",
                        "tx_hash", "fail_reason"])
            for o in sup.db.q("SELECT * FROM orders WHERE created_at>=? ORDER BY created_at", (since,)):
                bought = o["ton_amount"] and o["status"] in (S.DELIVERED.value, S.COMPLETED.value)
                fl = sup.econ.order_flow(o) if bought else None
                r = lambda v: "" if v is None else str(round(float(v), 4)).replace(".", ",")
                w.writerow([o["order_id"], fmt(o["created_at"]), fmt(o["delivered_at"]), o["buyer_username"],
                            o["tg_username"] or "", o["stars"], r(o["price"]), o["status"],
                            r(fl and fl.funpay_fee), r(fl and fl.withdraw_fee), r(fl and fl.net_rub),
                            r(fl and fl.ton_total), r(fl and fl.ton_rate), r(fl and fl.cost_rub),
                            r(fl and fl.profit), o["tx_hash"] or "", o["fail_reason"] or ""])
        elif what == "cashflow":
            w.writerow(["id", "ts", "kind", "rub", "ton", "note"])
            for c in sup.db.q("SELECT * FROM cashflow WHERE ts>=? ORDER BY ts", (since,)):
                w.writerow([c["id"], fmt(c["ts"]), CASHFLOW_KINDS.get(c["kind"], c["kind"]),
                            str(c["rub"]).replace(".", ","), str(c["ton"]).replace(".", ","), c["note"]])
        else:
            raise web.HTTPNotFound()
        audit(request, f"export.{what}", "", f"{days or 'all'} дн.")
        name = f"starsbot-{what}-{dt.date.today():%Y%m%d}.csv"
        return web.Response(body=("\ufeff" + buf.getvalue()).encode("utf-8"), content_type="text/csv",
                            charset="utf-8", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def money_action(request: web.Request):
        f = await request.post()
        act = f.get("action")
        if act == "cashflow":
            kind = str(f.get("kind", ""))
            if kind not in CASHFLOW_KINDS:
                raise back(request, "Неизвестный тип операции", "/money")
            rub, ton = _num(f.get("rub")) or 0.0, _num(f.get("ton")) or 0.0
            if kind == "topup" and (rub <= 0 or ton <= 0):
                raise back(request, "Для пополнения укажите и рубли, и TON — из них считается ваш реальный курс",
                           "/money")
            sup.econ.add_cashflow(kind, rub, ton, str(f.get("note", "")))
            audit(request, f"cashflow.{kind}", "", f"{rub} ₽ / {ton} TON")
            msg = "Операция записана"
        elif act == "delete":
            need(request, "admin")
            sup.db.exec("DELETE FROM cashflow WHERE id=?", (int(_num(f.get("id")) or 0),))
            audit(request, "cashflow.delete", str(f.get("id")))
            msg = "Запись удалена"
        elif act == "reset_start":
            need(request, "admin")
            w = wallet_kv()
            if w.get("balance") is None:
                raise back(request, "Баланс кошелька ещё не получен", "/money")
            # сверка считает от точки отсчёта: пополнения до неё уже входят в стартовый баланс
            now = time.time()
            sup.db.kv_set("wallet_start_balance", {"ton": w["balance"], "ts": now})
            audit(request, "reconcile.reset", "", str(w["balance"]))
            msg = "Точка отсчёта сверки = текущий баланс"
        else:
            msg = "?"
        raise back(request, msg, "/money")

    async def api_calc(request: web.Request):
        data = await request.json()
        prices = {int(k): float(v) for k, v in (data.get("prices") or {}).items() if _num(v, None)}
        channel = "ozon" if data.get("channel") == "ozon" else "funpay"
        return web.json_response({"rows": [f.as_dict() for f in sup.econ.calculator(prices, channel=channel)]})

    # ------------------------------------------------------------------ Ozon
    async def ozon_page(request: web.Request):
        ch = sup.engine.channels.get("ozon")
        if ch is None:
            return render("ozon.html", request, ch=None, oz=sup.cfg.get("ozon") or {})
        db = sup.db
        codes = db.q("SELECT c.*, o.status, o.tg_username, o.price FROM ozon_codes c LEFT JOIN orders o ON "
                     "o.order_id=c.order_id ORDER BY c.created_at DESC LIMIT 100")
        attention = db.q("SELECT * FROM ozon_postings WHERE codes_state IN ('pending','failed') ORDER BY first_seen")
        return render("ozon.html", request, ch=ch, oz=sup.cfg.ozon, s=ch.summary(), codes=codes, attention=attention,
                      calc=sup.econ.calculator(channel="ozon"))

    @friendly("/ozon")
    async def ozon_action(request: web.Request):
        need(request, "admin")
        ch = sup.engine.channels.get("ozon")
        f = await request.post()
        act = str(f.get("action", ""))
        if act == "products":
            products = []
            for d in sorted({int(x) for x in DENOMINATIONS} | {int(p.get("stars") or 0) for p in
                                                                  (sup.cfg.ozon.products or []) if p.get("stars")}):
                offer = str(f.get(f"offer_{d}", "")).strip()
                sku = str(f.get(f"sku_{d}", "")).strip()
                item: dict = {"stars": d, "offer_id": offer}
                if sku.isdigit():
                    item["sku"] = int(sku)
                products.append(item)
            extra = str(f.get("extra_stars", "")).strip()
            if extra.isdigit() and int(extra) >= 50 and str(f.get("extra_offer", "")).strip():
                products.append({"stars": int(extra), "offer_id": str(f.get("extra_offer")).strip()})
            sup.save({"ozon": {"products": products}})
            msg = "Товары Ozon сохранены" + (". Перезапустите бота, чтобы применить" if sup.restart_required else "")
        elif ch is None:
            msg = "Сначала включите Ozon в настройках"
        elif act == "poll":
            try:
                await ch.poll()
                msg = "Ozon проверен" + (f": {ch.last_error}" if ch.last_error else "")
            except Exception as e:  # noqa: BLE001
                msg = f"Ozon: {e}"
        elif act == "upload":
            number = str(f.get("posting", ""))
            db = sup.db
            db.exec("UPDATE ozon_postings SET codes_state='pending' WHERE posting_number=? AND codes_state='failed'",
                    (number,))
            ok = await ch.upload(number)
            msg = "Коды переданы в Ozon" if ok else "Ozon не принял коды — смотрите алерты"
        elif act == "test":
            try:
                ps = await ch.client.list_postings(time.time() - 7 * 86400)
                msg = f"Ozon API отвечает: отправлений за 7 дней — {len(ps)}"
            except Exception as e:  # noqa: BLE001
                msg = f"Ozon API: {e}"
        else:
            msg = "?"
        audit(request, f"ozon.{act}", str(f.get("posting", "")), msg)
        raise web.HTTPFound(URL("/ozon").with_query(msg=msg))

    async def analytics_page(request: web.Request):
        a = sup.analytics
        days = _num(request.query.get("days"), 30) or 30
        return render("analytics.html", request, days=days, s=a.summary(days), series=a.daily_series(int(days)),
                      denoms=a.denominations(days), fails=a.failure_reasons(days), heat=a.hourly_heatmap(int(days)),
                      top=a.top_customers(15), velocity=a.sales_velocity(3), report=a.text_report())

    async def alerts(request: web.Request):
        return render("alerts.html", request, rows=sup.db.q("SELECT * FROM alerts ORDER BY id DESC LIMIT 300"),
                      audit_rows=sup.db.q("SELECT * FROM audit ORDER BY id DESC LIMIT 200")
                      if request[ROLE] == "admin" else [])

    async def alerts_ack(request: web.Request):
        sup.db.exec("UPDATE alerts SET acknowledged=1")
        audit(request, "alerts.ack")
        raise web.HTTPFound("/alerts")

    # ------------------------------------------------------------------ управление ботом
    async def control(request: web.Request):
        act = (await request.post()).get("action")
        if act in ("dry_off", "dry_on", "quit", "check_fragment"):
            need(request, "admin")
        if act == "start":
            msg = await sup.start()
        elif act == "stop":
            msg = await sup.stop()
        elif act == "restart":
            msg = await sup.restart()
        elif act in ("pause", "resume"):
            msg = await sup.engine.handle_command(act, [])
        elif act == "check_fragment":
            ok = await sup.engine.ensure_fragment(force=True)
            msg = "Fragment: сессия активна" if ok else "Fragment: " + (sup.engine.fragment_status.get("error")
                                                                        or "сессия недоступна")
        elif act == "dry_off":
            sup.save({"general": {"dry_run": False}})
            msg = "Боевой режим включён" + (" — перезапустите бота" if sup.restart_required else "")
        elif act == "dry_on":
            sup.save({"general": {"dry_run": True}})
            msg = "Тестовый режим включён" + (" — перезапустите бота" if sup.restart_required else "")
        elif act == "quit" and sup.quit_event is not None:
            audit(request, "control.quit")
            asyncio.get_running_loop().call_later(0.5, sup.quit_event.set)
            return web.Response(text="<html><body style='font:16px system-ui;padding:40px'>StarsBot закрыт. "
                                     "Это окно можно закрыть.</body></html>", content_type="text/html")
        else:
            msg = "?"
        audit(request, f"control.{act}", "", msg)
        raise back(request, msg)

    async def api_status(request: web.Request):
        s = sup.analytics.summary(1)
        return web.json_response({**sup.status(), "summary": s, "capacity": sup.econ.wallet_capacity(),
                                  "rate": sup.econ.effective_rate()[0], "role": request[ROLE]})

    async def api_health(request: web.Request):
        h = sup.engine.health() if sup.state == "running" else {"ok": sup.state != "error", "checks": []}
        return web.json_response({**h, "state": sup.state}, status=200 if h["ok"] else 503)

    async def stream(request: web.Request):
        """SSE: события БД (заказы, алерты, кошелёк, Fragment) + новые строки журнала."""
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        loop = asyncio.get_running_loop()

        def listener(ev: dict) -> None:
            def put():
                if not queue.full():
                    queue.put_nowait(ev)
            loop.call_soon_threadsafe(put)

        sup.db.listeners.append(listener)
        last_log = int(request.query.get("log", 0)) or (log_handler.seq if log_handler else 0)
        want_log = request.query.get("logs") == "1"
        try:
            await resp.write(f"event: hello\ndata: {json.dumps(sup.status(), ensure_ascii=False)}\n\n".encode())
            ticks = 0
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), 1.0)
                    await resp.write(f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n".encode())
                    continue
                except asyncio.TimeoutError:
                    pass
                ticks += 1
                if want_log and log_handler:
                    lines = [l for l in log_handler.lines if l["n"] > last_log]
                    if lines:
                        last_log = lines[-1]["n"]
                        await resp.write(f"event: log\ndata: {json.dumps(lines[-200:], ensure_ascii=False)}\n\n"
                                         .encode())
                if ticks % 15 == 0:
                    await resp.write(f"event: status\ndata: {json.dumps(sup.status(), ensure_ascii=False)}\n\n"
                                     .encode())
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            if listener in sup.db.listeners:
                sup.db.listeners.remove(listener)
        return resp

    # ------------------------------------------------------------------ мастер настройки
    async def setup(request: web.Request):
        need(request, "admin")
        cfg = sup.cfg
        if request.method == "POST":
            f = await request.post()
            lots = []
            pos = {int(l.get("stars") or 0): int(l.get("position") or 0) for l in (cfg.funpay.lots or [])}
            for d in DENOMINATIONS:
                oid = str(f.get(f"lot_{d}", "")).strip()
                lots.append({"stars": d, "offer_id": int(oid) if oid.isdigit() else 0, "position": pos.get(d, 0)})
            chat_ids = [int(x) for x in str(f.get("tg_chat_ids", "")).replace(",", " ").split()
                        if x.lstrip("-").isdigit()]
            upd = {
                "general": {"dry_run": f.get("dry_run") == "on", "autostart": f.get("autostart") == "on",
                            "language": f.get("language", "ru")},
                "funpay": {"golden_key": str(f.get("golden_key", "")).strip(), "lots": lots},
                "fragment": {"cookies": str(f.get("fragment_cookies", "")).strip()},
                "ton": {"mnemonic": " ".join(str(f.get("mnemonic", "")).split()),
                        "address": str(f.get("ton_address", "")).strip(),
                        "version": f.get("ton_version", "v5r1"),
                        "toncenter_api_key": str(f.get("toncenter_api_key", "")).strip()},
                "notifications": {"telegram": {"enabled": bool(f.get("tg_token") or sup.secret_is_set(
                    "notifications.telegram", "bot_token")) and bool(chat_ids),
                    "bot_token": str(f.get("tg_token", "")).strip(), "admin_chat_ids": chat_ids}},
            }
            sup.save(upd)
            audit(request, "setup.save", "", "ok" if sup.configured else "; ".join(sup.problems))
            if sup.configured:
                raise web.HTTPFound("/?msg=Настройки сохранены. Нажмите «Запустить бота»")
            raise web.HTTPFound(URL("/setup").with_query(msg="Сохранено. Осталось: " + "; ".join(sup.problems)))
        lots = {int(l.get("stars") or 0): l.get("offer_id") for l in (cfg.funpay.lots or [])}
        return render("setup.html", request, cfg=cfg, lots=lots, denoms=DENOMINATIONS, problems=sup.problems,
                      secret=lambda s, k: sup.secret_is_set(s, k))

    async def api_test(request: web.Request):
        need(request, "admin")
        what = request.match_info["what"]
        data = await request.json() if request.can_read_body else {}
        wallet_data = {k: data.get(k) for k in ("mnemonic", "address", "version", "toncenter_api_key")
                       if data.get(k)}
        if what == "funpay":
            res = await sup.run_test(sup.test_funpay(data.get("golden_key") or None), 25)
        elif what == "wallet":
            res = await sup.run_test(sup.test_wallet(wallet_data), 30)
        elif what == "fragment":
            res = await sup.run_test(sup.test_fragment(data.get("cookies") or None,
                                                       {**wallet_data, "probe_username": data.get("probe_username")}))
        elif what == "telegram":
            ids = [int(x) for x in str(data.get("chat_ids", "")).replace(",", " ").split() if x.lstrip("-").isdigit()]
            res = await sup.run_test(sup.test_telegram(data.get("token") or None, ids or None), 20)
        else:
            res = {"ok": False, "text": "?"}
        audit(request, f"test.{what}", "", "ok" if res.get("ok") else "fail")
        return web.json_response(res)

    # ------------------------------------------------------------------ Подарки
    def _num_in(v, default=0, cast=int):
        try:
            return cast(str(v).strip().replace(",", ".")) if str(v).strip() != "" else default
        except ValueError:
            return default

    async def gifts_page(request: web.Request):
        svc = sup.engine.gifts
        g = sup.cfg.get("gifts") or {}
        cat = list(svc.tg._catalog[1]) if isinstance(getattr(svc.tg, "_catalog", None), tuple) else \
            list(getattr(svc.tg, "_catalog", []) or [])
        plans = []
        for l in (g.get("lots") or []):
            try:
                p = svc.plan_for(int(l["stars"]), cat or None)
                plans.append({"stars": int(l["stars"]), "offer_id": l.get("offer_id") or "", "position":
                              int(l.get("position") or 0), "summary": p.summary(), "cost": p.cost, "value": p.value})
            except Exception as e:  # noqa: BLE001
                plans.append({"stars": int(l["stars"]), "offer_id": l.get("offer_id") or "", "position":
                              int(l.get("position") or 0), "summary": str(e), "cost": None, "value": None})
        sends = sup.db.q("SELECT s.*, o.tg_username FROM gift_sends s LEFT JOIN orders o ON o.order_id=s.order_id "
                         "ORDER BY s.id DESC LIMIT 50")
        orders = sup.db.q("SELECT * FROM orders WHERE delivery='gift' ORDER BY created_at DESC LIMIT 30")
        topups = sup.db.q("SELECT * FROM orders WHERE channel='internal' ORDER BY created_at DESC LIMIT 10")
        stars = sup.db.kv_get("tg_stars") or {}
        return render("gifts.html", request, g=g, svc=svc, cat=cat, plans=plans, sends=sends, orders=orders,
                      topups=topups, stars=stars, account=svc.account, committed=sup.db.gift_stars_committed(),
                      has_session=bool(getattr(svc.tg, "has_session", False)), login=request.query.get("login", ""),
                      secret=lambda s_, k: sup.secret_is_set(s_, k))

    @friendly("/gifts")
    async def gifts_action(request: web.Request):
        need(request, "admin")
        f = await request.post()
        act = str(f.get("action", ""))
        svc = sup.engine.gifts
        g = sup.cfg.get("gifts") or {}
        login_step = ""
        if act == "save":
            lots = []
            for l in (g.get("lots") or []):
                d = int(l["stars"])
                oid = str(f.get(f"lot_{d}", "")).strip()
                lots.append({"stars": d, "offer_id": int(oid) if oid.isdigit() else 0,
                             "position": _num_in(f.get(f"pos_{d}"), 0)})
            extra = str(f.get("extra_stars", "")).strip()
            if extra.isdigit() and int(extra) > 0 and int(extra) not in {l["stars"] for l in lots}:
                lots.append({"stars": int(extra), "offer_id": 0, "position": 0})
            upd = {"gifts": {
                "enabled": f.get("enabled") == "on", "api_id": _num_in(f.get("api_id"), 0),
                "api_hash": str(f.get("api_hash", "")).strip(), "phone": str(f.get("phone", "")).strip(),
                "mode": "price" if f.get("mode") == "price" else "convert",
                "per_gift_penalty": _num_in(f.get("per_gift_penalty"), 3, float),
                "gift_message": str(f.get("gift_message", ""))[:120], "hide_sender_name": f.get("hide") == "on",
                "pilot_orders": _num_in(f.get("pilot_orders"), 1), "max_order_stars": _num_in(f.get("max_order_stars"), 0),
                "low_stars_alert": _num_in(f.get("low_stars_alert"), 500), "lots": lots,
                "topup": {"enabled": f.get("topup_enabled") == "on", "min_stars": _num_in(f.get("topup_min"), 1000),
                          "amount": max(50, _num_in(f.get("topup_amount"), 2500)),
                          "username": str(f.get("topup_username", "")).strip().lstrip("@")},
            }}
            if M.contains_forbidden_link(upd["gifts"]["gift_message"]):
                raise web.HTTPFound(URL("/gifts").with_query(msg="В подписи к подарку нельзя ссылки (правила FunPay)"))
            sup.save(upd)
            msg = "Сохранено" + (". Перезапустите бота, чтобы применить" if sup.restart_required else "")
        elif act == "send_code":
            phone = str(f.get("phone", "")).strip() or str(g.get("phone") or "")
            if phone and phone != g.get("phone"):
                sup.save({"gifts": {"phone": phone}}, rebuild=False)
            try:
                msg = await sup.engine.gifts.tg.send_code(phone)
                login_step = "code"
            except Exception as e:  # noqa: BLE001
                msg = f"Telegram: {e}"
        elif act == "sign_in":
            phone = str(g.get("phone") or f.get("phone") or "")
            try:
                me = await svc.tg.sign_in(phone, str(f.get("code", "")), str(f.get("password", "")))
                svc.account = {"user_id": me.user_id, "username": me.username, "name": me.name, "ts": time.time()}
                sup.db.kv_set("tg_account", svc.account)
                msg = f"Вход выполнен: {me.name} @{me.username or '—'}"
            except Exception as e:  # noqa: BLE001
                msg = f"Вход не выполнен: {e}"
                login_step = "password" if getattr(e, "code", "") == "password_needed" else "code"
        elif act == "logout":
            await svc.tg.logout_local()
            sup.db.kv_set("tg_account", {})
            svc.account = {}
            msg = "Сессия Telegram удалена с этого компьютера (в Telegram → Устройства можно завершить её и там)"
        elif act == "refresh":
            try:
                me = await svc.tg.whoami()
                svc.account = {"user_id": me.user_id, "username": me.username, "name": me.name, "ts": time.time()}
                sup.db.kv_set("tg_account", svc.account)
                bal = await svc.refresh_balance(max_age=0)
                cat = await svc.catalog()
                msg = f"Аккаунт @{me.username or me.name}: {bal if bal is not None else '?'} ⭐, подарков в каталоге: " \
                      f"{len([c for c in cat if c.usable])}"
            except Exception as e:  # noqa: BLE001
                msg = f"Telegram: {e}"
        elif act == "topup":
            amount = max(50, _num_in(f.get("amount"), 1000))
            uname = str((g.get("topup") or {}).get("username") or svc.account.get("username") or "")
            if not uname:
                msg = "Нет @username аккаунта — укажите его в настройках пополнения"
            elif sup.state != "running":
                msg = "Запустите бота: пополнение идёт через Fragment и кошелёк TON"
            else:
                msg = await svc.create_topup(amount, uname)
        else:
            msg = "?"
        audit(request, f"gifts.{act}", "", msg)
        q = {"msg": msg}
        if login_step:
            q["login"] = login_step
        raise web.HTTPFound(URL("/gifts").with_query(**q))

    # ------------------------------------------------------------------ Лоты
    async def funpay_page(request: web.Request):
        lm = sup.engine.lot_manager
        ap = sup.engine.autopilot
        unconfirmed = ap.unconfirmed(0, include_done=True)
        return render("funpay.html", request, rows=lm.rows(), fr=sup.engine.fragment_status,
                      fr_age=sup.engine.fragment_cookie_age_days(), files=ap.support_files(), unconfirmed=unconfirmed,
                      ap=ap.status(), fp=sup.cfg.funpay, rtba=sup.cfg.rtba, gifts_on=bool((sup.cfg.get("gifts") or {})
                                                                                           .get("enabled")))

    @friendly("/funpay")
    async def funpay_action(request: web.Request):
        need(request, "admin")
        f = await request.post()
        act = str(f.get("action", ""))
        lm = sup.engine.lot_manager
        ap = sup.engine.autopilot
        if act == "positions":
            for kind, section in (("username", "funpay"), ("gift", "gifts")):
                lots = [dict(l) for l in ((sup.cfg.get(section) or {}).get("lots") or [])]
                for l in lots:
                    key = f"pos_{kind}_{int(l['stars'])}"
                    if key in f:
                        l["position"] = max(0, _num_in(f.get(key), 0))
                    okey = f"offer_{kind}_{int(l['stars'])}"
                    if okey in f:
                        v = str(f.get(okey, "")).strip()
                        l["offer_id"] = int(v) if v.isdigit() else 0
                sup.save({section: {"lots": lots}}, rebuild=False)
                lm._apply_live({kind: lots})
            sup.save({"funpay": {"auto_create_lots": f.get("auto_create") == "on",
                                 "lot_price_per_star": _num_in(f.get("start_price"), 1.6, float)}}, rebuild=False)
            msg = "Лоты и позиции сохранены. Позиция применяется при следующей проверке рынка (раз в " \
                  f"{int(sup.cfg.rtba.interval_seconds) // 60 or 1} мин)"
        elif act == "create":
            if sup.state != "running":
                await sup.engine.fp.login()
            msg = "; ".join(await lm.create_missing(template_offer=_num_in(f.get("template"), 0)))
        elif act == "close_all":
            msg = await lm.close_all(reason="кнопка в панели")
        elif act == "open_all":
            e = sup.engine
            for section, prefix in (("funpay", "lot_amount:"), ("gifts", "gift_lot_amount:")):
                for l in ((sup.cfg.get(section) or {}).get("lots") or []):
                    if l.get("offer_id"):
                        sup.db.kv_set(prefix + str(l["offer_id"]), None)
            if sup.state == "running":
                e.wallet_info["ts"] = 0
                await e.wallet_watch()
                if e.gifts is not None and e.gifts.enabled:
                    await e.gifts.watch()
                msg = "Лоты включены — наличие выставлено по балансу кошелька"
            else:
                msg = "Лоты включатся сами при запуске бота"
        elif act == "toggle":
            msg = await lm.set_active(_num_in(f.get("offer_id"), 0), f.get("active") == "1")
        elif act == "support":
            ids = [x for x in f.getall("oid", [])]
            rows = [sup.db.get_order(x) for x in ids] if ids else ap.unconfirmed(
                float((sup.cfg.get("autopilot") or {}).get("support_after_hours") or 72), include_done=True)
            rows = [r for r in rows if r is not None]
            msg = ("Файл готов: " + Path(ap.make_support_file(rows)).name) if rows else \
                "Нет выданных и неподтверждённых заказов старше порога"
        elif act == "remind":
            n = await ap.confirm_reminders()
            msg = f"Напоминаний отправлено: {n}"
        else:
            msg = "?"
        audit(request, f"funpay.{act}", "", msg)
        raise web.HTTPFound(URL("/funpay").with_query(msg=msg[:1500]))

    async def support_download(request: web.Request):
        need(request, "admin")
        p = sup.engine.autopilot.support_path(request.match_info["name"])
        if p is None:
            raise web.HTTPNotFound(text="файл не найден")
        return web.Response(body=p.read_bytes(), content_type="text/plain", charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{p.name}"'})

    # ------------------------------------------------------------------ Кошелёк и автопилот
    async def crypto_page(request: web.Request):
        e = sup.engine
        ap = e.autopilot
        w = e.wallet
        wd = sup.db.q("SELECT * FROM withdrawals ORDER BY id DESC LIMIT 30")
        return render("crypto.html", request, w=w, winfo=wallet_kv(), ton=sup.cfg.ton,
                      ap=ap.status(), acfg=sup.cfg.get("autopilot") or {}, withdrawals=wd,
                      key_kind=getattr(w, "key_kind", ""), mnemonic_set=sup.secret_is_set("ton", "mnemonic"),
                      rec=sup.econ.reconcile(), rate=sup.econ.effective_rate()[0])

    @friendly("/crypto")
    async def crypto_action(request: web.Request):
        need(request, "admin")
        f = await request.post()
        act = str(f.get("action", ""))
        ap = sup.engine.autopilot
        if act == "save":
            words = " ".join(str(f.get("mnemonic", "")).split())
            upd = {"autopilot": {
                "enabled": f.get("enabled") == "on",
                "confirm_reminder_hours": _num_in(f.get("confirm_reminder_hours"), 24, float),
                "support_after_hours": _num_in(f.get("support_after_hours"), 72, float),
                "withdraw": {"enabled": f.get("w_enabled") == "on",
                             "min_wallet_ton": _num_in(f.get("min_wallet_ton"), 5, float),
                             "min_funpay_rub": _num_in(f.get("min_funpay_rub"), 1000, float),
                             "keep_rub": _num_in(f.get("keep_rub"), 0, float),
                             "max_per_day_rub": _num_in(f.get("max_per_day_rub"), 20000, float),
                             "method": str(f.get("method", "")).strip(), "wallet": str(f.get("wallet", "")).strip(),
                             "require_approval": f.get("require_approval") == "on"}},
                "ton": {"mnemonic": words, "address": str(f.get("address", "")).strip(),
                        "version": f.get("version") if f.get("version") in ("v5r1", "v4r2") else "v5r1",
                        "mnemonic_type": f.get("mnemonic_type") if f.get("mnemonic_type") in ("auto", "bip39", "ton")
                        else "auto",
                        "low_balance_ton": _num_in(f.get("low_balance_ton"), 3, float)}}
            sup.save(upd)
            msg = "Сохранено" + (". Перезапустите бота, чтобы применить" if sup.restart_required else "")
        elif act == "balance":
            try:
                if sup.state != "running":
                    await sup.engine.fp.login()
                info = await sup.engine.fp.get_balance()
                ap._save_state(funpay_balance=info.available_rub, funpay_balance_at=time.time(),
                               withdraw_methods=info.methods[:30])
                msg = f"Баланс FunPay: {info.available_rub if info.available_rub is not None else '?'} ₽; способов " \
                      f"вывода найдено: {len(info.methods)}"
            except Exception as e:  # noqa: BLE001
                msg = f"FunPay: {e}"
        elif act == "withdraw_now":
            msg = await ap.maybe_withdraw(force=True)
        elif act == "approve":
            msg = await ap.execute_withdraw(_num_in(f.get("wid"), 0))
        elif act == "cancel":
            msg = ap.cancel_withdraw(_num_in(f.get("wid"), 0))
        else:
            msg = "?"
        audit(request, f"crypto.{act}", "", msg)
        raise web.HTTPFound(URL("/crypto").with_query(msg=msg[:600]))

    async def api_wallet_addresses(request: web.Request):
        need(request, "admin")
        data = await request.json() if request.can_read_body else {}
        from starsbot.ton.mnemonic import MnemonicError, all_addresses
        try:
            items = all_addresses(str(data.get("mnemonic") or ""), str(data.get("kind") or "auto"))
        except MnemonicError as e:
            return web.json_response({"ok": False, "text": str(e)})
        return web.json_response({"ok": True, "items": items})

    # ------------------------------------------------------------------ настройки
    async def settings(request: web.Request):
        need(request, "admin")
        if request.method == "POST":
            f = await request.post()
            upd: dict = {}
            for section, fields in schema.SETTINGS:
                _deep_merge(upd, schema.parse_form(f, section, fields))
            sup.save(upd)
            audit(request, "settings.save")
            raise web.HTTPFound(URL("/settings").with_query(msg="Сохранено. Перезапустите бота, чтобы применить"
                                                            if sup.restart_required else "Сохранено"))
        return render("settings.html", request, sections=schema.SETTINGS, titles=schema.SECTION_TITLES,
                      val=lambda s, k: schema.get_value(sup.cfg, s, k),
                      secret=lambda s, k: sup.secret_is_set(s, k), problems=sup.problems,
                      config_path=str(sup.config_path))

    # ------------------------------------------------------------------ сообщения
    async def messages_page(request: web.Request):
        lang = request.query.get("lang", "ru") if request.query.get("lang") in ("ru", "en") else "ru"
        errors: dict[str, str] = {}
        if request.method == "POST":
            need(request, "admin")
            f = await request.post()
            lang = f.get("lang", "ru") if f.get("lang") in ("ru", "en") else "ru"
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
            audit(request, "messages.save", lang, f"ошибок: {len(errors)}")
            if not errors:
                raise web.HTTPFound(URL("/messages").with_query(lang=lang, msg="Тексты сохранены и уже действуют"))
        items = [(k, schema.MESSAGE_TITLES.get(k, k), M.template(lang, k), k in M.OVERRIDES.get(lang, {}))
                 for k in M.TEMPLATES[lang]]
        return render("messages.html", request, items=items, lang=lang, errors=errors,
                      vars=sorted(M.TEMPLATE_VARS))

    async def preview_message(request: web.Request):
        data = await request.json()
        err = M.validate_template(data.get("text", ""))
        if err:
            return web.json_response({"ok": False, "text": err})
        try:
            return web.json_response({"ok": True, "text": data["text"].format(**M.TEMPLATE_VARS)})
        except (KeyError, IndexError, ValueError) as e:
            return web.json_response({"ok": False, "text": f"Неизвестная переменная: {e}"})

    # ------------------------------------------------------------------ журнал
    async def logs(request: web.Request):
        return render("logs.html", request)

    async def api_logs(request: web.Request):
        after = int(_num(request.query.get("after"), 0) or 0)
        lines = [l for l in (log_handler.lines if log_handler else []) if l["n"] > after]
        return web.json_response({"lines": lines[-500:], "state": sup.status()})

    app = web.Application(middlewares=[auth], client_max_size=2 * 1024 * 1024)
    app.add_routes([
        web.get("/login", login), web.post("/login", login), web.post("/logout", logout), web.get("/api/ping", ping),
        web.get("/", dashboard), web.get("/orders", orders), web.get("/orders/{oid}", order),
        web.post("/orders/{oid}", order_action), web.get("/customers", customers),
        web.get("/customers/{cid:\\d+}", customer),
        web.post("/customers/{cid}", customer_action), web.get("/pricing", pricing),
        web.get("/money", money), web.post("/money", money_action), web.post("/api/calc", api_calc),
        web.get("/analytics", analytics_page), web.get("/alerts", alerts),
        web.post("/alerts/ack", alerts_ack), web.post("/control", control), web.get("/api/status", api_status),
        web.get("/api/summary", api_status), web.get("/api/health", api_health),
        web.get(r"/export/{what}.csv", export), web.get("/api/stream", stream),
        web.get("/setup", setup), web.post("/setup", setup),
        web.post("/api/test/{what}", api_test), web.get("/settings", settings), web.post("/settings", settings),
        web.get("/messages", messages_page), web.post("/messages", messages_page),
        web.post("/api/preview", preview_message), web.get("/logs", logs), web.get("/api/logs", api_logs),
        web.get("/ozon", ozon_page), web.post("/ozon", ozon_action),
        web.get("/gifts", gifts_page), web.post("/gifts", gifts_action),
        web.get("/funpay", funpay_page), web.post("/funpay", funpay_action),
        web.get("/support/{name}", support_download),
        web.get("/crypto", crypto_page), web.post("/crypto", crypto_action),
        web.post("/api/wallet/addresses", api_wallet_addresses),
    ])
    return app


# POST-маршруты, доступные оператору (остальные — только администратору; заказы и клиенты — оператору)
OPERATOR_POST = {"/control", "/alerts/ack", "/money", "/api/calc", "/api/preview"}


async def start_web(sup, host: str, port: int, log_handler=None) -> web.AppRunner:
    runner = web.AppRunner(make_app(sup, log_handler), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
