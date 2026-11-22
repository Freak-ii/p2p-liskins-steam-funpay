"""Контракт поставщика для ShopBot (Telegram-магазин экосистемы).

ShopBot продаёт в Telegram звёзды, подарки и Premium, а выдаёт их через этот бот: заказ магазина становится
обычным заказом StarsBot с channel='shop', поэтому кошелёк TON, seqno, очередь покупок, лимиты и риск-проверки
общие с FunPay/Ozon/GGSEL — двойной траты нет.

Доступ: заголовок X-Supply-Token = supply.token из config.yaml (минимум 16 символов; пусто — API выключен).
  GET  /api/supply/ping                         → {app, ok, products}
  GET  /api/supply/quote?product=stars&amount=N → {available, cost_rub, cost_ton, reason}
       product=gift&amount=<звёзд в подарке>[&gift=<эмодзи>&gift_title=<название>], product=premium&months=3|6|12
       (TG Gift выдаёт именно этот подарок; у StarsBot подарок — звёзды набором подарков)
  POST /api/supply/orders {ref, product, username, amount|months[, gift, gift_title]} → {ok, order_id, status}
  GET  /api/supply/orders/{ref}                 → {status: queued|delivering|delivered|failed|manual, order_id, detail}
ref идемпотентен: повторный POST с тем же ref возвращает уже созданный заказ.
"""
from __future__ import annotations

import hmac
import re
import time
from typing import Any

from aiohttp import web

from starsbot.core.models import OrderStatus as S

REF_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
BAD_USERNAME = {"not_found", "cannot_receive", "bad_format", "blacklisted"}
STATUS_MAP = {
    "new": "queued", "awaiting_username": "queued", "pending_approval": "queued", "ready": "queued",
    "purchasing": "delivering", "sent": "delivering", "delivered": "delivered", "completed": "delivered",
    "refunded": "failed", "blocked": "failed", "manual": "manual",
}


class ShopChannel:
    """Канал заказов магазина: покупатель общается с ShopBot, поэтому здесь писать некому."""

    name = "shop"

    def __init__(self, engine):
        self.engine = engine

    async def say(self, o, key: str, variables: dict) -> None:
        return None

    async def say_text(self, o, text: str) -> None:
        return None

    async def on_delivered(self, o) -> None:
        self.engine.db.update_order(o["order_id"], status=S.COMPLETED.value, completed_at=time.time())

    async def stock_sync(self, balance: float, per_star, committed: int) -> None:
        return None

    def loops(self, make_loop) -> list:
        return []

    def health_limits(self) -> dict:
        return {}


def supply_id(ref: str) -> str:
    return f"SHOP-{ref}"


def order_status(o) -> dict:
    if o is None:
        return {"status": "unknown", "order_id": None, "detail": "нет такого заказа"}
    st = STATUS_MAP.get(str(o["status"]), "queued")
    if str(o["status"]) == "awaiting_username" and (o["fail_reason"] or "") in BAD_USERNAME:
        st = "failed"                       # магазин сам переспросит @username у покупателя
    return {"status": st, "order_id": o["order_id"], "detail": o["fail_reason"] or "",
            "tx_hash": o["tx_hash"] or "", "stars": o["stars"]}


def _shop_gift(engine, amount: int, gift: str = "", title: str = "") -> tuple[dict | None, str]:
    """Для TG Gift (профиль giftshop): конкретный подарок заказа магазина. У StarsBot подарки — это звёзды,
    отправленные набором подарков, поэтому там (None, "")."""
    g = engine.gifts
    if g is None or not hasattr(g, "supply_lot"):
        return None, ""
    lot = g.supply_lot(amount, gift, title)
    if lot is None:
        what = f"{gift} " if gift else ""
        return None, f"подарка {what}за {amount} ⭐ сейчас нет в каталоге Telegram" + \
            ("" if gift else " (или их несколько — передайте эмодзи подарка)")
    return lot, ""


def quote(engine, product: str, amount: int = 0, months: int = 0, gift: str = "", title: str = "") -> dict:
    if engine.paused:
        return {"available": False, "reason": "StarsBot на паузе"}
    if product == "premium":
        prem = getattr(engine, "premium", None)
        if prem is None:
            return {"available": False, "reason": "Premium выключен в StarsBot"}
        return prem.supply_quote(months)
    if amount < 50 and product == "stars":
        return {"available": False, "reason": "минимум 50 звёзд"}
    per_rub, per_ton = engine.cost_per_star_rub(), engine.cost_per_star_ton()
    if product == "gift":
        g = engine.gifts
        if g is None or not g.enabled:
            return {"available": False, "reason": "подарки выключены"}
        shop = hasattr(g, "supply_lot")    # TG Gift: звёзды на аккаунт подарков поставляет StarsBot
        if shop:
            lot, why = _shop_gift(engine, amount, gift, title)
            if lot is None:
                return {"available": False, "reason": why}
        if not per_rub or (not per_ton and not shop):
            return {"available": False, "reason": "нет курса звезды (ещё не было покупок и котировок)"}
        need = g.cost_for(amount)          # звёзд аккаунта на этот подарок
        free = g.capacity(g.balance)
        ok = free >= need or bool(g.topup_on and (engine.fragment_ok or shop))
        out = {"available": ok, "reason": "" if ok else f"на аккаунте {free} ⭐, нужно {need}",
               "cost_rub": round(need * per_rub, 2), "cost_ton": round(need * (per_ton or 0), 6), "stars_needed": need}
        if shop:
            out["gift"] = {"emoji": lot.get("emoji") or "", "name": lot.get("name") or "", "stars": int(lot["stars"])}
        return out
    if not per_rub or not per_ton:
        return {"available": False, "reason": "нет курса звезды (ещё не было покупок и котировок)"}
    if product != "stars":
        return {"available": False, "reason": f"неизвестный товар {product}"}
    w = engine.wallet_info or {}
    cap = w.get("capacity")
    ok = bool(engine.fragment_ok) and (cap is None or int(cap) >= amount)
    reason = "" if ok else ("Fragment не подключён" if not engine.fragment_ok else f"TON хватит на {cap} ⭐")
    return {"available": ok, "reason": reason, "cost_rub": round(amount * per_rub, 2),
            "cost_ton": round(amount * per_ton, 6)}


