"""Выдача по заказам TG Gift: один лот FunPay = один подарок Telegram, количество лота = сколько подарков.

От GiftService (звёзды подарками) отличается тем, что набор подарков не подбирается под номинал, а задан лотом,
и звёзды на аккаунт не покупаются здесь же, а заказываются у StarsBot через общий файл координации.
"""
from __future__ import annotations

import logging
import re
import statistics
import time

from starsbot.core.models import OrderStatus as S
from starsbot.core.stars import stars_from_text
from starsbot.gifts.planner import Gift, GiftPlan, PlanError
from starsbot.gifts.service import GiftService
from starsbot.giftshop.collections import find_collection

log = logging.getLogger("giftshop")


def lot_kind(lot: dict | None) -> str:
    """gift — обычный подарок из каталога Telegram (раздел «Подарки»); nft — коллекционный (улучшенный) подарок;
    topup — «Звёзды → Подарком»: номинал звёзд набором обычных подарков (раздел «Звёзды»)."""
    return str((lot or {}).get("kind") or "gift")


def lot_title(lot: dict, lang: str = "ru") -> str:
    if lot_kind(lot) == "topup":
        return f"🎁 {int(lot.get('stars') or 0)} ⭐ подарками"
    if lot_kind(lot) == "nft":
        name = lot.get("name_en") or lot.get("name") or lot.get("collection") or "NFT"
        return f"{lot.get('emoji') or '💎'} {name}".strip()
    name = lot.get("name_en") if lang == "en" and lot.get("name_en") else lot.get("name")
    return f"{lot.get('emoji') or '🎁'} {name or 'Подарок'}".strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е"))


def _stem(name: str) -> str:
    """«Шампанское» → «шампанск»: ловим и «шампанское», и «шампанского» в описаниях конкурентов."""
    n = _norm(name)
    return n[:-2] if len(n) > 5 else n[:-1] if len(n) > 4 else n


def match_lot(description: str, lots: list[dict]) -> dict | None:
    """Лот, к которому относится описание: свой regex (match) > название + эмодзи > название > эмодзи.
    Если подходят несколько лотов с одинаковой уверенностью — None (лучше не угадывать)."""
    d = _norm(description)
    scored: list[tuple[int, dict]] = []
    for lot in lots:
        score = 0
        rx = lot.get("match")
        if rx:
            try:
                if re.search(rx, description or "", re.I):
                    score += 10
            except re.error:
                pass
        for name in (lot.get("name"), lot.get("name_en")):
            if name and _stem(name) and re.search(r"(?<![a-zа-я])" + re.escape(_stem(name)), d):
                score += 4
                break
        if lot.get("emoji") and lot["emoji"] in (description or ""):
            score += 2
        if score:
            scored.append((score, lot))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


