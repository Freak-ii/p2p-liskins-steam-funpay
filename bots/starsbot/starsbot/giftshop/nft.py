"""NFT-подарки TG Gift: цена на Fragment против рынка Telegram и выдача самым дешёвым путём.

Коллекционный (улучшенный) подарок можно достать двумя путями:

* **Fragment** (основной): fragment.com/gifts продаёт подарки за TON. Покупает и отправляет получателю StarsBot —
  у него кошелёк TON и сессия Fragment (задание в общем файле координации, см. starsbot/nft_supply.py).
  Себестоимость = цена на Fragment + газ на покупку и перевод, по курсу TON, который сообщает StarsBot;
* **Telegram**: рынок улучшенных подарков внутри Telegram за звёзды. TG Gift покупает подарок своим аккаунтом сразу
  на аккаунт покупателя. Себестоимость = цена в звёздах × ₽ за звезду (звёзды поставляет StarsBot).

Перед каждой выдачей бот берёт свежие цены обоих путей и выбирает дешёвый, но только если заказ остаётся в плюсе
(выручка после комиссий минус себестоимость ≥ giftshop.nft.min_profit_pct). Если ни один путь не даёт плюса —
заказ уходит продавцу, а лот выключается (правило продавца: при проблеме с заказом закрывать лоты).
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from starsbot.core.models import OrderStatus as S
from starsbot.fragment.client import parse_gift_listings
from starsbot.gifts.telegram import GiftError, TgUser
from starsbot.giftshop.collections import COLLECTIONS, Collection, from_title, slug_of

log = logging.getLogger("giftshop.nft")

DEFAULTS = {
    "enabled": True,
    "routes": ["fragment", "telegram"],   # какими путями можно выдавать (порядок не важен — выбирается дешёвый)
    "min_profit_pct": 3.0,                # заказ не дешевле себестоимости + N% (иначе — продавцу, лот выключается)
    "fragment_extra_ton": 0.25,           # газ на покупку и перевод подарка, TON (к цене на Fragment)
    "rub_per_ton": 0.0,                   # курс TON для себестоимости, ₽ (0 — какой сообщит StarsBot, иначе биржа +4%)
    "quote_minutes": 10,                  # цены коллекции держим N минут (для лотов и автоцены)
    "fragment_wait_minutes": 45,          # сколько ждать выполнения задания StarsBot до сообщения продавцу
    "max_display_amount": 3,              # «наличие» NFT-лота на FunPay
    "block_hours": 6,                     # на сколько выключать лот после проблемы с заказом
}


class PublicFragment:
    """Публичные страницы fragment.com/gifts (без входа): самые дешёвые выставленные подарки коллекции."""

    def __init__(self, user_agent: str = "Mozilla/5.0", min_interval: float = 1.2):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = asyncio.Lock()
        self.session: aiohttp.ClientSession | None = None

    async def gift_listings(self, collection: str, limit: int = 10) -> list[dict]:
        async with self._lock:
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25),
                                                     headers={"User-Agent": self.user_agent,
                                                              "Accept-Language": "en,ru;q=0.8"})
            try:
                async with self.session.get(f"https://fragment.com/gifts/{collection}",
                                            params={"sort": "price_asc", "filter": "sale"}) as r:
                    if r.status != 200:
                        return []
                    page = await r.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.info("Fragment %s: %s", collection, e)
                return []
        return parse_gift_listings(page)[:limit]

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()


class FakePublicFragment:
    """Демо: цены коллекций на Fragment (TON)."""

    def __init__(self, floors: dict[str, list[float]] | None = None):
        self.floors = floors if floors is not None else {}

    async def gift_listings(self, collection: str, limit: int = 10) -> list[dict]:
        return [{"item": f"{collection}-{i + 1}", "price": p}
                for i, p in enumerate(sorted(self.floors.get(collection, [])))][:limit]

    async def close(self) -> None:
        return None


class NftDesk:
    def __init__(self, svc, public=None):
        self.svc = svc                      # ShopGiftService
        self.engine = svc.engine
        self.db = svc.db
        self.public = public or PublicFragment()
        self._quotes: dict[str, tuple[float, dict]] = {}
        self.db.exec("CREATE TABLE IF NOT EXISTS nft_sends (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, "
                     "seq INTEGER, collection TEXT, route TEXT, ref TEXT, item TEXT, state TEXT, cost_rub REAL, "
                     "cost_ton REAL, cost_stars INTEGER, error TEXT, created_at REAL, updated_at REAL)")

    @property
    def c(self) -> dict:
        return {**DEFAULTS, **((self.svc.scfg.get("nft") or {}))}

    @property
    def enabled(self) -> bool:
        return bool(self.c.get("enabled", True))

    # ------------------------------------------------------------------ курсы
    def starsbot_info(self) -> dict:
        return self.engine.coord.get_info("starsbot", max_age=900) or {}

    def fragment_ready(self) -> bool:
        return "fragment" in (self.c.get("routes") or []) and bool(self.starsbot_info().get("nft"))

    def rub_per_ton(self) -> float | None:
        fixed = float(self.c.get("rub_per_ton") or 0)
        if fixed > 0:
            return fixed
        info = self.starsbot_info()
        if info.get("rub_per_ton"):
            return float(info["rub_per_ton"])
        v = getattr(self.engine.rates, "value", None)
        if v and getattr(self.engine.rates, "is_p2p", False):
            return round(float(v), 2)                  # уже цена закупки, надбавка не нужна
        return round(float(v) * 1.04, 2) if v else None

    def rub_per_star(self) -> float:
        return float(self.svc.cost_per_star_rub())

    # ------------------------------------------------------------------ коллекции и каталог
    def collection(self, lot_or_slug) -> Collection:
        if isinstance(lot_or_slug, dict):
            slug = str(lot_or_slug.get("collection") or slug_of(lot_or_slug.get("name_en") or lot_or_slug.get("name")))
            c = COLLECTIONS.get(slug)
            return c or Collection(slug, str(lot_or_slug.get("name_en") or lot_or_slug.get("name") or slug),
                                   str(lot_or_slug.get("name") or slug))
        return COLLECTIONS.get(str(lot_or_slug)) or from_title(str(lot_or_slug))

    def catalog_gift(self, coll: Collection):
        """Подарок-основа коллекции в каталоге Telegram (по названию): там id для рынка и мин. цена в звёздах."""
        raw = getattr(self.svc.tg, "_catalog", None)
        cat = raw[1] if isinstance(raw, tuple) else (raw or [])
        for g in cat:
            if g.title and slug_of(g.title) == coll.slug:
                return g
        return None

    def known_collections(self) -> dict[str, Collection]:
        out = dict(COLLECTIONS)
        raw = getattr(self.svc.tg, "_catalog", None)
        for g in (raw[1] if isinstance(raw, tuple) else (raw or [])):
            if g.title and (g.resale or g.resell_min) and slug_of(g.title) not in out:
                out[slug_of(g.title)] = from_title(g.title)
        return out

    # ------------------------------------------------------------------ котировка обоих путей
    async def quote(self, coll: Collection, *, fresh: bool = False) -> dict:
        """{'fragment': {...}|None, 'telegram': {...}|None, 'best': 'fragment'|'telegram'|None, 'cost_rub': …}"""
        hit = self._quotes.get(coll.slug)
        if hit and not fresh and time.time() - hit[0] < float(self.c["quote_minutes"]) * 60:
            return hit[1]
        routes = self.c.get("routes") or []
        q: dict = {"slug": coll.slug, "title": coll.en, "fragment": None, "telegram": None, "ts": time.time(),
                   "notes": []}
        rate = self.rub_per_ton()
        if "fragment" in routes:
            listings = await self.public.gift_listings(coll.slug, limit=5)
            if listings and rate:
                ton = float(listings[0]["price"])
                extra = float(self.c["fragment_extra_ton"])
                q["fragment"] = {"ton": ton, "extra_ton": extra, "rub": round((ton + extra) * rate, 2),
                                 "count": len(listings), "item": listings[0]["item"], "rate": rate,
                                 "ready": self.fragment_ready()}
            elif listings:
                q["notes"].append("нет курса TON — Fragment не оценить")
            else:
                q["notes"].append("на Fragment нет выставленных")
        if "telegram" in routes:
            g = self.catalog_gift(coll)
            listings = []
            if g is not None:
                try:
                    listings = await self.svc.tg.resale_listings(g.id, limit=5)
                except (GiftError, AttributeError) as e:
                    q["notes"].append(f"рынок Telegram: {e}")
            if listings:
                stars = int(listings[0]["stars"])
                q["telegram"] = {"stars": stars, "rub": round(stars * self.rub_per_star(), 2), "count": len(listings),
                                 "slug": listings[0]["slug"], "gift_id": g.id}
            elif g is not None and g.resell_min:
                q["telegram"] = {"stars": int(g.resell_min), "rub": round(g.resell_min * self.rub_per_star(), 2),
                                 "count": int(g.resale or 0), "slug": "", "gift_id": g.id, "estimate": True}
        # себестоимость — по путям, которыми бот реально может выдать: Fragment только когда StarsBot готов
        opts = [(r, q[r]["rub"]) for r in ("fragment", "telegram") if q[r] and (q[r].get("ready") if r == "fragment" else q[r].get("slug"))]
        q["best"], q["cost_rub"] = (min(opts, key=lambda x: x[1]) if opts else (None, None))
        if q["fragment"] and not q["fragment"]["ready"]:
            q["notes"].append("Fragment: кошелёк StarsBot не готов (нет связи через общий файл или выключено)")
        if q["fragment"] and q["telegram"]:
            q["saving_rub"] = round(abs(q["fragment"]["rub"] - q["telegram"]["rub"]), 2)
        self._quotes[coll.slug] = (time.time(), q)
        self.db.kv_set(f"nft_quote:{coll.slug}", q)
        return q

    def cached_quote(self, coll: Collection) -> dict | None:
        hit = self._quotes.get(coll.slug)
        return hit[1] if hit else self.db.kv_get(f"nft_quote:{coll.slug}")

    # ------------------------------------------------------------------ экономика заказа
    def revenue_per_item(self, o) -> float:
        qty = max(1, int(o["quantity"] or 1))
        price = float(o["price"] or 0) / qty
        econ = self.engine.econ
        keep = 1.0
        if econ is not None:
            f, w = econ.fee_pcts("funpay")
            keep = (1 - f / 100) * (1 - w / 100)
        return round(price * keep, 2)

    def max_cost(self, o) -> float:
        return round(self.revenue_per_item(o) / (1 + float(self.c["min_profit_pct"]) / 100), 2)

    def floor_price(self, coll: Collection, q: dict | None = None) -> float | None:
        """Минимальная цена лота: себестоимость дешёвого пути + маржа магазина, с поправкой на комиссии."""
        q = q or self.cached_quote(coll)
        if not q or not q.get("cost_rub"):
            return None
        margin = max(float(self.svc.scfg.get("min_margin_pct") or 0), float(self.c["min_profit_pct"]))
        fee = self.engine.econ.fees_pct() / 100 if self.engine.econ is not None else 0.0
        return round(float(q["cost_rub"]) * (1 + margin / 100) / max(1e-9, 1 - fee), 2)

    # ------------------------------------------------------------------ выдача
    def sends(self, oid: str) -> list:
        return self.db.q("SELECT * FROM nft_sends WHERE order_id=? ORDER BY seq, id", (oid,))

    async def deliver(self, o, lot: dict) -> None:
        """Заказ NFT-лота в статусе READY: по одному подарку на единицу количества, каждый — дешёвым путём."""
        e = self.engine
        oid = o["order_id"]
        coll = self.collection(lot)
        qty = max(1, int(o["quantity"] or 1))
        rows = self.sends(oid)
        if any(r["state"] in ("sending", "unknown") for r in rows):
            e._set(oid, S.MANUAL, "nft_unknown", fail_reason="nft_unknown")
            await e.notifier.notify("CRITICAL", "nft_unknown", f"#{oid}: исход покупки NFT {coll.en} неизвестен — "
                                    "проверьте подарки получателя и отметьте вручную.", order_id=oid)
            return
        waiting = [r for r in rows if r["state"] == "waiting"]
        if waiting:
            e._set(oid, S.PURCHASING, "nft_waiting")
            return
        sent = [r for r in rows if r["state"] == "sent"]
        if len(sent) >= qty:
            await self.finish(oid)
            return
        if not o["tg_recipient"] or ":" not in str(o["tg_recipient"]):
            e._set(oid, S.AWAITING_USERNAME, "no_recipient")
            await self.svc.accept_username(e.db.get_order(oid), o["tg_username"])
            return
        user_id, access_hash = (int(x) for x in str(o["tg_recipient"]).split(":", 1))
        user = TgUser(user_id, access_hash, o["tg_username"] or "", o["tg_name"] or "")
        seq = len(sent)
        failed_routes = {r["route"] for r in rows if r["seq"] == seq and r["state"] == "failed"}
        q = await self.quote(coll, fresh=True)
        limit = self.max_cost(o)
        options = []
        for route in ("fragment", "telegram"):
            info = q.get(route)
            if not info or route in failed_routes:
                continue
            if route == "fragment" and not info.get("ready"):
                continue
            if route == "telegram" and not info.get("slug"):
                continue
            options.append((info["rub"], route, info))
        options.sort(key=lambda x: x[0])
        good = [x for x in options if x[0] <= limit]
        if not good:
            best = options[0][0] if options else None
            await self.no_margin(o, lot, coll, best, limit, q)
            return
        cost, route, info = good[0]
        e._set(oid, S.PURCHASING, f"nft_{route}", purchase_started_at=time.time(), fail_reason="",
               purchase_attempts=int(o["purchase_attempts"] or 0) + 1)
        if route == "telegram":
            await self._via_telegram(o, coll, user, seq, info, limit)
        else:
            await self._via_fragment(o, coll, seq, info, limit)

    async def _via_telegram(self, o, coll: Collection, user: TgUser, seq: int, info: dict, limit_rub: float) -> None:
        e, svc = self.engine, self.svc
        oid = o["order_id"]
        rps = self.rub_per_star()
        max_stars = int(limit_rub / rps) if rps else int(info["stars"])
        balance = await svc.refresh_balance()
        if balance is not None and balance < int(info["stars"]):
            e._set(oid, S.READY, "nft_no_stars", gift_cost=int(info["stars"]))
            await svc.out_of_stars(e.db.get_order(oid), balance, int(info["stars"]))
            return
        now = time.time()
        cur = e.db.exec("INSERT INTO nft_sends(order_id,seq,collection,route,item,state,cost_stars,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (oid, seq, coll.slug, "telegram", info["slug"], "sending",
                                                      int(info["stars"]), now, now))
        sid = int(cur.lastrowid)
        e._set(oid, gift_cost=int(info["stars"]))
        try:
            res = await svc.tg.buy_resale(user, info["slug"], max_stars)
        except GiftError as err:
            if err.code in ("unknown", "network"):
                e.db.exec("UPDATE nft_sends SET state='unknown', error=?, updated_at=? WHERE id=?",
                          (str(err)[:300], time.time(), sid))
                e._set(oid, S.MANUAL, "nft_unknown", fail_reason="nft_unknown")
                await e.notifier.notify("CRITICAL", "nft_unknown", f"#{oid}: связь оборвалась при покупке NFT "
                                        f"{coll.en} на рынке Telegram ({err}). Проверьте историю звёзд аккаунта: если "
                                        "подарок ушёл — «Выдан вручную», если нет — «Одобрить».", order_id=oid)
                return
            e.db.exec("UPDATE nft_sends SET state='failed', error=?, updated_at=? WHERE id=?",
                      (str(err)[:300], time.time(), sid))
            if err.code == "no_stars":
                e._set(oid, S.READY, "nft_no_stars")
                await svc.out_of_stars(e.db.get_order(oid), await svc.refresh_balance() or 0, int(info["stars"]))
                return
            if err.code in ("not_found", "cannot_receive", "bad_format") and seq == 0:
                e._set(oid, S.AWAITING_USERNAME, "gift_rejected")
                await e.bad_username(e.db.get_order(oid), err.code, str(err))
                return
            # подарок успели купить другие / цена выросла — берём следующий вариант (свежая котировка)
            self._quotes.pop(coll.slug, None)
            e._set(oid, S.READY, "nft_retry", fail_reason=err.code)
            tries = int(e.db.scalar("SELECT COUNT(*) FROM nft_sends WHERE order_id=? AND seq=? AND route IN "
                                    "('telegram','telegram_try') AND state='failed'", (oid, seq), 0))
            if tries < 3:                      # ещё попытка рынком Telegram; после трёх — только другой путь
                e.db.exec("UPDATE nft_sends SET route='telegram_try' WHERE id=?", (sid,))
            e.enqueue(oid, 5)
            return
        stars = int(res.get("stars") or info["stars"])
        e.db.exec("UPDATE nft_sends SET state='sent', cost_stars=?, cost_rub=?, updated_at=? WHERE id=?",
                  (stars, round(stars * rps, 2), time.time(), sid))
        if svc.balance is not None:
            svc.balance = max(0, svc.balance - stars)
        svc.balance_ts = 0
        await self.continue_order(oid)

    async def _via_fragment(self, o, coll: Collection, seq: int, info: dict, limit_rub: float) -> None:
        e = self.engine
        oid = o["order_id"]
        rate = float(info.get("rate") or self.rub_per_ton() or 0)
        max_ton = round(limit_rub / rate - float(self.c["fragment_extra_ton"]), 3) if rate else 0
        ref = f"{oid}#{seq}#{int(time.time())}"
        if max_ton <= 0 or not e.coord.nft_job_add(ref, collection=coll.slug, title=coll.en,
                                                   username=str(o["tg_username"] or ""), max_ton=max_ton,
                                                   note=f"заказ FunPay #{oid}"):
            e._set(oid, S.READY, "nft_fragment_unavailable")
            now = time.time()
            e.db.exec("INSERT INTO nft_sends(order_id,seq,collection,route,ref,state,error,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (oid, seq, coll.slug, "fragment", ref, "failed",
                                                    "задание StarsBot не создано", now, now))
            e.enqueue(oid, 5)
            return
        now = time.time()
        e.db.exec("INSERT INTO nft_sends(order_id,seq,collection,route,ref,item,state,cost_ton,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (oid, seq, coll.slug, "fragment", ref, info.get("item") or "",
                                                  "waiting", info.get("ton"), now, now))
        e._set(oid, S.PURCHASING, "nft_fragment_job", fail_reason="")
        await e.notifier.notify("INFO", "nft_job", f"🎁 #{oid}: NFT {coll.en} через Fragment (≈{info['ton']:g} TON, "
                                f"не дороже {max_ton:g}) — задание StarsBot", order_id=oid)

    async def watch(self) -> None:
        """Раз в минуту: состояние заданий Fragment у StarsBot."""
        e = self.engine
        for r in self.db.q("SELECT * FROM nft_sends WHERE state='waiting' AND route='fragment'"):
            job = e.coord.nft_job(r["ref"]) if e.coord.enabled else None
            oid = r["order_id"]
            if job is None:
                continue
            st = job["state"]
            if st == "done":
                self.db.exec("UPDATE nft_sends SET state='sent', item=?, cost_rub=?, cost_ton=?, updated_at=? WHERE "
                             "id=?", (job.get("item") or r["item"], job.get("cost_rub"), job.get("cost_ton"),
                                      time.time(), r["id"]))
                await self.continue_order(oid)
            elif st in ("failed", "cancelled"):
                self.db.exec("UPDATE nft_sends SET state='failed', error=?, updated_at=? WHERE id=?",
                             (str(job.get("note") or st)[:300], time.time(), r["id"]))
                o = e.db.get_order(oid)
                if o is not None and S(o["status"]) == S.PURCHASING:
                    e._set(oid, S.READY, "nft_fragment_failed", fail_reason="nft_fragment")
                    e.enqueue(oid, 3)                       # тот же подарок — другим путём (если он в плюсе)
            elif st == "manual":
                self.db.exec("UPDATE nft_sends SET state='unknown', error=?, updated_at=? WHERE id=?",
                             (str(job.get("note") or "")[:300], time.time(), r["id"]))
                e._set(oid, S.MANUAL, "nft_manual", fail_reason="nft_manual")
                await e.notifier.notify("CRITICAL", "nft_manual", f"#{oid}: StarsBot купил NFT, но не смог отправить "
                                        f"его покупателю ({job.get('note')}). Отправьте вручную и отметьте заказ "
                                        "выданным.", order_id=oid)
            elif time.time() - float(r["created_at"]) > float(self.c["fragment_wait_minutes"]) * 60 and \
                    not self.db.kv_get(f"nft_slow:{r['id']}"):
                self.db.kv_set(f"nft_slow:{r['id']}", True)
                await e.notifier.notify("WARNING", "nft_slow", f"#{oid}: задание Fragment ({job['state']}) идёт "
                                        f"дольше {self.c['fragment_wait_minutes']} мин — проверьте StarsBot.",
                                        order_id=oid)

    async def continue_order(self, oid: str) -> None:
        o = self.engine.db.get_order(oid)
        if o is None:
            return
        qty = max(1, int(o["quantity"] or 1))
        sent = [r for r in self.sends(oid) if r["state"] == "sent"]
        if len(sent) >= qty:
            await self.finish(oid)
            return
        self.engine._set(oid, S.READY, "nft_next")
        self.engine.enqueue(oid, 2)

    async def finish(self, oid: str) -> None:
        e = self.engine
        rows = [r for r in self.sends(oid) if r["state"] == "sent"]
        cost = round(sum(float(r["cost_rub"] or 0) for r in rows), 2)
        routes = ", ".join(sorted({"Fragment" if r["route"] == "fragment" else "Telegram" for r in rows}))
        o = e._set(oid, S.DELIVERED, "delivered", delivered_at=time.time(), fail_reason="", cost_rub=cost or None,
                   gift_cost=sum(int(r["cost_stars"] or 0) for r in rows if r["route"] != "fragment"),
                   tx_hash="nft:" + ",".join(str(r["id"]) for r in rows))
        await e.say(o, "delivered", gifts=f"{self.collection(self.svc.lot_of_order(o) or {}).en} × {len(rows)}",
                    value=0)
        await e.notifier.notify("INFO", "delivered", f"🎁 Заказ #{oid}: NFT × {len(rows)} → @{o['tg_username']} "
                                f"({routes}, себестоимость {cost:g} ₽)", order_id=oid)
        self.svc.balance_ts = 0
        if o["fp_status"] == "closed":
            await e.on_buyer_confirmed(e.db.get_order(oid))

    async def no_margin(self, o, lot: dict, coll: Collection, best: float | None, limit: float, q: dict) -> None:
        """Ни один путь не даёт плюса: заказ — продавцу, лот выключить до проверки цен."""
        e = self.engine
        oid = o["order_id"]
        e._set(oid, S.MANUAL, "nft_no_margin", fail_reason="nft_price")
        await e.say(o, "delay")
        self.block(lot, f"заказ #{oid}: себестоимость выросла")
        why = (f"самый дешёвый путь {best:g} ₽" if best is not None else "подарка нет ни на Fragment, ни на рынке "
               "Telegram (или StarsBot не готов)")
        await e.notifier.notify("CRITICAL", "nft_no_margin", f"#{oid}: NFT {coll.en} — {why}, а заказ окупает не "
                                f"больше {limit:g} ₽. Лот выключен на {self.c['block_hours']} ч. Выдайте вручную "
                                "(/approve — купить дороже) или верните деньги.", order_id=oid,
                                buttons=[("✋ В ручную", f"reject {oid}")])

    def block(self, lot: dict, reason: str) -> None:
        if lot.get("offer_id"):
            self.db.kv_set(f"nft_block:{lot['offer_id']}", {"until": time.time() + float(self.c["block_hours"]) * 3600,
                                                            "reason": reason})
            self.db.kv_set(f"shop_lot_amount:{lot['offer_id']}", None)       # пересчитать наличие → выключится

    def blocked(self, lot: dict) -> str:
        b = self.db.kv_get(f"nft_block:{lot.get('offer_id')}") if lot.get("offer_id") else None
        return str(b.get("reason") or "") if b and float(b.get("until") or 0) > time.time() else ""

    def stock_amount(self, lot: dict) -> int:
        """Наличие NFT-лота: цена пути с плюсом есть, лот не заблокирован → небольшое число (подарков на рынке много,
        но цена у каждого своя — много сразу не обещаем)."""
        if not lot.get("sell", True) or self.blocked(lot) or not self.enabled:
            return 0
        q = self.cached_quote(self.collection(lot))
        if not q or not q.get("best"):
            return 0
        ready = (q.get("fragment") or {}).get("ready") or bool((q.get("telegram") or {}).get("slug"))
        if not ready:
            return 0
        count = sum(int((q.get(r) or {}).get("count") or 0) for r in ("fragment", "telegram"))
        return max(0, min(int(self.c["max_display_amount"]), count))

    async def recover(self) -> None:
        for r in self.db.q("SELECT * FROM nft_sends WHERE state='sending'"):
            self.db.exec("UPDATE nft_sends SET state='unknown' WHERE id=?", (r["id"],))
            self.engine._set(r["order_id"], S.MANUAL, "nft_unknown", fail_reason="nft_unknown")
            await self.engine.notifier.notify("CRITICAL", "nft_unknown", f"#{r['order_id']}: бот перезапустился во "
                                              "время покупки NFT на рынке Telegram — проверьте историю звёзд.",
                                              order_id=r["order_id"])

    async def close(self) -> None:
        await self.public.close()
