"""Движок TG Gift: тот же Engine StarsBot (заказы, чаты, автоответы, CRM, таймеры, автопилот),
но без Fragment и TON — товар выдаётся подарками Telegram, цены ведутся по разделу «Подарки»."""
from __future__ import annotations

import logging
import math
import re
import time

from starsbot.core import messages as M
from starsbot.core.engine import Engine, is_gift
from starsbot.core.models import CompetitorLot, FunPayOrder
from starsbot.funpay.client import FunPayError
from starsbot.giftshop.service import lot_title, match_lot

log = logging.getLogger("giftshop")

GIFT_TOPIC_RE = re.compile(r"подар|gift|мишк|мишек|медвед|роз[аыу]|торт|кубок|сердеч|сердц|букет|ракет|кольц|"
                           r"алмаз|шампанск|🧸|💝|🎁|🌹|🎂|💐|🚀|🍾|🏆|💍|💎", re.I)


class NullFragment:
    """TG Gift не покупает звёзды на Fragment сам — это делает StarsBot по заявке."""

    async def close(self) -> None:
        return None


class GiftShopEngine(Engine):
    BOT_TITLE = "TG Gift"

    # ------------------------------------------------------------------ какие заказы наши
    def is_our_order(self, description: str) -> bool:
        return False                                # звёзд TG Gift не продаёт

    def accepts_sale(self, so: FunPayOrder) -> bool:
        if self.gifts is None or not self.gifts.subcategory_ok(so.subcategory):
            return False
        return self.gifts.is_gift_order(so.description)

    def message_variants(self, o) -> list[str]:
        return ["_shop", "_gift"] if is_gift(o) else []

    def gift_title(self, o) -> str:
        lot = self.gifts.lot_of_order(o) if self.gifts is not None and o is not None else None
        return lot_title(lot, o["lang"] or "ru") if lot else "подарок"

    def _denominations(self) -> list:
        lots = self.gifts.lots if self.gifts is not None else []
        on_sale = [l for l in lots if l.get("offer_id") and l.get("sell", True)]
        return [lot_title(l) for l in (on_sale or lots)]

    # ------------------------------------------------------------------ пресейл: только вопросы о подарках
    def presale_topic(self, text: str) -> bool:
        return bool(GIFT_TOPIC_RE.search(text or "")) and not M.STARS_TOPIC_RE.search(text or "")

    def presale_hello(self, text: str) -> bool:
        return False            # на «здравствуйте / вы тут?» отвечает StarsBot — два ответа подряд выглядят как спам

    def presale_text(self, lang: str, about_topic: bool, text: str) -> str:
        out = M.render(lang, "presale_shop" if about_topic else "presale_hello_shop",
                       denominations=", ".join(self._denominations()))
        faq = M.detect_faq(text)
        if faq and faq not in ("faq_time", "faq_gift"):
            key = f"{faq}_shop" if f"{faq}_shop" in M.TEMPLATES.get(lang, M.TEMPLATES["ru"]) else faq
            try:
                out += "\n\n" + M.render(lang, key, status_hint="")
            except (KeyError, ValueError, IndexError):
                pass
        return out

    # ------------------------------------------------------------------ без Fragment и TON
    async def ensure_fragment(self, force: bool = False) -> bool:
        self.fragment_ok = False
        return False

    async def wallet_watch(self) -> None:
        return None

    async def fragment_age_reminder(self) -> None:
        return None

    def fragment_cookie_age_days(self) -> float | None:
        return None

    async def refresh_stock(self) -> None:
        if self.gifts is not None:
            await self.gifts.watch()

    def no_lots_yet(self) -> bool:
        """При запуске выставить недостающие лоты: подарки с sell=true без offer_id (giftshop.auto_create_lots)."""
        if self.gifts is None or not (self.cfg.get("giftshop") or {}).get("auto_create_lots", True):
            return False
        return any(not l.get("offer_id") and l.get("sell", True) for l in self.gifts.lots)

    def cost_per_star_rub(self) -> float | None:
        return self.gifts.cost_per_star_rub() if self.gifts is not None else None

    def cost_per_star_ton(self) -> float | None:
        return None

    def ton_to_rub(self, ton: float) -> float | None:
        return None

    def start_banner(self) -> str:
        acc = (self.gifts.account if self.gifts is not None else {}) or {}
        bal = self.gifts.balance if self.gifts is not None else None
        return (f"🎁 TG Gift запущен. FunPay: {self.fp.username}, аккаунт подарков: "
                f"@{acc.get('username') or '?'}, звёзд: {bal if bal is not None else '?'}"
                f"{' · DRY-RUN' if self.cfg.general.dry_run else ''}")

    def status_text(self) -> str:
        counts = {r["status"]: r["n"] for r in self.db.q("SELECT status, COUNT(*) n FROM orders GROUP BY status")}
        up = (time.time() - self.started_at) / 3600
        g = self.gifts
        lines = [f"🎁 TG Gift · {'⏸ ПАУЗА' if self.paused else '▶️ работает'} · аптайм {up:.1f} ч"
                 f"{' · DRY-RUN' if self.cfg.general.dry_run else ''}",
                 f"Аккаунт подарков: @{(g.account or {}).get('username') or '?'} · "
                 f"{g.balance if g.balance is not None else '?'} ⭐ (свободно {g.capacity(g.balance)})",
                 "Заказы: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
        return "\n".join(lines)

    def health(self) -> dict:
        h = super().health()
        h["checks"] = [c for c in h["checks"] if c["name"] not in ("ton_watch", "wallet", "fragment_session")]
        h["ok"] = all(c["ok"] for c in h["checks"])
        h["fragment_age_days"] = None
        return h

    async def handle_command(self, cmd: str, args: list[str]) -> str:
        if cmd in ("balance", "stock", "stars"):
            bal = await self.gifts.refresh_balance(max_age=0)
            return f"Аккаунт подарков: {bal if bal is not None else '?'} ⭐ (свободно {self.gifts.capacity(bal)})"
        if cmd == "topup":
            n = int(args[0]) if args and args[0].isdigit() else 1
            return await self.gifts.maybe_topup(need=n)
        return await super().handle_command(cmd, args)

    # ------------------------------------------------------------------ цены
    def _floor(self, lot: dict) -> float:
        """Минимальная цена лота (в редакторе): себестоимость подарка + маржа, с поправкой на комиссии."""
        cost = (self.cost_per_star_rub() or 0) * int(lot["stars"])
        margin = float((self.cfg.get("giftshop") or {}).get("min_margin_pct") or 0)
        fee = self.econ.fees_pct() / 100 if self.econ is not None else 0.0
        return round(cost * (1 + margin / 100) / max(1e-9, 1 - fee), 2)

    def competitors_for(self, lot: dict, public: list[CompetitorLot]) -> list[CompetitorLot]:
        """Конкуренты того же подарка, приведённые к виду, который понимает RTBA (quantity = цена подарка)."""
        lots = self.gifts.lots
        out = []
        for c in public:
            if match_lot(c.description, lots) is not lot:
                continue
            out.append(CompetitorLot(offer_id=c.offer_id, seller=c.seller, price=c.price, amount=c.amount,
                                     online=c.online, rating=c.rating, reviews=c.reviews, description=c.description,
                                     fields={"quantity": f"{int(lot['stars'])} звёзд", "method": "giftshop"}))
        return out

    async def rtba_step(self) -> None:
        cfg = self.cfg.rtba
        if self.paused or self.gifts is None:
            return
        entries = [l for l in self.gifts.lots if l.get("offer_id") and l.get("sell", True)]
        if not cfg.enabled:
            entries = [l for l in entries if int(l.get("position") or 0) > 0 or float(l.get("price") or 0) > 0]
        if not entries:
            return
        node = int(self.cfg.funpay.node_id)
        try:
            public = await self.fp.get_public_lots(node)
        except FunPayError as e:
            log.warning("RTBA подарков: %s", e)
            return
        own_ids = {str(l.get("offer_id")) for l in self.gifts.lots if l.get("offer_id")}
        cost = self.cost_per_star_rub()
        margin = float((self.cfg.get("giftshop") or {}).get("min_margin_pct") or 0)
        for lot in entries:
            offer_id, stars = str(lot["offer_id"]), int(lot["stars"])
            try:
                fields = await self.fp.get_lot_fields(int(offer_id), node)
            except FunPayError as e:
                log.warning("RTBA лот %s: %s", offer_id, e)
                continue
            current = float(str(fields.get("price") or 0).replace(",", ".") or 0) or None
            comps = self.competitors_for(lot, public)
            own_rows = [c for c in public if c.offer_id == offer_id]
            fixed = float(lot.get("price") or 0)
            if fixed > 0:
                target = max(fixed, self._floor(lot))
                applied = False
                if current is None or abs(current - target) >= 0.01:
                    fields["price"] = f"{target:.2f}"
                    try:
                        await self.fp.save_lot(fields)
                        applied = True
                    except FunPayError as e:
                        log.warning("цена лота %s: %s", offer_id, e)
                self._price_row(offer_id, stars, current, target, None, len(comps), None, self._floor(lot),
                                "своя цена из настроек", applied)
                continue
            self.rtba.calibrate(own_rows, offer_id, current)
            overrides = {"method_filter": "giftshop", "min_margin_pct": margin}
            pos = int(lot.get("position") or 0)
            if pos > 0:
                overrides.update(strategy="topN", target_position=pos)
            d = self.rtba.decide(comps, offer_id=offer_id, stars=stars, own_offer_ids=own_ids,
                                 own_username=self.fp.username, current_lot_price=current, cost_per_star_rub=cost,
                                 fee_pct=self.econ.fees_pct() if self.econ is not None else 0.0, overrides=overrides)
            applied = False
            if d.apply and d.target_lot_price is not None:
                try:
                    fields["price"] = f"{d.target_lot_price:.2f}"
                    await self.fp.save_lot(fields)
                    self.rtba.mark_applied(offer_id)
                    applied = True
                except FunPayError as e:
                    await self.notifier.notify("WARNING", "rtba_save_failed", f"RTBA: цена лота {lot_title(lot)} не "
                                               f"сохранена: {e}", throttle_key=f"rtba_save:{offer_id}")
            self._price_row(offer_id, stars, current, d.target_lot_price, d.best_competitor, d.competitors, d.position,
                            d.floor, d.reason + ("; " + "; ".join(d.notes) if d.notes else ""), applied)
            if any("пола" in n for n in d.notes):
                await self.notifier.notify("WARNING", "rtba_floor", f"RTBA {lot_title(lot)}: конкуренты ниже "
                                           f"себестоимости с маржой {margin:g}% (лидер {d.best_competitor}). Стоим на "
                                           f"минимуме {d.target_lot_price}.", throttle_key=f"rtba_floor:{offer_id}")
        self.db.emit({"type": "prices"})

    def _price_row(self, offer_id, stars, old, new, best, competitors, position, floor, reason, applied) -> None:
        self.db.exec("INSERT INTO price_history(ts,offer_id,stars,old_price,new_price,best_competitor,competitors,"
                     "position,floor,reason,applied) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (time.time(), offer_id, stars, old, new, best, competitors, position, floor, reason, int(applied)))

    async def price_guard(self) -> None:
        """Цена лота не ниже себестоимости подарка + минимальная маржа (дорожают звёзды — дорожает и подарок)."""
        s = self.cfg.get("safety") or {}
        if not s.get("price_guard", True) or self.paused or self.gifts is None:
            return
        node = int(self.cfg.funpay.node_id)
        for lot in self.gifts.lots:
            if not lot.get("offer_id"):
                continue
            floor = float(math.ceil(self._floor(lot) * 100) / 100)
            if floor <= 0:
                continue
            try:
                fields = await self.fp.get_lot_fields(int(lot["offer_id"]), node)
                price = float(str(fields.get("price") or 0).replace(",", "."))
                if price and price < floor:
                    await self.fp.update_lot(int(lot["offer_id"]), node, price=floor)
                    self.db.add_event("-", "price_guard", offer_id=lot["offer_id"], gift=lot_title(lot), old=price,
                                      new=floor)
                    await self.notifier.notify("WARNING", "price_guard_raise", f"Лот {lot_title(lot)}: цена "
                                               f"{price:g} ₽ была ниже себестоимости с маржой — поднял до {floor:g} ₽",
                                               throttle_key=f"pg:{lot['offer_id']}")
            except (FunPayError, ValueError) as e:
                log.warning("защита цены, лот %s: %s", lot.get("offer_id"), e)