class ShopGiftService(GiftService):
    @property
    def scfg(self):
        return self.cfg.get("giftshop") or {}

    # ------------------------------------------------------------------ лоты
    @property
    def lots(self) -> list[dict]:
        """Обычные подарки раздела «Подарки» (у каждого цена в звёздах)."""
        return [l for l in (self.scfg.get("lots") or []) if lot_kind(l) == "gift" and l.get("stars")]

    @property
    def nft_lots(self) -> list[dict]:
        return [l for l in (self.scfg.get("lots") or []) if lot_kind(l) == "nft" and l.get("collection")]

    @property
    def topup_lots(self) -> list[dict]:
        """Лоты «Звёзды → Подарком» (раздел FunPay «Звёзды»): номинал = звёзды при конвертации подарков."""
        if not self.topup_enabled:
            return []
        return [{**l, "kind": "topup"} for l in (self.scfg.get("topup_lots") or []) if l.get("stars")]

    @property
    def topup_enabled(self) -> bool:
        return self.enabled and bool((self.scfg.get("topup") or {}).get("enabled", True))

    @property
    def topup_node(self) -> int:
        return int((self.scfg.get("topup") or {}).get("node_id") or 2418)

    @property
    def all_lots(self) -> list[dict]:
        return self.lots + self.nft_lots + self.topup_lots

    def lot_by_offer(self, offer_id) -> dict | None:
        if not offer_id:
            return None
        for l in self.all_lots:
            if str(l.get("offer_id") or "") == str(offer_id):
                return l
        return None

    def node_of(self, lot: dict) -> int:
        return self.topup_node if lot_kind(lot) == "topup" else int(self.cfg.funpay.node_id)

    def lot_of_order(self, o) -> dict | None:
        if (o["channel"] or "") == "shop":          # заказ Telegram-магазина: подарок выбран в ShopBot
            saved = self.db.kv_get(f"shop_gift:{o['order_id']}")
            if saved:
                return self.lot_by_offer(saved.get("offer_id")) or saved
        lot = self.lot_by_offer(o["lot_offer_id"])
        if lot is None:
            found = self.lot_for(o["description"] or "")
            lot = found[0] if found else None
        return lot

    def nft_lot_for(self, description: str) -> dict | None:
        coll = find_collection(description)
        if coll is None:
            return None
        return next((l for l in self.nft_lots if l.get("collection") == coll.slug), None)

    def topup_lot_for(self, description: str) -> dict | None:
        d = description or ""
        if not re.search(r"(?i)подарк|gift", d) or re.search(r"(?i)с заходом|вход на аккаунт", d):
            return None
        stars = stars_from_text(d)
        lots = self.topup_lots
        configured = [l for l in lots if l.get("offer_id")]
        for l in configured or lots:
            if stars and int(l["stars"]) == stars:
                return l
        return None

    def lot_for(self, description: str) -> tuple[dict, int] | None:
        if find_collection(description) is not None:      # коллекционный подарок: только NFT-лоты, не обычные
            lot = self.nft_lot_for(description)
            return (lot, 0) if lot is not None else None
        configured = [l for l in self.lots if l.get("offer_id")]
        lot = match_lot(description, configured or self.lots)
        if lot is not None:
            return lot, int(lot["stars"])
        top = self.topup_lot_for(description)
        if top is not None:
            return top, int(top["stars"])
        return None

    def supply_lot(self, stars: int, emoji: str = "", title: str = "") -> dict | None:
        """Подарок для заказа магазина (ShopBot): свой лот с той же ценой и эмодзи, иначе разовый подарок
        из каталога Telegram. None — такого подарка в каталоге сейчас нет (или неоднозначно без эмодзи)."""
        stars, emoji = int(stars or 0), str(emoji or "").strip()
        if stars <= 0:
            return None
        same = [l for l in self.lots if int(l["stars"]) == stars]
        pick = [l for l in same if emoji and l.get("emoji") == emoji] or ([] if emoji else same[:1])
        lot = dict(pick[0]) if pick else {"stars": stars, "emoji": emoji, "name": str(title or "")[:40],
                                          "offer_id": 0, "sell": True}
        return lot if self.available(lot) else None

    def is_gift_order(self, description: str) -> bool:
        return self.enabled and self.lot_for(description) is not None

    def subcategory_ok(self, subcategory: str) -> bool:
        rx = str(self.scfg.get("subcategory_regex") or "")
        return not subcategory or not rx or bool(re.search(rx, subcategory))

    # ------------------------------------------------------------------ подарок и набор
    @staticmethod
    def resolve_gift(lot: dict, catalog: list[Gift] | None) -> Gift:
        """Подарок лота в живом каталоге Telegram: та же цена и тот же эмодзи (или явный gift_id)."""
        stars = int(lot["stars"])
        if not catalog:
            # каталог ещё не загружен — оценка для сообщений; отправка всегда идёт по живому каталогу
            return Gift(-stars, stars, 0, str(lot.get("name") or ""), emoji=str(lot.get("emoji") or ""))
        usable = [g for g in catalog if g.usable]
        if lot.get("gift_id"):
            for g in usable:
                if g.id == int(lot["gift_id"]):
                    return g
            raise PlanError(f"подарка {lot_title(lot)} (id {lot['gift_id']}) больше нет в каталоге Telegram")
        same = [g for g in usable if g.stars == stars]
        if lot.get("emoji"):
            by_emoji = [g for g in same if g.emoji == lot["emoji"]]
            if by_emoji:
                return by_emoji[0]
            raise PlanError(f"подарка {lot_title(lot)} за {stars} ⭐ сейчас нет в каталоге Telegram")
        if len(same) == 1:
            return same[0]
        raise PlanError(f"не удалось однозначно найти подарок {lot_title(lot)} в каталоге Telegram")

    def plan_for_order(self, o, catalog: list[Gift] | None, done: list | None = None) -> GiftPlan:
        lot = self.lot_of_order(o)
        if lot is None:
            raise PlanError("заказ не относится ни к одному лоту подарков")
        if lot_kind(lot) == "nft":
            return GiftPlan([], mode="nft")              # NFT выдаёт NftDesk, набора подарков нет
        if lot_kind(lot) == "topup":
            return GiftService.plan_for_order(self, o, catalog, done)     # набор под номинал (конвертация)
        gift = self.resolve_gift(lot, catalog)
        qty = max(1, int(o["quantity"] or 1))
        left = max(0, qty - len(done or []))
        return GiftPlan([gift] * left, mode="shop")

    def cost_for(self, stars: int) -> int:
        return int(stars)

    def topup_cost_for(self, stars: int) -> int:
        """Сколько звёзд аккаунта уйдёт на номинал «Подарком» (🧸15 даёт 13 ⭐ при конвертации)."""
        return GiftService.cost_for(self, stars)

    def available(self, lot: dict) -> bool:
        if lot_kind(lot) == "topup":
            return True
        if lot_kind(lot) == "nft":
            return self.nft is not None and self.nft.stock_amount(lot) > 0
        raw = getattr(self.tg, "_catalog", None)
        cat = raw[1] if isinstance(raw, tuple) else raw
        if not cat:
            return True
        try:
            self.resolve_gift(lot, cat)
            return True
        except PlanError:
            return False

    # ------------------------------------------------------------------ себестоимость
    def cost_per_star_rub(self) -> float:
        """₽ за звезду: по последним поставкам от StarsBot (их фактическая себестоимость), иначе из настроек."""
        vals = []
        for r in self.engine.coord.supply_history(self.account_key, 20):
            if r.get("state") == "done" and r.get("cost_rub") and r.get("stars"):
                vals.append(float(r["cost_rub"]) / int(r["stars"]))
        if vals:
            return round(statistics.median(vals[:5]), 4)
        return float(self.scfg.get("rub_per_star") or 1.35)

    # ------------------------------------------------------------------ снабжение
    @property
    def account_key(self) -> str:
        uid = self.account.get("user_id")
        return f"tggift:{uid}" if uid else "tggift"

    @property
    def topup_on(self) -> bool:
        return self.enabled and bool((self.scfg.get("supply") or {}).get("enabled", True))

    async def maybe_topup(self, need: int = 0) -> str:
        """Заявка StarsBot на звёзды: когда свободных меньше порога (или их не хватило на заказ)."""
        if not self.topup_on:
            return "снабжение выключено"
        coord = self.engine.coord
        if not coord.enabled:
            return "нет общего файла координации — запустите TG Gift из Центра рядом со StarsBot"
        s = self.scfg.get("supply") or {}
        username = str(self.account.get("username") or "").lstrip("@")
        balance = int(self.balance or 0)
        committed = self.db.gift_stars_committed()
        free = max(0, balance - committed)
        min_stars = int(s.get("min_stars") or 0)
        want = 0
        if need > 0 or free < min_stars:
            want = max(int(s.get("amount") or 1000), int(need) + 50, 50)
        if want and not username:
            await self.engine.notifier.notify("WARNING", "supply_no_username", "TG Gift: у аккаунта подарков нет "
                                              "@username — задайте его в Telegram (Настройки → Имя пользователя), "
                                              "иначе StarsBot не сможет купить на него звёзды.",
                                              throttle_key="supply_no_username")
        coord.supply_request(self.account_key, username=username, balance=balance, committed=committed, need=want,
                             min_stars=min_stars, note=f"свободно {free} ⭐")
        if want:
            self.db.kv_set("supply_requested", {"stars": want, "ts": time.time()})
            return f"заявка StarsBot: {want} ⭐ на @{username or '?'}"
        return "звёзд достаточно"

    async def create_topup(self, amount: int, username: str) -> str:
        return await self.maybe_topup(need=int(amount))

    def supply_status(self) -> dict:
        coord = self.engine.coord
        req = next((r for r in coord.supply_requests(max_age=10 ** 9) if r["account"] == self.account_key), None)
        return {"enabled": self.topup_on, "coord": coord.enabled, "request": req,
                "history": coord.supply_history(self.account_key, 15), "rub_per_star": self.cost_per_star_rub()}

    # ------------------------------------------------------------------ выдача: NFT отдельно
    nft = None                                   # giftshop.nft.NftDesk (задаёт Supervisor)
    assortment = None                            # giftshop.market.Assortment (задаёт Supervisor)

    async def deliver(self, o) -> None:
        lot = self.lot_of_order(o)
        if lot is not None and lot_kind(lot) == "nft":
            if self.nft is None:
                self.engine._set(o["order_id"], S.MANUAL, "nft_off", fail_reason="nft_off")
                return
            async with self._lock:
                cur = self.engine.db.get_order(o["order_id"])
                if cur is not None and S(cur["status"]) == S.READY:
                    await self.nft.deliver(cur, lot)
            return
        await super().deliver(o)

    async def recover(self) -> None:
        nft_orders = {r["order_id"] for r in self.db.q("SELECT DISTINCT order_id FROM nft_sends")} \
            if self.nft is not None else set()
        if self.nft is not None:
            await self.nft.recover()
        for o in self.db.q("SELECT * FROM orders WHERE delivery='gift' AND status=?", (S.PURCHASING.value,)):
            if o["order_id"] in nft_orders:
                waiting = self.db.scalar("SELECT COUNT(*) FROM nft_sends WHERE order_id=? AND state='waiting'",
                                         (o["order_id"],), 0)
                if not waiting:
                    self.engine._set(o["order_id"], S.READY, "recovered_unsent")
                continue
            sending = self.db.q("SELECT * FROM gift_sends WHERE order_id=? AND state='sending'", (o["order_id"],))
            if sending:
                self.db.exec("UPDATE gift_sends SET state='unknown' WHERE order_id=? AND state='sending'",
                             (o["order_id"],))
                self.engine._set(o["order_id"], S.MANUAL, "gift_unknown", fail_reason="gift_unknown")
                await self.engine.notifier.notify("CRITICAL", "gift_unknown", f"#{o['order_id']}: бот перезапустился "
                                                  "во время отправки подарка — проверьте историю звёзд аккаунта.",
                                                  order_id=o["order_id"])
            else:
                self.engine._set(o["order_id"], S.READY, "recovered_unsent")

    async def watch(self) -> None:
        await super().watch()
        if self.nft is not None:
            try:
                await self.nft.watch()
            except Exception as err:  # noqa: BLE001
                log.warning("NFT: %s", err)
        self.engine.coord.set_info("tggift", {"topup_lots": [str(l["offer_id"]) for l in self.topup_lots
                                                             if l.get("offer_id")],
                                              "nft_lots": len([l for l in self.nft_lots if l.get("offer_id")]),
                                              "account": self.account.get("username") or "", "ts": time.time()})

    # ------------------------------------------------------------------ наличие лотов
    async def _set_amount(self, lot: dict, amount: int, event: str, orbit_key: str = "") -> None:
        off = bool(orbit_key) and self.engine.orbit_closed(orbit_key)
        if off:
            amount = 0
        key = f"shop_lot_amount:{lot['offer_id']}"
        if self.db.kv_get(key) == amount:
            return
        active = not off and (amount > 0 or not self.gcfg.get("deactivate_when_empty", True))
        try:
            await self.engine.fp.update_lot(int(lot["offer_id"]), self.node_of(lot),
                                            amount=max(amount, 1 if active else 0), active=active)
            self.db.kv_set(key, amount)
            self.db.add_event("-", event, offer_id=lot["offer_id"], gift=lot_title(lot), amount=amount, active=active)
        except Exception as err:  # noqa: BLE001
            await self.engine.notifier.notify("WARNING", "lot_update_failed", f"Лот {lot_title(lot)} "
                                              f"({lot['offer_id']}): {err}", throttle_key=f"lot:{lot['offer_id']}")

    async def stock_sync(self, balance: int | None) -> None:
        g = self.gcfg
        if not g.get("stock_sync", True) or self.engine.paused:
            return
        free = self.capacity(balance)
        cap = int(g.get("max_display_amount") or 200)
        for lot in self.topup_lots:
            if lot.get("offer_id"):
                cost = self.topup_cost_for(int(lot["stars"]))
                ok = bool(lot.get("sell", True))
                await self._set_amount(lot, max(0, min(free // cost if cost else 0, cap)) if ok else 0,
                                       "topup_lot_stock", "topup")
        for lot in self.nft_lots:
            if lot.get("offer_id"):
                await self._set_amount(lot, self.nft.stock_amount(lot) if self.nft is not None else 0,
                                       "nft_lot_stock", "nft")
        node = int(self.cfg.funpay.node_id)
        for lot in self.lots:
            if not lot.get("offer_id"):
                continue
            stars = int(lot["stars"])
            off = self.engine.orbit_closed("gifts")
            ok = bool(lot.get("sell", True)) and self.available(lot) and not off
            amount = max(0, min(free // stars if stars else 0, int(g.get("max_display_amount") or 200))) if ok else 0
            key = f"shop_lot_amount:{lot['offer_id']}"
            if self.db.kv_get(key) == amount:
                continue
            active = not off and (amount > 0 or not g.get("deactivate_when_empty", True))
            try:
                await self.engine.fp.update_lot(int(lot["offer_id"]), node, amount=max(amount, 1 if active else 0),
                                                active=active)
                self.db.kv_set(key, amount)
                self.db.add_event("-", "shop_lot_stock", offer_id=lot["offer_id"], gift=lot_title(lot), amount=amount,
                                  active=active)
            except Exception as err:  # noqa: BLE001
                await self.engine.notifier.notify("WARNING", "lot_update_failed", f"Лот {lot_title(lot)} "
                                                  f"({lot['offer_id']}): {err}", throttle_key=f"lot:{lot['offer_id']}")