async def create(engine, ref: str, product: str, username: str, amount: int = 0, months: int = 0,
                 gift: str = "", title: str = "") -> dict:
    if product == "premium":
        prem = getattr(engine, "premium", None)
        if prem is None:
            return {"ok": False, "error": "Premium выключен в StarsBot"}
        return await prem.supply_order(ref, username, months, channel="shop")
    oid = supply_id(ref)
    existing = engine.db.get_order(oid)
    if existing is not None:
        return {"ok": True, **order_status(existing)}
    q = quote(engine, product, amount, gift=gift, title=title)
    if not q.get("available"):
        return {"ok": False, "error": q.get("reason") or "недоступно"}
    is_gift = product == "gift"
    lot = _shop_gift(engine, amount, gift, title)[0] if is_gift else None
    if lot is not None:                     # TG Gift: один конкретный подарок, как лот FunPay
        from starsbot.giftshop.service import lot_title
        engine.db.kv_set(f"shop_gift:{oid}", lot)
        description = f"{lot_title(lot)} · магазин"
    else:
        description = f"{amount} звёзд, {'Подарком' if is_gift else 'По username'} · магазин"
    engine.db.create_order(order_id=oid, buyer_username="(магазин)", buyer_id=0, chat_id=None,
                           description=description, lot_offer_id=str(lot.get("offer_id") or "") if lot else None,
                           quantity=1, stars_per_unit=amount, stars=amount, price=0, currency="₽",
                           fp_status="shop", channel="shop", delivery="gift" if is_gift else "fragment",
                           approved=1, lang="ru", notes=f"ShopBot ref {ref}")   # антифрод — в ShopBot (RTBA)
    engine.db.add_event(oid, "created", stars=amount, shop_ref=ref, product=product,
                        **({"gift": description} if lot is not None else {}))
    await engine.accept_username(engine.db.get_order(oid), username, source="shop")
    return {"ok": True, **order_status(engine.db.get_order(oid))}


def status(engine, ref: str) -> dict:
    o = engine.db.get_order(supply_id(ref))
    if o is None:
        prem = getattr(engine, "premium", None)
        if prem is not None:
            return prem.supply_status(ref)
    return order_status(o)


def install(app: web.Application, sup) -> None:
    """Регистрирует /api/supply/* и канал «shop». Вызывается из web/server.py одной строкой."""

    def engine():
        e = sup.engine
        if "shop" not in e.channels:
            e.channels["shop"] = ShopChannel(e)
        return e

    def check(request: web.Request) -> None:
        token = str((sup.cfg.get("supply") or {}).get("token") or "")
        given = request.headers.get("X-Supply-Token", "")
        if len(token) < 16 or not hmac.compare_digest(given, token):
            raise web.HTTPUnauthorized(text="bad supply token")

    async def ping(request):
        check(request)
        e = engine()
        products = ["stars"] + (["gift"] if e.gifts is not None and e.gifts.enabled else []) + \
            (["premium"] if getattr(e, "premium", None) is not None else [])
        app_name = "tggift" if getattr(e, "profile", "") == "giftshop" or hasattr(e.gifts, "supply_lot") else "starsbot"
        return web.json_response({"app": app_name, "ok": True, "products": products, "paused": e.paused})

    async def get_quote(request):
        check(request)
        q = request.query
        try:
            amount, months = int(q.get("amount") or 0), int(q.get("months") or 0)
        except ValueError:
            raise web.HTTPBadRequest(text="amount/months")
        return web.json_response(quote(engine(), q.get("product", "stars"), amount, months,
                                       gift=str(q.get("gift") or "")[:8], title=str(q.get("gift_title") or "")[:40]))

    async def post_order(request):
        check(request)
        d: dict[str, Any] = await request.json()
        ref, product = str(d.get("ref") or ""), str(d.get("product") or "stars")
        username = str(d.get("username") or "").lstrip("@")
        if not REF_RE.match(ref) or not USER_RE.match(username):
            raise web.HTTPBadRequest(text="bad ref or username")
        try:
            res = await create(engine(), ref, product, username, int(d.get("amount") or 0), int(d.get("months") or 0),
                               gift=str(d.get("gift") or "")[:8], title=str(d.get("gift_title") or "")[:40])
        except (TypeError, ValueError) as e:
            raise web.HTTPBadRequest(text=str(e))
        sup.db.audit("supply", "shop_order", ref, str(res)[:300])
        return web.json_response(res)

    async def get_order(request):
        check(request)
        return web.json_response(status(engine(), request.match_info["ref"]))

    app.router.add_get("/api/supply/ping", ping)
    app.router.add_get("/api/supply/quote", get_quote)
    app.router.add_post("/api/supply/orders", post_order)
    app.router.add_get("/api/supply/orders/{ref}", get_order)


PUBLIC_PATHS = ("/api/supply/",)
