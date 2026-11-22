"""Лоты TG Gift на FunPay: один лот на подарок. Выставить недостающие, обновить тексты, включить/выключить.

Форма раздела «Подарки» берётся у FunPay (пустая форма или ваш существующий лот как образец). Выпадающие
списки выставляются по тексту вариантов: вариант с названием подарка, иначе «обычный»/«по username», иначе
остаётся как в образце. Новый лот создаётся выключенным — его включит синхронизация наличия, когда на аккаунте
есть звёзды. Проверьте первый созданный лот глазами на FunPay.
"""
from __future__ import annotations

import hashlib
import logging
import re

from starsbot.funpay import lots as star_lots
from starsbot.funpay.client import FunPayError
from starsbot.giftshop.collections import COLLECTIONS, Collection, find_collection, norm
from starsbot.giftshop.service import lot_kind, lot_title, match_lot

log = logging.getLogger("giftshop")

SUMMARY_RU = "{gift} — подарок Telegram на ваш аккаунт | Автовыдача 1–5 мин | По username"
SUMMARY_EN = "{gift_en} — Telegram gift to your account | Instant delivery | By username"
DESC_RU = (
    "{gift} — обычный подарок Telegram ({stars} ⭐ в каталоге Telegram)\n"
    "⚡ Автовыдача 24/7: обычно 1–5 минут после оплаты\n\n"
    "Как получить:\n"
    "1. Оплатите лот (можно несколько штук — придёт столько же подарков)\n"
    "2. Пришлите в чат @username получателя (свой или друга)\n"
    "3. Бот проверит аккаунт и сразу отправит подарок\n\n"
    "✅ Без входа в аккаунт — не нужны пароли и коды\n"
    "✅ Подарок появится в профиле → «Подарки», его можно оставить или конвертировать в звёзды\n"
    "❗ Проверьте username: подарок на чужой аккаунт вернуть невозможно\n"
    "❗ Подтвердите заказ, когда подарок придёт\n"
    "💬 Вопросы — в чат, бот отвечает сразу"
)
DESC_EN = (
    "{gift_en} - a regular Telegram gift, delivered by username.\n"
    "Automatic delivery 24/7, usually 1-5 minutes after payment.\n"
    "Pay, send the recipient's @username in the chat, and the bot sends the gift. No login needed.\n"
    "Please double-check the username. Confirm the order once the gift arrives."
)
NFT_SUMMARY_RU = "{name} ({ru}) — коллекционный NFT-подарок Telegram | Любая модель | Автовыдача по username"
NFT_SUMMARY_EN = "{name} — collectible Telegram NFT gift | Any model | Delivery by username"
NFT_DESC_RU = (
    "{name} ({ru}) — коллекционный (улучшенный) подарок Telegram, NFT\n"
    "Модель, фон и узор — любые (случайный экземпляр из продающихся сейчас, обычно самый недорогой)\n"
    "⚡ Автовыдача 24/7: обычно 5–30 минут после оплаты\n\n"
    "Как получить:\n"
    "1. Оплатите лот\n"
    "2. Пришлите в чат @username получателя\n"
    "3. Бот купит подарок и передаст его на этот аккаунт\n\n"
    "✅ Без входа в аккаунт — не нужны пароли и коды\n"
    "✅ Подарок появится в профиле → «Подарки», его можно хранить, передать или продать\n"
    "❗ Проверьте username: переданный подарок вернуть невозможно\n"
    "❗ Нужна конкретная модель или номер — напишите в чат до оплаты\n"
    "💬 Вопросы — в чат, бот отвечает сразу"
)
NFT_DESC_EN = (
    "{name} - a collectible (upgraded) Telegram NFT gift, any model.\n"
    "Automatic delivery 24/7, usually 5-30 minutes after payment.\n"
    "Pay, send the recipient's @username in the chat, and the bot transfers the gift. No login needed.\n"
    "Please double-check the username. Need a specific model? Ask in the chat before paying."
)
SALE_OPTION_RX = re.compile(r"продаж|sale", re.I)
TEXT_KEYS = ("summary_ru", "desc_ru", "summary_en", "desc_en")
GENERIC_OPTION_RX = re.compile(r"обычн|не\s*улучш|regular|standard|username|юзернейм|автовыдач", re.I)


