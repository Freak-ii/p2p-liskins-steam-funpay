"""RTBA — Real-Time Bid Adjustment: автоматическое управление ценой лота по рынку FunPay.

Логика:
 1. Снимаем публичную таблицу предложений подкатегории, фильтруем ключи Mann Co.
 2. Отбрасываем себя, игнор-лист, мелкие лоты, офлайн (опционально) и демпинг-выбросы (IQR).
 3. Целевая цена по стратегии: top1 (дешевле лидера), topN (встать на позицию N), median.
 4. Подтягиваемся вверх, если мы дешевле следующего конкурента больше чем на raise_gap_pct.
 5. Надбавка за дефицит при малом остатке.
 6. Ограничиваем полом (себестоимость + маржа, с учётом комиссии) и потолком.
 7. Гистерезис (min_change) и лимит обновлений в час — без «дёрганья» цены.

Цена в публичной таблице — цена для покупателя. Отношение «цена покупателя / цена в
редакторе лота» калибруется автоматически по нашему же лоту в таблице.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from mannbot.core.models import CompetitorLot


@dataclass
class PriceDecision:
    target_buyer_price: float | None
    target_lot_price: float | None
    reason: str
    best_competitor: float | None = None
    competitors: int = 0
    position: int | None = None
    apply: bool = False
    notes: list[str] = field(default_factory=list)


def filter_competitors(lots: list[CompetitorLot], cfg, own_offer_ids: set[str], own_username: str
                       ) -> list[CompetitorLot]:
    ignore = {s.lower() for s in (cfg.ignore_competitors or []) if s}
    out = []
    for lot in lots:
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


def price_floor(cfg) -> float:
    floor = float(cfg.floor_price or 0)
    if cfg.cost_per_key:
        fee = float(cfg.funpay_fee_pct or 0) / 100
        by_margin = float(cfg.cost_per_key) * (1 + float(cfg.min_margin_pct) / 100) / max(1e-9, 1 - fee)
        floor = max(floor, by_margin)
    return round(floor, 2)


class RTBAEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.markup: float = 1.0           # цена покупателя / цена лота
        self.updates: list[float] = []     # время применённых обновлений

    def calibrate(self, lots: list[CompetitorLot], own_offer_ids: set[str], own_lot_price: float | None) -> None:
        if not own_lot_price:
            return
        for lot in lots:
            if lot.offer_id in own_offer_ids and lot.price > 0:
                ratio = lot.price / own_lot_price
                if 0.8 < ratio < 1.6:
                    self.markup = round(ratio, 4)
                return

    def decide(self, lots: list[CompetitorLot], *, own_offer_ids: set[str], own_username: str,
               current_lot_price: float | None, stock: int, now: float | None = None) -> PriceDecision:
        cfg = self.cfg
        now = now or time.time()
        comps = filter_competitors(lots, cfg, own_offer_ids, own_username)
        floor_lot = price_floor(cfg)
        ceil_lot = float(cfg.ceiling_price or 0) or None
        floor_buyer = floor_lot * self.markup
        if not comps:
            target = (ceil_lot or current_lot_price or floor_lot)
            return self._finish(PriceDecision(target * self.markup, target, "нет конкурентов — держим потолок/текущую"),
                                current_lot_price, floor_lot, ceil_lot, now)
        prices = [c.price for c in comps]
        best = prices[0]
        strategy = cfg.strategy
        if strategy == "median":
            anchor = statistics.median(prices)
            reason = f"медиана рынка {anchor:.2f}"
        elif strategy == "topN":
            pos = max(1, int(cfg.target_position))
            anchor = prices[min(pos, len(prices)) - 1]
            reason = f"позиция {pos}: цена конкурента {anchor:.2f}"
        else:
            anchor = best
            reason = f"лидер рынка {best:.2f}"
        undercut = max(float(cfg.undercut_abs or 0), anchor * float(cfg.undercut_pct or 0) / 100)
        target_buyer = anchor - undercut

        # Мы и так ниже всех с большим отрывом? Подтягиваемся к следующему конкуренту.
        notes = []
        if current_lot_price:
            cur_buyer = current_lot_price * self.markup
            gap = (best - cur_buyer) / best * 100 if best else 0
            if gap > float(cfg.raise_gap_pct):
                notes.append(f"мы дешевле лидера на {gap:.1f}% — поднимаем цену")
        # дефицит
        if stock <= int(cfg.scarcity_stock_threshold) and stock > 0:
            prem = float(cfg.scarcity_premium_pct or 0)
            if prem:
                target_buyer *= 1 + prem / 100
                notes.append(f"малый остаток ({stock}) — надбавка {prem}%")
        if target_buyer < floor_buyer:
            notes.append(f"рынок ниже нашего пола ({floor_buyer:.2f}) — стоим на полу")
        target_lot = target_buyer / self.markup
        position = 1 + sum(1 for p in prices if p < target_buyer)
        d = PriceDecision(target_buyer, target_lot, reason, best_competitor=round(best, 2), competitors=len(comps),
                          position=position, notes=notes)
        return self._finish(d, current_lot_price, floor_lot, ceil_lot, now)

    def _finish(self, d: PriceDecision, current: float | None, floor: float, ceil: float | None,
                now: float) -> PriceDecision:
        cfg = self.cfg
        t = d.target_lot_price or floor
        t = max(t, floor)
        if ceil:
            t = min(t, ceil)
        d.target_lot_price = round(t, 2)
        d.target_buyer_price = round(t * self.markup, 2)
        self.updates = [u for u in self.updates if now - u < 3600]
        if current is not None and abs(d.target_lot_price - current) < float(cfg.min_change):
            d.notes.append("изменение меньше min_change — не трогаем")
            return d
        if len(self.updates) >= int(cfg.max_updates_per_hour):
            d.notes.append("лимит обновлений в час исчерпан")
            return d
        d.apply = True
        return d

    def mark_applied(self, now: float | None = None) -> None:
        self.updates.append(now or time.time())
