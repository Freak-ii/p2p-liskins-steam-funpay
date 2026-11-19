"""Управление своими лотами на FunPay: выставить недостающие номиналы, включить/выключить, «закрыть всё».

Новый лот создаётся по форме FunPay: берём форму вашего существующего лота (образец) или пустую форму раздела,
выставляем «Количество» и «Способ получения» по тексту вариантов («50 звёзд», «По username» / «Подарком»),
цену, наличие и описание. Так бот не зависит от внутренних id вариантов, которые FunPay может менять.
"""
from __future__ import annotations

import logging
import re
from starsbot.core.stars import stars_from_text
from starsbot.funpay.client import FunPayError

log = logging.getLogger("lots")

METHOD_RX = {"username": re.compile(r"username|юзернейм", re.I), "gift": re.compile(r"подарк|gift", re.I)}


def _pick(options: list[tuple[str, str]], test) -> str | None:
    for value, text in options:
        if value and test(text):
            return value
    return None


def build_lot_fields(base: dict[str, str], selects: dict[str, list[tuple[str, str]]], *, stars: int, kind: str,
                     price: float, amount: int, description: str = "") -> dict[str, str]:
    """Поля формы нового лота из образца: количество, способ, цена, наличие, описание."""
    f = {k: v for k, v in base.items() if k not in ("offer_id", "csrf_token")}
    qty_set = method_set = False
    for name, opts in selects.items():
        if not qty_set:
            v = _pick(opts, lambda t: stars_from_text(t) == stars and "друг" not in t.lower())
            if v is not None and any(stars_from_text(t) for _, t in opts):
                f[name] = v
                qty_set = True
                continue
        if not method_set:
            v = _pick(opts, lambda t: bool(METHOD_RX[kind].search(t)))
            if v is not None:
                f[name] = v
                method_set = True
    if not qty_set:
        raise FunPayError(f"в форме лота нет варианта «{stars} звёзд» — создайте этот лот вручную")
    if not method_set:
        raise FunPayError("в форме лота нет способа «" + ("По username" if kind == "username" else "Подарком") + "»")
    f["price"] = f"{price:.2f}"
    f["amount"] = str(max(1, int(amount)))
    f["active"] = "on"
    if description:
        for key in list(f):
            if re.match(r"fields\[desc\]\[ru\]", key):
                f[key] = description
    return f


