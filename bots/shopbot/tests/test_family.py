"""Свои семьи Яндекс Плюс: сортировка аккаунтов, выдача приглашений, сроки, продление, переселение."""
import asyncio
import json
import time

from aiohttp.test_utils import TestClient, TestServer

from shopbot.__main__ import App
from shopbot.web.server import make_app
from tests.test_services import ADMIN, U, buttons, make, texts

DAY = 86400


def acc(shop, label, months_paid, cost=2600, **kw):
    return shop.services.family.add_account(label, login=f"{label}@yandex.ru", paid_until=time.time() + months_paid * 30 * DAY,
                                            cost_rub=cost, period_months=12, **kw)


async def buy(shop, bot, vid="1", login="buyer@ya.ru", uid=U, money=10000):
    db = shop.db
    db.touch_user(uid, "buyer", "B")
    db.credit(uid, "rub", money, "topup", "", "тест")
    await shop.rtba.tick()
    for st in ["buy:service", "sv:yandex_plus", f"sx:yandex_plus:{vid}", f"sa:yandex_plus:{vid}"]:
        await bot.on_callback(uid, st)
    await bot.on_text(uid, "buyer", "B", login)
    assert "pay:balance" in buttons(bot, uid), texts(bot, uid)[-2:]
    await bot.on_callback(uid, "pay:balance")
    oid = db.scalar("SELECT id FROM orders WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (uid,))
    for _ in range(2):
        await shop.poll_deliveries()
    return db.order(oid)


def test_sorting_where_can_add(tmp_path):
    shop, _ = make(tmp_path)
    fam = shop.services.family
    short = acc(shop, "short", 2)
    long_ = acc(shop, "long", 12)
    paused = acc(shop, "paused", 12)
    fam.update_account(paused, status="paused")
    risky = acc(shop, "risky", 12)
    fam.update_account(risky, risk=2)
    # 1 мес: годится короткий (самый короткий достаточный срок), длинный бережём
    assert fam.pick("yandex_plus", 1)["id"] == short
    # 3 мес: короткому не хватает срока (2 мес < 3 + запас) — длинный
    assert fam.pick("yandex_plus", 3)["id"] == long_
    why = fam.reasons(fam.account(short), 3)
    assert any("мало для 3" in w for w in why)
    assert fam.reasons(fam.account(paused), 1) == ["пауза"]
    assert fam.reasons(fam.account(risky), 1) == ["риск 2"]
    # места кончились — аккаунт пропускается
    for i in range(3):
        assert fam.reserve("yandex_plus", f"O{i}", U, "m", 1)["account"] == short
    assert "мест нет" in fam.reasons(fam.account(short), 1)
    assert fam.pick("yandex_plus", 1)["id"] == long_


def test_add_limit_and_cooldown(tmp_path):
    shop, _ = make(tmp_path, services={"family": {"max_adds_30d": 2, "seat_cooldown_days": 1}})
    fam = shop.services.family
    a = acc(shop, "a", 12)
    fam.reserve("yandex_plus", "O1", U, "m1", 1)
    s1 = fam.seat("O1")
    assert fam.removed(s1["id"])
    assert fam.seats_used(a) == 1            # исключённое место «остывает» сутки
    fam.reserve("yandex_plus", "O2", U, "m2", 1)
    assert "лимит добавлений за 30 дней" in fam.reasons(fam.account(a), 1)


def test_buy_with_invite_pool_is_automatic_and_joined(tmp_path):
    shop, bot = make(tmp_path)
    a = acc(shop, "fam1", 12)
    shop.services.family.add_invites(a, ["https://yandex.ru/family/invite/AAA", "not-a-link"])
    o = asyncio.run(buy(shop, bot, "3"))
    assert o["status"] == "delivered", o["fail_reason"]
    out = "\n".join(texts(bot, U))
    assert "https://yandex.ru/family/invite/AAA" in out and "14 дней" in out
    assert f"fj:{o['id']}" in buttons(bot, U)
    ev = {e["kind"]: json.loads(e["data"]) for e in shop.db.events(o["id"])}
    assert ev["terms_accepted"]["shown"] and ev["service_payload"]["source"] == "family"
    # себестоимость места: 2600 ₽ / 12 мес / 3 места × 3 мес × 1.15 (простой)
    assert abs(o["cost_rub"] - 249.17) < 0.01
    assert o["price_rub"] >= o["cost_rub"] * 1.08
    s = shop.services.family.seat(o["id"])
    assert s["status"] == "invited" and s["member"] == "buyer@ya.ru"
    asyncio.run(bot.on_callback(U, f"fj:{o['id']}"))
    assert shop.services.family.seat(o["id"])["status"] == "active"
    assert "Готово" in texts(bot, U)[-1]


def test_no_pool_operator_sends_link(tmp_path):
    shop, bot = make(tmp_path)
    acc(shop, "fam1", 12)
    o = asyncio.run(buy(shop, bot, "1"))
    assert o["status"] == "manual"
    task = texts(bot, ADMIN)[-1]
    assert "fam1" in task and "Поделиться ссылкой" in task
    assert asyncio.run(shop.admin_service_deliver(o["id"], "готово")).startswith("пришлите ссылку")
    assert asyncio.run(shop.admin_service_deliver(o["id"], "https://yandex.ru/inv/X цена 80")) == "выдано"
    assert shop.db.order(o["id"])["status"] == "delivered"
    assert shop.services.family.seat(o["id"])["invite"] == "https://yandex.ru/inv/X"
    # «не получается» → оператор шлёт новую ссылку
    asyncio.run(bot.on_callback(U, f"fp:{o['id']}"))
    assert f"adm:famlink:{o['id']}" in buttons(bot, ADMIN)
    assert asyncio.run(shop.admin_family_link(o["id"], "https://yandex.ru/inv/Y")) == "ссылка отправлена"
    assert "inv/Y" in texts(bot, U)[-1]


def test_no_seats_falls_back_to_resale_bot(tmp_path):
    shop, bot = make(tmp_path)
    o = asyncio.run(buy(shop, bot, "2"))
    assert o["status"] == "manual"
    job = shop.db.one("SELECT * FROM service_jobs WHERE order_id=?", (o["id"],))
    assert job["source"] == "resale" and "TavernaPokemona_Bot" in job["detail"]


def test_expiry_reminder_removal_and_renewal(tmp_path):
    shop, bot = make(tmp_path)
    fam = shop.services.family
    a = acc(shop, "fam1", 12)
    fam.add_invites(a, ["https://yandex.ru/inv/1"])
    o = asyncio.run(buy(shop, bot, "1"))
    asyncio.run(bot.on_callback(U, f"fj:{o['id']}"))
    s = fam.seat(o["id"])
    fam.mark(s["id"], until=time.time() + 2 * DAY)
    asyncio.run(shop.poll_deliveries())
    assert f"fr:{o['id']}" in buttons(bot, U)
    # продление: то же место, срок +1 мес
    asyncio.run(bot.on_callback(U, f"fr:{o['id']}"))
    asyncio.run(bot.on_callback(U, f"fr2:{o['id']}:1"))
    asyncio.run(bot.on_callback(U, "pay:balance"))
    asyncio.run(shop.poll_deliveries())
    s2 = fam.db.one("SELECT * FROM family_seats WHERE id=?", (s["id"],))
    assert s2["until"] > time.time() + 30 * DAY and s2["status"] == "active"
    assert "продлена" in "\n".join(texts(bot, U))
    # срок вышел → задача исключить → место свободно
    fam.mark(s["id"], until=time.time() - 1)
    asyncio.run(shop.poll_deliveries())
    assert fam.db.one("SELECT status FROM family_seats WHERE id=?", (s["id"],))["status"] == "leaving"
    assert f"adm:famrm:{s['id']}" in buttons(bot, ADMIN)
    asyncio.run(bot.on_callback(ADMIN, f"adm:famrm:{s['id']}"))
    assert fam.db.one("SELECT status FROM family_seats WHERE id=?", (s["id"],))["status"] == "free"


def test_ban_migrates_members(tmp_path):
    shop, bot = make(tmp_path)
    fam = shop.services.family
    a = acc(shop, "fam1", 6)
    b = acc(shop, "fam2", 12)
    fam.add_invites(a, ["https://yandex.ru/inv/a"])
    o = asyncio.run(buy(shop, bot, "1"))
    assert fam.seat(o["id"])["account_id"] == a
    fam.add_invites(b, ["https://yandex.ru/inv/b"])
    res = asyncio.run(shop.admin_family_ban(a))
    assert res.startswith("переселено 1")
    s = fam.seat(o["id"])
    assert s["account_id"] == b and s["invite"] == "https://yandex.ru/inv/b"
    assert "inv/b" in texts(bot, U)[-1]
    assert fam.account(a)["status"] == "banned"


def test_refund_before_join_frees_seat(tmp_path):
    shop, bot = make(tmp_path)
    fam = shop.services.family
    a = acc(shop, "fam1", 12)
    fam.add_invites(a, ["https://yandex.ru/inv/1"])
    o = asyncio.run(buy(shop, bot, "1"))
    assert asyncio.run(shop.admin_family_refund(o["id"])).startswith("деньги возвращены")
    assert fam.seats_used(a) == 0
    assert shop.db.order(o["id"])["status"] == "refunded"


def test_family_panel(tmp_path):
    app = App(tmp_path, demo=True)
    app.cfg.save({"dashboard": {"token": "admin-token-12345"}})

    async def go():
        await app.start()
        c = TestClient(TestServer(make_app(app)))
        await c.start_server()
        try:
            await c.get("/login?token=admin-token-12345")
            await c.post("/services", data={"action": "fam_add", "label": "Семья 1", "login": "f1@ya.ru",
                                            "seats": "3", "paid_until": "31.12.2027", "cost_rub": "2600",
                                            "months": "12"})
            fam = app.shop.services.family
            aid = fam.accounts()[0]["id"]
            await c.post("/services", data={"action": "fam_invites", "account": str(aid),
                                            "links": "https://yandex.ru/inv/1\nhttps://yandex.ru/inv/2"})
            assert len(fam.fresh_invites(aid)) == 2
            page = await (await c.get("/services")).text()
            assert "Семья 1" in page and "✅ можно" in page
            await c.post("/services", data={"action": "fam_status", "account": str(aid), "status": "paused",
                                            "risk": "1"})
            assert fam.account(aid)["status"] == "paused"
            data = await (await c.get("/api/services")).json()
            assert data["family"]["accounts"][0]["why"] == ["пауза"]
        finally:
            await c.close()
            await app.shop.stop()
    asyncio.run(go())
