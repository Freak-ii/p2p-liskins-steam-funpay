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
from starsbot.giftshop.collections import find_collection
from starsbot.giftshop.service import lot_kind, lot_title, match_lot

log = logging.getLogger("giftshop")

GIFT_TOPIC_RE = re.compile(r"подар|gift|nft|нфт|коллекц|улучш|мишк|мишек|медвед|роз[аыу]|торт|кубок|сердеч|сердц|букет|ракет|кольц|"
                           r"алмаз|шампанск|🧸|💝|🎁|🌹|🎂|💐|🚀|🍾|🏆|💍|💎", re.I)


STARS_SECTION_RE = re.compile(r"(?i)зв[её]зды|\bstars\b")


def is_rent(c: CompetitorLot) -> bool:
    return "аренд" in str(c.fields.get("type2") or "") or bool(re.search(r"(?i)\bаренд", c.description or ""))


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
        if self.gifts is None:
            return False
        if so.subcategory and STARS_SECTION_RE.search(so.subcategory):
            # раздел «Звёзды»: берём только способ «Подарком» на наш номинал (остальное — StarsBot)
            return self.gifts.topup_lot_for(so.description) is not None
        if not self.gifts.subcategory_ok(so.subcategory):
            return False
        return self.gifts.is_gift_order(so.description)

    def message_variants(self, o) -> list[str]:
        if not is_gift(o):
            return []
        lot = self.gifts.lot_of_order(o) if self.gifts is not None else None
        return ["_gift"] if lot_kind(lot) == "topup" else ["_shop", "_gift"]

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

    def presale_any(self, text: str) -> bool:
        return False            # на прочие сообщения без заказа шаблоном отвечает StarsBot (тот же аккаунт FunPay)

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
        return any(not l.get("offer_id") and l.get("sell", True) for l in self.gifts.all_lots)

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
        if g.nft is not None:
            nft = [l for l in g.nft_lots if l.get("offer_id") and l.get("sell", True)]
            lines.append(f"NFT-лотов: {len(nft)} · Fragment через StarsBot: "
                         f"{'готов' if g.nft.fragment_ready() else 'нет'}")
        if g.topup_enabled:
            lines.append(f"«Звёзды → Подарком»: лотов {len([l for l in g.topup_lots if l.get('offer_id')])}")
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
        if cmd in ("assort", "nftmarket") and getattr(self.gifts, "assortment", None) is not None:
            return await self.gifts.assortment.command(args)
        if cmd == "nft" and self.gifts.nft is not None:
            if not args:
                lots = [l for l in self.gifts.nft_lots if l.get("sell", True)]
                return "NFT-лоты: " + (", ".join(l["collection"] for l in lots) or "нет")
            coll = self.gifts.nft.collection(args[0].lower())
            q = await self.gifts.nft.quote(coll, fresh=True)
            f, t = q.get("fragment") or {}, q.get("telegram") or {}
            return (f"{coll.title}: Fragment {f.get('ton', '—')} TON ≈ {f.get('rub', '—')} ₽"
                    f"{'' if f.get('ready', True) else ' (кошелёк StarsBot не готов)'}; рынок Telegram "
                    f"{t.get('stars', '—')} ⭐ ≈ {t.get('rub', '—')} ₽; дешевле: {q.get('best') or 'нет пути'}")
        return await super().handle_command(cmd, args)

    # ------------------------------------------------------------------ цены
    def lot_cost_rub(self, lot: dict) -> float | None:
        """Себестоимость одной единицы лота, ₽: подарок (звёзды × ₽/звезду), номинал «Подарком» (звёзды набора),
        NFT — дешёвый путь из последней котировки (Fragment или рынок Telegram)."""
        kind = lot_kind(lot)
        rps = self.cost_per_star_rub() or 0
        if kind == "nft":
            nft = self.gifts.nft if self.gifts is not None else None
            q = nft.cached_quote(nft.collection(lot)) if nft is not None else None
            return float(q["cost_rub"]) if q and q.get("cost_rub") else None
        if kind == "topup":
            return rps * self.gifts.topup_cost_for(int(lot["stars"]))
        return rps * int(lot["stars"])

    def _margin(self, lot: dict) -> float:
        margin = float((self.cfg.get("giftshop") or {}).get("min_margin_pct") or 0)
        if lot_kind(lot) == "nft" and self.gifts is not None and self.gifts.nft is not None:
            margin = max(margin, float(self.gifts.nft.c["min_profit_pct"]))
        return margin

    def _floor(self, lot: dict) -> float:
        """Минимальная цена лота (в редакторе): себестоимость + маржа, с поправкой на комиссии."""
        cost = self.lot_cost_rub(lot) or 0
        margin = self._margin(lot)
        fee = self.econ.fees_pct() / 100 if self.econ is not None else 0.0
        return round(cost * (1 + margin / 100) / max(1e-9, 1 - fee), 2)

    def competitors_for(self, lot: dict, public: list[CompetitorLot]) -> list[CompetitorLot]:
        """Конкуренты того же товара, приведённые к виду, который понимает RTBA.
        Подарок: quantity = цена подарка, без коллекционных и аренды. NFT: та же коллекция, продажа, quantity = 1.
        «Подарком»: как есть (раздел «Звёзды», RTBA сам смотрит количество и способ)."""
        kind = lot_kind(lot)
        if kind == "topup":
            return list(public)
        lots = self.gifts.lots
        out = []
        for c in public:
            if is_rent(c):
                continue
            coll = find_collection(c.description)
            if kind == "nft":
                if coll is None or coll.slug != lot.get("collection"):
                    continue
                out.append(CompetitorLot(offer_id=c.offer_id, seller=c.seller, price=c.price, amount=c.amount,
                                         online=c.online, rating=c.rating, reviews=c.reviews,
                                         description=c.description, fields={"quantity": "1 звёзд",
                                                                            "method": "giftshop"}))
                continue
            if coll is not None or match_lot(c.description, lots) is not lot:
                continue
            out.append(CompetitorLot(offer_id=c.offer_id, seller=c.seller, price=c.price, amount=c.amount,
                                     online=c.online, rating=c.rating, reviews=c.reviews, description=c.description,
                                     fields={"quantity": f"{int(lot['stars'])} звёзд", "method": "giftshop"}))
        return out

    def rtba_entries(self) -> list[dict]:
        g = self.gifts
        entries = [l for l in g.all_lots if l.get("offer_id") and l.get("sell", True)]
        if not self.cfg.rtba.enabled:
            entries = [l for l in entries if int(l.get("position") or 0) > 0 or float(l.get("price") or 0) > 0]
        return entries

    def _rtba_args(self, lot: dict) -> tuple[int, float | None, str]:
        """(звёзд для RTBA, ₽ за «звезду», фильтр способа). NFT считается как 1 «звезда» ценой себестоимости."""
        kind = lot_kind(lot)
        cost = self.lot_cost_rub(lot)
        if kind == "nft":
            return 1, cost, "giftshop"
        stars = int(lot["stars"])
        if kind == "topup":
            rx = str((self.cfg.get("giftshop") or {}).get("topup", {}).get("method_filter") or "подарк")
            return stars, (cost / stars if cost else None), rx
        return stars, self.cost_per_star_rub(), "giftshop"

    async def rtba_step(self) -> None:
        if self.paused or self.gifts is None:
            return
        entries = self.rtba_entries()
        if not entries:
            return
        g = self.gifts
        if g.nft is not None:
            for lot in entries:
                if lot_kind(lot) == "nft":
                    try:
                        await g.nft.quote(g.nft.collection(lot))
                    except Exception as e:  # noqa: BLE001
                        log.warning("котировка NFT %s: %s", lot.get("collection"), e)
        pages: dict[int, list[CompetitorLot]] = {}
        own_ids = {str(l.get("offer_id")) for l in g.all_lots if l.get("offer_id")}
        for lot in entries:
            node = g.node_of(lot)
            if node not in pages:
                try:
                    pages[node] = await self.fp.get_public_lots(node)
                except FunPayError as e:
                    log.warning("RTBA подарков, раздел %s: %s", node, e)
                    pages[node] = []
                    continue
            public = pages[node]
            if not public:
                continue
            await self._rtba_lot(lot, node, public, own_ids)
        self.db.emit({"type": "prices"})

    async def _rtba_lot(self, lot: dict, node: int, public: list[CompetitorLot], own_ids: set[str]) -> None:
        offer_id = str(lot["offer_id"])
        stars, cost, method = self._rtba_args(lot)
        if lot_kind(lot) == "nft" and not cost:
            return                                   # нет котировки — цену не трогаем (лот выключит stock_sync)
        try:
            fields = await self.fp.get_lot_fields(int(offer_id), node)
        except FunPayError as e:
            log.warning("RTBA лот %s: %s", offer_id, e)
            return
        current = float(str(fields.get("price") or 0).replace(",", ".") or 0) or None
        comps = self.competitors_for(lot, public)
        own_rows = [c for c in public if c.offer_id == offer_id]
        floor = self._floor(lot)
        fixed = float(lot.get("price") or 0)
        if fixed > 0:
            target = max(fixed, floor)
            applied = False
            if current is None or abs(current - target) >= 0.01:
                fields["price"] = f"{target:.2f}"
                try:
                    await self.fp.save_lot(fields)
                    applied = True
                except FunPayError as e:
                    log.warning("цена лота %s: %s", offer_id, e)
            self._price_row(offer_id, stars, current, target, None, len(comps), None, floor,
                            "своя цена из настроек", applied)
            return
        margin = self._margin(lot)
        self.rtba.calibrate(own_rows, offer_id, current)
        overrides = {"method_filter": method, "min_margin_pct": margin}
        if lot_kind(lot) == "nft":
            overrides.update(floor_price_per_star=0, ceiling_price_per_star=0)
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

    def _price_row(self, offer_id, stars, old, new, best, competitors, position, floor, reason, applied) -> None:
        self.db.exec("INSERT INTO price_history(ts,offer_id,stars,old_price,new_price,best_competitor,competitors,"
                     "position,floor,reason,applied) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (time.time(), offer_id, stars, old, new, best, competitors, position, floor, reason, int(applied)))

    async def price_guard(self) -> None:
        """Цена лота не ниже себестоимости подарка + минимальная маржа (дорожают звёзды — дорожает и подарок)."""
        s = self.cfg.get("safety") or {}
        if not s.get("price_guard", True) or self.paused or self.gifts is None:
            return
        for lot in self.gifts.all_lots:
            if not lot.get("offer_id"):
                continue
            node = self.gifts.node_of(lot)
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


def shop_economics(cfg, db, rates, engine):
    """Экономика TG Gift: себестоимость заказа — звёзды подарков × ₽ за звезду (cost_rub заказа), без TON."""
    from starsbot.economics import Economics, Flow

    class ShopEconomics(Economics):
        def order_flow(self, o) -> Flow:
            price = float(o["price"] or 0)
            fee_pct, wd_pct = self.fee_pcts("funpay")
            fp_fee = price * fee_pct / 100
            wd_fee = (price - fp_fee) * wd_pct / 100
            net = price - fp_fee - wd_fee
            cost = float(o["cost_rub"]) if o["cost_rub"] is not None else None
            if cost is None and o["gift_cost"] and engine.cost_per_star_rub():
                cost = int(o["gift_cost"]) * float(engine.cost_per_star_rub())
            profit = net - cost if cost is not None else None
            keep = (1 - fee_pct / 100) * (1 - wd_pct / 100)
            return Flow(stars=int(o["stars"] or 0), price=round(price, 2), funpay_fee=round(fp_fee, 2),
                        withdraw_fee=round(wd_fee, 2), net_rub=round(net, 2), ton_stars=0.0, ton_gas=0.0,
                        ton_total=0.0, ton_rate=None, cost_rub=round(cost, 2) if cost is not None else None,
                        profit=round(profit, 2) if profit is not None else None,
                        margin_pct=round(profit / price * 100, 1) if profit is not None and price else None,
                        breakeven_price=round(cost / keep, 2) if cost is not None and keep > 0 else None,
                        ton_per_star=None)

    return ShopEconomics(cfg, db, rates, engine)
