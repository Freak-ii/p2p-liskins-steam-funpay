import asyncio
import time

from starsbot.app.launcher import DEMO_OVERRIDES
from starsbot.app.supervisor import Supervisor
from starsbot.core.models import OrderStatus as S
from starsbot.demo import run_scenario


def make(tmp_path, **over):
    sup = Supervisor(tmp_path, demo=True, overrides={**DEMO_OVERRIDES, **over})
    e = sup.engine
    e.cfg.deal.message_rate_limit_seconds = 0
    e.cfg.deal.purchase_pause_seconds = 0
    return sup, e, e.fp, e.wallet


async def drain(e, wallet, rounds=3):
    for _ in range(rounds):
        await e.funpay_poll()
        while not e.queue.empty():
            oid = e.queue.get_nowait()
            e._queued.discard(oid)
            await e.purchase(oid)
            wallet.tick()
            await e.watch_purchases()
        wallet.tick()
        await e.watch_purchases()


async def boot(e):
    e.fragment_ok = True
    e._fragment_state(session="ok", kyc="verified", checked_at=time.time())
    await e.wallet_watch()
    await e.funpay_poll()


def test_full_scenario(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    res = asyncio.run(run_scenario(e, fp, wallet, verbose=False))
    a, b, c, d, ev, g = (sup.db.get_order(x) for x in res["orders"])
    assert a["status"] == S.COMPLETED.value
    for o in (b, c, d, ev, g):
        assert o["status"] == S.DELIVERED.value, (o["buyer_username"], o["status"])
    assert b["tg_username"] == "bob_real" and b["username_attempts"] >= 1
    assert c["stars"] == 500 and c["tg_username"] == "carol_star"
    assert g["approved"] == 1 and g["stars"] == 2000
    # «потерянная» транзакция Eve: ровно одна реальная оплата, повтор только после истечения срока
    paid = [h for h in wallet.history if abs(h["value"] - int(round(150 * 0.0115 * 1e9))) < 10]
    assert len(paid) == 1
    assert sup.db.scalar("SELECT COUNT(*) FROM purchases WHERE order_id=?", (res["orders"][4],)) == 2
    # просьба подтвердить заказ — только после выдачи (п. 2.1.2 правил FunPay)
    for oid in res["orders"]:
        o = sup.db.get_order(oid)
        texts = [t for ch, t in fp.outbox if ch == o["chat_id"]]
        first_confirm = next((i for i, t in enumerate(texts) if "подтвердите" in t.lower()), None)
        if first_confirm is not None:
            assert sup.db.get_order(oid)["delivered_at"]
    # никаких контактов и сторонних ссылок в ответах бота
    assert not any("http" in t and "funpay.com" not in t for _, t in fp.outbox)
    assert sup.analytics.summary(1)["delivered"] == 6
    assert sup.econ.reconcile()["diff"] == 0


def test_lost_tx_is_not_paid_twice_before_expiry(tmp_path):
    sup, e, fp, wallet = make(tmp_path)

    async def go():
        await boot(e)
        wallet.drop_next = True
        oid = fp.buyer_pays("Eve", 15, 150, username="eve_e")
        for _ in range(4):
            await drain(e, wallet)
        return oid
    oid = asyncio.run(go())
    o = sup.db.get_order(oid)
    assert o["status"] == S.SENT.value and not wallet.history     # ждём, второй раз не платим
    assert sup.db.scalar("SELECT COUNT(*) FROM purchases WHERE order_id=?", (oid,)) == 1


def test_fragment_session_expired_hides_lots_and_blocks_purchases(tmp_path):
    sup, e, fp, wallet = make(tmp_path)

    async def go():
        await boot(e)
        e.fragment.logged_in = False
        ok = await e.ensure_fragment(force=True)
        oid = fp.buyer_pays("Alice", 11, 100, username="alice_tg")
        await drain(e, wallet)
        return ok, oid
    ok, oid = asyncio.run(go())
    assert not ok and e.fragment_status["session"] == "expired"
    assert not wallet.history
    assert sup.db.get_order(oid)["status"] in (S.READY.value, S.AWAITING_USERNAME.value)
    assert all(l["amount"] == "0" for l in fp.lots.values() if l)
    assert sup.db.scalar("SELECT COUNT(*) FROM alerts WHERE code LIKE 'fragment%'") >= 1


def test_out_of_funds_waits_and_resumes(tmp_path):
    sup, e, fp, wallet = make(tmp_path)

    async def go():
        await boot(e)
        wallet.balance = int(0.5 * 1e9)
        oid = fp.buyer_pays("Greta", 21, 100, username="helen_h")
        await drain(e, wallet)
        st1 = sup.db.get_order(oid)["status"]
        wallet.balance += int(50 * 1e9)
        await e.wallet_watch()
        await drain(e, wallet)
        return oid, st1
    oid, st1 = asyncio.run(go())
    assert st1 in (S.READY.value, S.MANUAL.value)
    assert sup.db.get_order(oid)["status"] == S.DELIVERED.value
    assert len(wallet.history) == 1


def test_foreign_chat_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "coord.sqlite3"
    monkeypatch.setenv("FUNPAY_COORD_DB", str(path))
    sup, e, fp, wallet = make(tmp_path / "bot")
    from starsbot.coord import Coordinator
    mann = Coordinator(path, "mannbot")

    async def go():
        await boot(e)
        mann.claim_chat("530", "MANN0001")           # MannBot ведёт заказ ключей в чате покупателя 30
        fp.buyer_says(30, "а звёзды на 500 есть? быстро приходят?")
        fp.buyer_says(31, "а звёзды на 500 есть? быстро приходят?")   # тот же вопрос в свободном чате
        await drain(e, wallet)
    asyncio.run(go())
    assert not [t for ch, t in fp.outbox if ch == "530"]
    assert [t for ch, t in fp.outbox if ch == "531"]


def test_order_claimed_by_other_bot_is_skipped(tmp_path, monkeypatch):
    path = tmp_path / "coord.sqlite3"
    monkeypatch.setenv("FUNPAY_COORD_DB", str(path))
    sup, e, fp, wallet = make(tmp_path / "bot")
    from starsbot.coord import Coordinator

    async def go():
        await boot(e)
        oid = fp.buyer_pays("Zed", 40, 50, username="alice_tg")
        Coordinator(path, "mannbot").claim_order(oid)
        await drain(e, wallet)
        return oid
    oid = asyncio.run(go())
    assert sup.db.get_order(oid) is None and not wallet.history


def test_pilot_mode_and_daily_limit_in_live_mode(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    e.cfg.general.demo = False                     # как в боевом режиме: предохранители включены
    e.cfg.safety.pilot_orders = 1
    e.cfg.safety.daily_ton_limit = 2.0

    async def go():
        await boot(e)
        a = fp.buyer_pays("Alice", 11, 100, username="alice_tg")       # 1.15 TON
        await drain(e, wallet)
        st_pilot = sup.db.get_order(a)["status"]
        await e.handle_command("approve", [a])
        await drain(e, wallet)
        b = fp.buyer_pays("Bob", 12, 75, username="bob_real")          # 0.8625 TON: 2.01 > 2.0 за сутки
        await drain(e, wallet)
        return a, b, st_pilot
    a, b, st_pilot = asyncio.run(go())
    assert st_pilot == S.PENDING_APPROVAL.value
    assert sup.db.get_order(a)["status"] == S.DELIVERED.value and len(wallet.history) == 1
    assert sup.db.get_order(b)["status"] == S.PENDING_APPROVAL.value and len(wallet.history) == 1
    assert e.live_purchases() == 1 and abs(e.spent_ton_24h() - 1.15) < 1e-6
    assert sup.db.scalar("SELECT COUNT(*) FROM alerts WHERE text LIKE '%пилотный режим%'") == 1
    assert sup.db.scalar("SELECT COUNT(*) FROM alerts WHERE text LIKE '%лимита 2 TON%'") == 1


def test_returning_buyer_greeting_hints_last_recipient(tmp_path):
    sup, e, fp, wallet = make(tmp_path)

    async def go():
        await boot(e)
        fp.buyer_pays("Alice", 11, 100, username="alice_tg")
        await drain(e, wallet)
        first = [t for ch, t in fp.outbox if ch == "511"]
        fp.buyer_pays("Alice", 11, 50)                 # второй заказ без username
        await drain(e, wallet)
        second = [t for ch, t in fp.outbox if ch == "511"][len(first):]
        return first, second
    first, second = asyncio.run(go())
    assert "снова" not in first[0]
    assert "Рады видеть вас снова" in second[0] and "@alice_tg" in second[0]
    assert "{" not in second[0]


def test_db_backup_and_health(tmp_path):
    from starsbot.crm.db import Database
    db = Database(tmp_path / "data" / "starsbot.sqlite3")
    db.kv_set("x", 1)
    folder = tmp_path / "data" / "backups"
    for day in ("20260101", "20260102", "20260103"):
        (folder / f"starsbot-{day}.sqlite3").parent.mkdir(parents=True, exist_ok=True)
        (folder / f"starsbot-{day}.sqlite3").write_bytes(b"old")
    dest = db.backup(folder, keep=2)
    assert dest.exists() and Database(dest).kv_get("x") == 1
    assert sorted(p.name for p in folder.glob("*.sqlite3"))[0] == "starsbot-20260103.sqlite3"
    assert len(list(folder.glob("*.sqlite3"))) == 2

    sup, e, fp, wallet = make(tmp_path / "bot")
    e.last_ok.update(funpay_poll=time.time(), ton_watch=time.time(), wallet=time.time())
    e._fragment_state(session="ok", kyc="verified")
    assert e.health()["ok"]
    e.last_ok["funpay_poll"] = time.time() - 3600
    e.started_at = time.time() - 4000
    h = e.health()
    assert not h["ok"] and [c["name"] for c in h["checks"] if not c["ok"]] == ["funpay_poll"]
    asyncio.run(e.watchdog())
    assert sup.db.scalar("SELECT COUNT(*) FROM alerts WHERE code='watchdog'") == 1


def test_fragment_cookie_age_reminder(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    assert e.fragment_cookie_age_days() is None
    sup.save({"fragment": {"cookies": "stel_ssid=a; stel_token=b"}}, rebuild=False)
    first = sup.cfg.fragment.cookies_set_at
    assert first and abs(first - time.time()) < 5
    sup.save({"fragment": {"cookies": ""}}, rebuild=False)          # пусто = не менять, отметка та же
    assert sup.cfg.fragment.cookies_set_at == first
    e.cfg.fragment.cookies_set_at = time.time() - 20 * 86400
    e.cfg.fragment.session_warn_days = 14
    asyncio.run(e.fragment_age_reminder())
    asyncio.run(e.fragment_age_reminder())                          # второй раз в те же сутки — тишина
    assert sup.db.scalar("SELECT COUNT(*) FROM alerts WHERE code='fragment_age'") == 1
    assert e.health()["fragment_age_days"] >= 20


def test_faq_after_delivery_and_refund_request(tmp_path):
    sup, e, fp, wallet = make(tmp_path)

    async def go():
        await boot(e)
        fp.buyer_pays("Alice", 11, 100, username="alice_tg")
        await drain(e, wallet)
        n = len(fp.outbox)
        fp.buyer_says(11, "звёзды не пришли(")
        await drain(e, wallet)
        after = [t for _, t in fp.outbox[n:]]
        fp.buyer_pays("Bob", 12, 50)
        await drain(e, wallet)
        n = len(fp.outbox)
        fp.buyer_says(12, "передумал, верните деньги")
        await drain(e, wallet)
        return after, [t for _, t in fp.outbox[n:]]
    after, refund = asyncio.run(go())
    assert len(after) == 1 and "Мои звёзды" in after[0] and "@alice_tg" in after[0]
    assert "не подтверждайте" in refund[0]
    assert sup.db.scalar("SELECT COUNT(*) FROM alerts WHERE code IN ('buyer_not_arrived','buyer_refund')") == 2


def test_review_reply(tmp_path):
    sup, e, fp, wallet = make(tmp_path)

    async def go():
        await boot(e)
        fp.buyer_pays("Alice", 11, 100, username="alice_tg")
        fp.buyer_pays("Bob", 12, 50, username="bob_tg")
        await drain(e, wallet)
        by_buyer = {so.buyer_username: oid for oid, so in fp.sales.items()}
        good, bad = by_buyer["Alice"], by_buyer["Bob"]
        fp.reviews[bad] = 2
        for oid in (good, bad, good):                   # повторная проверка того же отзыва — без второго ответа
            await e._fetch_review_later(oid, delay=0)
        return good, bad
    good, bad = asyncio.run(go())
    assert list(fp.review_replies) == [good] and "Alice" in fp.review_replies[good]
    assert sup.db.scalar("SELECT COUNT(*) FROM alerts WHERE code='bad_review'") == 1
