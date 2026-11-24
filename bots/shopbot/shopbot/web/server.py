"""Панель ShopBot: сводка, RTBA-пульт, заказы, мини-CRM, выкуп и выплаты, канал, настройки, демо-чат.

Роли как у StarsBot: admin (dashboard.token) — всё; operator (operator_token) — заказы, выплаты, клиенты;
viewer (viewer_token) — только просмотр.
"""
from __future__ import annotations

import hmac
import json
import os
import time
from pathlib import Path

import jinja2
from aiohttp import web

from shopbot import VERSION
from shopbot.engine import STATUS_TITLES
from shopbot.pricing import METHOD_TITLES, PRODUCT_TITLES
from shopbot.rtba import TITLES

COOKIE = "shop_token"
LEVEL = {"viewer": 0, "operator": 1, "admin": 2}
TEMPLATES = Path(__file__).parent / "templates"

SETTINGS = [  # (путь, подпись, тип)
    ("general.shop_name", "Название магазина", "str"), ("general.support_username", "Поддержка (@)", "str"),
    ("general.dry_run", "Тестовый режим (выдача выключена)", "bool"),
    ("catalog.stars.margin_pct", "Звёзды: маржа, %", "float"),
    ("catalog.stars.min_margin_pct", "Звёзды: минимальная маржа, %", "float"),
    ("catalog.stars.undercut_funpay_pct", "Звёзды: дешевле FunPay на, %", "float"),
    ("catalog.stars.funpay_ref_rub_per_star", "Звёзды: цена FunPay за ⭐, ₽", "float"),
    ("catalog.gifts.margin_pct", "Подарки: маржа, %", "float"), ("catalog.premium.margin_pct", "Premium: маржа, %", "float"),
    ("catalog.steam_keys.margin_pct", "Ключи Steam: маржа, %", "float"),
    ("payments.card.fee_pct", "Карта: комиссия клиенту, %", "float"),
    ("payments.crypto.fee_pct", "Крипта: конвертация, % (не меньше 2)", "float"),
    ("payments.stars.rub_per_star", "Оплата звёздами: ₽ за 1 ⭐", "float"),
    ("payments.wallet.address", "Общий кошелёк TON (адрес хаба)", "str"),
    ("payments.wallet.spread_pct", "Кошелёк: спред в курсе, %", "float"),
    ("payments.wallet.min_spread_pct", "Кошелёк: минимальный спред, %", "float"),
    ("payments.gift.account", "Оплата подарками: аккаунт приёма (@)", "str"),
    ("payments.gift.haircut_pct", "Оплата подарками: скидка оценки, %", "float"),
    ("payments.skins.trade_url", "Оплата скинами: ссылка на обмен магазина", "str"),
    ("payments.skins.haircut_pct", "Оплата скинами: скидка к Market.CSGO, %", "float"),
    ("payments.card.enabled", "Карта (эквайринг) включена", "bool"),
    ("miniapp.url", "Мини-приложение: постоянный адрес (https)", "str"),
    ("buyback.fee_pct", "Выкуп: комиссия, %", "float"), ("buyback.stars.usd_per_star", "Выкуп звёзд: $ за 1 ⭐", "float"),
    ("buyback.gifts.gift_account", "Аккаунт приёма подарков (@)", "str"),
    ("buyback.daily_cap_usdt", "Выкуп: лимит в сутки, USDT", "float"),
    ("rtba.auto_payout_usdt", "Авто-выплата до, USDT", "float"), ("rtba.payout_reserve_usdt", "Резерв выплат, USDT", "float"),
    ("rtba.user_daily_rub", "Лимит клиента в сутки, ₽", "float"), ("rtba.new_user_daily_rub", "Лимит нового клиента, ₽", "float"),
    ("rtba.max_order_rub", "Максимальный заказ, ₽", "float"),
    ("referral.reward_pct", "Рефералка: % пригласившему", "float"),
    ("referral.friend_discount_pct", "Рефералка: скидка другу, %", "float"),
    ("channel.enabled", "Канал: публикация разрешена", "bool"), ("channel.chat_id", "Канал: chat_id", "str"),
]
SECRET_FIELDS = [("telegram.bot_token", "Токен бота магазина"), ("payments.card.yookassa.shop_id", "ЮKassa shopId"),
                 ("payments.card.yookassa.secret_key", "ЮKassa секретный ключ"),
                 ("payments.crypto.cryptopay.token", "Crypto Pay токен"),
                 ("suppliers.stars.token", "Токен поставщика StarsBot"), ("suppliers.gifts.token", "Токен TG Gift"),
                 ("suppliers.premium.token", "Токен Premium (StarsBot)")]


