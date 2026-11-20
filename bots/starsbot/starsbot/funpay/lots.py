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


DESC_RU_USERNAME = (
    "⭐ {stars} звёзд Telegram — официально через Fragment, по username\n"
    "⚡ Автовыдача 24/7: обычно 1–5 минут после оплаты\n\n"
    "Как получить:\n"
    "1. Оплатите лот\n"
    "2. Пришлите в чат @username получателя (можно свой или друга)\n"
    "3. Бот проверит аккаунт, покажет имя и сразу отправит звёзды\n\n"
    "✅ Без входа в аккаунт — не нужны пароли, коды и почта\n"
    "✅ Звёзды видны в Telegram → Настройки → «Мои звёзды»\n"
    "✅ Можно купить несколько лотов — пришлём сумму на один аккаунт\n"
    "❗ Проверьте username: звёзды на чужой аккаунт вернуть невозможно\n"
    "❗ Подтвердите заказ, когда звёзды придут\n"
    "💬 Вопросы — в чат, бот отвечает сразу"
)
DESC_RU_GIFT = (
    "🎁 Telegram Stars подарками (≈{stars} ⭐) — по username\n"
    "⚡ Автовыдача 24/7: обычно 1–5 минут после оплаты\n\n"
    "Как получить:\n"
    "1. Оплатите лот\n"
    "2. Пришлите в чат @username получателя\n"
    "3. Бот отправит подарки — их можно оставить в профиле или конвертировать в звёзды\n\n"
    "✅ Без входа в аккаунт\n"
    "❗ Проверьте username: подарки на чужой аккаунт вернуть невозможно\n"
    "💬 Вопросы — в чат, бот отвечает сразу"
)
DESC_EN_USERNAME = (
    "{stars} Telegram Stars - official purchase via Fragment, delivered by username.\n"
    "Automatic delivery 24/7, usually 1-5 minutes after payment.\n\n"
    "How to get them:\n"
    "1. Pay for the offer.\n"
    "2. Send the recipient's @username in the chat (yours or a friend's).\n"
    "3. The bot checks the account, shows the name and sends the stars right away.\n\n"
    "No account login needed: no passwords or codes.\n"
    "Stars appear in Telegram - Settings - My Stars.\n"
    "Please double-check the username: stars sent to a wrong account cannot be returned.\n"
    "Confirm the order once the stars arrive. Questions - in the chat."
)
DESC_EN_GIFT = (
    "Telegram Stars as gifts (about {stars} stars), delivered by username.\n"
    "Automatic delivery 24/7, usually 1-5 minutes after payment.\n"
    "Pay, send the recipient's @username in the chat, and the bot sends the gifts. Gifts can be kept or converted "
    "to stars. Please double-check the username. Questions - in the chat."
)
# старые короткие тексты из прежних версий — заменяются новыми автоматически
OLD_DESCRIPTIONS = {"⚡ Автовыдача 1–5 минут. Укажите @username — звёзды придут на аккаунт.",
                    "🎁 Автовыдача подарками 1–5 минут. Укажите @username — подарки придут на аккаунт, "
                    "их можно конвертировать в звёзды."}


def lot_texts(stars: int, kind: str, description: str = "") -> dict[str, str]:
    """Краткое и полное описание лота на русском и английском (FunPay проверяет английские поля отдельно)."""
    gift = kind != "username"
    desc = description if description and description not in OLD_DESCRIPTIONS else \
        (DESC_RU_GIFT if gift else DESC_RU_USERNAME)
    try:
        desc = desc.format(stars=stars)
    except (KeyError, IndexError, ValueError):
        pass
    return {
        "summary_ru": (f"🎁 {stars} ⭐ Telegram подарками | Автовыдача 1–5 мин | По username" if gift else
                       f"⭐ {stars} звёзд Telegram | Автовыдача 1–5 мин | По username | Fragment"),
        "summary_en": (f"{stars} Telegram Stars as gifts | Instant delivery | By username" if gift else
                       f"{stars} Telegram Stars | Instant delivery 1-5 min | By username | Fragment"),
        "desc_ru": desc,
        "desc_en": (DESC_EN_GIFT if gift else DESC_EN_USERNAME).format(stars=stars),
    }


