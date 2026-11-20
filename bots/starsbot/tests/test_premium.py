import asyncio

from test_engine import boot, drain, make

from starsbot.config import load_config, validate_config
from starsbot.core.models import CompetitorLot, OrderStatus as S
from starsbot.demo import DEMO_PREMIUM_TON, run_premium_demo
from starsbot.ggsel.fake import DEMO_PREMIUM_PRODUCTS, DEMO_PRODUCTS
from starsbot.premium.service import months_from_text, months_text


def test_months_parsing():
    assert months_from_text("Telegram Premium, 3 месяца, Подарком") == 3
    assert months_from_text("Премиум на 12 мес") == 12
    assert months_from_text("Telegram Premium 1 год") == 12
    assert months_from_text("Premium на полгода") == 6
    assert months_from_text("6 months Telegram Premium") == 6
    assert months_from_text("Telegram Premium") is None
    assert months_text(3) == "3 месяца" and months_text(12) == "12 месяцев" and months_text(1) == "1 месяц"
    assert months_text(6, "en") == "6 months"


def test_order_detection(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    pr = e.premium
    assert e.is_premium_order("Telegram Premium, 3 месяца, Подарком по username")
    assert not e.is_premium_order("Telegram Premium, 3 месяца, С заходом на аккаунт")   # вход в аккаунт — не наш
    assert not e.is_premium_order("Telegram Premium, 1 месяц")                          # Fragment дарит 3/6/12
    assert not e.is_premium_order("100 звёзд, По username")
    assert not e.is_our_order("Telegram Premium, 3 месяца")                             # звёздам не мешает
    pr.c["enabled"] = False
    assert not e.is_premium_order("Telegram Premium, 3 месяца")


def test_premium_flow(tmp_path):
    sup, e, fp, wallet = make(tmp_path)

    async def run():
        await boot(e)
        a = fp.buyer_pays_premium("Nina", 41, 3, 560.0, username="@alice_tg")
        b = fp.buyer_pays_premium("Oleg", 42, 12, 1350.0)
        await drain(e, wallet)
        fp.buyer_says(42, "@bob_real")
        await drain(e, wallet)
        await drain(e, wallet)
        await e.wallet_watch()
        return a, b
    a, b = asyncio.run(run())
    oa, ob = sup.db.get_order(a), sup.db.get_order(b)
    assert oa["delivery"] == "premium" and oa["premium_months"] == 3 and oa["stars"] == 0
    assert oa["status"] == S.DELIVERED.value and abs(oa["ton_amount"] - DEMO_PREMIUM_TON[3]) < 1e-6
    assert ob["status"] == S.DELIVERED.value and ob["tg_username"] == "bob_real" and ob["premium_months"] == 12
    texts = [t for ch, t in fp.outbox if ch == oa["chat_id"]]
    assert any("Premium на 3 месяца" in t for t in texts)
    assert not any("⭐ Telegram Stars" in t for t in texts)
    # подтвердить просим только после выдачи
    first_confirm = next(i for i, t in enumerate(texts) if "подтвердите выполнение" in t.lower())
    assert "подарен" in texts[first_confirm]
    # покупки Premium не портят цену звезды
    assert e.cost_per_star_ton() is None or e.cost_per_star_ton() < 0.1
    # котировка записана — себестоимость известна
    assert abs(e.premium.ton_for(3) - DEMO_PREMIUM_TON[3]) < 1e-6
    # сверка кошелька учитывает траты на Premium
    rec = e.econ.reconcile()
    assert not rec.get("alarm"), rec


def test_premium_multi_qty_and_faq(tmp_path):
    sup, e, fp, wallet = make(tmp_path)

    async def run():
        await boot(e)
        m = fp.buyer_pays_premium("Multi", 45, 3, 560.0, username="alice_tg", qty=2)
        c = fp.buyer_pays_premium("Faq", 46, 6, 800.0)
        await drain(e, wallet)
        fp.buyer_says(46, "это premium без входа?")
        await drain(e, wallet)
        return m, c
    m, c = asyncio.run(run())
    assert sup.db.get_order(m)["status"] == S.MANUAL.value
    oc = sup.db.get_order(c)
    texts = [t for ch, t in fp.outbox if ch == oc["chat_id"]]
    assert any("заказ Telegram Premium на 6 месяцев" in t for t in texts), texts


def test_reserve_and_stock(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    e.cfg.rtba.fallback_ton_per_star = 0.0115
    pr = e.premium
    for m, ton in DEMO_PREMIUM_TON.items():
        pr.record_quote(m, ton)

    async def run():
        await boot(e)
        before = e.wallet_info["capacity"]
        b = fp.buyer_pays_premium("Oleg", 42, 12, 1350.0)        # ждёт username — TON зарезервированы
        await drain(e, wallet)
        await e.wallet_watch()
        return before, e.wallet_info["capacity"], b
    before, after, b = asyncio.run(run())
    assert sup.db.get_order(b)["status"] == S.AWAITING_USERNAME.value
    assert pr.committed_ton() > DEMO_PREMIUM_TON[12]
    assert after < before                                    # звёздам доступно меньше
    lot = fp._lot("303")
    assert int(lot["amount"]) >= 1 and lot["active"] == "on"
    # мало денег — лоты Premium скрываются
    wallet.balance = int(1e9)

    async def poor():
        for l in pr.lots:
            sup.db.kv_set(f"premium_lot_amount:{l['offer_id']}", None)
        await e.wallet_watch()
    asyncio.run(poor())
    assert fp._lot("303")["active"] == ""


def test_rtba_floor_and_position(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    pr = e.premium
    for m, ton in DEMO_PREMIUM_TON.items():
        pr.record_quote(m, ton)
    asyncio.run(boot(e))
    lots = fp._premium_lots()
    d = pr.decide(lots, {"months": 3, "offer_id": 301, "position": 1}, 600.0, {"301", "302", "303"})
    floor = pr.floor_price(3)
    assert floor and d["target"] >= floor
    # конкуренты «с заходом» не учитываются
    comps = pr.competitors(lots, 3, {"301"})
    assert all("заход" not in c.description.lower() for c in comps) and comps
    # рынок ниже пола — стоим на полу
    cheap = [CompetitorLot(offer_id="9", seller="X", price=10.0, amount=5, description="Telegram Premium 3 месяца, подарком",
                           fields={})]
    d2 = pr.decide(cheap, {"months": 3, "offer_id": 301, "position": 1}, 600.0, {"301"})
    assert d2["target"] == floor
    asyncio.run(pr.rtba_step())
    assert sup.db.scalar("SELECT COUNT(*) FROM price_history WHERE reason LIKE 'Premium%'") == 3


def test_loss_guard(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    e.cfg.general.demo = False                     # предохранитель убытка работает только «в бою»
    e.cfg.general.dry_run = False
    e.cfg.safety.pilot_orders = 0

    async def run():
        await boot(e)
        o = fp.buyer_pays_premium("Cheap", 47, 12, 300.0, username="alice_tg")   # 9.35 TON ≈ 1000 ₽ за 300 ₽
        await drain(e, wallet)
        return o
    oid = asyncio.run(run())
    o = sup.db.get_order(oid)
    assert o["status"] == S.PENDING_APPROVAL.value and o["fail_reason"] == "price_guard"
    assert not [p for p in sup.db.q("SELECT * FROM purchases WHERE order_id=?", (oid,))]


def test_supply_orders(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    pr = e.premium

    async def run():
        await boot(e)
        q0 = pr.supply_quote(3)
        pr.record_quote(3, DEMO_PREMIUM_TON[3])
        q = pr.supply_quote(3)
        r1 = await pr.supply_order("ref-1", "@carol_star", 3, channel="shop", price=700)
        r2 = await pr.supply_order("ref-1", "@carol_star", 3, channel="shop", price=700)   # идемпотентно
        bad = await pr.supply_order("ref-2", "@nobody_here", 3)
        await drain(e, wallet)
        return q0, q, r1, r2, bad
    q0, q, r1, r2, bad = asyncio.run(run())
    assert not q0["available"] and q["available"] and q["cost_rub"] > 0
    assert r1["ok"] and r1["order_id"] == r2["order_id"] == "PR-SUP-ref-1"
    assert sup.db.scalar("SELECT COUNT(*) FROM orders WHERE order_id LIKE 'PR-SUP-ref-1'") == 1
    assert pr.supply_status("ref-1")["status"] == "delivered"
    o = sup.db.get_order("PR-SUP-ref-1")
    assert o["status"] == S.COMPLETED.value and o["chat_id"] is None
    assert pr.supply_status("ref-2")["status"] == "failed"
    assert pr.supply_status("nope")["status"] == "failed"
    assert not pr.supply_quote(1)["available"]


def test_ggsel_premium(tmp_path):
    sup, e, fp, wallet = make(tmp_path, ggsel={"enabled": True, "products": DEMO_PRODUCTS, "price_mode": "margin",
                                               "premium_products": DEMO_PREMIUM_PRODUCTS})
    ch = e.channels["ggsel"]
    gg = ch.client
    sup.db.kv_set("ggsel_initialized", True)
    e.premium.record_quote(6, DEMO_PREMIUM_TON[6])

    async def run():
        await boot(e)
        inv = gg.buyer_buys(5200006, 990.0, "@alice_tg")
        await ch.poll()
        await drain(e, wallet)
        await e.wallet_watch()
        return inv
    inv = asyncio.run(run())
    o = sup.db.get_order(f"GG-{inv}")
    assert o["delivery"] == "premium" and o["premium_months"] == 6 and o["status"] == S.DELIVERED.value
    texts = gg.seller_texts(inv)
    assert any("Premium на 6 месяцев" in t for t in texts) and not any("FunPay" in t for t in texts)
    assert "price" in gg.offers.get(5200006, {}) and gg.offers[5200006]["price"] >= e.premium.floor_price(6, "ggsel")


def test_demo_and_presale(tmp_path):
    sup, e, fp, wallet = make(tmp_path)

    async def run():
        await boot(e)
        return await run_premium_demo(e, fp, wallet)
    a, b, login, shop = asyncio.run(run())
    assert sup.db.get_order(a)["status"] == S.COMPLETED.value
    assert sup.db.get_order(b)["status"] == S.DELIVERED.value
    assert sup.db.get_order(login) is None                 # лот «с заходом» бот не трогает
    assert sup.db.get_order(shop)["status"] == S.COMPLETED.value
    assert any("Telegram Premium" in t and "без входа" in t for ch, t in fp.outbox if ch == "544")
    assert "Premium" in asyncio.run(e.handle_command("premium", []))


def test_config_validation():
    cfg = load_config(None, {"general": {"demo": False}, "premium": {"enabled": True, "lots": [{"months": 1}]}})
    assert any("premium.lots" in p for p in validate_config(cfg))