def nest(path: str, value) -> dict:
    out: dict = {}
    node = out
    parts = path.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    return out


def make_app(app_state) -> web.Application:
    """app_state: shopbot.__main__.App (shop, bot, cfg, db, demo)."""
    st = app_state
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)
    env.filters["ts"] = lambda t: time.strftime("%d.%m %H:%M", time.localtime(float(t))) if t else "—"
    env.filters["j"] = lambda s: json.loads(s or "[]")
    env.globals.update(STATUS=STATUS_TITLES, METHODS=METHOD_TITLES, PRODUCTS=PRODUCT_TITLES, RTBA_TITLES=TITLES,
                       VERSION=VERSION)

    def tokens() -> dict:
        d = st.cfg.get("dashboard") or {}
        return {r: str(d.get(k) or "") for r, k in (("admin", "token"), ("operator", "operator_token"),
                                                   ("viewer", "viewer_token")) if len(str(d.get(k) or "")) >= 12}

    def role_for(t: str) -> str | None:
        for r, v in tokens().items():
            if t and hmac.compare_digest(t, v):
                return r
        return None

    def need(request, role: str) -> None:
        if LEVEL[request["role"]] < LEVEL[role]:
            raise web.HTTPForbidden(text="недостаточно прав")

    def render(name: str, request, **ctx) -> web.Response:
        ctx.update(path=request.path, role=request.get("role"), demo=st.demo, msg=request.query.get("msg", ""),
                   shop_name=st.cfg.get("general.shop_name"), dry_run=st.cfg.get("general.dry_run"),
                   alerts_open=st.db.scalar("SELECT COUNT(*) FROM alerts WHERE acknowledged=0 AND level!='INFO'",
                                            (), 0))
        return web.Response(text=env.get_template(name).render(**ctx), content_type="text/html")

    def back(request, msg: str) -> web.HTTPFound:
        ref = request.headers.get("Referer") or "/"
        path = "/" + ref.split("//", 1)[-1].split("/", 1)[-1].split("?")[0] if "//" in ref else ref.split("?")[0]
        from urllib.parse import quote
        return web.HTTPFound(f"{path}?msg={quote(msg)}")

    @web.middleware
    async def auth(request, handler):
        if request.path in ("/login", "/api/ping", "/app", "/app/"):   # /app — публичная витрина мини-приложения
            return await handler(request)
        role = role_for(request.cookies.get(COOKIE, "") or request.headers.get("X-Dashboard-Token", ""))
        if not role:
            if request.path.startswith("/api/"):
                raise web.HTTPUnauthorized(text="login required")
            raise web.HTTPFound("/login")
        request["role"] = role
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin.split("//")[-1] != request.host:
                raise web.HTTPForbidden(text="bad origin")
            need(request, "operator")
        return await handler(request)

    async def login(request):
        given = request.query.get("token", "")
        if request.method == "POST":
            given = str((await request.post()).get("token", ""))
        if given and role_for(given):
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE, given, httponly=True, samesite="Strict", max_age=90 * 86400)
            raise resp
        return web.Response(text=env.get_template("login.html").render(shop_name=st.cfg.get("general.shop_name")),
                            content_type="text/html")

    async def miniapp(request):
        return web.FileResponse(Path(__file__).parent / "miniapp" / "index.html",
                                headers={"Cache-Control": "no-cache"})

    async def ping(request):
        return web.json_response({"app": "shopbot", "ok": True, "pid": os.getpid(), "version": VERSION})

    async def status(request):
        sh = st.shop
        s1 = sh.summary(1)
        tre = st.db.kv_get("treasury") or {}
        closed = [p["title"] for p in sh.rtba.points() if p["state"] == "closed"]
        return web.json_response({"app": "shopbot", "demo": st.demo, "dry_run": st.cfg.get("general.dry_run"),
                                  "state": "running", "summary": s1, "rates": sh.rates.snapshot(),
                                  "treasury_usdt": tre.get("usdt"), "rtba_closed": closed,
                                  "bot": bool(st.cfg.get("telegram.bot_token")) or st.demo})

    async def api_sources(request):
        from shopbot.economy import sources_report
        days = max(1.0, min(365.0, float(request.query.get("days") or 30)))
        return web.json_response(sources_report(st.db, days))

    async def api_reviews(request):
        from shopbot.economy import review_stats
        rows = st.db.q("SELECT order_id, user_id, rating, text, product, status, created_at FROM reviews "
                       "ORDER BY created_at DESC LIMIT 100")
        return web.json_response({**review_stats(st.db), "items": [dict(r) for r in rows]})

    async def api_economy(request):
        """Сводка экономики: уровни клиентов, курс выкупа, скидки и награды за 30 дней (без себестоимости по товарам)."""
        from shopbot import economy as E
        since = time.time() - 30 * 86400
        tiers: dict[str, int] = {}
        for u in st.db.q("SELECT * FROM users WHERE last_seen>=?", (since,)):
            t = E.tier_for(st.cfg, st.db, u)
            tiers[t.title] = tiers.get(t.title, 0) + 1
        agg = st.db.one("SELECT SUM(loyalty_rub) loy, SUM(discount_rub) disc, SUM(ref_reward_rub) refs, "
                        "SUM(bonus_rub) bonus, SUM(margin_rub) margin, SUM(total_rub) rev, COUNT(*) n FROM orders "
                        "WHERE kind='buy' AND status='delivered' AND created_at>=?", (since,))
        rate, fee, why = E.buyback_usd_per_star(st.cfg, None, st.shop.xtr_stock())
        return web.json_response({"tiers": tiers, "orders": agg["n"] or 0, "revenue_rub": round(agg["rev"] or 0, 2),
                                  "margin_rub": round(agg["margin"] or 0, 2), "loyalty_rub": round(agg["loy"] or 0, 2),
                                  "discount_rub": round(agg["disc"] or 0, 2), "ref_rewards_rub": round(agg["refs"] or 0, 2),
                                  "bonus_spent_rub": round(agg["bonus"] or 0, 2), "xtr_stock": st.shop.xtr_stock(),
                                  "buyback": {"usd_per_star": rate, "fee_pct": fee, "why": why},
                                  "reviews": E.review_stats(st.db)})

    async def dashboard(request):
        sh = st.shop
        attention = st.db.q("SELECT * FROM orders WHERE status IN ('manual','payout_pending','need_recipient') "
                            "ORDER BY created_at DESC LIMIT 20")
        alerts = st.db.q("SELECT * FROM alerts WHERE acknowledged=0 ORDER BY id DESC LIMIT 12")
        return render("dashboard.html", request, s1=sh.summary(1), s7=sh.summary(7), points=sh.rtba.points(),
                      rates=sh.rates.snapshot(), treasury=st.db.kv_get("treasury") or {}, attention=attention,
                      alerts=alerts)

    async def orders(request):
        q, status_f, kind = request.query.get("q", ""), request.query.get("status", ""), request.query.get("kind", "")
        sql, args = "SELECT o.*, u.username FROM orders o LEFT JOIN users u ON u.id=o.user_id WHERE 1=1", []
        if status_f:
            sql += " AND o.status=?"
            args.append(status_f)
        if kind:
            sql += " AND o.kind=?"
            args.append(kind)
        if q:
            sql += " AND (o.id LIKE ? OR o.recipient LIKE ? OR u.username LIKE ?)"
            args += [f"%{q}%"] * 3
        rows = st.db.q(sql + " ORDER BY o.created_at DESC LIMIT 300", args)
        return render("orders.html", request, rows=rows, q=q, status_f=status_f, kind=kind)

    async def order(request):
        o = st.db.order(request.match_info["oid"])
        if o is None:
            raise web.HTTPNotFound()
        return render("order.html", request, o=o, u=st.db.user(o["user_id"]), events=st.db.events(o["id"]),
                      payments=st.db.q("SELECT * FROM payments WHERE order_id=?", (o["id"],)))

    async def order_action(request):
        oid = request.match_info["oid"]
        f = await request.post()
        a = f.get("action")
        sh = st.shop
        if a == "delivered":
            res = await sh.admin_delivered(oid)
        elif a == "payout":
            res = await sh.payout(oid, approved_by=request["role"])
        elif a == "reject":
            res = await sh.reject(oid, str(f.get("reason") or "отклонено оператором"))
        elif a == "gift":
            res = await sh.confirm_gift_received(oid, who=request["role"])
        elif a == "asset":
            res = await sh.confirm_asset(oid, who=request["role"])
        elif a == "note":
            st.db.update_order(oid, note=str(f.get("text") or "")[:500])
            res = "заметка сохранена"
        else:
            res = "?"
        st.db.audit(request["role"], f"order_{a}", oid, res)
        raise web.HTTPFound(f"/orders/{oid}?msg={res}")

    async def loop_page(request):
        return render("loop.html", request, s=st.shop.loop.summary(), rows=st.shop.loop.rows(200),
                      miniapp=(st.shop.miniapp.status() if st.shop.miniapp else st.db.kv_get("miniapp") or {}))

    async def loop_action(request):
        f = await request.post()
        res = st.shop.loop.mark_done(int(f.get("id") or 0), str(f.get("note") or ""))
        st.db.audit(request["role"], "loop_done", str(f.get("id")), res)
        raise back(request, res)

    async def customers(request):
        q = request.query.get("q", "")
        sql = "SELECT * FROM users"
        args: list = []
        if q:
            sql += " WHERE username LIKE ? OR first_name LIKE ? OR CAST(id AS TEXT)=?"
            args = [f"%{q}%", f"%{q}%", q]
        rows = st.db.q(sql + " ORDER BY last_seen DESC LIMIT 300", args)
        return render("customers.html", request, rows=rows, q=q)

    async def customer(request):
        uid = int(request.match_info["uid"])
        u = st.db.user(uid)
        if u is None:
            raise web.HTTPNotFound()
        return render("customer.html", request, u=u,
                      orders=st.db.q("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC", (uid,)),
                      ledger=st.db.q("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 100", (uid,)),
                      invited=st.db.q("SELECT * FROM users WHERE referred_by=?", (uid,)),
                      referrer=st.db.user(u["referred_by"]) if u["referred_by"] else None)

    async def customer_action(request):
        uid = int(request.match_info["uid"])
        f = await request.post()
        a = f.get("action")
        if a == "block":
            st.db.exec("UPDATE users SET blocked=1 WHERE id=?", (uid,))
        elif a == "unblock":
            st.db.exec("UPDATE users SET blocked=0 WHERE id=?", (uid,))
        elif a == "note":
            st.db.exec("UPDATE users SET note=?, tags=? WHERE id=?", (str(f.get("note") or "")[:500],
                                                                      str(f.get("tags") or "")[:200], uid))
        elif a == "credit":
            need(request, "admin")
            try:
                st.db.credit(uid, str(f.get("account") or "rub"), float(f.get("amount") or 0), "manual",
                             f"panel:{request['role']}", str(f.get("reason") or "корректировка"))
            except ValueError as e:
                raise web.HTTPFound(f"/customers/{uid}?msg={e}")
        st.db.audit(request["role"], f"customer_{a}", str(uid), "ok")
        raise web.HTTPFound(f"/customers/{uid}?msg=готово")

    async def rtba_page(request):
        log = st.db.q("SELECT * FROM rtba_log ORDER BY id DESC LIMIT 200")
        return render("rtba.html", request, points=st.shop.rtba.points(), log=log,
                      fails=st.db.q("SELECT * FROM supplier_fails ORDER BY id DESC LIMIT 30"))

    async def rtba_action(request):
        need(request, "admin")
        f = await request.post()
        if f.get("action") == "tick":
            await st.shop.rtba.tick()
            raise web.HTTPFound("/rtba?msg=проверено")
        st.shop.rtba.set_mode(str(f.get("key")), str(f.get("mode")), request["role"])
        await st.shop.rtba.tick()
        st.db.audit(request["role"], "rtba_mode", str(f.get("key")), str(f.get("mode")))
        raise web.HTTPFound("/rtba?msg=режим сохранён")

    async def channel_page(request):
        posts = await st.shop.channel.build()
        return render("channel.html", request, posts=posts, enabled=st.cfg.get("channel.enabled"),
                      chat_id=st.cfg.get("channel.chat_id"))

    async def channel_action(request):
        need(request, "admin")
        f = await request.post()
        res = await st.shop.channel.publish(str(f.get("post")))
        st.db.audit(request["role"], "channel_publish", str(f.get("post")), res)
        raise web.HTTPFound(f"/channel?msg={res}")

    async def settings(request):
        if request.method == "POST":
            need(request, "admin")
            f = await request.post()
            changes: dict = {}
            from shopbot.config import merge
            for path, _, typ in SETTINGS:
                if typ == "bool":
                    val = f.get(path) == "on"
                elif path not in f:
                    continue
                elif typ == "float":
                    try:
                        val = float(str(f[path]).replace(",", "."))
                    except ValueError:
                        continue
                    if path == "payments.crypto.fee_pct":
                        val = max(val, 2.0)            # минимальная комиссия конвертации — 2%
                else:
                    val = str(f[path]).strip()
                changes = merge(changes, nest(path, val))
            for path, _ in SECRET_FIELDS:
                v = str(f.get(path) or "").strip()
                if v:
                    changes = merge(changes, nest(path, v))
            st.cfg.save(changes)
            st.db.audit(request["role"], "settings", "", ",".join(sorted(changes)))
            await st.shop.rtba.tick()
            raise web.HTTPFound("/settings?msg=сохранено (токены бота и ключи оплаты — после перезапуска)")
        fields = [(p, t, typ, st.cfg.get(p)) for p, t, typ in SETTINGS]
        secrets_ = [(p, t, bool(st.cfg.get(p))) for p, t in SECRET_FIELDS]
        return render("settings.html", request, fields=fields, secrets=secrets_)

    async def alerts_ack(request):
        st.db.exec("UPDATE alerts SET acknowledged=1")
        raise back(request, "тревоги прочитаны")

    # ------------------------------------------------------------------ демо-чат
    async def demo_page(request):
        if not st.demo:
            raise web.HTTPNotFound()
        users = st.db.q("SELECT * FROM users ORDER BY id")
        return render("demo.html", request, users=users, uid=int(request.query.get("uid") or 7777))

    async def demo_chat(request):
        if not st.demo:
            raise web.HTTPNotFound()
        uid = int(request.query.get("uid") or 7777)
        since = float(request.query.get("since") or 0)
        msgs = [m for m in st.bot.outbox if m["chat"] == uid and m["ts"] > since]
        return web.json_response({"messages": msgs})

    async def demo_send(request):
        if not st.demo:
            raise web.HTTPNotFound()
        d = await request.json()
        uid = int(d.get("uid") or 7777)
        if d.get("data"):
            await st.bot.on_callback(uid, str(d["data"]))
        else:
            await st.bot.on_text(uid, str(d.get("username") or f"demo{uid}"), "Демо", str(d.get("text") or ""))
        await st.shop.poll_payments()
        await st.shop.poll_deliveries()
        return web.json_response({"ok": True})

    async def demo_pay(request):
        pid = request.match_info["pid"]
        for p in st.shop.providers.values():
            if pid in getattr(p, "payments", {}):
                p.pay(pid)
        await st.shop.poll_payments()
        return web.Response(text="<h3>Демо-оплата прошла ✅</h3><p>Вернитесь в демо-чат.</p>", content_type="text/html")

    app = web.Application(middlewares=[auth])
    app.add_routes([
        web.get("/login", login), web.post("/login", login), web.get("/api/ping", ping),
        web.get("/app", miniapp), web.get("/app/", miniapp),
        web.get("/api/status", status), web.get("/api/sources", api_sources), web.get("/api/reviews", api_reviews),
        web.get("/api/economy", api_economy), web.get("/", dashboard), web.get("/orders", orders),
        web.get("/orders/{oid}", order), web.post("/orders/{oid}", order_action), web.get("/customers", customers),
        web.get("/loop", loop_page), web.post("/loop", loop_action),
        web.get(r"/customers/{uid:\d+}", customer), web.post(r"/customers/{uid:\d+}", customer_action),
        web.get("/rtba", rtba_page), web.post("/rtba", rtba_action), web.get("/channel", channel_page),
        web.post("/channel", channel_action), web.get("/settings", settings), web.post("/settings", settings),
        web.post("/alerts/ack", alerts_ack), web.get("/demo", demo_page), web.get("/api/demo/chat", demo_chat),
        web.post("/api/demo/send", demo_send), web.get("/demo/pay/{pid}", demo_pay),
    ])
    return app
