"""Демо-сценарий магазина: клиенты проходят все ветки через того же бота, что и в бою (без Telegram и денег).

Покупка звёзд картой, подарок криптой (TON), Premium звёздами, ключи Steam с баланса, приглашённый друг со
скидкой и бонусом пригласившему, неверный @username → исправление, сбой поставщика → возврат на баланс,
выкуп звёзд за USDT (авто и крупная выплата на проверку), выкуп подарка за звёзды, закрытие пункта RTBA.
"""
from __future__ import annotations

import json

from shopbot.tg import ShopTelegram

ALICE, BOB, CAROL, DAVE, EVE = 1001, 1002, 1003, 1004, 1005
TRADE = "https://steamcommunity.com/tradeoffer/new/?partner=123456789&token=AbCd1234"


def last_button(bot: ShopTelegram, uid: int, prefix: str) -> str:
    for m in reversed(bot.outbox):
        if m["chat"] != uid or not m["buttons"]:
            continue
        for row in m["buttons"]["inline_keyboard"]:
            for b in row:
                if str(b.get("callback_data", "")).startswith(prefix):
                    return b["callback_data"]
    raise AssertionError(f"нет кнопки {prefix} у {uid}")


async def settle(shop, rounds: int = 4) -> None:
    for _ in range(rounds):
        await shop.poll_payments()
        await shop.poll_deliveries()


async def buy(bot, uid: int, start: list[str], method_prefix: str, *, pay: bool = True, texts: list[str] = ()) -> str:
    for step in start:
        await bot.on_callback(uid, step)
    for t in texts:
        await bot.on_text(uid, "", "", t)
    await bot.on_callback(uid, last_button(bot, uid, method_prefix))
    oid = bot.db.scalar("SELECT id FROM orders WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (uid,))
    if pay and bot.db.order(oid)["status"] == "awaiting_payment":
        await bot.on_callback(uid, last_button(bot, uid, "demo:pay:"))
    await settle(bot.shop)
    return oid


async def run_scenario(shop, bot: ShopTelegram) -> dict:
    db = shop.db
    await shop.rtba.tick()
    for uid, name in ((ALICE, "alice_tg"), (BOB, "bob_real"), (DAVE, "dave_d"), (EVE, "eve_e")):
        await bot.on_text(uid, name, name.split("_")[0].title(), "/start")
    alice = db.user(ALICE)
    await bot.on_text(CAROL, "carol_star", "Carol", f"/start ref_{alice['ref_code']}")   # друг Алисы

    res: dict = {}
    res["stars_card"] = await buy(bot, ALICE, ["buy:stars", "st:100", "to:me"], "pay:card")
    res["gift_crypto"] = await buy(bot, BOB, ["buy:gift", "gf:rose"], "pay:crypto:TON", texts=["@alice_tg"])
    res["premium_xtr"] = await buy(bot, CAROL, ["buy:premium", "pm:3", "to:me"], "pay:stars")   # скидка друга
    # неверный получатель → бот переспрашивает → выдача
    res["bad_username"] = await buy(bot, DAVE, ["buy:stars", "st:50"], "pay:card", texts=["@nobody_here"])
    await bot.on_text(DAVE, "", "", "@dave_d")
    await settle(shop)
    # сбой поставщика → возврат на баланс, потом покупка ключей Steam с этого баланса
    shop.suppliers["premium"].down = True
    await bot.on_callback(EVE, "buy:premium")
    await bot.on_callback(EVE, "pm:6")
    await bot.on_callback(EVE, "to:me")
    shop.suppliers["premium"].down = False
    eve_oid = await buy(bot, EVE, ["buy:premium", "pm:6", "to:me"], "pay:crypto:USDT", pay=False)
    shop.suppliers["premium"].down = True           # поставщик упал после создания заказа
    await bot.on_callback(EVE, last_button(bot, EVE, "demo:pay:"))
    for _ in range(6):
        await shop.deliver(eve_oid)                 # сбои подряд → RTBA закроет Premium, заказ — возврат
        await shop.poll_deliveries()
    res["refund"] = eve_oid
    await shop.rtba.tick()
    res["premium_closed"] = not shop.rtba.is_open("buy.premium")
    shop.suppliers["premium"].down = False
    db.credit(EVE, "rub", 600, "topup", "demo", "демо: пополнение баланса")
    res["keys_balance"] = await buy(bot, EVE, ["buy:steam_keys", "kq:3"], "pay:balance", texts=[TRADE])
    # выкуп звёзд за USDT (небольшой — автоматически)
    await bot.on_callback(BOB, "sell")
    await bot.on_callback(BOB, "sell:stars")
    await bot.on_text(BOB, "", "", "800")
    await bot.on_callback(BOB, "sellok")
    await bot.on_callback(BOB, last_button(bot, BOB, "demo:pay:"))
    await settle(shop)
    res["sell_small"] = db.scalar("SELECT id FROM orders WHERE user_id=? AND kind='sell' ORDER BY rowid DESC", (BOB,))
    # крупный выкуп — на проверку оператору
    await bot.on_callback(ALICE, "sell:stars")
    await bot.on_text(ALICE, "", "", "5000")
    await bot.on_callback(ALICE, "sellok")
    await bot.on_callback(ALICE, last_button(bot, ALICE, "demo:pay:"))
    await settle(shop)
    res["sell_review"] = db.scalar("SELECT id FROM orders WHERE user_id=? AND kind='sell' ORDER BY rowid DESC",
                                   (ALICE,))
    # выкуп подарка за звёзды: оператор подтверждает, что подарок пришёл
    await bot.on_callback(CAROL, "sell:gift")
    await bot.on_callback(CAROL, "sg:cup")
    await bot.on_callback(CAROL, "sp:stars")
    gift_oid = db.scalar("SELECT id FROM orders WHERE user_id=? AND kind='sell' ORDER BY rowid DESC", (CAROL,))
    await shop.confirm_gift_received(gift_oid, who="demo")
    res["sell_gift"] = gift_oid
    res["summary"] = shop.summary()
    res["outbox"] = len(bot.outbox)
    return res


def describe(res: dict) -> str:
    return json.dumps({k: v for k, v in res.items() if k != "summary"}, ensure_ascii=False, indent=1)