def lot_texts(lot: dict, custom: dict | None = None) -> dict[str, str]:
    if lot_kind(lot) == "nft":
        c = COLLECTIONS.get(str(lot.get("collection") or ""))
        v = {"name": lot.get("name_en") or (c.en if c else lot.get("collection")),
             "ru": lot.get("name") or (c.ru if c else "")}
        out = {"summary_ru": NFT_SUMMARY_RU, "summary_en": NFT_SUMMARY_EN, "desc_ru": NFT_DESC_RU,
               "desc_en": NFT_DESC_EN}
        return {k: t.format(**v) for k, t in out.items()}
    v = {"gift": lot_title(lot), "gift_en": lot_title(lot, "en"), "stars": int(lot.get("stars") or 0),
         "name": lot.get("name") or ""}
    out = {"summary_ru": SUMMARY_RU, "summary_en": SUMMARY_EN, "desc_ru": DESC_RU, "desc_en": DESC_EN}
    for k, text in (custom or {}).items():
        if k in out and str(text or "").strip():
            out[k] = str(text)
    res = {}
    for k, text in out.items():
        try:
            res[k] = text.format(**v)
        except (KeyError, IndexError, ValueError):
            res[k] = text
    return res


def apply_lot_texts(f: dict[str, str], lot: dict, custom: dict | None = None) -> dict[str, str]:
    t = lot_texts(lot, custom)
    for key in list(f):
        if key.startswith("fields[desc]"):
            f[key] = t["desc_en"] if key.endswith("[en]") else t["desc_ru"]
        elif key.startswith("fields[summary]"):
            f[key] = t["summary_en"] if key.endswith("[en]") else t["summary_ru"]
    f.setdefault("fields[summary][ru]", t["summary_ru"])
    f.setdefault("fields[desc][ru]", t["desc_ru"])
    return f


def build_lot_fields(base: dict[str, str], selects: dict[str, list[tuple[str, str]]], lot: dict, *, price: float,
                     custom: dict | None = None) -> tuple[dict[str, str], list[str]]:
    """Поля новой карточки подарка. Возвращает (поля, заметки о выпадающих списках, которые бот не узнал)."""
    f = {k: v for k, v in base.items() if k not in ("offer_id", "csrf_token")}
    notes = []
    for name, opts in selects.items():
        named = [v for v, t in opts if v and match_lot(t, [lot]) is lot]
        if named:
            f[name] = named[0]
            continue
        generic = [v for v, t in opts if v and GENERIC_OPTION_RX.search(t)]
        if generic:
            f[name] = generic[0]
            continue
        sale = [v for v, t in opts if v and SALE_OPTION_RX.search(t)]
        if sale:
            f[name] = sale[0]
            continue
        if any(v for v, _ in opts):
            notes.append(f"{name}: оставлен вариант из образца")
    f["price"] = f"{price:.2f}"
    f["amount"] = "1"
    f.pop("active", None)                     # новый лот — выключен: включит синхронизация наличия
    f.pop("deleted", None)
    apply_lot_texts(f, lot, custom)
    return f, notes


def build_nft_fields(base: dict[str, str], selects: dict[str, list[tuple[str, str]]], lot: dict, *, price: float
                     ) -> tuple[dict[str, str], list[str]]:
    """Карточка NFT-лота: «Тип подарка» — вариант коллекции (fp_type или русское название), тип — «Продажа»."""
    f = {k: v for k, v in base.items() if k not in ("offer_id", "csrf_token")}
    coll = COLLECTIONS.get(str(lot.get("collection") or ""))
    wanted = [norm(x) for x in ((coll.fp_type, coll.ru) if coll else ()) + (lot.get("name") or "",) if x]
    notes = []
    for name, opts in selects.items():
        texts = [(v, norm(t)) for v, t in opts if v]
        sale = [v for v, t in opts if v and SALE_OPTION_RX.search(t)]
        if sale and len(texts) <= 4:
            f[name] = sale[0]
            continue
        pick = None
        for w in wanted:
            pick = next((v for v, t in texts if t == w), None) or next((v for v, t in texts if w and w in t), None)
            if pick:
                break
        if pick is None and coll is not None:
            pick = next((v for v, t in opts if v and find_collection(t) == coll), None)
        if pick is None:
            pick = next((v for v, t in texts if t in ("прочее", "другое", "other")), None)
        if pick is not None:
            f[name] = pick
        elif texts:
            notes.append(f"{name}: оставлен вариант из образца")
    f["price"] = f"{price:.2f}"
    f["amount"] = "1"
    f.pop("active", None)
    f.pop("deleted", None)
    apply_lot_texts(f, lot)
    return f, notes


