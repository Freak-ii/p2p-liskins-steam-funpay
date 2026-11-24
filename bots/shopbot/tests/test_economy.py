import asyncio
import time

from shopbot import economy as E
from shopbot.config import Config
from shopbot.db import Database
from shopbot.engine import Shop
from shopbot.tg import ShopTelegram


def make(tmp_path, **over):
    cfg = Config(tmp_path, demo=True, overrides=over)
    db = Database(":memory:")
    shop = Shop(cfg, db, demo=True)
    bot = ShopTelegram(shop)
    return shop, bot


def delivered(db, uid, total, days_ago=1, recipient="x"):
    oid = db.new_order_id("buy")
    db.create_order(oid, user_id=uid, kind="buy", product="stars", amount=100, status="delivered", total_rub=total,
                    price_rub=total, cost_rub=total * 0.85, recipient=recipient,
                    created_at=time.time() - days_ago * 86400)
    return oid


def test_tiers_by_turnover_and_window(tmp_path):
    shop, _ = make(tmp_path)
    db = shop.db
    u, _ = db.touch_user(1, "whale")
    assert E.tier_for(shop.cfg, db, u).id == "base"
    delivered(db, 1, 20000)
    t = E.tier_for(shop.cfg, db, db.user(1))
    assert t.id == "silver" and t.next_title == "Золото"
    delivered(db, 1, 200000, days_ago=40)            # вне окна 30 дней — не считается
    assert E.tier_for(shop.cfg, db, db.user(1)).id == "silver"
    db.exec("UPDATE users SET tags='tier:whale' WHERE id=1")   # уровень вручную после переговоров
    assert E.tier_for(shop.cfg, db, db.user(1)).id == "whale"


def test_discounts_never_break_margin_floor(tmp_path):
    shop, _ = make(tmp_path)
    cfg = shop.cfg
    t = E.Tier("whale", "Кит", 0, discount_pct=3)
    # маржа 4%: скидка 3% не должна опустить её ниже 2%
    d = E.loyalty_discount(cfg, t, 104.0, 100.0, "stars", 0.0)
    assert d == 2.0
    # реферальная скидка уже съела запас — уровень ничего не добавляет
    assert E.loyalty_discount(cfg, t, 104.0, 100.0, "stars", 2.0) == 0.0


def test_buyback_rate_beats_telegram_withdrawal_and_respects_cap(tmp_path):
    shop, _ = make(tmp_path)
    rate, fee, _ = E.buyback_usd_per_star(shop.cfg)
    assert fee == 0 and abs(rate - 0.013 * 1.01) < 1e-6
    rate_w, _, _ = E.buyback_usd_per_star(shop.cfg, E.Tier("whale", "Кит", 0, buyback_bonus_pct=10))
    assert rate_w <= 0.015 * 0.94 + 1e-9              # бонус кита упирается в спред
    full, _, why = E.buyback_usd_per_star(shop.cfg, None, xtr_stock=10 ** 6)
    assert full < 0.013 and "запас" in why


def test_new_seller_goes_to_review_and_self_referral_denied(tmp_path):
    shop, _ = make(tmp_path)
    db = shop.db
    u, _ = db.touch_user(7, "newbie")
    r = E.sell_risk(shop.cfg, db, u, 16.0)
    assert r.ok and r.review and "новый продавец" in r.reason
    assert not E.sell_risk(shop.cfg, db, u, 5.0).review
    ref, _ = db.touch_user(8, "inviter")
    delivered(db, 8, 100, recipient="@friend")
    oid = delivered(db, 7, 100, recipient="@friend")
    assert E.self_referral(db, db.order(oid), ref)
    oid2 = delivered(db, 7, 100, recipient="@inviter")
    assert E.self_referral(db, db.order(oid2), ref)


def test_start_payload_sources_and_report(tmp_path):
    shop, bot = make(tmp_path)
    db = shop.db
    assert E.parse_start("src_ivan_yt__stars") == ("", "ivan_yt", "stars")
    assert E.parse_start("ref_abc__src_tt") == ("abc", "tt", "")
    assert E.parse_start("src_BAD-LABEL")[1] == ""
    asyncio.run(bot.on_text(11, "a", "A", "/start src_ivan_yt"))
    asyncio.run(bot.on_text(11, "a", "A", "/start src_other"))      # первая метка побеждает
    assert db.user(11)["source"] == "ivan_yt"
    oid = delivered(db, 11, 500)
    db.exec("UPDATE orders SET source='ivan_yt' WHERE id=?", (oid,))
    rep = E.sources_report(db, 30)
    assert rep[0]["source"] == "ivan_yt" and rep[0]["buyers"] == 1 and rep[0]["revenue_rub"] == 500


def test_reviews_bonus_once_and_low_rating_alert(tmp_path):
    shop, _ = make(tmp_path)
    db = shop.db
    db.touch_user(5, "buyer")
    oid = delivered(db, 5, 400)
    msg = asyncio.run(shop.add_review(5, oid, 5))
    assert "Спасибо" in msg and db.user(5)["bonus_rub"] == 5
    asyncio.run(shop.add_review(5, oid, 2, "долго"))                  # правка — без второго бонуса
    assert db.user(5)["bonus_rub"] == 5
    assert db.scalar("SELECT COUNT(*) FROM alerts WHERE code='review_low'") == 1
    assert "своему" in asyncio.run(shop.add_review(6, oid, 5))
    assert E.review_stats(db) == {"count": 1, "avg": 2.0}


def test_bonus_covers_only_share_of_order(tmp_path):
    shop, _ = make(tmp_path)
    u, _ = shop.db.touch_user(9, "b")
    shop.db.credit(9, "bonus", 1000, "test")
    assert E.bonus_usable(shop.cfg, shop.db.user(9), 100) == 30.0
