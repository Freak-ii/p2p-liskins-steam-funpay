"""RTBA — Real-Time Bid Adjustment: автоматическая цена каждого лота-номинала по рынку FunPay.

Логика (для каждого номинала отдельно):
 1. Снимаем публичную таблицу раздела «Звёзды», оставляем лоты того же номинала и способа получения
    («По username»): у строк таблицы есть атрибуты data-f-quantity / data-f-method.
 2. Отбрасываем себя, игнор-лист, мелкие лоты, офлайн (опционально) и демпинг-выбросы (IQR).
 3. Целевая цена по стратегии: top1 (дешевле лидера), topN (встать на позицию N), median.
 4. Подтягиваемся вверх, если мы дешевле лидера больше чем на raise_gap_pct.
 5. Пол цены = себестоимость звёзд на Fragment (TON × курс) + маржа, с учётом комиссии; потолок — из настроек.
 6. Гистерезис (min_change) и лимит обновлений в час — без «дёрганья» цены.

Цена в таблице — цена для покупателя, в редакторе лота — цена продавца. Их отношение калибруется
автоматически по нашему же лоту в таблице.
"""
from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field

from starsbot.core.models import CompetitorLot
from starsbot.core.stars import stars_from_text


@dataclass
class PriceDecision:
    target_buyer_price: float | None
    target_lot_price: float | None
    reason: str
    best_competitor: float | None = None
    competitors: int = 0
    position: int | None = None
    floor: float | None = None
    apply: bool = False
    notes: list[str] = field(default_factory=list)


def lot_stars(lot: CompetitorLot) -> int | None:
    return stars_from_text(lot.fields.get("quantity", "")) or stars_from_text(lot.description)


def lot_matches(lot: CompetitorLot, stars: int, method_filter: str) -> bool:
    if lot_stars(lot) != stars:
        return False
    if method_filter:
        method = lot.fields.get("method") or lot.description
        if not re.search(method_filter, method, re.I):
            return False
    return True


def filter_competitors(lots: list[CompetitorLot], cfg, stars: int, own_offer_ids: set[str], own_username: str
                       ) -> list[CompetitorLot]:
    ignore = {s.lower() for s in (cfg.ignore_competitors or []) if s}
    out = []
    for lot in lots:
        if not lot_matches(lot, stars, cfg.method_filter):
            continue
        if lot.offer_id in own_offer_ids or (own_username and lot.seller.lower() == own_username.lower()):
            continue
        if lot.seller.lower() in ignore:
            continue
        if lot.amount and lot.amount < int(cfg.competitor_min_amount):
            continue
        if cfg.competitor_only_online and not lot.online:
            continue
        out.append(lot)
    out.sort(key=lambda l: l.price)
    k = float(cfg.outlier_iqr_factor or 0)
    if k > 0 and len(out) >= 5:
        prices = [l.price for l in out]
        q = statistics.quantiles(prices, n=4)
        lo = q[0] - k * (q[2] - q[0])
        out = [l for l in out if l.price >= lo]
    return out


def price_floor(cfg, stars: int, cost_per_star_rub: float | None, fee_pct: float = 0.0) -> float:
    """Минимальная цена лота (в редакторе): полная себестоимость + маржа с поправкой на комиссии FunPay и вывода."""
    floor = float(cfg.floor_price_per_star or 0) * stars
    if cost_per_star_rub:
        fee = float(fee_pct or 0) / 100
        by_margin = stars * cost_per_star_rub * (1 + float(cfg.min_margin_pct) / 100) / max(1e-9, 1 - fee)
        floor = max(floor, by_margin)
    return round(floor, 2)


class RTBAEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.markup: dict[str, float] = {}         # offer_id → цена покупателя / цена лота
        self.updates: dict[str, list[float]] = {}  # offer_id → время применённых обновлений

    def calibrate(self, lots: list[CompetitorLot], offer_id: str, own_lot_price: float | None) -> None:
        if not own_lot_price:
            return
        for lot in lots:
            if lot.offer_id == offer_id and lot.price > 0:
                ratio = lot.price / own_lot_price
                if 0.8 < ratio < 1.6:
                    self.markup[offer_id] = round(ratio, 4)
                return

    def decide(self, lots: list[CompetitorLot], *, offer_id: str, stars: int, own_offer_ids: set[str],
               own_username: str, current_lot_price: float | None, cost_per_star_rub: float | None,
               fee_pct: float = 0.0, now: float | None = None, overrides: dict | None = None) -> PriceDecision:
        """overrides — настройки конкретного лота: своя позиция (strategy/target_position) и способ (method_filter)."""
        from starsbot.config import Section
        cfg = Section({**self.cfg, **overrides}) if overrides else self.cfg
        self._cur = cfg
        now = now or time.time()
        markup = self.markup.get(offer_id, 1.0)
        comps = filter_competitors(lots, cfg, stars, own_offer_ids, own_username)
        floor_lot = price_floor(cfg, stars, cost_per_star_rub, fee_pct)
        ceil_lot = float(cfg.ceiling_price_per_star or 0) * stars or None
        floor_buyer = floor_lot * markup
        if not comps:
            target = ceil_lot or current_lot_price or floor_lot
            d = PriceDecision(target * markup, target, "нет конкурентов — держим потолок/текущую", floor=floor_lot)
            return self._finish(d, offer_id, markup, current_lot_price, floor_lot, ceil_lot, now)
        prices = [c.price for c in comps]
        best = prices[0]
        if cfg.strategy == "median":
            anchor = statistics.median(prices)
            reason = f"медиана рынка {anchor:.2f}"
        elif cfg.strategy == "topN":
            pos = max(1, int(cfg.target_position))
            anchor = prices[min(pos, len(prices)) - 1]
            reason = f"позиция {pos}: цена конкурента {anchor:.2f}"
        else:
            anchor = best
            reason = f"лидер рынка {best:.2f}"
        undercut = max(float(cfg.undercut_abs or 0), anchor * float(cfg.undercut_pct or 0) / 100)
        target_buyer = anchor - undercut
        notes = []
        if current_lot_price:
            cur_buyer = current_lot_price * markup
            gap = (best - cur_buyer) / best * 100 if best else 0
            if gap > float(cfg.raise_gap_pct):
                notes.append(f"мы дешевле лидера на {gap:.1f}% — поднимаем цену")
        if not cost_per_star_rub and not cfg.floor_price_per_star:
            notes.append("себестоимость неизвестна — пол цены не задан")
        if target_buyer < floor_buyer:
            notes.append(f"рынок ниже нашего пола ({floor_buyer:.2f}) — стоим на полу")
        position = 1 + sum(1 for p in prices if p < max(target_buyer, floor_buyer))
        d = PriceDecision(target_buyer, target_buyer / markup, reason, best_competitor=round(best, 2),
                          competitors=len(comps), position=position, floor=floor_lot, notes=notes)
        return self._finish(d, offer_id, markup, current_lot_price, floor_lot, ceil_lot, now)

    def _finish(self, d: PriceDecision, offer_id: str, markup: float, current: float | None, floor: float,
                ceil: float | None, now: float) -> PriceDecision:
        cfg = getattr(self, "_cur", None) or self.cfg
        t = max(d.target_lot_price or floor, floor)
        if ceil:
            t = min(t, ceil)
        d.target_lot_price = round(t, 2)
        d.target_buyer_price = round(t * markup, 2)
        ups = [u for u in self.updates.get(offer_id, []) if now - u < 3600]
        self.updates[offer_id] = ups
        if d.target_lot_price <= 0:
            d.notes.append("нет ориентира цены — не трогаем")
            return d
        if current is not None and abs(d.target_lot_price - current) < float(cfg.min_change):
            d.notes.append("изменение меньше min_change — не трогаем")
            return d
        if len(ups) >= int(cfg.max_updates_per_hour):
            d.notes.append("лимит обновлений в час исчерпан")
            return d
        d.apply = True
        return d

    def mark_applied(self, offer_id: str, now: float | None = None) -> None:
        self.updates.setdefault(offer_id, []).append(now or time.time())
