"""Контракт поставщика для ShopBot: ключи TF2 (Mann Co. Supply Crate Key) обменом Steam.

Тот же контракт, что у StarsBot (starsbot/supply_api.py), поэтому в ShopBot достаточно suppliers.steam_keys
kind=http, url=http://127.0.0.1:8801 и одного токена. Заказ магазина — обычный заказ MannBot с номером
SHOP-<ref>: тот же склад, резерв assetid, подтверждение Steam Guard, реестр обменов с LisBot и защита от
двойной выдачи. Чата FunPay у такого заказа нет, покупателю пишет ShopBot.

Доступ: заголовок X-Supply-Token = supply.token из config.yaml MannBot (минимум 16 символов; пусто — API выключен).
  GET  /api/supply/ping                                   → {app, ok, products}
  GET  /api/supply/quote?product=steam_keys&amount=N      → {available, cost_rub, free, reason}
  POST /api/supply/orders {ref, product, username|trade_url, amount} → {ok, order_id, status}
  GET  /api/supply/orders/{ref}                           → {status: queued|delivering|delivered|failed|manual, detail}
Ошибки данных покупателя (плохая ссылка, трейд-бан, задержка обмена, отклонённый трейд) — status=failed с кодом в
detail: ShopBot просит новую ссылку и создаёт новый ref. ref идемпотентен.
"""
from __future__ import annotations

import hmac
import re
from typing import Any

from aiohttp import web

from mannbot.core.models import OrderStatus as S

SHOP_PREFIX = "SHOP-"
REF_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
STATUS_MAP = {
    "new": "queued", "awaiting_link": "queued", "pending_approval": "manual", "ready_to_send": "queued",
    "offer_sent": "delivering", "escrow": "delivering", "delivered": "delivered", "completed": "delivered",
    "refunded": "failed", "manual": "manual", "blocked": "failed",
}
PRODUCTS = ("steam_keys", "tf2_keys")


def is_shop(o) -> bool:
    return o is not None and str(o["order_id"]).startswith(SHOP_PREFIX)


def supply_id(ref: str) -> str:
    return f"{SHOP_PREFIX}{ref}"


def order_status(o) -> dict:
    if o is None:
        return {"status": "unknown", "order_id": None, "detail": "нет такого заказа"}
    return {"status": STATUS_MAP.get(str(o["status"]), "queued"), "order_id": o["order_id"],
            "detail": o["fail_reason"] or "", "offer_id": o["offer_id"] or "", "keys": o["keys_qty"]}


def _cost_per_key(engine) -> float:
    return float(engine.cfg.rtba.get("cost_per_key") or 0) or float(engine.cfg.rtba.get("floor_price") or 0)


async def quote(engine, sup_state: str, product: str, amount: int) -> dict:
    if product not in PRODUCTS:
        return {"available": False, "reason": f"неизвестный товар {product}"}
    if sup_state != "running":
        return {"available": False, "reason": "MannBot не запущен"}
    if engine.paused:
        return {"available": False, "reason": "MannBot на паузе"}
    amount = max(1, int(amount or 1))
    if amount > int(engine.cfg.steam.max_keys_per_offer):
        return {"available": False, "reason": f"не больше {engine.cfg.steam.max_keys_per_offer} ключей за раз"}
    cost = _cost_per_key(engine)
    if cost <= 0:
        return {"available": False, "reason": "не задана себестоимость ключа (rtba.cost_per_key)"}
    free = await engine.available_keys()
    if free is None:
        return {"available": False, "reason": "инвентарь Steam недоступен"}
    ok = free >= amount
    return {"available": ok, "reason": "" if ok else f"свободно {max(free, 0)} ключей", "free": max(free, 0),
            "cost_rub": round(cost * amount, 2)}


async def create(engine, sup_state: str, ref: str, product: str, trade_url: str, amount: int) -> dict:
    oid = supply_id(ref)
    existing = engine.db.get_order(oid)
    if existing is not None:
        return {"ok": True, **order_status(existing)}
    q = await quote(engine, sup_state, product, amount)
    if not q.get("available"):
        return {"ok": False, "error": q.get("reason") or "недоступно"}
    keys = max(1, int(amount or 1))
    engine.db.create_order(order_id=oid, buyer_username="(магазин)", buyer_id=0, chat_id=None,
                           description=f"{keys} ключ(ей) TF2 · магазин", quantity=keys, keys_qty=keys, price=0,
                           currency="₽", fp_status="shop", lot_offer_id="", lang=engine.cfg.general.language,
                           approved=1)                      # антифрод — в ShopBot (RTBA)
    engine.db.add_event(oid, "created", keys=keys, shop_ref=ref, product=product)
    o = engine._set(oid, S.AWAITING_LINK, "shop_order")
    await engine.notifier.notify("INFO", "shop_order", f"🛍 Заказ магазина #{oid}: {keys} ключ(ей)", order_id=oid)
    await engine.handle_trade_link(o, trade_url)
    return {"ok": True, **order_status(engine.db.get_order(oid))}


def install(app: web.Application, sup) -> None:
    """Регистрирует /api/supply/*. Вызывается из web/server.py; путь /api/supply/ — без входа в панель."""

    def check(request: web.Request) -> None:
        token = str((sup.cfg.get("supply") or {}).get("token") or "")
        given = request.headers.get("X-Supply-Token", "")
        if len(token) < 16 or not hmac.compare_digest(given, token):
            raise web.HTTPUnauthorized(text="bad supply token")

    async def ping(request):
        check(request)
        return web.json_response({"app": "mannbot", "ok": True, "products": list(PRODUCTS[:1]),
                                  "running": sup.state == "running", "paused": sup.engine.paused})

    async def get_quote(request):
        check(request)
        try:
            amount = int(request.query.get("amount") or 1)
        except ValueError:
            raise web.HTTPBadRequest(text="amount")
        return web.json_response(await quote(sup.engine, sup.state, request.query.get("product", "steam_keys"),
                                             amount))

    async def post_order(request):
        check(request)
        d: dict[str, Any] = await request.json()
        ref = str(d.get("ref") or "")
        url = str(d.get("trade_url") or d.get("username") or "").strip()
        if not REF_RE.match(ref) or "steamcommunity.com/tradeoffer/new" not in url:
            raise web.HTTPBadRequest(text="bad ref or trade_url")
        try:
            res = await create(sup.engine, sup.state, ref, str(d.get("product") or "steam_keys"), url,
                               int(d.get("amount") or 1))
        except (TypeError, ValueError) as e:
            raise web.HTTPBadRequest(text=str(e))
        sup.db.add_event(supply_id(ref), "supply_api", result=str(res)[:300])
        return web.json_response(res)

    async def get_order(request):
        check(request)
        return web.json_response(order_status(sup.db.get_order(supply_id(request.match_info["ref"]))))

    app.router.add_get("/api/supply/ping", ping)
    app.router.add_get("/api/supply/quote", get_quote)
    app.router.add_post("/api/supply/orders", post_order)
    app.router.add_get("/api/supply/orders/{ref}", get_order)


PUBLIC_PREFIX = "/api/supply/"
