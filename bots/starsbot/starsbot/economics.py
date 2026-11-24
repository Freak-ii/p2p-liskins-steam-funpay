"""Деньги по кругу: FunPay → вывод → покупка TON → кошелёк бота → Fragment → звёзды покупателю.

Считает:
  * эффективный курс TON (по вашим пополнениям из журнала, иначе биржевой + спред + комиссия перевода);
  * экономику одного заказа и калькулятор по номиналам: цена → комиссии → чистыми → себестоимость → прибыль;
  * итоги периода: что пришло, что ушло и куда, что заморожено в неподтверждённых заказах;
  * сверку кошелька: баланс по учёту (пополнения − покупки − газ) против баланса в сети.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from starsbot.core.models import OrderStatus as S

DAY = 86400
MARKETS = ("ozon", "ggsel", "avito", "wb")        # площадки кроме FunPay: комиссии и товары — в своих разделах конфига


@dataclass
class Flow:
    """Один «круг» денег для заказа на stars звёзд по цене price."""
    stars: int
    price: float                 # цена лота (что получает продавец на FunPay), ₽
    funpay_fee: float
    withdraw_fee: float
    net_rub: float               # дошло до вас после вывода
    ton_stars: float             # TON на сами звёзды (Fragment)
    ton_gas: float               # TON на комиссию сети
    ton_total: float
    ton_rate: float | None       # ₽ за 1 TON с учётом спреда и комиссии пополнения
    cost_rub: float | None       # во что обошлись звёзды в рублях
    profit: float | None
    margin_pct: float | None
    breakeven_price: float | None
    ton_per_star: float | None

    def as_dict(self) -> dict:
        return asdict(self)


class Economics:
    def __init__(self, cfg, db, rates, engine=None):
        self.cfg = cfg               # весь конфиг
        self.db = db
        self.rates = rates
        self.engine = engine

    @property
    def e(self):
        return self.cfg.economics

    # ------------------------------------------------------------------ курс
    def topup_rate(self, days: float = 60) -> float | None:
        """Средний курс ваших пополнений (₽ за TON) за последние days дней.
        economics.cost_rate_rub > 0 — курс задан вручную (например, цена следующей закупки), записи не учитываются."""
        fixed = float(self.e.get("cost_rate_rub") or 0) if hasattr(self.e, "get") else 0.0
        if fixed > 0:
            return fixed
        row = self.db.one("SELECT SUM(rub) r, SUM(ton) t FROM cashflow WHERE kind='topup' AND rub>0 AND ton>0 AND ts>=?",
                          (time.time() - days * DAY,))
        if row and row["t"]:
            return float(row["r"]) / float(row["t"])
        return None

    def effective_rate(self) -> tuple[float | None, str]:
        """Сколько рублей реально стоит 1 TON на кошельке бота.

        Порядок: ручной курс → живая цена закупки (P2P USDT × биржа, economics.cost_basis=live) → журнал пополнений →
        биржа + спред. Цены лотов считаем по цене СЛЕДУЮЩЕЙ закупки: средняя за 60 дней врёт, когда GRAM движется."""
        if not self._fixed_rate() and self.live():
            overhead = float(self.e.deposit_fee_ton or 0) / max(1e-9, float(self.e.typical_topup_ton or 0) or 1e9)
            return round(self.rates.value * (1 + overhead), 2), "P2P USDT × биржа"
        mine = self.topup_rate()
        if mine:
            return round(mine, 2), "ваши пополнения"
        market = self.rates.value if self.rates else None
        if not market:
            return None, "нет курса"
        overhead = float(self.e.deposit_fee_ton or 0) / max(1e-9, float(self.e.typical_topup_ton or 0) or 1e9)
        rate = market * (1 + float(self.e.exchange_spread_pct or 0) / 100) * (1 + overhead)
        return round(rate, 2), "биржевой курс + спред"

    def _fixed_rate(self) -> float:
        return float(self.e.get("cost_rate_rub") or 0) if hasattr(self.e, "get") else 0.0

    def live(self) -> bool:
        """Считаем по живой цене закупки? (есть свежий P2P-курс и cost_basis=live)"""
        basis = str(self.e.get("cost_basis") or "live") if hasattr(self.e, "get") else "live"
        return basis == "live" and bool(getattr(self.rates, "is_p2p", False))

    def star_ton_live(self) -> float | None:
        """Звезда на Fragment стоит фиксированные доллары (0.015 $), поэтому в GRAM цена меняется вместе с курсом.
        Цена из прошлых покупок устаревает: после падения GRAM на 7% бот недосчитал бы 7% себестоимости."""
        usd = float(self.e.get("star_usd") or 0) if hasattr(self.e, "get") else 0.0
        gram = getattr(self.rates, "gram_usdt", None) if self.rates else None
        if usd > 0 and gram and getattr(self.rates, "is_p2p", False):
            return usd / float(gram)
        return None

    def rate_warning(self, threshold_pct: float = 4.0) -> str:
        """Ручной курс разошёлся с реальной ценой закупки или не учтён вывод с FunPay — полы цен неверны."""
        out = [w for w in (self._fx_warning(threshold_pct), self.fee_warning()) if w]
        return ". ".join(out)

    def fee_warning(self) -> str:
        """Выручка FunPay возвращается в круг только через вывод (карта 3%, USDT 6%): без него пол цены занижен."""
        if float(self.e.withdraw_fee_pct or 0) > 0:
            return ""
        return ("Комиссия вывода с FunPay стоит 0%, а деньги на закупку TON выводятся на карту (3%) или в USDT (6%): "
                "пол цены занижен на 3–6%. Укажите комиссию вывода в «Деньги»")

    def _fx_warning(self, threshold_pct: float) -> str:
        fixed = self._fixed_rate()
        if not fixed or not getattr(self.rates, "is_p2p", False):
            return ""
        diff = (fixed / float(self.rates.value) - 1) * 100
        if abs(diff) < threshold_pct:
            return ""
        return (f"Курс вручную {fixed:g} ₽ за GRAM, а закупка сейчас {self.rates.value:.2f} ₽ ({diff:+.1f}%): "
                + ("лоты дороже, чем нужно" if diff > 0 else "бот может продавать в минус")
                + ". Поставьте 0, чтобы курс считался сам")

    def ton_per_star(self) -> float | None:
        return self.engine.cost_per_star_ton() if self.engine else None

    def cost_per_star_rub(self) -> float | None:
        """Себестоимость звезды в рублях по эффективному курсу (газ сети считается на заказ, см. flow())."""
        rate, _ = self.effective_rate()
        tps = self.ton_per_star()
        return tps * rate if rate and tps else None

    def fees_pct(self) -> float:
        """Суммарная доля комиссий FunPay и вывода (для пола цены RTBA)."""
        f = float(self.e.funpay_fee_pct or 0) / 100
        w = float(self.e.withdraw_fee_pct or 0) / 100
        return round((1 - (1 - f) * (1 - w)) * 100, 3)

    # ------------------------------------------------------------------ один круг
    def fee_pcts(self, channel: str = "funpay") -> tuple[float, float]:
        """(комиссия площадки, комиссия вывода) в процентах для канала продаж."""
        if channel in MARKETS:
            oz = self.cfg.get(channel) or {}
            return float(oz.get("commission_pct") or 0), float(oz.get("withdraw_fee_pct") or 0)
        return float(self.e.funpay_fee_pct or 0), float(self.e.withdraw_fee_pct or 0)

    def flow(self, stars: int, price: float, *, ton_per_star: float | None = None, ton_stars: float | None = None,
             gas_ton: float | None = None, rate: float | None = None, channel: str = "funpay",
             payout: float | None = None) -> Flow:
        """funpay_fee — комиссия площадки (FunPay или Ozon); payout — фактическая выплата площадки, если известна."""
        e = self.e
        fee_pct, wd_pct = self.fee_pcts(channel)
        funpay_fee = price - payout if payout is not None else price * fee_pct / 100
        after_fp = price - funpay_fee
        withdraw_fee = after_fp * wd_pct / 100   # фикс. комиссия — на вывод, не на заказ
        net = after_fp - withdraw_fee
        tps = ton_per_star if ton_per_star is not None else self.ton_per_star()
        if ton_stars is None:
            ton_stars = stars * tps if tps else 0.0
        gas = float(e.gas_per_purchase_ton or 0) if gas_ton is None else gas_ton
        ton_total = ton_stars + gas
        if rate is None:
            rate, _ = self.effective_rate()
        cost = ton_total * rate if (rate and ton_stars) else None
        profit = net - cost if cost is not None else None
        keep = (1 - fee_pct / 100) * (1 - wd_pct / 100)
        breakeven = cost / keep if cost is not None and keep > 0 else None
        return Flow(stars=stars, price=round(price, 2), funpay_fee=round(funpay_fee, 2),
                    withdraw_fee=round(withdraw_fee, 2), net_rub=round(net, 2), ton_stars=round(ton_stars, 6),
                    ton_gas=round(gas, 6), ton_total=round(ton_total, 6), ton_rate=rate,
                    cost_rub=round(cost, 2) if cost is not None else None,
                    profit=round(profit, 2) if profit is not None else None,
                    margin_pct=round(profit / price * 100, 1) if profit is not None and price else None,
                    breakeven_price=round(breakeven, 2) if breakeven is not None else None,
                    ton_per_star=round(ton_stars / stars, 7) if stars and ton_stars else None)

    def order_flow(self, o) -> Flow:
        """Фактическая экономика выполненного заказа (по сумме, которую реально заплатили Fragment)."""
        # курс p2p — уже цена закупки, биржевой (CoinGecko, tonapi) дополняем спредом
        spread = 0.0 if getattr(self.rates, "is_p2p", False) else float(self.e.exchange_spread_pct or 0)
        rate = float(o["ton_rate"]) * (1 + spread / 100) if o["ton_rate"] else None
        mine = self.topup_rate()
        if mine:
            rate = mine
        channel = (o["channel"] if "channel" in o.keys() else None) or "funpay"
        payout = o["payout"] if "payout" in o.keys() and o["payout"] is not None else None
        return self.flow(int(o["stars"] or 0), float(o["price"] or 0), ton_stars=float(o["ton_amount"] or 0),
                         gas_ton=float(o["tx_fee_ton"]) if o["tx_fee_ton"] is not None else None, rate=rate,
                         channel=channel, payout=float(payout) if payout is not None else None)

    def current_prices(self, channel: str = "funpay") -> dict[int, float]:
        """Текущая цена каждого номинала: последнее решение RTBA (только FunPay) или цена из последнего заказа."""
        out: dict[int, float] = {}
        if channel == "funpay":
            for r in self.db.q("SELECT stars, new_price FROM price_history WHERE id IN (SELECT MAX(id) FROM "
                               "price_history GROUP BY stars)"):
                if r["stars"] and r["new_price"]:
                    out[int(r["stars"])] = float(r["new_price"])
        for r in self.db.q("SELECT stars_per_unit, price, quantity FROM orders WHERE COALESCE(channel,'funpay')<>'internal' AND stars_per_unit IS NOT NULL "
                           "AND COALESCE(channel,'funpay')=? ORDER BY created_at DESC LIMIT 200", (channel,)):
            s = int(r["stars_per_unit"])
            if s not in out and r["quantity"]:
                out[s] = round(float(r["price"]) / int(r["quantity"]), 2)
        return out

    def calculator(self, prices: dict[int, float] | None = None, channel: str = "funpay") -> list[Flow]:
        prices = {**self.current_prices(channel), **(prices or {})}
        if channel in MARKETS:
            items = (self.cfg.get(channel) or {}).get("products") or []
        else:
            items = self.cfg.funpay.lots or []
        denoms = sorted({int(l["stars"]) for l in items if l.get("stars")} | set(prices))
        return [self.flow(d, float(prices.get(d) or 0), channel=channel) for d in denoms]

    def min_price(self, stars: int, channel: str = "funpay", margin_pct: float | None = None) -> float | None:
        """Самая низкая цена, при которой маржа (прибыль / цена) не ниже margin_pct.

        Прибыль = цена × (1 − комиссия площадки) × (1 − комиссия вывода) − себестоимость звёзд и газа, поэтому
        цена ≥ себестоимость / (доля, что доходит до вас − маржа). None — нет курса TON или цены звезды."""
        if margin_pct is None:
            margin_pct = float((self.cfg.get(channel) or {}).get("min_margin_pct") or 3.0)
        cost = self.flow(int(stars), 0.0, channel=channel).cost_rub
        fee_pct, wd_pct = self.fee_pcts(channel)
        keep = (1 - fee_pct / 100) * (1 - wd_pct / 100) - float(margin_pct) / 100
        if cost is None or keep <= 0:
            return None
        return round(cost / keep, 2)

    def target_price(self, stars: int, channel: str, manual: float | None = None) -> dict:
        """Цена для площадки: ручная цена, но не ниже пола по марже; в режиме margin — по целевой марже."""
        c = self.cfg.get(channel) or {}
        floor = self.min_price(stars, channel, float(c.get("min_margin_pct") or 3.0))
        mode = str(c.get("price_mode") or "fixed")
        if mode == "margin":
            want = self.min_price(stars, channel, float(c.get("target_margin_pct") or 8.0))
        else:
            want = float(manual) if manual else None
        price = max(x for x in (want, floor) if x is not None) if (want or floor) else None
        step = float(c.get("price_round") or 1)
        if price is not None and step > 0:
            import math
            price = math.ceil(price / step - 1e-9) * step   # округляем вверх: маржа не опускается ниже пола
        return {"stars": int(stars), "price": round(price, 2) if price is not None else None, "floor": floor,
                "want": want, "raised": bool(floor and want is not None and want < floor), "mode": mode}

    # ------------------------------------------------------------------ кошелёк
    def wallet_capacity(self) -> list[dict]:
        """Сколько заказов каждого номинала можно выдать на текущий баланс."""
        w = self.db.kv_get("wallet") or {}
        balance = w.get("balance")
        tps = self.ton_per_star()
        gas = float(self.e.gas_per_purchase_ton or 0)
        reserve = float(self.cfg.ton.fee_reserve_ton or 0)
        committed = self.db.stars_committed()
        out = []
        denoms = sorted({int(l["stars"]) for l in (self.cfg.funpay.lots or []) if l.get("stars")})
        for d in denoms:
            n = None
            if balance is not None and tps:
                free = max(0.0, float(balance) - reserve - committed * tps)
                n = int(free / (d * tps + gas))
            out.append({"stars": d, "ton": round(d * tps + gas, 4) if tps else None, "orders": n})
        return out

    def reconcile(self) -> dict:
        """Баланс по учёту против баланса в сети. Расхождение = неучтённые пополнения/выводы или чужие списания.
        Считается от точки отсчёта (первый замер баланса или ручной сброс на странице «Деньги»)."""
        w = self.db.kv_get("wallet") or {}
        start = self.db.kv_get("wallet_start_balance")
        if start is None:
            return {"known": False, "actual": w.get("balance")}
        since = float(start.get("ts") or 0)
        topped = float(self.db.scalar("SELECT SUM(ton) FROM cashflow WHERE kind IN ('topup','adjust') AND ts>=?",
                                      (since,), 0) or 0)
        withdrawn = float(self.db.scalar("SELECT SUM(ton) FROM cashflow WHERE kind='withdraw_ton' AND ts>=?",
                                         (since,), 0) or 0)
        # реальные списания с кошелька: покупки на Fragment, включая пополнение звёзд аккаунта (у подарков TON
        # в заказе — только оценка себестоимости, с кошелька они не списываются)
        spent = float(self.db.scalar("SELECT SUM(ton_amount) FROM orders WHERE COALESCE(delivery,'fragment') IN ('fragment','premium') "
                                     "AND delivered_at>=? AND ton_amount>0", (since,), 0) or 0)
        gas = float(self.db.scalar("SELECT SUM(tx_fee_ton) FROM orders WHERE COALESCE(delivery,'fragment') IN ('fragment','premium') "
                                   "AND delivered_at>=?", (since,), 0) or 0)
        expected = float(start["ton"]) + topped - withdrawn - spent - gas
        actual = w.get("balance")
        diff = round(float(actual) - expected, 4) if actual is not None else None
        return {"known": True, "start": start, "topups": round(topped, 4), "withdrawn": round(withdrawn, 4),
                "spent": round(spent, 4), "gas": round(gas, 4), "expected": round(expected, 4), "actual": actual,
                "diff": diff, "alarm": diff is not None and abs(diff) > float(self.e.balance_mismatch_alert_ton or 0)}

    # ------------------------------------------------------------------ период
    def period(self, days: float = 30) -> dict:
        since = time.time() - days * DAY
        rows = self.db.q("SELECT * FROM orders WHERE COALESCE(channel,'funpay')<>'internal' AND created_at>=?", (since,))
        delivered = [r for r in rows if r["delivered_at"] and r["status"] != S.REFUNDED.value]
        confirmed = [r for r in delivered if r["status"] == S.COMPLETED.value]
        frozen = [r for r in rows if r["status"] in (S.DELIVERED.value,)]
        waiting = [r for r in rows if S(r["status"]).is_active and r["status"] != S.DELIVERED.value]
        flows = [self.order_flow(r) for r in delivered]
        gross = sum(f.price for f in flows)
        fp_fee = sum(f.funpay_fee for f in flows)
        wd_fee = sum(f.withdraw_fee for f in flows)
        ton_spent = sum(f.ton_stars for f in flows)
        gas = sum(f.ton_gas for f in flows)
        cost = sum(f.cost_rub or 0 for f in flows)
        profit = sum(f.profit or 0 for f in flows) if all(f.profit is not None for f in flows) else None
        top = self.db.one("SELECT SUM(rub) r, SUM(ton) t, COUNT(*) n FROM cashflow WHERE kind='topup' AND ts>=?",
                          (since,))
        expenses = float(self.db.scalar("SELECT SUM(rub) FROM cashflow WHERE kind='expense' AND ts>=?", (since,), 0) or 0)
        withdrawals = float(self.db.scalar("SELECT SUM(rub) FROM cashflow WHERE kind='withdraw_funpay' AND ts>=?",
                                           (since,), 0) or 0)
        return {
            "days": days, "orders": len(delivered), "stars": sum(int(r["stars"] or 0) for r in delivered),
            "gross": round(gross, 2), "funpay_fee": round(fp_fee, 2), "withdraw_fee": round(wd_fee, 2),
            "net": round(gross - fp_fee - wd_fee, 2), "ton_spent": round(ton_spent, 4), "gas": round(gas, 4),
            "cost": round(cost, 2), "expenses": round(expenses, 2),
            "profit": round(profit - expenses, 2) if profit is not None else None,
            "confirmed_rub": round(sum(r["price"] or 0 for r in confirmed), 2),
            "frozen_rub": round(sum(r["price"] or 0 for r in frozen), 2),
            "waiting_rub": round(sum(r["price"] or 0 for r in waiting), 2),
            "topup_rub": round(float(top["r"] or 0), 2) if top else 0, "topup_ton": round(float(top["t"] or 0), 4)
            if top else 0, "topups": int(top["n"] or 0) if top else 0, "withdrawn_funpay": round(withdrawals, 2),
            "by_denomination": self._by_denomination(delivered),
            "by_channel": self._by_channel(delivered),
        }

    def _by_channel(self, rows) -> dict[str, dict]:
        acc: dict[str, dict] = {}
        for r in rows:
            f = self.order_flow(r)
            ch = (r["channel"] if "channel" in r.keys() else None) or "funpay"
            a = acc.setdefault(ch, {"orders": 0, "stars": 0, "gross": 0.0, "fees": 0.0, "net": 0.0, "cost": 0.0,
                                    "profit": 0.0})
            a["orders"] += 1
            a["stars"] += int(r["stars"] or 0)
            a["gross"] += f.price
            a["fees"] += f.funpay_fee + f.withdraw_fee
            a["net"] += f.net_rub
            a["cost"] += f.cost_rub or 0
            a["profit"] += f.profit or 0
        for a in acc.values():
            for k in ("gross", "fees", "net", "cost", "profit"):
                a[k] = round(a[k], 2)
            a["margin_pct"] = round(a["profit"] / a["gross"] * 100, 1) if a["gross"] else None
        return acc

    def _by_denomination(self, rows) -> list[dict]:
        acc: dict[int, dict] = {}
        for r in rows:
            f = self.order_flow(r)
            a = acc.setdefault(int(r["stars_per_unit"] or r["stars"] or 0),
                               {"stars": int(r["stars_per_unit"] or r["stars"] or 0), "orders": 0, "gross": 0.0,
                                "cost": 0.0, "profit": 0.0, "ton": 0.0})
            a["orders"] += 1
            a["gross"] += f.price
            a["cost"] += f.cost_rub or 0
            a["profit"] += f.profit or 0
            a["ton"] += f.ton_total
        out = []
        for a in sorted(acc.values(), key=lambda x: x["stars"]):
            a = {k: round(v, 4 if k == "ton" else 2) if isinstance(v, float) else v for k, v in a.items()}
            a["margin_pct"] = round(a["profit"] / a["gross"] * 100, 1) if a["gross"] else None
            out.append(a)
        return out

    def add_cashflow(self, kind: str, rub: float, ton: float, note: str = "") -> None:
        assert kind in ("topup", "withdraw_funpay", "withdraw_ozon", "withdraw_ggsel", "withdraw_ton", "expense", "adjust")
        self.db.exec("INSERT INTO cashflow(ts,kind,rub,ton,note) VALUES(?,?,?,?,?)",
                     (time.time(), kind, float(rub or 0), float(ton or 0), note[:300]))
        self.db.emit({"type": "cashflow", "kind": kind})
