"""Цены и прозрачный расчёт. Клиент видит каждую строку: товар, скидку, комиссию способа оплаты, курс и итог.
Не показываем только себестоимость и маржу (общие схемы спредов) — они остаются в панели владельца.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

METHOD_TITLES = {"card": "Картой / СБП", "crypto": "Криптой (@CryptoBot)", "stars": "Звёздами Telegram",
                 "balance": "С баланса магазина", "wallet": "Криптой на кошелёк", "gift": "Подарками Telegram",
                 "skins": "Скинами Steam"}
METHODS = ("wallet", "crypto", "stars", "gift", "skins", "card", "balance")


def wallet_spread(cfg) -> float:
    """Спред общего кошелька: заданный, но не меньше минимального (сеть + обмен в круге)."""
    return max(float(cfg.get("payments.wallet.spread_pct") or 0), float(cfg.get("payments.wallet.min_spread_pct") or 0))


def gift_star_rub(cfg) -> float:
    """Сколько рублей засчитываем за 1 ⭐ цены продажи подарка (курс звезды − haircut)."""
    return float(cfg.get("payments.stars.rub_per_star") or 1.25) * (1 - float(cfg.get("payments.gift.haircut_pct") or 0) / 100)
PRODUCT_TITLES = {"stars": "Звёзды Telegram", "gift": "Подарок Telegram", "premium": "Telegram Premium",
                  "steam_keys": "Ключи для Steam"}


def r2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


@dataclass
class Quote:
    product: str
    title: str
    amount: int
    cost_rub: float
    price_rub: float
    discount_rub: float = 0.0
    fee_pct: float = 0.0
    fee_rub: float = 0.0
    total_rub: float = 0.0
    method: str = ""
    asset: str = "RUB"
    pay_amount: float = 0.0
    rate: float = 1.0
    rate_source: str = ""
    lines: list[tuple[str, str]] = field(default_factory=list)

    @property
    def margin_rub(self) -> float:
        return r2(self.price_rub - self.discount_rub - self.cost_rub)

    def as_dict(self) -> dict[str, Any]:
        return {"product": self.product, "title": self.title, "amount": self.amount, "price_rub": self.price_rub,
                "discount_rub": self.discount_rub, "fee_pct": self.fee_pct, "fee_rub": self.fee_rub,
                "total_rub": self.total_rub, "method": self.method, "asset": self.asset,
                "pay_amount": self.pay_amount, "rate": self.rate, "lines": self.lines}


def shop_price(cfg, product: str, cost_rub: float, amount: int) -> tuple[float, str]:
    """Цена товара для клиента и объяснение для журнала RTBA (клиенту не показывается)."""
    key = {"gift": "gifts"}.get(product, product)
    c = cfg.get(f"catalog.{key}") or {}
    target = cost_rub * (1 + float(c.get("margin_pct", 10)) / 100)
    floor = cost_rub * (1 + float(c.get("min_margin_pct", 3)) / 100) + float(cfg.get("rtba.min_quote_margin_rub") or 0)
    why = f"себестоимость {r2(cost_rub)} + маржа {c.get('margin_pct')}%"
    if product == "stars" and c.get("funpay_ref_rub_per_star"):
        ref = float(c["funpay_ref_rub_per_star"]) * amount * (1 - float(c.get("undercut_funpay_pct", 0)) / 100)
        if ref < target:
            target = ref
            why = f"дешевле FunPay на {c.get('undercut_funpay_pct')}% ({r2(ref)})"
    if target < floor:
        target, why = floor, f"минимальная маржа {c.get('min_margin_pct')}%"
    return r2(math.ceil(target * 100) / 100), why


def referral_discount(cfg, user, price_rub: float, cost_rub: float, product: str) -> float:
    """Скидка приглашённому на первый заказ — в пределах маржи (RTBA не даёт уйти ниже минимальной)."""
    if not cfg.get("referral.enabled") or user is None or not user["referred_by"] or user["first_order_at"]:
        return 0.0
    key = {"gift": "gifts"}.get(product, product)
    floor = cost_rub * (1 + float(cfg.get(f"catalog.{key}.min_margin_pct") or 0) / 100)
    want = min(price_rub * float(cfg.get("referral.friend_discount_pct") or 0) / 100,
               float(cfg.get("referral.friend_discount_max_rub") or 0))
    return r2(max(0.0, min(want, price_rub - floor)))


def apply_method(cfg, q: Quote, method: str, rates, asset: str = "USDT") -> Quote:
    base = r2(q.price_rub - q.discount_rub)
    q.method = method
    q.lines = [(q.title, f"{q.price_rub:.2f} ₽")]
    if q.discount_rub:
        q.lines.append(("Скидка по приглашению", f"−{q.discount_rub:.2f} ₽"))
    if method == "card":
        q.fee_pct = float(cfg.get("payments.card.fee_pct") or 0)
        q.asset, q.rate, q.rate_source = "RUB", 1.0, ""
    elif method == "crypto":
        q.fee_pct = max(float(cfg.get("payments.crypto.fee_pct") or 0),
                        float(cfg.get("payments.crypto.min_fee_pct") or 2))
        q.asset = asset.upper()
        q.rate, q.rate_source = r2(rates.rub_per(q.asset)), rates.source
    elif method == "stars":
        q.fee_pct, q.asset = 0.0, "XTR"
        q.rate, q.rate_source = float(cfg.get("payments.stars.rub_per_star") or 1.25), "магазин"
    elif method == "wallet":
        # комиссии строкой нет — спред внутри курса обмена (виден клиенту рядом с биржевым)
        q.fee_pct, q.asset = 0.0, asset.upper()
        market = rates.rub_per(q.asset)
        q.rate, q.rate_source = r2(market * (1 - wallet_spread(cfg) / 100)), f"Bybit {r2(market)} ₽"
    elif method == "gift":
        q.fee_pct, q.asset = 0.0, "GIFT"
        q.rate, q.rate_source = round(gift_star_rub(cfg), 4), "цена продажи подарка"
    elif method == "skins":
        q.fee_pct, q.asset = 0.0, "USD"
        market = rates.rub_per("USDT")
        haircut = float(cfg.get("payments.skins.haircut_pct") or 0)
        q.rate, q.rate_source = r2(market * (1 - haircut / 100)), f"{cfg.get('payments.skins.market') or 'Market.CSGO'}"
    else:
        q.fee_pct, q.asset, q.rate = 0.0, "RUB", 1.0
    q.fee_rub = r2(base * q.fee_pct / 100)
    q.total_rub = r2(base + q.fee_rub)
    if q.fee_rub:
        label = {"card": "Комиссия за оплату картой/СБП", "crypto": "Конвертация по курсу"}.get(method, "Комиссия")
        q.lines.append((f"{label} {q.fee_pct:g}%", f"+{q.fee_rub:.2f} ₽"))
    if method == "crypto":
        digits = 2 if q.asset == "USDT" else 4
        q.pay_amount = math.ceil(q.total_rub / q.rate * 10 ** digits) / 10 ** digits
        q.lines.append((f"Курс {q.asset} ({q.rate_source})", f"{q.rate:.2f} ₽"))
        q.lines.append(("К оплате", f"{q.pay_amount:g} {q.asset}"))
    elif method == "stars":
        q.pay_amount = float(math.ceil(q.total_rub / q.rate))
        q.lines.append(("Курс звезды", f"{q.rate:g} ₽"))
        q.lines.append(("К оплате", f"{int(q.pay_amount)} ⭐"))
    elif method == "wallet":
        digits = 2 if q.asset == "USDT" else 3
        q.pay_amount = math.ceil(q.total_rub / q.rate * 10 ** digits) / 10 ** digits
        q.lines.append((f"Курс обмена {q.asset}", f"{q.rate:.2f} ₽ (биржа {q.rate_source.split()[-2]} ₽)"))
        q.lines.append(("К оплате", f"{q.pay_amount:g} {q.asset} на кошелёк TON"))
    elif method == "gift":
        q.pay_amount = float(math.ceil(q.total_rub / q.rate))
        q.lines.append(("Подарок засчитывается", f"1 ⭐ цены продажи = {q.rate:.2f} ₽"))
        q.lines.append(("К оплате", f"подарки на {int(q.pay_amount)} ⭐ (цена продажи)"))
    elif method == "skins":
        q.pay_amount = math.ceil(q.total_rub / q.rate * 100) / 100
        q.lines.append(("Скины засчитываются", f"1 $ по {q.rate_source} = {q.rate:.2f} ₽"))
        q.lines.append(("К оплате", f"скины на {q.pay_amount:g} $ по {q.rate_source}"))
    else:
        q.pay_amount = q.total_rub
        q.lines.append(("К оплате", f"{q.total_rub:.2f} ₽"))
    return q


# ---------------------------------------------------------------------- выкуп
@dataclass
class SellQuote:
    product: str
    amount: int               # звёзд / ценность подарка в звёздах
    payout: str               # usdt | stars
    gross: float
    fee_pct: float
    fee: float
    net: float
    asset: str
    lines: list[tuple[str, str]] = field(default_factory=list)


def sell_quote(cfg, product: str, stars_value: int, payout: str, title: str = "") -> SellQuote:
    fee_pct = float(cfg.get("buyback.fee_pct") or 3)
    if payout == "stars":
        gross = float(stars_value)
        fee = float(math.ceil(gross * fee_pct / 100))
        net = gross - fee
        lines = [(title or "Подарок", f"{stars_value} ⭐ (цена продажи Telegram)"),
                 (f"Комиссия магазина {fee_pct:g}%", f"−{int(fee)} ⭐"), ("Вы получите", f"{int(net)} ⭐")]
        return SellQuote(product, stars_value, "stars", gross, fee_pct, fee, net, "XTR", lines)
    rate = float(cfg.get("buyback.stars.usd_per_star") or 0.0115)
    gross = round(stars_value * rate, 4)
    fee = round(gross * fee_pct / 100, 4)
    net = math.floor((gross - fee) * 100) / 100
    lines = [(title or f"{stars_value} ⭐", f"{stars_value} ⭐ × {rate:g} $ = {gross:.2f} USDT"),
             (f"Комиссия магазина {fee_pct:g}%", f"−{fee:.2f} USDT"), ("Вы получите", f"{net:.2f} USDT")]
    return SellQuote(product, stars_value, "usdt", gross, fee_pct, fee, net, "USDT", lines)