class LotManager:
    def __init__(self, engine, save_config=None):
        self.engine = engine
        self.save_config = save_config        # Supervisor.save — записать id новых лотов в config.yaml

    @property
    def cfg(self):
        return self.engine.cfg

    def rows(self) -> list[dict]:
        """Все лоты бота для панели: способ, номинал, id, позиция, наличие из последней синхронизации."""
        db = self.engine.db
        out = []
        for kind, lots in (("username", self.cfg.funpay.lots or []), ("gift", (self.cfg.get("gifts") or {}).get("lots") or [])):
            for l in lots:
                oid = l.get("offer_id") or 0
                key = ("gift_lot_amount:" if kind == "gift" else "lot_amount:") + str(oid)
                last = db.one("SELECT new_price, position, best_competitor, ts FROM price_history WHERE offer_id=? "
                              "ORDER BY ts DESC LIMIT 1", (str(oid),)) if oid else None
                out.append({"kind": kind, "stars": int(l.get("stars") or 0), "offer_id": oid,
                            "position": int(l.get("position") or 0), "amount": db.kv_get(key) if oid else None,
                            "price": last["new_price"] if last else None, "rank": last["position"] if last else None,
                            "best": last["best_competitor"] if last else None})
        return out

    def _start_price(self, stars: int, kind: str) -> float:
        per = float(self.cfg.funpay.get("lot_price_per_star") or 1.6)
        if kind == "gift" and self.engine.gifts is not None:
            per = per * self.engine.gifts.cost_for(stars) / stars
        cost = self.engine.cost_per_star_rub()
        if cost:
            per = max(per, cost * 1.15 * (self.engine.gifts.cost_for(stars) / stars if kind == "gift" and
                                           self.engine.gifts is not None else 1))
        return round(per * stars, 2)

    async def create_missing(self, kinds: tuple[str, ...] = ("username", "gift"), template_offer: int = 0) -> list[str]:
        """Выставить на FunPay лоты для номиналов без offer_id. Возвращает отчёт по каждому."""
        e = self.engine
        node = int(self.cfg.funpay.node_id)
        report: list[str] = []
        sources = {"username": self.cfg.funpay.lots or []}
        if (self.cfg.get("gifts") or {}).get("enabled"):
            sources["gift"] = self.cfg.gifts.lots or []
        templates: dict[str, int] = {}
        for kind, lots in sources.items():
            existing = [int(l["offer_id"]) for l in lots if l.get("offer_id")]
            templates[kind] = int(template_offer or (existing[0] if existing else 0))
        updates: dict[str, list[dict]] = {}
        for kind in kinds:
            lots = [dict(l) for l in sources.get(kind, [])]
            if not lots:
                continue
            changed = False
            for l in lots:
                if l.get("offer_id") or not l.get("stars"):
                    continue
                stars = int(l["stars"])
                try:
                    base, selects = await e.fp.get_lot_form(node, templates.get(kind) or 0)
                    desc = str(self.cfg.funpay.get("gift_lot_description" if kind == "gift" else "lot_description")
                               or "")
                    fields = build_lot_fields(base, selects, stars=stars, kind=kind,
                                              price=self._start_price(stars, kind), amount=1, description=desc)
                    new_id = await e.fp.create_lot(fields)
                    if not new_id and not e.fp.dry_run:
                        new_id = await self._find_new(node, stars, kind)
                except FunPayError as err:
                    report.append(f"{stars} ⭐ {'подарком' if kind == 'gift' else 'по username'}: {err}")
                    continue
                if new_id:
                    l["offer_id"] = int(new_id)
                    changed = True
                    e.db.add_event("-", "lot_created", stars=stars, lot_kind=kind, offer_id=new_id)
                report.append(f"{stars} ⭐ {'подарком' if kind == 'gift' else 'по username'}: " +
                              (f"выставлен, id {new_id}" if new_id else "dry-run — лот не создан"))
            if changed:
                updates[kind] = lots
        if updates and self.save_config is not None:
            upd: dict = {}
            if "username" in updates:
                upd.setdefault("funpay", {})["lots"] = updates["username"]
            if "gift" in updates:
                upd.setdefault("gifts", {})["lots"] = updates["gift"]
            self.save_config(upd, rebuild=False)
        self._apply_live(updates)
        return report or ["все номиналы уже выставлены"]

    async def _find_new(self, node: int, stars: int, kind: str) -> str:
        known = {str(l.get("offer_id")) for l in (self.cfg.funpay.lots or [])} | \
            {str(l.get("offer_id")) for l in ((self.cfg.get("gifts") or {}).get("lots") or [])}
        for own in await self.engine.fp.get_own_lots(node):
            if own.offer_id in known:
                continue
            if stars_from_text(own.description) == stars and METHOD_RX[kind].search(own.description):
                return own.offer_id
        return ""

    async def set_active(self, offer_id: int, active: bool) -> str:
        try:
            await self.engine.fp.update_lot(int(offer_id), int(self.cfg.funpay.node_id), active=active)
        except FunPayError as err:
            return f"лот {offer_id}: {err}"
        self.engine.db.add_event("-", "lot_toggle", offer_id=offer_id, active=active)
        return f"лот {offer_id} {'включён' if active else 'выключен'}"

    async def close_all(self, kind: str | None = None, reason: str = "") -> str:
        """Выключить все лоты бота (или одного способа) — например, когда нечем выдавать."""
        done = 0
        for r in self.rows():
            if r["offer_id"] and (kind is None or r["kind"] == kind):
                msg = await self.set_active(int(r["offer_id"]), False)
                done += "выключен" in msg
                key = ("gift_lot_amount:" if r["kind"] == "gift" else "lot_amount:") + str(r["offer_id"])
                self.engine.db.kv_set(key, 0)
        if reason:
            self.engine.db.add_event("-", "lots_closed", lot_kind=kind or "all", reason=reason)
        return f"выключено лотов: {done}"

    def set_position(self, kind: str, stars: int, position: int) -> None:
        section = "gifts" if kind == "gift" else "funpay"
        lots = [dict(l) for l in ((self.cfg.get(section) or {}).get("lots") or [])]
        for l in lots:
            if int(l.get("stars") or 0) == int(stars):
                l["position"] = max(0, int(position))
        if self.save_config is not None:
            self.save_config({section: {"lots": lots}}, rebuild=False)
        self._apply_live({"gift" if kind == "gift" else "username": lots})

    def _apply_live(self, updates: dict[str, list[dict]]) -> None:
        """Работающий бот сразу видит новые id и позиции, без перезапуска."""
        if "username" in updates:
            self.cfg.funpay["lots"] = updates["username"]
        if "gift" in updates and self.cfg.get("gifts") is not None:
            self.cfg.gifts["lots"] = updates["gift"]
