"""Подбор подарков Telegram под номинал заказа.

На FunPay в разделе «Звёзды → Подарком» номинал — это сколько звёзд покупатель получит, когда конвертирует
подарок (у обычных подарков Telegram: 15 ⭐ → 13, 25 → 21, 50 → 43, 100 → 85). Поэтому по умолчанию бот
подбирает набор подарков, у которого сумма «конвертации» не меньше номинала, а стоимость для нас минимальна
(режим convert). Режим price — сумма цен подарков ровно (или чуть больше) номинала, для лотов «подарки на N ⭐».
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Gift:
    id: int
    stars: int                 # цена подарка для отправителя
    convert_stars: int         # сколько звёзд получит получатель при конвертации
    title: str = ""
    limited: bool = False
    sold_out: bool = False
    require_premium: bool = False
    emoji: str = ""
    resale: int = 0             # сколько улучшенных (NFT) экземпляров этого подарка выставлено на рынке Telegram
    resell_min: int = 0         # самая низкая цена на рынке Telegram, ⭐ (0 — не продаётся)

    @property
    def usable(self) -> bool:
        return not self.sold_out and not self.limited and not self.require_premium and self.stars > 0


@dataclass
class GiftPlan:
    gifts: list[Gift] = field(default_factory=list)
    mode: str = "convert"

    @property
    def cost(self) -> int:
        return sum(g.stars for g in self.gifts)

    @property
    def value(self) -> int:
        return sum(g.convert_stars for g in self.gifts)

    def summary(self) -> str:
        groups: dict[int, int] = {}
        for g in self.gifts:
            groups[g.id] = groups.get(g.id, 0) + 1
        by_id = {g.id: g for g in self.gifts}
        parts = [f"{n}×{by_id[i].emoji or '🎁'}{by_id[i].stars}⭐" for i, n in groups.items()]
        return " + ".join(parts)

    def to_json(self) -> list[dict]:
        return [{"id": g.id, "stars": g.stars, "convert": g.convert_stars, "emoji": g.emoji} for g in self.gifts]


class PlanError(Exception):
    pass


def _choose(catalog: list[Gift], allowed: set[int] | None) -> list[Gift]:
    best: dict[int, Gift] = {}
    for g in catalog:
        if not g.usable or (allowed and g.id not in allowed):
            continue
        cur = best.get(g.stars)
        if cur is None or g.convert_stars > cur.convert_stars:       # из подарков одной цены — с лучшей конвертацией
            best[g.stars] = g
    return sorted(best.values(), key=lambda g: g.stars)


def plan_gifts(catalog: list[Gift], stars: int, *, mode: str = "convert", allowed_ids: list[int] | None = None,
               max_gifts: int = 40, per_gift_penalty: float = 1.0) -> GiftPlan:
    """Набор подарков для номинала stars. Минимизируем стоимость + штраф за каждый подарок
    (per_gift_penalty ⭐: чем больше, тем меньше подарков в наборе, ценой небольшой переплаты)."""
    pool = _choose(catalog, set(int(x) for x in allowed_ids or []) or None)
    if not pool:
        raise PlanError("в каталоге Telegram нет доступных подарков (все распроданы или ограничены)")
    if stars <= 0:
        raise PlanError("номинал должен быть больше нуля")
    weight = (lambda g: g.convert_stars) if mode == "convert" else (lambda g: g.stars)
    pool = [g for g in pool if weight(g) > 0]
    top = max(weight(g) for g in pool)
    limit = stars + top                       # перебор «с запасом» — до одного лишнего подарка
    INF = (10 ** 9, 10 ** 9)
    # dp[v] = (стоимость, число подарков) минимальная, чтобы набрать ровно v единиц «веса»
    dp: list[tuple[float, int]] = [INF] * (limit + 1)
    choice: list[Gift | None] = [None] * (limit + 1)
    dp[0] = (0, 0)
    for v in range(1, limit + 1):
        for g in pool:
            w = weight(g)
            if w <= v and dp[v - w] != INF:
                cand = (dp[v - w][0] + g.stars + per_gift_penalty, dp[v - w][1] + 1)
                if cand < dp[v]:
                    dp[v], choice[v] = cand, g
    best_v = None
    for v in range(stars, limit + 1):
        if dp[v] != INF and (best_v is None or dp[v] < dp[best_v]):
            best_v = v
    if best_v is None:
        raise PlanError(f"не удалось собрать {stars} ⭐ из доступных подарков")
    gifts: list[Gift] = []
    v = best_v
    while v > 0:
        g = choice[v]
        assert g is not None
        gifts.append(g)
        v -= weight(g)
    if len(gifts) > max_gifts:
        raise PlanError(f"для {stars} ⭐ нужно {len(gifts)} подарков — больше лимита {max_gifts}")
    gifts.sort(key=lambda g: -g.stars)
    return GiftPlan(gifts, mode)


# Стандартные подарки Telegram — только для оценок (наличие лотов, калькулятор), пока каталог не загружен.
# id отрицательные: отправить такой «подарок» нельзя, отправка всегда идёт по живому каталогу аккаунта.
DEFAULT_CATALOG = [
    Gift(-15, 15, 13, "Мишка", emoji="🧸"),
    Gift(-25, 25, 21, "Роза", emoji="🌹"),
    Gift(-50, 50, 43, "Торт", emoji="🎂"),
    Gift(-100, 100, 85, "Кубок", emoji="🏆"),
]
