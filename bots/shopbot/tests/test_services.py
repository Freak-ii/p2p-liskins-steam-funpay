import asyncio
import json

from shopbot.config import Config
from shopbot.db import Database
from shopbot.engine import Shop
from shopbot.tg import ShopTelegram

ADMIN, U = 900, 2001


def make(tmp_path, **over):
    over.setdefault("telegram", {"admin_chat_ids": [ADMIN]})
    cfg = Config(tmp_path, demo=True, overrides=over)
    shop = Shop(cfg, Database(":memory:"), demo=True)
    bot = ShopTelegram(shop)
    shop.bot = bot
    return shop, bot


def texts(bot, uid):
    return [m["text"] for m in bot.outbox if m["chat"] == uid]


def buttons(bot, uid):
    out = []
    for m in bot.outbox:
        if m["chat"] == uid and m["buttons"]:
            out += [b.get("callback_data") for row in m["buttons"]["inline_keyboard"] for b in row]
    return out


async def flow(shop, bot, steps, inputs=(), method="pay:balance", money=10000):
    db = shop.db
    db.touch_user(U, "buyer", "B")
    db.credit(U, "rub", money, "topup", "", "тест")
    await shop.rtba.tick()
    for st in steps:
        await bot.on_callback(U, st)
    for t in inputs:
        await bot.on_text(U, "buyer", "B", t)
    assert method in buttons(bot, U), buttons(bot, U)[-6:]
    await bot.on_callback(U, method)
    oid = db.scalar("SELECT id FROM orders WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (U,))
    for _ in range(3):
        await shop.poll_deliveries()
    return db.order(oid)


def test_vpn_from_own_panel_with_consent(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:vpn", "sx:vpn:3", "sa:vpn:3"]))
    assert o["status"] == "delivered" and o["price_rub"] == 449
    out = "\n".join(texts(bot, U))
    assert "vless://" in out and "Happ" in out
    ev = {e["kind"]: json.loads(e["data"]) for e in shop.db.events(o["id"])}
    assert ev["terms_accepted"]["shown"] and ev["terms_accepted"]["notice_hash"]
    assert ev["service_payload"]["source"] == "panel"
    # себестоимость своего сервера: 450 ₽ / 60 ключей × 3 мес
    assert abs(o["cost_rub"] - 22.5) < 0.01


def test_appstore_from_stock_fifo_cost(tmp_path):
    shop, bot = make(tmp_path)
    shop.services.stock.add("appstore_us:20", ["AAAA-1111", "BBBB-2222"], 1780, "Bitrefill USDT")
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:appstore_us", "sx:appstore_us:20", "sa:appstore_us:20"]))
    assert o["status"] == "delivered"
    assert "AAAA-1111" in "\n".join(texts(bot, U))
    assert o["cost_rub"] == 1780 and o["margin_rub"] > 100
    assert shop.services.stock.count("appstore_us:20") == 1


def test_claude_resale_operator_delivers_code(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:claude_pro", "sx:claude_pro:1", "sa:claude_pro:1"]))
    assert o["status"] == "manual"
    adm = "\n".join(texts(bot, ADMIN))
    assert "ggsel.net/catalog/itunes-us" in adm and f"adm:svc:{o['id']}" in buttons(bot, ADMIN)
    # без кода выдать нельзя
    assert "пришлите код" in asyncio.run(shop.admin_service_deliver(o["id"], "готово"))
    asyncio.run(bot.on_callback(ADMIN, f"adm:svc:{o['id']}"))
    asyncio.run(bot.on_text(ADMIN, "", "", "XYZ-CLAUDE-20"))
    o = shop.db.order(o["id"])
    assert o["status"] == "delivered"
    out = "\n".join(texts(bot, U))
    assert "XYZ-CLAUDE-20" in out and "Upgrade to Pro" in out
    # «Мои заказы» → код можно показать снова
    asyncio.run(bot.on_callback(U, f"code:{o['id']}"))
    assert texts(bot, U)[-1].count("XYZ-CLAUDE-20") == 1


def test_spotify_login_password_encrypted_and_wiped(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:spotify", "sx:spotify:3", "sa:spotify:3"],
                         inputs=["buyer@mail.com", "S3cret-pass"]))
    assert o["status"] == "manual" and o["recipient"] == "buyer@mail.com"
    raw = shop.db.scalar("SELECT data FROM order_secrets WHERE order_id=?", (o["id"],))
    assert raw and "S3cret-pass" not in raw                              # зашифровано
    sess = shop.db.q("SELECT data FROM sessions")
    assert all("S3cret-pass" not in (r["data"] or "") for r in sess)    # и в сессии не лежит открытым
    assert "S3cret-pass" in "\n".join(texts(bot, ADMIN))                # оператор видит (под спойлером)
    assert o["cost_rub"] == 846 and o["price_rub"] == 973                # 846 × 1.15
    asyncio.run(shop.admin_service_deliver(o["id"], "готово"))
    assert shop.db.order(o["id"])["status"] == "delivered"
    assert shop.db.scalar("SELECT COUNT(*) FROM order_secrets") == 0


def test_steam_topup_amount_and_login(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:steam_topup", "sx:steam_topup:rub", "sa:steam_topup:rub"],
                         inputs=["50", "1000", "gaben_login"]))
    assert "Сумма от 100" in "\n".join(texts(bot, U))
    assert o["amount"] == 1000 and o["recipient"] == "gaben_login"
    assert o["price_rub"] == 1100 and o["cost_rub"] == 1070


def test_operator_refund_and_rtba_closes_overpriced(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(flow(shop, bot, ["buy:service", "sv:claude_pro", "sx:claude_pro:1", "sa:claude_pro:1"]))
    asyncio.run(bot.on_callback(ADMIN, f"adm:svcfail:{o['id']}"))
    assert shop.db.order(o["id"])["status"] == "refunded"
    assert shop.db.user(U)["balance_rub"] == 10000
    # ChatGPT: перепродажа карты дороже рынка → закрыт; карта со склада (куплена за USDT) — снова открыт
    pt = {p["key"]: p for p in shop.rtba.points()}
    assert pt["svc.vpn"]["state"] == "open"
    shop.cfg.data["catalog"]["services"]["max_over_market_pct"] = 0.5
    gpt = next(i for i in shop.cfg.data["catalog"]["services"]["items"] if i["id"] == "chatgpt_plus")
    gpt["offers"][0]["rub_per_usd"] = 115           # карты подорожали: 2300 ₽ + 3% > рынка 2222 ₽
    asyncio.run(shop.rtba.tick())
    assert not shop.rtba.is_open("svc.chatgpt_plus")
    assert "дороже рынка" in shop.rtba.point("svc.chatgpt_plus")["reason"]
    shop.services.stock.add("appstore_us:20", ["C1"], 1760)
    asyncio.run(shop.rtba.tick())
    assert shop.rtba.is_open("svc.chatgpt_plus")


def test_economics_table(tmp_path):
    shop, _ = make(tmp_path)
    rows = {r["key"]: r for r in shop.services.economics()}
    assert rows["vpn:12"]["source"] == "panel" and rows["vpn:12"]["margin_pct"] > 1000
    assert rows["appstore_us:20"]["cost_rub"] == 2120 and rows["appstore_us:20"]["price_rub"] == 2187   # floor 3%
    shop.services.stock.add("appstore_us:20", ["S1"], 1771)
    r = {r["key"]: r for r in shop.services.economics()}["appstore_us:20"]
    assert r["source"] == "stock" and r["price_rub"] == 2078 and r["margin_pct"] > 17      # 2120 − 2%
    assert rows["spotify:12"]["price_rub"] == 2474
