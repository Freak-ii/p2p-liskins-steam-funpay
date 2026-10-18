"""CRM центра: единый список сделок обоих ботов (предметы LisBot и заказы MannBot FunPay) с фильтрами и поиском,
карточки покупателей FunPay с риск-скорингом, заметки и теги.

Читаем базы ботов только на чтение (через помощники аналитики). Заметки, теги и чёрный список записываются
через API самих ботов: LisBot `POST /api/item`, MannBot `POST /api/orders/{id}` и `POST /api/customers/{id}`.
Поэтому для записи бот должен быть запущен.
"""
from __future__ import annotations

import json
import logging
import time

import aiohttp

from center.analytics import _cached, _r, _rows
from center.workers import RUNNING

log = logging.getLogger("center.crm")

DAY = 86400
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

LIS_STATUS = {
    "bought": "Куплен (ждём трейд)", "locked_on_lis": "Заблокирован на LIS", "trade_sent": "Трейд отправлен",
    "protected": "Trade Protection", "ready": "Готов к продаже", "listing": "Выставляется", "listed": "На ТП",
    "sold": "Продан", "failed": "Не состоялся", "reversed": "Откат сделки", "dry_run": "Симуляция", "manual": "Вручную",
}
MANN_STATUS = {
    "new": "Новый", "awaiting_link": "Ждём трейд-ссылку", "pending_approval": "На проверке", "ready_to_send": "Готовим обмен",
    "offer_sent": "Трейд отправлен", "escrow": "Удержание Steam", "delivered": "Ключи выданы", "completed": "Подтверждён",
    "refunded": "Возврат", "manual": "Вручную", "blocked": "Заблокирован",
}
# группы статусов для фильтра: в работе / требует внимания / закрыта успешно / не состоялась
GROUPS = {"open": "В работе", "attention": "Требует внимания", "done": "Завершена", "failed": "Не состоялась"}
LIS_GROUP = {"sold": "done", "failed": "failed", "reversed": "failed", "dry_run": "failed", "manual": "attention"}
MANN_GROUP = {"delivered": "done", "completed": "done", "refunded": "failed", "blocked": "failed",
              "pending_approval": "attention", "manual": "attention"}
SORTS = ("created_at", "closed_at", "amount", "profit")


def _cur(v) -> str:
    return "USD" if str(v or "").strip() in ("$", "USD") else "RUB"


def _jl(s, default):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