class ShopLotManager:
    """Лоты TG Gift: обычные и NFT-подарки (giftshop.lots, раздел «Подарки») и номиналы «Подарком»
    (giftshop.topup_lots, раздел «Звёзды»). В панели строка лота знает свой список: shop или topup."""

    LISTS = {"shop": "lots", "topup": "topup_lots"}

    def __init__(self, engine, save_config=None):
        self.engine = engine
        self.save_config = save_config

    @property
    def cfg(self):
        return self.engine.cfg

    @property
    def scfg(self):
        return self.cfg.get("giftshop") or {}

    @property
    def node(self) -> int:
        return int(self.cfg.funpay.node_id)

    def node_for(self, lst: str) -> int:
        return self.engine.gifts.topup_node if lst == "topup" and self.engine.gifts is not None else self.node

    def custom(self) -> dict:
        raw = self.scfg.get("lot_texts") or {}
        return {k: str(raw.get(k) or "").strip() for k in TEXT_KEYS if str(raw.get(k) or "").strip()}

    def _lots(self, lst: str = "shop") -> list[dict]:
        return [dict(l) for l in (self.scfg.get(self.LISTS[lst]) or [])]

    def _save(self, lots: list[dict], lst: str = "shop") -> None:
        key = self.LISTS[lst]
        if self.save_config is not None:
            self.save_config({"giftshop": {key: lots}}, rebuild=False)
        self.cfg.giftshop[key] = lots

    def _topup_on(self) -> bool:
        g = self.engine.gifts
        return g is not None and getattr(g, "topup_enabled", False)

    def rows(self) -> list[dict]:
        db, g = self.engine.db, self.engine.gifts
        nft = getattr(g, "nft", None) if g is not None else None
        out = []
        lists = [("shop", self.scfg.get("lots") or [])]
        if self._topup_on():
            lists.append(("topup", [{**l, "kind": "topup"} for l in (self.scfg.get("topup_lots") or [])]))
        for lst, lots in lists:
            for i, l in enumerate(lots):
                kind = lot_kind(l)
                oid = l.get("offer_id") or 0
                last = db.one("SELECT new_price, position, best_competitor, competitors, floor, reason, ts FROM "
                              "price_history WHERE offer_id=? ORDER BY ts DESC LIMIT 1", (str(oid),)) if oid else None
                quote = nft.cached_quote(nft.collection(l)) if kind == "nft" and nft is not None else None
                floor = self.engine._floor(l) if (l.get("stars") or kind == "nft") else None
                out.append({"i": i, "list": lst, "kind": "shop" if kind == "gift" else kind, "title": lot_title(l),
                            "emoji": l.get("emoji") or "", "name": l.get("name") or "",
                            "collection": l.get("collection") or "", "auto": bool(l.get("auto")),
                            "stars": int(l.get("stars") or 0), "offer_id": oid,
                            "position": int(l.get("position") or 0), "fixed_price": float(l.get("price") or 0),
                            "sell": bool(l.get("sell", True)),
                            "amount": db.kv_get(f"shop_lot_amount:{oid}") if oid else None,
                            "available": g.available(l) if g is not None else True,
                            "floor": floor or None, "quote": quote,
                            "blocked": nft.blocked(l) if kind == "nft" and nft is not None else "",
                            "price": last["new_price"] if last else None, "rank": last["position"] if last else None,
                            "best": last["best_competitor"] if last else None,
                            "competitors": last["competitors"] if last else None})
        return out

    def _start_price(self, lot: dict) -> float:
        floor = self.engine._floor(lot)
        markup = float(self.scfg.get("start_markup_pct") or 30)
        margin = float(self.scfg.get("min_margin_pct") or 0)
        return round(max(floor, floor * (1 + markup / 100) / (1 + margin / 100)), 2)

    def _creatable(self, l: dict, lst: str) -> str:
        """'' — можно выставлять; иначе почему нет."""
        e = self.engine
        kind = "topup" if lst == "topup" else lot_kind(l)
        if kind == "nft":
            if not l.get("collection"):
                return "нет коллекции"
            if not self.engine._floor(l):
                return "нет цены ни на Fragment, ни на рынке Telegram — не выставляю"
            return ""
        if not l.get("stars"):
            return "нет цены в звёздах"
        if kind == "gift" and e.gifts is not None and not e.gifts.available(l):
            return "этого подарка сейчас нет в каталоге Telegram — не выставляю"
        return ""

    async def create_missing(self, kinds=None, template_offer: int = 0) -> list[str]:
        e = self.engine
        report = []
        lists = ["shop"] + (["topup"] if self._topup_on() else [])
        for lst in lists:
            if kinds and lst not in kinds:
                continue
            lots = self._lots(lst)
            node = self.node_for(lst)
            changed = False
            for idx, raw in enumerate(lots):
                l = {**raw, "kind": "topup"} if lst == "topup" else raw
                if l.get("offer_id") or not l.get("sell", True):
                    continue
                why = self._creatable(l, lst)
                if why:
                    report.append(f"{lot_title(l)}: {why}")
                    continue
                kind = lot_kind(l)
                same = [x for x in lots if x.get("offer_id") and (lst == "topup" or lot_kind(x) == kind)]
                template = int(template_offer or next((int(x["offer_id"]) for x in same), 0))
                try:
                    base, selects = await e.fp.get_lot_form(node, template)
                    notes: list[str] = []
                    price = self._start_price(l)
                    if kind == "topup":
                        fields = star_lots.build_lot_fields(base, selects, stars=int(l["stars"]), kind="gift",
                                                            price=price, amount=1, active=False,
                                                            custom=star_lots.custom_texts(self.cfg, "gift"))
                    elif kind == "nft":
                        fields, notes = build_nft_fields(base, selects, l, price=price)
                    else:
                        fields, notes = build_lot_fields(base, selects, l, price=price, custom=self.custom())
                    new_id = await e.fp.create_lot(fields, force=True)
                    if not new_id:
                        new_id = await self._find_new(l, lst)
                    if not new_id:
                        raise FunPayError("FunPay принял форму, но лот не найден в ваших лотах — проверьте раздел "
                                          "вручную")
                except FunPayError as err:
                    report.append(f"{lot_title(l)}: {err}")
                    continue
                lots[idx]["offer_id"] = int(new_id)
                changed = True
                e.db.kv_set(f"shop_lot_amount:{new_id}", None)
                e.db.add_event("-", "lot_created", gift=lot_title(l), offer_id=new_id, notes="; ".join(notes))
                report.append(f"{lot_title(l)}: выставлен, id {new_id} (включится, когда есть что выдавать)"
                              + (f"; {'; '.join(notes)}" if notes else ""))
            if changed:
                self._save(lots, lst)
        return report or ["все лоты уже выставлены"]

    async def _find_new(self, lot: dict, lst: str = "shop") -> str:
        from starsbot.core.stars import stars_from_text
        known = {str(l.get("offer_id")) for x in self.LISTS for l in self._lots(x) if l.get("offer_id")}
        kind = lot_kind(lot)
        for own in await self.engine.fp.get_own_lots(self.node_for(lst)):
            if own.offer_id in known:
                continue
            d = own.description
            if kind == "topup" and stars_from_text(d) == int(lot["stars"]) and re.search(r"(?i)подарк", d):
                return own.offer_id
            if kind == "nft" and (find_collection(d) or Collection("", "", "")).slug == lot.get("collection"):
                return own.offer_id
            if kind == "gift" and find_collection(d) is None and match_lot(d, [lot]) is lot:
                return own.offer_id
        return ""

    async def refresh_texts(self, force: bool = False) -> str:
        e = self.engine
        if not force and self.cfg.funpay.get("manage_lot_texts") is False:
            return "тексты лотов ведёте вы — бот их не меняет"
        shop = self._lots("shop")
        sig = hashlib.sha1(repr([lot_texts(l, self.custom()) for l in shop]).encode()).hexdigest()[:12]
        if not force and e.db.kv_get("shop_texts_sig") == sig:
            return "описания лотов актуальны"
        done, errors = 0, []
        for l in shop:                      # тексты «Подарком» ведутся как у StarsBot (funpay.gift_lot_texts)
            if not l.get("offer_id"):
                continue
            try:
                fields = await e.fp.get_lot_fields(int(l["offer_id"]), self.node)
                apply_lot_texts(fields, l, None if lot_kind(l) == "nft" else self.custom())
                await e.fp.save_lot(fields)
                done += 1
            except FunPayError as err:
                errors.append(f"{l['offer_id']}: {err}")
        if not errors and not e.fp.dry_run:
            e.db.kv_set("shop_texts_sig", sig)
        return f"описания обновлены у {done} лотов" + (f"; ошибки: {'; '.join(errors)}" if errors else "")

    async def set_active(self, offer_id: int, active: bool, node: int | None = None) -> str:
        try:
            await self.engine.fp.update_lot(int(offer_id), node or self.node, active=active)
        except FunPayError as err:
            return f"лот {offer_id}: {err}"
        self.engine.db.add_event("-", "lot_toggle", offer_id=offer_id, active=active)
        return f"лот {offer_id} {'включён' if active else 'выключен'}"

    async def close_all(self, kind: str | None = None, reason: str = "") -> str:
        done = 0
        for r in self.rows():
            if kind and r["kind"] != kind:
                continue
            if r["offer_id"]:
                msg = await self.set_active(int(r["offer_id"]), False, self.node_for(r["list"]))
                done += "выключен" in msg
                self.engine.db.kv_set(f"shop_lot_amount:{r['offer_id']}", 0)
        if reason:
            self.engine.db.add_event("-", "lots_closed", lot_kind=kind or "shop", reason=reason)
        return f"выключено лотов: {done}"

    def update(self, i: int, lst: str = "shop", **fields) -> str:
        """Правка лота из панели: offer_id, позиция, своя цена, продавать ли."""
        lst = lst if lst in self.LISTS else "shop"
        lots = self._lots(lst)
        if not 0 <= i < len(lots):
            return "лот не найден"
        l = lots[i]
        for k in ("offer_id", "position"):
            if k in fields and fields[k] is not None:
                l[k] = max(0, int(fields[k]))
        if "price" in fields and fields["price"] is not None:
            l["price"] = max(0.0, float(fields["price"]))
        if "sell" in fields and fields["sell"] is not None:
            l["sell"] = bool(fields["sell"])
            if lot_kind(l) == "nft":
                l["manual"] = True          # ручное решение: автоподбор этот лот больше не включает/не выключает
        self._save(lots, lst)
        if l.get("offer_id"):
            self.engine.db.kv_set(f"shop_lot_amount:{l['offer_id']}", None)    # пересчитать наличие
        return f"{lot_title(l)}: сохранено"

    def add(self, emoji: str, name: str, stars: int, gift_id: int = 0) -> str:
        lots = self._lots()
        lot = {"kind": "gift", "emoji": emoji.strip(), "name": name.strip(), "stars": int(stars), "offer_id": 0,
               "position": 3, "price": 0, "sell": True}
        if gift_id:
            lot["gift_id"] = int(gift_id)
        lots.append(lot)
        self._save(lots)
        return f"{lot_title(lot)}: добавлен"

    def add_nft(self, coll: Collection, *, auto: bool = False, position: int = 3) -> str:
        """NFT-лот коллекции (вручную из панели — в том числе дорогие; auto — добавлен автоподбором)."""
        lots = self._lots()
        if any(lot_kind(l) == "nft" and l.get("collection") == coll.slug for l in lots):
            for l in lots:
                if lot_kind(l) == "nft" and l.get("collection") == coll.slug and not l.get("sell", True):
                    l["sell"] = True
                    if not auto:
                        l["manual"] = True
                    self._save(lots)
                    return f"💎 {coll.en}: снова продаётся"
            return f"💎 {coll.en}: лот уже есть"
        lot = {"kind": "nft", "collection": coll.slug, "emoji": "💎", "name": coll.ru, "name_en": coll.en,
               "offer_id": 0, "position": int(position), "price": 0, "sell": True}
        if auto:
            lot["auto"] = True
        else:
            lot["manual"] = True
        lots.append(lot)
        self._save(lots)
        return f"💎 {coll.en}: добавлен"

    def set_nft_sell(self, slug: str, sell: bool) -> bool:
        lots = self._lots()
        for l in lots:
            if lot_kind(l) == "nft" and l.get("collection") == slug and bool(l.get("sell", True)) != sell:
                l["sell"] = sell
                self._save(lots)
                if l.get("offer_id"):
                    self.engine.db.kv_set(f"shop_lot_amount:{l['offer_id']}", None)
                return True
        return False
