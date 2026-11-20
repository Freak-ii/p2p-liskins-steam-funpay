import asyncio
import json

from shopbot.config import Config
from shopbot.db import Database
from shopbot.demo import ALICE, BOB, CAROL, DAVE, EVE, run_scenario
from shopbot.engine import Shop
from shopbot.channel import Channel
from shopbot.tg import ShopTelegram


def make(tmp_path, **over):
    cfg = Config(tmp_path, demo=True, overrides=over)
    db = Database(":memory:")
    shop = Shop(cfg, db, demo=True)
    bot = ShopTelegram(shop)
    Channel(shop)
    return shop, bot


def test_demo_scenario_all_branches(tmp_path):
    shop, bot = make(tmp_path)
    res = asyncio.run(run_scenario(shop, bot))
    db = shop.db
    st = {k: db.order(v)["status"] for k, v in res.items() if isinstance(v, str) and db.order(v)}
    assert st["stars_card"] == "delivered"
    assert st["gift_crypto"] == "delivered"
    assert st["premium_xtr"] == "delivered"
    assert st["bad_username"] == "delivered" and db.order(res["bad_username"])["recipient"] == "dave_d"
    assert st["refund"] == "refunded" and res["premium_closed"]
    assert st["keys_balance"] == "delivered"
    assert st["sell_small"] == "paid_out"
    assert st["sell_review"] == "payout_pending"
    assert st["sell_gift"] == "paid_out"
    # прозрачность: разбивка есть у каждого заказа, комиссия карты отдельной строкой
    card = db.order(res["stars_card"])
    lines = dict(json.loads(card["breakdown"]))
    assert any(k.startswith("Комиссия эквайринга 3.5%") for k in lines)
    assert abs(card["total_rub"] - (card["price_rub"] + card["fee_rub"])) < 0.011
    # крипта: комиссия не меньше 2%
    gift = db.order(res["gift_crypto"])
    assert gift["pay_currency"] == "TON" and gift["fee_rub"] >= round(gift["price_rub"] * 0.02, 2) - 0.01
    # рефералка: Кэрол получила скидку, Алиса — бонус, не больше 40% маржи
    prem = db.order(res["premium_xtr"])
    assert prem["discount_rub"] > 0 and prem["referrer_id"] == ALICE
    assert 0 < prem["ref_reward_rub"] <= prem["margin_rub"] * 0.4 + 0.01
    assert db.user(ALICE)["bonus_rub"] == prem["ref_reward_rub"]
    # выкуп: 800 ⭐ × 0.0115 $ − 3%
    small = db.order(res["sell_small"])
    assert small["payout_amount"] == 8.92 and shop.providers["crypto"].transfers[0]["user_id"] == BOB
    # подарок 🏆 (85 ⭐ продажи) − 3% → 82 ⭐ на звёздный баланс
    assert db.user(CAROL)["stars_credit"] == 82
    # возврат: деньги Евы пришли на баланс и потрачены на ключи
    assert db.scalar("SELECT COUNT(*) FROM ledger WHERE user_id=? AND kind='refund'", (EVE,)) == 1
    # баланс никогда не уходит в минус, журнал сходится с балансами
    for uid in (ALICE, BOB, CAROL, DAVE, EVE):
        u = db.user(uid)
        for acc, col in (("rub", "balance_rub"), ("bonus", "bonus_rub"), ("stars", "stars_credit")):
            s = db.scalar("SELECT SUM(delta) FROM ledger WHERE user_id=? AND account=?", (uid, acc), 0) or 0
            assert abs(float(s) - float(u[col])) < 0.001 and u[col] >= 0
    assert res["summary"]["orders"] == 5   # звёзды, подарок, Premium, исправленный @, ключи


def test_admin_payout_and_limits(tmp_path):
    shop, bot = make(tmp_path)
    res = asyncio.run(run_scenario(shop, bot))
    oid = res["sell_review"]

    async def go():
        assert await shop.payout(oid, approved_by="test") == "выплачено"
        assert shop.db.order(oid)["status"] == "paid_out"
        # повтор не платит второй раз
        assert await shop.payout(oid, approved_by="test") == "заявка не ждёт выплаты"
        # лимит нового клиента
        await bot.on_text(2001, "newbie", "New", "/start")
        await bot.on_callback(2001, "buy:stars")
        await bot.on_text(2001, "", "", "5000")
        await bot.on_callback(2001, "to:me")
        await bot.on_callback(2001, "pay:card")
        assert "суточный лимит" in bot.outbox[-1]["text"]
        # RTBA off закрывает раздел в боте
        shop.rtba.set_mode("buy.gift", "off")
        await shop.rtba.tick()
        await bot.on_callback(2001, "buy:gift")
        assert "недоступен" in bot.outbox[-1]["text"]
    asyncio.run(go())


def test_channel_posts_are_drafts(tmp_path):
    shop, bot = make(tmp_path)

    async def go():
        await shop.rtba.tick()
        posts = await shop.channel.build()
        assert {p["id"] for p in posts} == {"showcase", "prices", "sell", "referral"}
        prices = next(p for p in posts if p["id"] == "prices")
        assert "⭐ 100" in prices["text"] and "Premium 3" in prices["text"]
        assert "не подключён" in await shop.channel.publish("prices")
    asyncio.run(go())


def test_pricing_undercuts_funpay_but_keeps_min_margin(tmp_path):
    from shopbot.pricing import shop_price
    cfg = Config(tmp_path, demo=True)
    price, why = shop_price(cfg, "stars", 125.6, 100)        # себестоимость 1.256 ₽/⭐
    assert price <= 155 * 0.95 + 0.01 and price >= 125.6 * 1.04
    price2, why2 = shop_price(cfg, "stars", 150, 100)       # дорогой TON — держим минимальную маржу
    assert price2 >= 150 * 1.04 and "минимальная" in why2
