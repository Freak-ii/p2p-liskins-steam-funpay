import time

from starsbot.config import load_config
from starsbot.core.models import CompetitorLot
from starsbot.core.risk import RiskEngine
from starsbot.core.rtba import RTBAEngine, filter_competitors, price_floor
from starsbot.crm.db import Database
from starsbot.economics import Economics
from starsbot.rates import StaticRate


def cfg(**over):
    return load_config(None, {"rtba": {"enabled": True, "min_margin_pct": 5, "undercut_abs": 0.5, "min_change": 0.3,
                                       "competitor_min_amount": 2, **over.pop("rtba", {})}, **over})


def lot(i, seller, price, stars=100, method="по username", amount=10):
    return CompetitorLot(offer_id=str(i), seller=seller, price=price, amount=amount,
                         description=f"{stars} звёзд, {method}", fields={"quantity": f"{stars} звёзд", "method": method})


MARKET = [lot(1, "me", 150), lot(2, "A", 140), lot(3, "B", 142), lot(4, "Tiny", 90, amount=1), lot(5, "C", 145),
          lot(6, "Gift", 120, method="подарком"), lot(7, "Big", 300, stars=250), lot(8, "D", 149)]


def test_filter_by_denomination_and_method():
    comps = filter_competitors(MARKET, cfg().rtba, 100, {"1"}, "me")
    assert [l.seller for l in comps] == ["A", "B", "C", "D"]


def test_top1_undercut_and_floor():
    c = cfg()
    eng = RTBAEngine(c.rtba)
    d = eng.decide(MARKET, offer_id="1", stars=100, own_offer_ids={"1"}, own_username="me", current_lot_price=150,
                   cost_per_star_rub=1.2, fee_pct=3)
    assert d.apply and d.target_lot_price == 139.5 and d.best_competitor == 140
    # себестоимость выше рынка → стоим на полу (маржа + комиссии), в минус не продаём
    d = eng.decide(MARKET, offer_id="1", stars=100, own_offer_ids={"1"}, own_username="me", current_lot_price=150,
                   cost_per_star_rub=1.4, fee_pct=3)
    floor = price_floor(c.rtba, 100, 1.4, 3)
    assert d.target_lot_price == floor and floor > 140


def test_rate_limit_and_hysteresis():
    eng = RTBAEngine(cfg(rtba={"max_updates_per_hour": 1}).rtba)
    kw = dict(offer_id="1", stars=100, own_offer_ids={"1"}, own_username="me", cost_per_star_rub=1.0)
    d = eng.decide(MARKET, current_lot_price=139.6, **kw)
    assert not d.apply                                       # изменение меньше min_change
    d = eng.decide(MARKET, current_lot_price=150, **kw)
    assert d.apply
    eng.mark_applied("1")
    assert not eng.decide(MARKET, current_lot_price=150, **kw).apply


def test_risk_scores():
    c = load_config(None, {"risk": {"large_order_stars": 1000, "blacklist_usernames": ["scammer_x"]}})
    db = Database()
    r = RiskEngine(c.risk, db)
    small = {"order_id": "A", "buyer_id": 1, "buyer_username": "u", "stars": 100, "price": 150}
    assert r.assess(small, tg_username="good_user").score == 0
    big = {**small, "order_id": "B", "stars": 2000}
    assert r.assess(big, tg_username="good_user").score > 0
    assert r.assess(small, tg_username="scammer_x").decision == "block"


def test_economics_full_cycle():
    c = load_config(None, {"economics": {"funpay_fee_pct": 0, "withdraw_fee_pct": 3, "exchange_spread_pct": 2,
                                         "gas_per_purchase_ton": 0.006},
                           "funpay": {"lots": [{"stars": 50, "offer_id": 1}, {"stars": 100, "offer_id": 2}]},
                           "ton": {"fee_reserve_ton": 0.1}})
    db = Database()

    class Eng:
        def cost_per_star_ton(self):
            return 0.0115
    econ = Economics(c, db, StaticRate(100.0), Eng())
    assert econ.effective_rate() == (102.0, "биржевой курс + спред")
    f = econ.flow(100, 150)
    assert f.withdraw_fee == 4.5 and f.net_rub == 145.5
    assert f.ton_total == 1.156 and f.cost_rub == round(1.156 * 102, 2)
    assert f.profit == round(145.5 - 1.156 * 102, 2) and f.breakeven_price < 150
    # реальный курс — по журналу пополнений
    econ.add_cashflow("topup", 11000, 100)
    assert econ.effective_rate() == (110.0, "ваши пополнения")
    # ёмкость кошелька: (баланс − резерв) / (TON за заказ)
    db.kv_set("wallet", {"balance": 10.1})
    cap = {x["stars"]: x["orders"] for x in econ.wallet_capacity()}
    assert cap[50] == int(10.0 / (50 * 0.0115 + 0.006)) and cap[100] == int(10.0 / 1.156)
    # сверка от точки отсчёта: пополнения до неё уже в стартовом балансе
    db.kv_set("wallet_start_balance", {"ton": 10.1, "ts": time.time() + 1})
    assert not econ.reconcile()["alarm"]
    db.kv_set("wallet_start_balance", {"ton": 10.1, "ts": time.time() - 60})
    rec = econ.reconcile()                      # 10.1 + 100 TON пополнения, а в сети 10.1 → тревога
    assert rec["expected"] == 110.1 and rec["diff"] == -100 and rec["alarm"]
