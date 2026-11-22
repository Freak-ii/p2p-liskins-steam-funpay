"""Страницы панели TG Gift: главная магазина подарков и «Подарки · лоты» (аккаунт, лоты, цены, снабжение)."""
from __future__ import annotations

import time

from aiohttp import web
from yarl import URL

from starsbot.core import messages as M


def shop_routes(sup, *, render, friendly, need, audit, num_in):
    """Возвращает (обработчик главной для TG Gift, список маршрутов). У StarsBot маршруты не регистрируются."""
    shop = getattr(sup, "profile", "stars") == "giftshop"

    def catalog_list():
        raw = getattr(sup.engine.gifts.tg, "_catalog", None)
        cat = raw[1] if isinstance(raw, tuple) else (raw or [])
        return sorted(cat, key=lambda g: (g.stars, g.emoji))

    async def shop_dashboard(request: web.Request):
        a, db, g = sup.analytics, sup.db, sup.engine.gifts
        if not sup.configured and not sup.status()["demo"] and not db.scalar("SELECT COUNT(*) FROM orders"):
            raise web.HTTPFound("/shop")
        days = int(float(request.query.get("days") or 7) or 7)
        stars = db.kv_get("tg_stars") or {}
        sends24 = db.scalar("SELECT COUNT(*) FROM gift_sends WHERE state='sent' AND updated_at>=?",
                            (time.time() - 86400,), 0)
        return render("shop_dashboard.html", request, s=a.summary(days), days=days, stars=stars, account=g.account,
                      committed=db.gift_stars_committed(), free=g.capacity(stars.get("balance")),
                      supply=g.supply_status(), sends24=sends24, rows=sup.engine.lot_manager.rows(),
                      active=db.active_orders()[:20], status=g.status, problems=sup.problems,
                      health=sup.engine.health() if sup.state == "running" else None,
                      feed=db.q("SELECT * FROM order_events WHERE order_id NOT IN ('-', '') ORDER BY id DESC LIMIT 25"))

    async def shop_page(request: web.Request):
        g = sup.engine.gifts
        return render("shop.html", request, rows=sup.engine.lot_manager.rows(), g=sup.cfg.get("gifts") or {},
                      sc=sup.cfg.get("giftshop") or {}, rtba=sup.cfg.rtba, account=g.account,
                      stars=sup.db.kv_get("tg_stars") or {}, committed=sup.db.gift_stars_committed(),
                      supply=g.supply_status(), cat=catalog_list(), svc=g, problems=sup.problems,
                      inherited=getattr(sup, "inherited", []), has_session=bool(getattr(g.tg, "has_session", False)),
                      login=request.query.get("login", ""), funpay=sup.cfg.funpay,
                      sends=sup.db.q("SELECT s.*, o.tg_username FROM gift_sends s LEFT JOIN orders o ON "
                                     "o.order_id=s.order_id ORDER BY s.id DESC LIMIT 30"),
                      secret=lambda s_, k: sup.secret_is_set(s_, k))

    @friendly("/shop")
    async def shop_action(request: web.Request):
        need(request, "admin")
        f = await request.post()
        act = str(f.get("action", ""))
        lm = sup.engine.lot_manager
        g = sup.engine.gifts
        if act == "save_settings":
            upd = {
                "gifts": {"api_id": num_in(f.get("api_id"), 0), "api_hash": str(f.get("api_hash", "")).strip(),
                          "gift_message": str(f.get("gift_message", ""))[:120],
                          "hide_sender_name": f.get("hide") == "on",
                          "pilot_orders": num_in(f.get("pilot_orders"), 0),
                          "max_order_stars": num_in(f.get("max_order_stars"), 1000),
                          "low_stars_alert": num_in(f.get("low_stars_alert"), 150)},
                "giftshop": {"min_margin_pct": num_in(f.get("min_margin_pct"), 5, float),
                             "rub_per_star": num_in(f.get("rub_per_star"), 1.35, float),
                             "auto_create_lots": f.get("auto_create") == "on",
                             "supply": {"enabled": f.get("supply_on") == "on",
                                        "min_stars": num_in(f.get("supply_min"), 300),
                                        "amount": max(50, num_in(f.get("supply_amount"), 1000))}},
                "rtba": {"enabled": f.get("rtba_on") == "on",
                         "target_position": max(1, num_in(f.get("target_position"), 3))},
            }
            key = str(f.get("golden_key", "")).strip()
            if key:
                upd["funpay"] = {"golden_key": key}
            if M.contains_forbidden_link(upd["gifts"]["gift_message"]):
                raise web.HTTPFound(URL("/shop").with_query(msg="В подписи к подарку нельзя ссылки (правила FunPay)"))
            sup.save(upd)
            msg = "Сохранено" + (". Перезапустите бота, чтобы применить" if sup.restart_required else "")
        elif act == "save_lot":
            i = num_in(f.get("i"), -1)
            msg = lm.update(i, str(f.get("list") or "shop"), offer_id=num_in(f.get("offer_id"), 0),
                            position=num_in(f.get("position"), 0),
                            price=num_in(f.get("price"), 0, float), sell=f.get("sell") == "on")
        elif act == "add_lot":
            emoji, name = str(f.get("emoji", "")).strip(), str(f.get("name", "")).strip()
            st = num_in(f.get("stars"), 0)
            msg = lm.add(emoji, name, st, num_in(f.get("gift_id"), 0)) if emoji and name and st > 0 else \
                "укажите эмодзи, название и цену подарка в звёздах"
        elif act == "create":
            msg = "; ".join(await lm.create_missing())
        elif act == "texts":
            msg = await lm.refresh_texts(force=True)
        elif act == "close_all":
            msg = await lm.close_all(reason="кнопка в панели")
        elif act == "stock":
            sup.engine.reset_lot_amounts()
            await g.watch()
            msg = "Наличие лотов пересчитано по балансу звёзд"
        elif act == "add_nft" and g.nft is not None:
            name = str(f.get("collection", "")).strip()
            if not name:
                msg = "укажите коллекцию (как на fragment.com/gifts)"
            else:
                coll = g.nft.collection(name)
                msg = lm.add_nft(coll, position=max(1, num_in(f.get("position"), 3)))
                await g.nft.quote(coll, fresh=True)
                msg += "; " + "; ".join(await lm.create_missing(kinds=["shop"]))
        elif act == "assort_scan" and g.assortment is not None:
            msg = await g.assortment.scan(force=True)
        elif act == "assort_save" and g.assortment is not None:
            a = dict(sup.cfg.giftshop.get("assortment") or {})
            a.update(enabled=f.get("enabled") == "on", max_lots=max(0, num_in(f.get("max_lots"), 8)),
                     max_cost_rub=num_in(f.get("max_cost_rub"), 5000, float),
                     min_margin_pct=num_in(f.get("min_margin_pct"), 8, float))
            sup.save({"giftshop": {"assortment": a}})
            msg = "Сохранено"
        elif act in ("assort_pin", "assort_ban") and g.assortment is not None:
            msg = await g.assortment.command([act.split("_")[1], str(f.get("slug", ""))])
        elif act == "assort_clear" and g.assortment is not None:
            a = dict(sup.cfg.giftshop.get("assortment") or {})
            a.update(pinned=[], banned=[])
            sup.save({"giftshop": {"assortment": a}})
            msg = "Закрепления и запреты сброшены"
        elif act == "supply":
            msg = await g.maybe_topup(need=max(1, num_in(f.get("amount"), 0)))
        else:
            msg = "?"
        audit(request, f"shop.{act}", "", msg)
        raise web.HTTPFound(URL("/shop").with_query(msg=str(msg)[:600]))

    if not shop:
        return None, []
    return shop_dashboard, [web.get("/shop", shop_page), web.post("/shop", shop_action)]
