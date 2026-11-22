import time

from mannbot.config import load_config
from mannbot.core.models import CompetitorLot
from mannbot.core.risk import RiskEngine
from mannbot.core.rtba import RTBAEngine, filter_competitors, price_floor
from mannbot.crm.db import Database


def cfg(**rtba):
    return load_config(None, {"rtba": {"enabled": True, "cost_per_key": 150, "min_margin_pct": 5,
                                       "ceiling_price": 260, "undercut_abs": 0.5, "min_change": 0.3,
                                       "competitor_min_amount": 2, "scarcity_premium_pct": 0, **rtba}})


def lot(i, seller, price, amount=10):
    return CompetitorLot(offer_id=str(i), seller=seller, price=price, amount=amount)


MARKET = [lot(1, "me", 200), lot(2, "A", 190), lot(3, "B", 195), lot(4, "Tiny", 100, 1), lot(5, "C", 199),
          lot(6, "D", 205), lot(7, "E", 210)]


def test_filter_excludes_self_and_small():
    c = cfg()
    comps = filter_competitors(MARKET, c.rtba, {"1"}, "me")
    assert [l.seller for l in comps] == ["A", "B", "C", "D", "E"]


def test_top1_undercut():
    e = RTBAEngine(cfg().rtba)
    d = e.decide(MARKET, own_offer_ids={"1"}, own_username="me", current_lot_price=200, stock=50)
    assert d.target_lot_price == 189.5 and d.apply and d.position == 1


def test_floor_and_ceiling():
    assert price_floor(cfg().rtba) == 157.5
    e = RTBAEngine(cfg().rtba)
    cheap = [lot(2, "A", 120), lot(3, "B", 121)]
    d = e.decide(cheap, own_offer_ids=set(), own_username="me", current_lot_price=170, stock=50)
    assert d.target_lot_price == 157.5
    d2 = RTBAEngine(cfg().rtba).decide([lot(2, "A", 400)], own_offer_ids=set(), own_username="me",
                                       current_lot_price=200, stock=50)
    assert d2.target_lot_price == 260


def test_hysteresis_and_rate_limit():
    e = RTBAEngine(cfg(max_updates_per_hour=1).rtba)
    d = e.decide(MARKET, own_offer_ids={"1"}, own_username="me", current_lot_price=189.6, stock=50)
    assert not d.apply                                   # изменение 0.1 < min_change
    e.mark_applied()
    d = e.decide(MARKET, own_offer_ids={"1"}, own_username="me", current_lot_price=180, stock=50)
    assert not d.apply                                   # лимит в час


def test_topn_and_markup_calibration():
    e = RTBAEngine(cfg(strategy="topN", target_position=2).rtba)
    e.calibrate([lot(1, "me", 210)], {"1"}, 200)
    assert e.markup == 1.05
    d = e.decide(MARKET, own_offer_ids={"1"}, own_username="me", current_lot_price=None, stock=50)
    assert d.target_buyer_price == round((195 - 0.5), 2) or abs(d.target_lot_price * 1.05 - 194.5) < 0.02


def test_risk_scores():
    c = load_config(None, {"risk": {"large_order_keys": 20, "manual_review_score": 40, "block_score": 90,
                                    "blacklist_steam_ids": ["765"]}})
    db = Database(":memory:")
    db.touch_customer(5, "Buyer", new_order_total=100, keys=25)
    db.create_order(order_id="A1", buyer_id=5, buyer_username="Buyer", keys_qty=25, status="awaiting_link")
    r = RiskEngine(c.risk, db).assess(db.get_order("A1"))
    assert r.decision == "review" and r.score == 40
    assert RiskEngine(c.risk, db).assess(db.get_order("A1"), steam_id="765").decision == "block"
    # один Steam-аккаунт у разных покупателей FunPay
    db.touch_customer(6, "Other")
    db.customer_add_steam_id(6, "999")
    db.create_order(order_id="B1", buyer_id=5, buyer_username="Buyer", keys_qty=1, status="awaiting_link",
                    created_at=time.time())
    r2 = RiskEngine(c.risk, db).assess(db.get_order("B1"), steam_id="999")
    assert any("другие покупатели" in x for x in r2.reasons)