class CrmError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Crm:
    def __init__(self, center):
        self.center = center

    @property
    def an(self):
        return self.center.analytics

    # ------------------------------------------------------------------ сделки
    def lis_deals(self) -> list[dict]:
        """Все сделки LisBot (общий кэш до следующей записи бота: список и словари не менять)."""
        db = self.an.lis_db
        return _cached(("crm.lis", str(db)), db, self._lis_deals)

    def _lis_deals(self) -> list[dict]:
        out = []
        for r in _rows(self.an.lis_db, "SELECT * FROM items ORDER BY id"):
            r = dict(r)
            sold = r["status"] == "sold"
            profit = r.get("profit_usd")
            if sold and profit is None and r.get("sold_usd") is not None:
                profit = r["sold_usd"] - (r["buy_price_usd"] or 0)
            dry = bool(r.get("dry_run")) or r["status"] == "dry_run"
            out.append({
                "bot": "lisbot", "id": f"L{r['id']}", "ref": str(r["id"]), "name": r["name"], "qty": 1, "currency": "USD",
                "kind": "покупка → продажа", "amount": r["buy_price_usd"], "revenue": r.get("sold_usd") if sold else None,
                "profit": profit if sold else None, "expected": r.get("expected_profit_usd"),
                "status": r["status"], "status_text": LIS_STATUS.get(r["status"], r["status"]),
                "group": "failed" if dry else LIS_GROUP.get(r["status"], "open"),
                "counterparty": r.get("partner_steamid") or "", "customer_id": None,
                "created_at": (r["created_at"] or 0) / 1000, "closed_at": (r["sold_at"] or 0) / 1000 or None,
                "notes": r.get("notes") or "", "tags": r.get("tags") or "", "dry_run": dry, "mode": r.get("mode") or "",
            })
        return out

    def mann_deals(self) -> list[dict]:
        """Все заказы MannBot (общий кэш до следующей записи бота: список и словари не менять)."""
        db, costs = self.an.mann_db, self.an.mann_costs()
        return _cached(("crm.mann", str(db), costs), db, lambda: self._mann_deals(*costs))

    def _mann_deals(self, cost_key: float, fee: float) -> list[dict]:
        out = []
        for r in _rows(self.an.mann_db, "SELECT * FROM orders ORDER BY created_at"):
            r = dict(r)
            price = float(r.get("price") or 0)
            keys = int(r.get("keys_qty") or r.get("quantity") or 0)
            done = bool(r.get("delivered_at")) and r["status"] != "refunded"
            profit = price - price * fee - cost_key * keys if done and cost_key else None
            out.append({
                "bot": "mannbot", "id": f"M{r['order_id']}", "ref": str(r["order_id"]),
                "name": r.get("description") or "Mann Co. Key", "qty": keys, "currency": _cur(r.get("currency")),
                "kind": "продажа FunPay", "amount": price, "revenue": price if done else None, "profit": profit,
                "expected": None, "status": r["status"], "status_text": MANN_STATUS.get(r["status"], r["status"]),
                "group": MANN_GROUP.get(r["status"], "open"),
                "counterparty": r.get("buyer_username") or "", "customer_id": r.get("buyer_id"),
                "steam_id": r.get("steam_id64") or "", "risk": r.get("risk_score") or 0, "stars": r.get("review_stars"),
                "created_at": r.get("created_at") or r.get("paid_at") or 0,
                "closed_at": r.get("completed_at") or r.get("delivered_at"),
                "notes": r.get("notes") or "", "tags": "", "dry_run": False, "mode": "funpay",
            })
        return out

    def all_deals(self) -> list[dict]:
        return self.lis_deals() + self.mann_deals()

    def deals(self, *, limit: int = 100, offset: int = 0, **filters) -> dict:
        rows, groups, totals = self._select(**filters)
        limit = max(1, min(int(limit), 1000))
        return {"total": len(rows), "offset": offset, "limit": limit, "groups": groups, "group_names": GROUPS,
                "totals": {c: {k: _r(v) for k, v in t.items()} for c, t in totals.items()},
                "rows": [{k: _r(v) if isinstance(v, float) else v for k, v in d.items()} for d in rows[offset:offset + limit]]}

    def _select(self, *, q: str = "", bot: str = "", group: str = "", days: float | None = None, dry: bool = False,
                sort: str = "created_at", desc: bool = True) -> tuple[list[dict], dict, dict]:
        """Отфильтрованные и отсортированные сделки (вся выборка), счётчики групп и итоги по валютам."""
        if bot == "lisbot":
            rows = list(self.lis_deals())
        elif bot == "mannbot":
            rows = list(self.mann_deals())
        else:
            rows = self.all_deals()
        if not dry:
            rows = [d for d in rows if not d["dry_run"]]
        if days:
            since = time.time() - days * DAY
            rows = [d for d in rows if d["created_at"] >= since or (d["closed_at"] or 0) >= since]
        if q:
            ql = q.lower()
            rows = [d for d in rows if any(ql in str(d.get(k) or "").lower() for k in
                                           ("id", "ref", "name", "counterparty", "steam_id", "notes", "tags", "status_text"))]
        groups = {g: 0 for g in GROUPS}
        for d in rows:
            groups[d["group"]] += 1
        if group in GROUPS:
            rows = [d for d in rows if d["group"] == group]
        sort = sort if sort in SORTS else "created_at"
        rows.sort(key=lambda d: (d[sort] is not None, d[sort] or 0), reverse=desc)
        totals: dict[str, dict] = {}
        for d in rows:
            t = totals.setdefault(d["currency"], {"deals": 0, "amount": 0.0, "revenue": 0.0, "profit": 0.0})
            t["deals"] += 1
            t["amount"] += d["amount"] or 0
            t["revenue"] += d["revenue"] or 0
            t["profit"] += d["profit"] or 0
        return rows, groups, totals

    def deal(self, bot: str, ref: str) -> dict:
        if bot == "lisbot":
            d = next((x for x in self.lis_deals() if x["ref"] == ref), None)
            if not d:
                raise CrmError("сделка не найдена", 404)
            item = _rows(self.an.lis_db, "SELECT * FROM items WHERE id=?", (int(ref) if ref.isdigit() else -1,))
            events = [{"ts": e["ts"] / 1000, "type": e["type"], "data": _jl(e["data"], e["data"])} for e in
                      _rows(self.an.lis_db, "SELECT * FROM events WHERE item_id=? ORDER BY id", (int(ref),))]
            return {"deal": d, "raw": dict(item[0]) if item else {}, "events": events, "messages": [], "customer": None}
        if bot == "mannbot":
            d = next((x for x in self.mann_deals() if x["ref"] == ref), None)
            if not d:
                raise CrmError("сделка не найдена", 404)
            o = dict(_rows(self.an.mann_db, "SELECT * FROM orders WHERE order_id=?", (ref,))[0])
            events = [{"ts": e["ts"], "type": e["kind"], "data": _jl(e["data"], e["data"])} for e in
                      _rows(self.an.mann_db, "SELECT * FROM order_events WHERE order_id=? ORDER BY id", (ref,))]
            msgs = [dict(m) for m in _rows(self.an.mann_db, "SELECT direction, text, ts FROM messages WHERE chat_id=? "
                                           "ORDER BY id DESC LIMIT 60", (o.get("chat_id") or "",))][::-1]
            o["risk_reasons"] = _jl(o.get("risk_reasons"), [])
            o.pop("trade_url", None)                       # токен трейд-ссылки в CRM не нужен
            cust = self.customer_brief(o["buyer_id"]) if o.get("buyer_id") is not None else None
            return {"deal": d, "raw": o, "events": events, "messages": msgs, "customer": cust}
        raise CrmError("неизвестный бот", 404)

    # ------------------------------------------------------------------ покупатели FunPay
    def _customer_rows(self) -> tuple[list[dict], dict[int, list[dict]], dict[str, set[int]]]:
        """Покупатели, их заказы и владельцы Steam-аккаунтов (общий кэш до следующей записи MannBot: не менять)."""
        db = self.an.mann_db
        return _cached(("crm.customers", str(db)), db, self._load_customers)

    def _load_customers(self) -> tuple[list[dict], dict[int, list[dict]], dict[str, set[int]]]:
        db = self.an.mann_db
        custs = [dict(r) for r in _rows(db, "SELECT * FROM customers")]
        orders: dict[int, list[dict]] = {}
        for o in _rows(db, "SELECT order_id, buyer_id, buyer_username, chat_id, status, price, currency, keys_qty, "
                           "quantity, risk_score, review_stars, steam_id64, created_at, delivered_at, completed_at "
                           "FROM orders ORDER BY created_at"):
            orders.setdefault(o["buyer_id"], []).append(dict(o))
        known = {c["funpay_id"] for c in custs}
        for bid, os_ in orders.items():              # покупатели, которых нет в customers (старые базы)
            if bid is not None and bid not in known:
                custs.append({"funpay_id": bid, "username": os_[-1]["buyer_username"], "first_seen": os_[0]["created_at"],
                              "last_seen": os_[-1]["created_at"], "steam_ids": "[]", "blacklisted": 0, "notes": "", "tags": ""})
        steam_owners: dict[str, set[int]] = {}
        for c in custs:
            ids = set(_jl(c.get("steam_ids"), [])) | {o["steam_id64"] for o in orders.get(c["funpay_id"], []) if o["steam_id64"]}
            c["steam_ids"] = sorted(str(i) for i in ids)
            for s in c["steam_ids"]:
                steam_owners.setdefault(s, set()).add(c["funpay_id"])
        return custs, orders, steam_owners

    @staticmethod
    def _stats(c: dict, orders: list[dict]) -> dict:
        done = [o for o in orders if o["delivered_at"] and o["status"] != "refunded"]
        stars = [o["review_stars"] for o in orders if o["review_stars"]]
        cnt = lambda st: sum(1 for o in orders if o["status"] == st)  # noqa: E731
        spent = sum(float(o["price"] or 0) for o in done)
        return {
            "orders": len(orders), "completed": cnt("completed"), "delivered": len(done), "refunded": cnt("refunded"),
            "blocked": cnt("blocked"), "active": sum(1 for o in orders if o["status"] not in ("completed", "refunded", "blocked")),
            "spent": _r(spent), "currency": _cur(orders[-1]["currency"]) if orders else "RUB",
            "keys": sum(int(o["keys_qty"] or o["quantity"] or 0) for o in done),
            "avg_check": _r(spent / len(done)) if done else None,
            "confirm_rate": _r(cnt("completed") / len(done) * 100, 1) if done else None,
            "reviews": len(stars), "avg_stars": _r(sum(stars) / len(stars), 1) if stars else None,
            "first_order": orders[0]["created_at"] if orders else None, "last_order": orders[-1]["created_at"] if orders else None,
        }

    @staticmethod
    def _risk(c: dict, st: dict, orders: list[dict], steam_owners: dict[str, set[int]]) -> dict:
        """Риск-скоринг покупателя 0–100 по его истории (отдельно от антифрода MannBot для каждого заказа)."""
        score, reasons = 0, []

        def add(points: int, why: str) -> None:
            nonlocal score
            score += points
            reasons.append({"points": points, "text": why})

        if c.get("blacklisted"):
            add(100, "в чёрном списке")
        if st["refunded"]:
            add(min(60, 20 * st["refunded"]), f"возвратов: {st['refunded']}")
        if st["blocked"]:
            add(min(60, 30 * st["blocked"]), f"заказов заблокировано антифродом: {st['blocked']}")
        top = max((o["risk_score"] or 0 for o in orders), default=0)
        if top >= 20:
            add(min(40, top // 2), f"максимальный риск заказа по антифроду MannBot: {top}")
        shared = sorted({u for s in c["steam_ids"] for u in steam_owners.get(s, ()) if u != c["funpay_id"]})
        if shared:
            add(35, f"Steam-аккаунт общий с другими покупателями ({len(shared)})")
        if len(c["steam_ids"]) >= 3:
            add(15, f"Steam-аккаунтов: {len(c['steam_ids'])}")
        stale = sum(1 for o in orders if o["status"] == "delivered" and o["delivered_at"]
                    and time.time() - o["delivered_at"] > 3 * DAY)
        if stale:
            add(min(30, 10 * stale), f"не подтвердил на FunPay больше 3 дней: {stale}")
        low = sum(1 for o in orders if o["review_stars"] and o["review_stars"] <= 3)
        if low:
            add(min(30, 15 * low), f"отзывов на 3★ и ниже: {low}")
        if st["completed"] >= 5 and not st["refunded"] and not c.get("blacklisted"):
            add(-20, f"постоянный покупатель: {st['completed']} подтверждённых заказов")
        score = max(0, min(100, score))
        return {"score": score, "level": "high" if score >= 60 else "medium" if score >= 25 else "low",
                "reasons": reasons, "shared_with": shared}

    def _build(self, c: dict, orders: list[dict], steam_owners) -> dict:
        st = self._stats(c, orders)
        return {"funpay_id": c["funpay_id"], "username": c.get("username") or "", "first_seen": c.get("first_seen"),
                "last_seen": c.get("last_seen"), "steam_ids": c["steam_ids"], "blacklisted": bool(c.get("blacklisted")),
                "notes": c.get("notes") or "", "tags": c.get("tags") or "", "stats": st,
                "risk": self._risk(c, st, orders, steam_owners)}

    def customers(self, *, q: str = "", risk: str = "", tag: str = "", sort: str = "last_order",
                  limit: int = 200, offset: int = 0) -> dict:
        custs, orders, owners = self._customer_rows()
        rows = [self._build(c, orders.get(c["funpay_id"], []), owners) for c in custs]
        if q:
            ql = q.lower()
            rows = [c for c in rows if ql in c["username"].lower() or ql in str(c["funpay_id"]) or ql in c["notes"].lower()
                    or ql in c["tags"].lower() or any(ql in s for s in c["steam_ids"])]
        levels = {lv: sum(1 for c in rows if c["risk"]["level"] == lv) for lv in ("low", "medium", "high")}
        if risk in levels:
            rows = [c for c in rows if c["risk"]["level"] == risk]
        if tag:
            rows = [c for c in rows if tag.lower() in [t.strip().lower() for t in c["tags"].split(",")]]
        key = {"spent": lambda c: c["stats"]["spent"] or 0, "orders": lambda c: c["stats"]["orders"],
               "risk": lambda c: c["risk"]["score"]}.get(sort, lambda c: c["stats"]["last_order"] or c["last_seen"] or 0)
        rows.sort(key=key, reverse=True)
        tags = sorted({t.strip() for c in rows for t in c["tags"].split(",") if t.strip()})
        return {"total": len(rows), "levels": levels, "tags": tags, "rows": rows[offset:offset + max(1, min(int(limit), 1000))]}

    def customer_brief(self, funpay_id: int) -> dict | None:
        custs, orders, owners = self._customer_rows()
        c = next((x for x in custs if x["funpay_id"] == funpay_id), None)
        return self._build(c, orders.get(funpay_id, []), owners) if c else None

    def customer(self, funpay_id: int) -> dict:
        custs, orders, owners = self._customer_rows()
        c = next((x for x in custs if x["funpay_id"] == funpay_id), None)
        if c is None:
            raise CrmError("покупатель не найден", 404)
        mine = orders.get(funpay_id, [])
        card = self._build(c, mine, owners)
        by_id = {x["funpay_id"]: x for x in custs}
        card["shared_with"] = [{"funpay_id": u, "username": by_id[u].get("username") or ""}
                               for u in card["risk"]["shared_with"] if u in by_id]
        card["orders"] = [{"order_id": o["order_id"], "status": o["status"], "status_text": MANN_STATUS.get(o["status"], o["status"]),
                           "price": o["price"], "currency": _cur(o["currency"]), "keys": o["keys_qty"] or o["quantity"],
                           "risk": o["risk_score"] or 0, "stars": o["review_stars"], "created_at": o["created_at"],
                           "delivered_at": o["delivered_at"], "completed_at": o["completed_at"]} for o in reversed(mine)]
        card["reviews"] = [{"order_id": o["order_id"], "stars": o["review_stars"], "ts": o["completed_at"] or o["delivered_at"]}
                           for o in reversed(mine) if o["review_stars"]]
        chats = sorted({o["chat_id"] for o in mine if o["chat_id"]})
        card["messages"] = [dict(m) for m in _rows(
            self.an.mann_db, f"SELECT order_id, direction, text, ts FROM messages WHERE chat_id IN ({','.join('?' * len(chats))}) "
            "ORDER BY id DESC LIMIT 80", tuple(chats))][::-1] if chats else []
        return card

    # ------------------------------------------------------------------ запись через API ботов
    def _worker(self, name: str):
        w = self.center.workers[name]
        if w.state != RUNNING:
            raise CrmError(f"{w.title} не запущен — запустите его, чтобы сохранить изменения", 409)
        return w

    async def _post(self, w, path: str, body: dict) -> dict:
        kw = {"headers": w._headers()} if hasattr(w, "_headers") else {"cookies": w._cookies()}
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, **kw) as s:
                async with s.post(f"{w.base_url}{path}", json=body, allow_redirects=False) as r:
                    if r.status in (301, 302, 401, 403):
                        raise CrmError(f"{w.title} отклонил запрос (нет доступа)", 502)
                    d = await r.json(content_type=None)
                    if r.status >= 400 or not d.get("ok"):
                        raise CrmError(str(d.get("error") or f"ошибка {r.status}"), 404 if r.status == 404 else 502)
                    return d
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise CrmError(f"{w.title} не ответил: {e}", 502) from e

    async def update_deal(self, bot: str, ref: str, *, notes: str | None = None, tags: str | None = None) -> str:
        if bot == "lisbot":
            if not ref.isdigit():
                raise CrmError("сделка не найдена", 404)
            body = {"id": int(ref), **({"notes": notes[:2000]} if notes is not None else {}),
                    **({"tags": tags[:200]} if tags is not None else {})}
            await self._post(self._worker("lisbot"), "/api/item", body)
        elif bot == "mannbot":
            if notes is None:
                raise CrmError("у заказов MannBot есть только заметка; теги ставятся покупателю")
            await self._post(self._worker("mannbot"), f"/api/orders/{ref}", {"notes": notes[:2000], "by": "center"})
        else:
            raise CrmError("неизвестный бот", 404)
        self.center.events.add(bot, "info", "crm.edit", f"CRM: изменены заметки сделки {bot[0].upper()}{ref}")
        return "Сохранено"

    async def update_customer(self, funpay_id: int, *, notes: str | None = None, tags: str | None = None,
                              blacklisted: bool | None = None) -> str:
        body = {k: v for k, v in (("notes", notes), ("tags", tags), ("blacklisted", blacklisted)) if v is not None}
        if not body:
            raise CrmError("нечего сохранять")
        await self._post(self._worker("mannbot"), f"/api/customers/{funpay_id}", body)
        what = "в чёрный список" if blacklisted else "убран из чёрного списка" if blacklisted is False else "изменён"
        self.center.events.add("mannbot", "warn" if blacklisted else "info", "crm.customer",
                               f"CRM: покупатель FunPay {funpay_id} {what}")
        return "Сохранено"

    # ------------------------------------------------------------------ экспорт выборки
    EXPORT_COLS = [("bot", "Бот"), ("id", "ID"), ("name", "Предмет"), ("qty", "Кол-во"), ("currency", "Валюта"),
                   ("amount", "Сумма"), ("revenue", "Выручка"), ("profit", "Прибыль"), ("status_text", "Статус"),
                   ("counterparty", "Контрагент"), ("created_at", "Создана"), ("closed_at", "Закрыта"),
                   ("notes", "Заметка"), ("tags", "Теги")]

    def export_rows(self, **filters) -> list[list]:
        import datetime as dt
        out = [[t for _, t in self.EXPORT_COLS]]
        for d in self._select(**filters)[0]:          # выгрузка — вся выборка, а не одна страница
            d = {k: _r(v) if isinstance(v, float) else v for k, v in d.items()}
            for k in ("created_at", "closed_at"):
                d[k] = dt.datetime.fromtimestamp(d[k]).strftime("%Y-%m-%d %H:%M") if d[k] else ""
            out.append([d[k] for k, _ in self.EXPORT_COLS])
        return out