def apply_lot_texts(f: dict[str, str], *, stars: int, kind: str, description: str = "") -> dict[str, str]:
    t = lot_texts(stars, kind, description)
    for key in list(f):
        if key.startswith("fields[desc]"):
            f[key] = t["desc_ru"] if key.endswith("[ru]") else t["desc_en"]
        elif key.startswith("fields[summary]"):
            f[key] = t["summary_ru"] if key.endswith("[ru]") else t["summary_en"]
    f.setdefault("fields[summary][ru]", t["summary_ru"])
    return f


def build_lot_fields(base: dict[str, str], selects: dict[str, list[tuple[str, str]]], *, stars: int, kind: str,
                     price: float, amount: int, description: str = "", active: bool = True) -> dict[str, str]:
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
    if active:
        f["active"] = "on"
    else:
        f.pop("active", None)                 # чекбокс не отправлен = лот выключен
    f.pop("deleted", None)
    apply_lot_texts(f, stars=stars, kind=kind, description=description)
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
                    # в тестовом режиме лот всё равно создаётся, но выключенным: покупатель не сможет оплатить,
                    # пока бот ничего не выдаёт; включит его синхронизация наличия после выхода из теста
                    test_mode = bool(e.fp.dry_run)
                    fields = build_lot_fields(base, selects, stars=stars, kind=kind,
                                              price=self._start_price(stars, kind), amount=1, description=desc,
                                              active=not test_mode)
                    new_id = await e.fp.create_lot(fields, force=True)
                    if not new_id:
                        new_id = await self._find_new(node, stars, kind)
                    if not new_id:
                        raise FunPayError("FunPay принял форму, но лот не найден в ваших лотах — проверьте раздел вручную")
                except FunPayError as err:
                    report.append(f"{stars} ⭐ {'подарком' if kind == 'gift' else 'по username'}: {err}")
                    continue
                if new_id:
                    l["offer_id"] = int(new_id)
                    changed = True
                    e.db.add_event("-", "lot_created", stars=stars, lot_kind=kind, offer_id=new_id)
                report.append(f"{stars} ⭐ {'подарком' if kind == 'gift' else 'по username'}: выставлен, id {new_id}" +
                              (" (выключен, пока включён тестовый режим)" if test_mode else ""))
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

    async def refresh_texts(self, force: bool = False) -> str:
        """Обновить краткое и полное описание у уже выставленных лотов, когда тексты поменялись."""
        import hashlib
        e = self.engine
        node = int(self.cfg.funpay.node_id)
        sig_src = repr((lot_texts(50, "username", str(self.cfg.funpay.get("lot_description") or "")),
                        lot_texts(50, "gift", str(self.cfg.funpay.get("gift_lot_description") or ""))))
        sig = hashlib.sha1(sig_src.encode()).hexdigest()[:12]
        if not force and e.db.kv_get("lot_texts_sig") == sig:
            return "описания лотов актуальны"
        done, errors = 0, []
        for r in self.rows():
            if not r["offer_id"]:
                continue
            desc = str(self.cfg.funpay.get("gift_lot_description" if r["kind"] == "gift" else "lot_description") or "")
            try:
                fields = await e.fp.get_lot_fields(int(r["offer_id"]), node)
                apply_lot_texts(fields, stars=r["stars"], kind=r["kind"], description=desc)
                await e.fp.save_lot(fields)
                done += 1
            except FunPayError as err:
                errors.append(f"{r['offer_id']}: {err}")
        if not errors and not e.fp.dry_run:
            e.db.kv_set("lot_texts_sig", sig)
        return f"описания обновлены у {done} лотов" + (f"; ошибки: {'; '.join(errors)}" if errors else "")

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
