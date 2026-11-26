"""Экономика магазина: уровни лояльности (киты), курс выкупа звёзд, антискам, отзывы и метки трафика.

Принципы (см. README → «Экономика»):
  * покупателю выгоднее, чем на FunPay: там к цене продавца +14% комиссии площадки, у нас её нет;
  * владельцу выгодно: все скидки (лояльность, приглашение) вместе не опускают маржу ниже loyalty.min_margin_after_pct;
  * выкуп звёзд — на 1% выше официального курса вывода Telegram (0.013 $ за ⭐ через Fragment после 21 дня ожидания),
    но не выше «ценности использования» звезды (0.015 $ на Fragment) минус минимальный спред;
  * всё рискованное (новые продавцы, самоприглашения, всплески) уходит на проверку, а не в отказ.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass

DAY = 86400
SRC_RE = re.compile(r"^[a-z0-9_]{1,32}$")


# ---------------------------------------------------------------------- уровни лояльности
@dataclass
class Tier:
    id: str
    title: str
    min_rub: float
    discount_pct: float = 0.0
    buyback_bonus_pct: float = 0.0
    limit_mult: float = 1.0
    turnover: float = 0.0
    next_title: str = ""
    next_rub: float = 0.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def turnover_rub(cfg, db, uid: int, now: float | None = None) -> float:
    """Оборот клиента за окно: выданные покупки (₽) + выкуп у него (USDT по курсу ≈ ₽)."""
    now = now or time.time()
    since = now - float(cfg.get("loyalty.window_days") or 30) * DAY
    buy = float(db.scalar("SELECT SUM(total_rub) FROM orders WHERE user_id=? AND kind='buy' AND status='delivered' "
                          "AND created_at>=?", (uid, since), 0) or 0)
    sold = float(db.scalar("SELECT SUM(payout_amount) FROM orders WHERE user_id=? AND kind='sell' AND "
                           "status='paid_out' AND payout_asset='USDT' AND created_at>=?", (uid, since), 0) or 0)
    usdt_rub = float(cfg.get("rates.usdt_rub") or 86.8)
    return round(buy + sold * usdt_rub, 2)


def tier_for(cfg, db, user) -> Tier:
    tiers = sorted(cfg.get("loyalty.tiers") or [], key=lambda t: float(t.get("min_rub") or 0))
    base = Tier("base", "Клиент", 0)
    if user is None or not tiers:
        return base
    tv = turnover_rub(cfg, db, user["id"])
    manual = str(user["tags"] or "")
    cur, nxt = tiers[0], None
    for i, t in enumerate(tiers):
        forced = f"tier:{t['id']}" in manual              # владелец может вручную дать уровень (тег в CRM)
        if tv >= float(t.get("min_rub") or 0) or forced:
            cur = t
            nxt = tiers[i + 1] if i + 1 < len(tiers) else None
    if not cfg.get("loyalty.enabled"):
        cur = tiers[0]
    return Tier(str(cur["id"]), str(cur.get("title") or cur["id"]), float(cur.get("min_rub") or 0),
                float(cur.get("discount_pct") or 0), float(cur.get("buyback_bonus_pct") or 0),
                float(cur.get("limit_mult") or 1), tv, str((nxt or {}).get("title") or ""),
                float((nxt or {}).get("min_rub") or 0))


def bounded_discount(cfg, price_rub: float, cost_rub: float, product: str, want_rub: float,
                     already_rub: float = 0.0) -> float:
    """Скидка не опускает маржу заказа ниже loyalty.min_margin_after_pct (и минимальной маржи товара)."""
    key = {"gift": "gifts", "service": "services"}.get(product, product)
    floor_pct = max(float(cfg.get("loyalty.min_margin_after_pct") or 0),
                    float(cfg.get(f"catalog.{key}.min_margin_pct") or 0) / 2)
    floor = cost_rub * (1 + floor_pct / 100)
    room = price_rub - already_rub - floor
    return round(max(0.0, min(want_rub, room)), 2)


def loyalty_discount(cfg, tier: Tier, price_rub: float, cost_rub: float, product: str, already_rub: float) -> float:
    if not cfg.get("loyalty.enabled") or tier.discount_pct <= 0:
        return 0.0
    return bounded_discount(cfg, price_rub, cost_rub, product, price_rub * tier.discount_pct / 100, already_rub)


# ---------------------------------------------------------------------- выкуп звёзд
def buyback_usd_per_star(cfg, tier: Tier | None = None, xtr_stock: int | None = None) -> tuple[float, float, str]:
    """(курс выкупа $ за ⭐ без отдельной комиссии, комиссия %, пояснение).

    auto: официальный курс вывода Telegram × (1 + beat_pct), но не выше use_usd × (1 − min_spread_pct);
          бонус уровня клиента добавляется сверху в тех же пределах; при переполненном запасе XTR курс — как вывод.
    fixed: старое поведение (usd_per_star и отдельная комиссия fee_pct)."""
    b = cfg.get("buyback.stars") or {}
    if str(b.get("mode") or "fixed") != "auto":
        return float(b.get("usd_per_star") or 0.0115), float(cfg.get("buyback.fee_pct") or 3), "фиксированный курс"
    ref = float(b.get("ref_usd_per_star") or 0.013)
    use = float(b.get("use_usd_per_star") or 0.015)
    cap = use * (1 - float(b.get("min_spread_pct") or 6) / 100)
    rate = ref * (1 + float(b.get("beat_pct") or 1) / 100)
    why = f"на {float(b.get('beat_pct') or 1):g}% выше вывода Telegram ({ref:g} $)"
    if tier is not None and tier.buyback_bonus_pct:
        rate *= 1 + tier.buyback_bonus_pct / 100
        why += f", +{tier.buyback_bonus_pct:g}% уровень «{tier.title}»"
    full = int(b.get("max_stock_stars") or 0)
    if full and xtr_stock is not None and xtr_stock >= full:
        rate, why = ref * (1 - float(b.get("full_stock_cut_pct") or 2) / 100), "запас звёзд полон — курс как у вывода"
    rate = min(rate, cap)
    return math.floor(rate * 1e6) / 1e6, 0.0, why


# ---------------------------------------------------------------------- антискам
@dataclass
class Risk:
    ok: bool = True
    review: bool = False
    reason: str = ""


def sell_risk(cfg, db, user, payout_usdt: float, now: float | None = None, exclude: str = "") -> Risk:
    """Выкуп: новые продавцы и всплески — на проверку. Оплата звёздами у нас мгновенная, а Telegram может отозвать
    звёзды, купленные с украденной карты, поэтому первые продажи нового клиента проверяет человек."""
    now = now or time.time()
    a = cfg.get("antiscam") or {}
    if user is None or user["blocked"]:
        return Risk(False, False, "клиент заблокирован")
    age_h = (now - float(user["created_at"] or now)) / 3600
    done = int(db.scalar("SELECT COUNT(*) FROM orders WHERE user_id=? AND status IN ('delivered','paid_out')",
                         (user["id"],), 0) or 0)
    new = age_h < float(a.get("new_seller_hours") or 72) or done < int(a.get("new_seller_min_deals") or 1)
    day = float(db.scalar("SELECT SUM(payout_amount) FROM orders WHERE user_id=? AND kind='sell' AND payout_asset="
                          "'USDT' AND status IN ('paid_out','payout_pending','received') AND created_at>=? AND id<>?",
                          (user["id"], now - DAY, exclude), 0) or 0)
    cap = float(a.get("new_seller_daily_usdt" if new else "seller_daily_usdt") or 0)
    if cap and day + payout_usdt > cap:
        return Risk(True, True, f"{'новый продавец: ' if new else ''}за сутки {day + payout_usdt:.2f} USDT > {cap:g}")
    return Risk()


def self_referral(db, order, referrer) -> str:
    """Приглашение «самого себя»: тот же получатель, что у пригласившего, или пригласивший — получатель."""
    if referrer is None:
        return ""
    rec = str(order["recipient"] or "").lstrip("@").lower()
    if rec and rec == str(referrer["username"] or "").lower():
        return "получатель — сам пригласивший"
    if rec and db.scalar("SELECT COUNT(*) FROM orders WHERE user_id=? AND lower(recipient) IN (?,?)",
                         (referrer["id"], rec, "@" + rec), 0):
        return "тот же получатель, что в заказах пригласившего"
    return ""


def referral_day_cap_left(cfg, db, referrer_id: int, now: float | None = None) -> float:
    cap = float(cfg.get("antiscam.ref_daily_cap_rub") or 0)
    if not cap:
        return float("inf")
    got = float(db.scalar("SELECT SUM(delta) FROM ledger WHERE user_id=? AND kind='ref_reward' AND ts>=?",
                          (referrer_id, (now or time.time()) - DAY), 0) or 0)
    return max(0.0, cap - got)


def bonus_usable(cfg, user, total_rub: float) -> float:
    """Сколько бонусов можно списать в заказ (бонусы не выводятся, только скидка до N% заказа)."""
    share = float(cfg.get("antiscam.bonus_pay_share_pct") or 0) / 100
    return round(max(0.0, min(float(user["bonus_rub"] or 0), total_rub * share)), 2) if user is not None else 0.0


# ---------------------------------------------------------------------- метки трафика
def parse_start(arg: str) -> tuple[str, str, str]:
    """/start payload → (ref_code, source, product). Форматы: ref_X, src_Y, src_Y__stars, ref_X__src_Y, stars."""
    ref = src = prod = ""
    for part in (arg or "").split("__"):
        if part.startswith("ref_"):
            ref = part[4:]
        elif part.startswith("src_") and SRC_RE.match(part[4:]):
            src = part[4:]
        elif part:
            prod = part
    return ref, src, prod


def sources_report(db, days: float = 30) -> list[dict]:
    since = time.time() - days * DAY
    rows = db.q("SELECT u.source s, COUNT(*) starts, "
                "SUM(CASE WHEN EXISTS(SELECT 1 FROM orders o WHERE o.user_id=u.id AND o.kind='buy' AND "
                "o.status='delivered') THEN 1 ELSE 0 END) buyers FROM users u WHERE COALESCE(u.source,'')<>'' AND "
                "u.created_at>=? GROUP BY u.source", (since,))
    out = {r["s"]: {"source": r["s"], "starts": r["starts"], "buyers": r["buyers"] or 0, "orders": 0,
                    "revenue_rub": 0.0} for r in rows}
    for r in db.q("SELECT source s, COUNT(*) n, SUM(total_rub) rev FROM orders WHERE kind='buy' AND "
                  "status='delivered' AND COALESCE(source,'')<>'' AND created_at>=? GROUP BY source", (since,)):
        d = out.setdefault(r["s"], {"source": r["s"], "starts": 0, "buyers": 0, "orders": 0, "revenue_rub": 0.0})
        d["orders"], d["revenue_rub"] = r["n"], round(float(r["rev"] or 0), 2)
    return sorted(out.values(), key=lambda d: (-d["revenue_rub"], -d["starts"]))


# ---------------------------------------------------------------------- отзывы
def review_stats(db) -> dict:
    r = db.one("SELECT COUNT(*) n, AVG(rating) avg FROM reviews WHERE status<>'hidden'")
    return {"count": int(r["n"] or 0), "avg": round(float(r["avg"] or 0), 2)}
