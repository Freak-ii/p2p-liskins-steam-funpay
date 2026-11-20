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

from starsbot.funpay.client import FunPayError
from starsbot.giftshop.service import lot_title, match_lot

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
TEXT_KEYS = ("summary_ru", "desc_ru", "summary_en", "desc_en")
GENERIC_OPTION_RX = re.compile(r"обычн|не\s*улучш|regular|standard|username|юзернейм|автовыдач", re.I)


def lot_texts(lot: dict, custom: dict | None = None) -> dict[str, str]:
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
        if any(v for v, _ in opts):
            notes.append(f"{name}: оставлен вариант из образца")
    f["price"] = f"{price:.2f}"
    f["amount"] = "1"
    f.pop("active", None)                     # новый лот — выключен: включит синхронизация наличия
    f.pop("deleted", None)
    apply_lot_texts(f, lot, custom)
    return f, notes


class ShopLotManager:
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

    def custom(self) -> dict:
        raw = self.scfg.get("lot_texts") or {}
        return {k: str(raw.get(k) or "").strip() for k in TEXT_KEYS if str(raw.get(k) or "").strip()}

    def _lots(self) -> list[dict]:
        return [dict(l) for l in (self.scfg.get("lots") or [])]

    def _save(self, lots: list[dict]) -> None:
        if self.save_config is not None:
            self.save_config({"giftshop": {"lots": lots}}, rebuild=False)
        self.cfg.giftshop["lots"] = lots

    def rows(self) -> list[dict]:
        db, g = self.engine.db, self.engine.gifts
        out = []
        for i, l in enumerate(self.scfg.get("lots") or []):
            oid = l.get("offer_id") or 0
            last = db.one("SELECT new_price, position, best_competitor, competitors, floor, reason, ts FROM "
                          "price_history WHERE offer_id=? ORDER BY ts DESC LIMIT 1", (str(oid),)) if oid else None
            out.append({"i": i, "kind": "shop", "title": lot_title(l), "emoji": l.get("emoji") or "",
                        "name": l.get("name") or "", "stars": int(l.get("stars") or 0), "offer_id": oid,
                        "position": int(l.get("position") or 0), "fixed_price": float(l.get("price") or 0),
                        "sell": bool(l.get("sell", True)),
                        "amount": db.kv_get(f"shop_lot_amount:{oid}") if oid else None,
                        "available": g.available(l) if g is not None else True,
                        "floor": self.engine._floor(l) if l.get("stars") else None,
                        "price": last["new_price"] if last else None, "rank": last["position"] if last else None,
                        "best": last["best_competitor"] if last else None,
                        "competitors": last["competitors"] if last else None})
        return out

    def _start_price(self, lot: dict) -> float:
        floor = self.engine._floor(lot)
        markup = float(self.scfg.get("start_markup_pct") or 30)
        margin = float(self.scfg.get("min_margin_pct") or 0)
        return round(max(floor, floor * (1 + markup / 100) / (1 + margin / 100)), 2)

    async def create_missing(self, kinds=None, template_offer: int = 0) -> list[str]:
        e = self.engine
        lots = self._lots()
        template = int(template_offer or next((int(l["offer_id"]) for l in lots if l.get("offer_id")), 0))
        report, changed = [], False
        for l in lots:
            if l.get("offer_id") or not l.get("stars") or not l.get("sell", True):
                continue
            if e.gifts is not None and not e.gifts.available(l):
                report.append(f"{lot_title(l)}: этого подарка сейчас нет в каталоге Telegram — не выставляю")
                continue
            try:
                base, selects = await e.fp.get_lot_form(self.node, template)
                fields, notes = build_lot_fields(base, selects, l, price=self._start_price(l), custom=self.custom())
                new_id = await e.fp.create_lot(fields, force=True)
                if not new_id:
                    new_id = await self._find_new(l)
                if not new_id:
                    raise FunPayError("FunPay принял форму, но лот не найден в ваших лотах — проверьте раздел вручную")
            except FunPayError as err:
                report.append(f"{lot_title(l)}: {err}")
                continue
            l["offer_id"] = int(new_id)
            changed = True
            e.db.kv_set(f"shop_lot_amount:{new_id}", None)
            e.db.add_event("-", "lot_created", gift=lot_title(l), offer_id=new_id, notes="; ".join(notes))
            report.append(f"{lot_title(l)}: выставлен, id {new_id} (включится, когда на аккаунте есть звёзды)"
                          + (f"; {'; '.join(notes)}" if notes else ""))
        if changed:
            self._save(lots)
        return report or ["все подарки уже выставлены"]

    async def _find_new(self, lot: dict) -> str:
        known = {str(l.get("offer_id")) for l in self._lots() if l.get("offer_id")}
        for own in await self.engine.fp.get_own_lots(self.node):
            if own.offer_id not in known and match_lot(own.description, [lot]) is lot:
                return own.offer_id
        return ""

    async def refresh_texts(self, force: bool = False) -> str:
        e = self.engine
        if not force and self.cfg.funpay.get("manage_lot_texts") is False:
            return "тексты лотов ведёте вы — бот их не меняет"
        sig = hashlib.sha1(repr([lot_texts(l, self.custom()) for l in self._lots()]).encode()).hexdigest()[:12]
        if not force and e.db.kv_get("shop_texts_sig") == sig:
            return "описания лотов актуальны"
        done, errors = 0, []
        for l in self._lots():
            if not l.get("offer_id"):
                continue
            try:
                fields = await e.fp.get_lot_fields(int(l["offer_id"]), self.node)
                apply_lot_texts(fields, l, self.custom())
                await e.fp.save_lot(fields)
                done += 1
            except FunPayError as err:
                errors.append(f"{l['offer_id']}: {err}")
        if not errors and not e.fp.dry_run:
            e.db.kv_set("shop_texts_sig", sig)
        return f"описания обновлены у {done} лотов" + (f"; ошибки: {'; '.join(errors)}" if errors else "")

    async def set_active(self, offer_id: int, active: bool) -> str:
        try:
            await self.engine.fp.update_lot(int(offer_id), self.node, active=active)
        except FunPayError as err:
            return f"лот {offer_id}: {err}"
        self.engine.db.add_event("-", "lot_toggle", offer_id=offer_id, active=active)
        return f"лот {offer_id} {'включён' if active else 'выключен'}"

    async def close_all(self, kind: str | None = None, reason: str = "") -> str:
        done = 0
        for r in self.rows():
            if r["offer_id"]:
                msg = await self.set_active(int(r["offer_id"]), False)
                done += "выключен" in msg
                self.engine.db.kv_set(f"shop_lot_amount:{r['offer_id']}", 0)
        if reason:
            self.engine.db.add_event("-", "lots_closed", lot_kind="shop", reason=reason)
        return f"выключено лотов: {done}"

    def update(self, i: int, **fields) -> str:
        """Правка лота из панели: offer_id, позиция, своя цена, продавать ли."""
        lots = self._lots()
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
        self._save(lots)
        if l.get("offer_id"):
            self.engine.db.kv_set(f"shop_lot_amount:{l['offer_id']}", None)    # пересчитать наличие
        return f"{lot_title(l)}: сохранено"

    def add(self, emoji: str, name: str, stars: int, gift_id: int = 0) -> str:
        lots = self._lots()
        lot = {"emoji": emoji.strip(), "name": name.strip(), "stars": int(stars), "offer_id": 0, "position": 3,
               "price": 0, "sell": True}
        if gift_id:
            lot["gift_id"] = int(gift_id)
        lots.append(lot)
        self._save(lots)
        return f"{lot_title(lot)}: добавлен"
