import asyncio
import base64
import time

from aiohttp.test_utils import TestClient, TestServer
from pytoniq_core import Address, Cell, MessageAny
from pytoniq_core.crypto.keys import mnemonic_new

from starsbot.core.models import CompetitorLot, OrderStatus as S, TonMessage
from starsbot.core.rtba import RTBAEngine
from starsbot.config import load_config
from starsbot.funpay.lots import build_lot_fields
from starsbot.gifts.planner import DEFAULT_CATALOG, plan_gifts
from starsbot.ton import mnemonic as MN
from starsbot.ton.wallet import TonWallet, to_nano
from starsbot.web.server import make_app
from tests.test_engine import boot, drain, make
from tests.test_wallet_fragment import FakeApi, wallet_data

W12 = ("abandon " * 11 + "about").split()


# ---------------------------------------------------------------- сид-фраза 12 слов
def test_slip10_vector_and_bip39_checksum():
    assert MN.slip10_ed25519(bytes.fromhex("000102030405060708090a0b0c0d0e0f"), (0,)).hex() == \
        "68e0fe46dfb67e368c75379acec591dad19df3cde26e63b93a8e704f1dade7a3"
    assert MN.bip39_checksum_ok(W12)
    assert not MN.bip39_checksum_ok(("abandon " * 12).split())
    assert MN.normalize_words("1. Abandon 2.about\n3) zoo") == ["abandon", "about", "zoo"]


def test_12_words_wallet_detects_bip39_key_and_version():
    cands = MN.candidates(W12)
    assert cands[0].kind == "bip39"
    addr4 = MN.friendly(cands[0].address("v4r2"))
    w = TonWallet(" ".join(W12), addr4, version="v5r1")          # указали не ту версию — адрес исправит
    assert w.version == "v4r2" and w.key_kind == "bip39" and w.public_key == cands[0].public_key
    w.api = FakeApi(wallet_data("v4r2", cands[0].public_key, wallet_id=MN.V4R2_WALLET_ID))
    st = asyncio.run(w.refresh())
    assert st.active and st.seqno == 7


def test_ton_24_words_still_work_and_mismatch_is_reported():
    words = mnemonic_new(24)
    c = MN.candidates(words)[0]
    assert c.kind == "ton"
    addr = MN.friendly(c.address("v5r1"))
    w = TonWallet(words, addr, version="v5r1")
    w.api = FakeApi(wallet_data("v5r1", c.public_key))
    assert asyncio.run(w.refresh()).active
    other = TonWallet(mnemonic_new(24), "UQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqEBI", version="v5r1")
    other.api = FakeApi(wallet_data("v5r1", c.public_key))
    try:
        asyncio.run(other.refresh())
        raise AssertionError("должна быть ошибка ключа")
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "code", "") == "key_mismatch"


class UninitApi(FakeApi):
    def __init__(self, active_addr: str | None = None):
        self.active_addr, self.sent, self.balance = active_addr, [], to_nano(3)

    async def address_info(self, address):
        if self.active_addr and Address(address) == Address(self.active_addr):
            return {"balance": str(self.balance), "state": "uninitialized", "data": ""}
        return {"balance": "0", "state": "uninitialized", "data": ""}


def test_new_wallet_without_address_is_found_and_deployed_by_first_transfer():
    c = MN.candidates(W12)[0]
    target = MN.friendly(c.address("v5r1"))
    w = TonWallet(" ".join(W12), "", version="v5r1", api=UninitApi(target))
    st = asyncio.run(w.refresh())
    assert w.friendly_address == target and st.deploy and st.seqno == 0
    sent = asyncio.run(w.transfer([TonMessage("EQBAjaOyi2wGWlk-EDkSabqqnF-MrrwMadnwqrurKpkla9nE", to_nano(1))],
                                  int(time.time()) + 300))
    msg = MessageAny.deserialize(Cell.one_from_boc(base64.b64decode(sent.boc)).begin_parse())
    assert msg.init is not None                                   # StateInit приложен — кошелёк развернётся
    assert Address((0, msg.init.serialize().hash)) == Address(target)


def test_config_accepts_12_words_without_address():
    from starsbot.config import validate_config
    cfg = load_config(None, {"ton": {"mnemonic": " ".join(W12), "address": ""}})
    assert not [p for p in validate_config(cfg) if p.startswith("ton.")]
    cfg = load_config(None, {"ton": {"mnemonic": "one two three"}})
    assert any("12 или 24" in p for p in validate_config(cfg))


# ---------------------------------------------------------------- подбор подарков
def test_gift_plans_match_funpay_denominations():
    assert plan_gifts(DEFAULT_CATALOG, 13).cost == 15
    assert plan_gifts(DEFAULT_CATALOG, 21).cost == 25
    assert plan_gifts(DEFAULT_CATALOG, 43).cost == 50
    assert plan_gifts(DEFAULT_CATALOG, 85).cost == 100
    for n in (50, 100, 250, 500):
        p = plan_gifts(DEFAULT_CATALOG, n, per_gift_penalty=3)
        assert p.value >= n and p.cost <= n * 1.35
        assert sum(g.stars for g in plan_gifts(DEFAULT_CATALOG, n, mode="price").gifts) == n


# ---------------------------------------------------------------- заказ подарками целиком
def test_gift_order_end_to_end(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    tg = e.gifts.tg
    start = tg.balance

    async def go():
        await boot(e)
        oid = fp.buyer_pays("Gina", 77, 85, method="Подарком")
        await drain(e, wallet)
        o = sup.db.get_order(oid)
        assert o["delivery"] == "gift" and o["status"] == S.AWAITING_USERNAME.value
        greet = [t for c, t in fp.outbox if c == o["chat_id"]][-1]
        assert "подарками" in greet and "username" in greet
        fp.buyer_says(77, "@gina_tg")
        await drain(e, wallet)
        await drain(e, wallet)
        return oid

    oid = asyncio.run(go())
    o = sup.db.get_order(oid)
    assert o["status"] == S.DELIVERED.value, (o["status"], o["fail_reason"])
    assert [s for _, _, s in tg.sent] == [100] and tg.balance == start - 100
    last = [t for c, t in fp.outbox if c == o["chat_id"]][-1]
    assert "Конвертировать в звёзды" in last and "подтвердите" in last.lower()
    assert sup.db.scalar("SELECT COUNT(*) FROM gift_sends WHERE order_id=? AND state='sent'", (oid,)) == 1
    # Fragment и кошелёк TON не трогали
    assert not sup.db.scalar("SELECT COUNT(*) FROM purchases WHERE order_id=?", (oid,))


def test_gift_unknown_outcome_goes_to_seller_and_resend_after_approve(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    tg = e.gifts.tg

    async def go():
        await boot(e)
        oid = fp.buyer_pays("Hank", 78, 13, method="Подарком", username="hank_tg")
        tg.fail_next = "unknown"
        await drain(e, wallet)
        await drain(e, wallet)
        assert sup.db.get_order(oid)["status"] == S.MANUAL.value
        assert sup.db.get_order(oid)["fail_reason"] == "gift_unknown"
        assert not tg.sent
        await e.admin_approve(oid)
        await drain(e, wallet)
        return oid

    oid = asyncio.run(go())
    assert sup.db.get_order(oid)["status"] == S.DELIVERED.value
    assert len(tg.sent) == 1


def test_out_of_stars_triggers_fragment_topup_and_closes_lots(tmp_path):
    sup, e, fp, wallet = make(tmp_path, gifts={"enabled": True, "pilot_orders": 0, "pause_between_gifts": 0,
                                               "lots": [{"stars": 43, "offer_id": 150}],
                                               "topup": {"enabled": True, "min_stars": 100, "amount": 500}})
    tg = e.gifts.tg
    tg.balance = 20
    e.fragment.users["my_shop_account"] = "Магазин"

    async def go():
        await boot(e)
        await e.gifts.watch()                                    # мало звёзд → пополнение через Fragment
        top = e.gifts.topup_pending()
        assert top is not None and top["stars"] == 500 and top["channel"] == "internal"
        await drain(e, wallet)
        await drain(e, wallet)
        done = sup.db.get_order(top["order_id"])
        assert done["status"] == S.COMPLETED.value, done["status"]
        assert sup.db.scalar("SELECT COUNT(*) FROM purchases WHERE order_id=?", (top["order_id"],)) == 1
        oid = fp.buyer_pays("Ivy", 79, 43, method="Подарком", username="ivy_tg")
        await drain(e, wallet)
        return oid

    oid = asyncio.run(go())
    o = sup.db.get_order(oid)
    assert o["status"] == S.MANUAL.value and o["fail_reason"] == "out_of_stars"   # в эмуляторе звёзды не приходят
    # внутренние пополнения не попадают в продажи
    assert all(r["channel"] != "internal" for r in sup.econ.db.q("SELECT channel FROM orders WHERE "
                                                                 "COALESCE(channel,'funpay')<>'internal'"))


# ---------------------------------------------------------------- лоты и позиция
def test_build_lot_fields_picks_options_by_text():
    base = {"offer_id": "5", "node_id": "2418", "fields[quantity]": "1", "fields[method]": "1",
            "fields[desc][ru]": "", "price": "1"}
    selects = {"fields[quantity]": [("1", "50 звёзд"), ("4", "13 звёзд"), ("99", "Другое количество")],
               "fields[method]": [("1", "По username"), ("2", "Подарком")]}
    f = build_lot_fields(base, selects, stars=13, kind="gift", price=20, amount=3, description="Авто")
    assert f["fields[quantity]"] == "4" and f["fields[method]"] == "2" and f["price"] == "20.00"
    assert f["fields[desc][ru]"] == "Авто" and "offer_id" not in f and f["active"] == "on"


def test_create_missing_lots_and_close_all(tmp_path):
    sup, e, fp, wallet = make(tmp_path, funpay={"lots": [{"stars": 50, "offer_id": 1}, {"stars": 75, "offer_id": 0}]})
    rep = asyncio.run(e.lot_manager.create_missing(kinds=("username",)))
    assert any("выставлен" in r for r in rep)
    new = next(l for l in e.cfg.funpay.lots if int(l["stars"]) == 75)["offer_id"]
    assert new and fp.lots[str(new)]["fields[quantity]"] == "2" and fp.lots[str(new)]["fields[method]"] == "1"
    asyncio.run(e.lot_manager.close_all())
    assert fp.lots["1"]["active"] == "" and fp.lots[str(new)]["active"] == ""


def test_rtba_per_lot_position():
    cfg = load_config(None).rtba
    eng = RTBAEngine(cfg)
    lots = [CompetitorLot(str(i), f"s{i}", 20 + i, 10, description="13 звёзд, Подарком",
                          fields={"quantity": "13 звёзд", "method": "подарком"}) for i in range(6)]
    lots.append(CompetitorLot("99", "u", 5, 10, description="13 звёзд, По username",
                              fields={"quantity": "13 звёзд", "method": "по username"}))
    d = eng.decide(lots, offer_id="500", stars=13, own_offer_ids={"500"}, own_username="me", current_lot_price=None,
                   cost_per_star_rub=0.5, overrides={"method_filter": "подарк", "strategy": "topN",
                                                     "target_position": 3})
    assert d.target_buyer_price is not None and 21 < d.target_buyer_price < 22      # чуть дешевле 3-го (22 ₽)


# ---------------------------------------------------------------- автопилот
def test_autopilot_reminder_support_file_and_withdraw(tmp_path):
    sup, e, fp, wallet = make(tmp_path, autopilot={"enabled": True, "confirm_reminder_hours": 1,
                                                   "support_after_hours": 2,
                                                   "withdraw": {"enabled": True, "min_wallet_ton": 1000,
                                                                "min_funpay_rub": 100, "require_approval": True,
                                                                "max_per_day_rub": 5000}})
    ap = e.autopilot
    e.fragment.users["jack_tg"] = "Jack"

    async def go():
        await boot(e)
        oid = fp.buyer_pays("Jack", 80, 50, username="jack_tg")
        await drain(e, wallet)
        await drain(e, wallet)
        assert sup.db.get_order(oid)["status"] == S.DELIVERED.value
        sup.db.update_order(oid, delivered_at=time.time() - 3 * 3600)
        await ap.tick()
        return oid

    oid = asyncio.run(go())
    o = sup.db.get_order(oid)
    assert o["confirm_reminded"] == 1 and o["support_file"]
    texts = [t for c, t in fp.outbox if c == o["chat_id"]]
    assert sum("всё ли пришло" in t for t in texts) == 1
    body = open(o["support_file"], encoding="utf-8").read()
    assert f"#{oid}" in body and "jack_tg" in body and "Переписка" in body
    w = sup.db.one("SELECT * FROM withdrawals ORDER BY id DESC")
    assert w["state"] == "pending" and w["amount_rub"] == 5000 and w["method"] == "ton"
    assert not fp.withdrawals                                  # без кнопки деньги не выводятся
    msg = asyncio.run(e.handle_command("withdraw_ok", [str(w["id"])]))
    assert "вывод" in msg and fp.withdrawals and sup.db.one("SELECT state FROM withdrawals")["state"] == "sent"


def test_new_pages_render(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    sup.save({"dashboard": {"token": "admin-token-12345"}}, rebuild=False)

    async def go():
        await boot(e)
        client = TestClient(TestServer(make_app(sup)))
        await client.start_server()
        r = await client.get("/login?token=admin-token-12345", allow_redirects=False)
        assert r.status == 302
        try:
            for path in ("/gifts", "/funpay", "/crypto", "/gifts?login=code", "/setup", "/settings"):
                r = await client.get(path)
                assert r.status == 200, (path, await r.text())
            t = await (await client.get("/")).text()
            assert "Продажи" in t and "Выдача" in t and "Финансы" in t
            r = await client.post("/api/wallet/addresses", json={"mnemonic": " ".join(W12)})
            items = (await r.json())["items"]
            assert {x["version"] for x in items} == {"v5r1", "v4r2"}
            r = await client.post("/funpay", data={"action": "positions", "pos_gift_13": "3", "pos_username_50": "2"},
                                  allow_redirects=False)
            assert r.status == 302
            r = await client.post("/funpay", data={"action": "support"}, allow_redirects=False)
            assert r.status == 302
        finally:
            await client.close()

    asyncio.run(go())
    assert next(l for l in e.cfg.gifts.lots if int(l["stars"]) == 13)["position"] == 3
    assert next(l for l in e.cfg.funpay.lots if int(l["stars"]) == 50)["position"] == 2


def test_create_lot_in_test_mode_is_real_but_inactive(tmp_path):
    """Тестовый режим: лот создаётся на FunPay выключенным, с кратким описанием своего номинала."""
    sup, e, fp, wallet = make(tmp_path, funpay={"lots": [{"stars": 50, "offer_id": 1}, {"stars": 75, "offer_id": 0}]})
    fp.dry_run = True
    rep = asyncio.run(e.lot_manager.create_missing(kinds=("username",)))
    new = next(l for l in e.cfg.funpay.lots if int(l["stars"]) == 75)["offer_id"]
    assert new and "выключен" in rep[0]
    lot = fp.lots[str(new)]
    assert not lot.get("active") and "75 звёзд" in lot["fields[summary][ru]"]


def test_build_lot_fields_rewrites_template_summary_and_inactive():
    base = {"fields[quantity]": "1", "fields[method]": "1", "fields[summary][ru]": "100 звёзд по username",
            "active": "on", "deleted": ""}
    selects = {"fields[quantity]": [("1", "100 звёзд"), ("2", "250 звёзд")],
               "fields[method]": [("1", "По username"), ("2", "Подарком")]}
    f = build_lot_fields(base, selects, stars=250, kind="username", price=500, amount=1, active=False)
    assert "250 звёзд" in f["fields[summary][ru]"] and "active" not in f and "deleted" not in f


def test_build_lot_fields_english_fields_get_english_text():
    base = {"fields[quantity]": "1", "fields[method]": "1", "fields[desc][ru]": "", "fields[desc][en]": "",
            "fields[summary][en]": ""}
    selects = {"fields[quantity]": [("1", "50 звёзд")], "fields[method]": [("1", "По username")]}
    f = build_lot_fields(base, selects, stars=50, kind="username", price=80, amount=1, description="Автовыдача")
    assert f["fields[desc][ru]"] == "Автовыдача"
    assert f["fields[desc][en]"].isascii() and f["fields[summary][en]"].isascii()


def test_config_patch_file_applied_without_secrets(tmp_path):
    from starsbot.app.supervisor import Supervisor
    (tmp_path / "config.patch.yaml").write_text(
        "economics:\n  withdraw_fee_pct: 6\nfunpay:\n  golden_key: hacked\n", encoding="utf-8")
    sup = Supervisor(tmp_path, demo=False)
    assert float(sup.cfg.economics.withdraw_fee_pct) == 6
    assert sup.cfg.funpay.golden_key != "hacked"
    assert not (tmp_path / "config.patch.yaml").exists() and (tmp_path / "config.patch.applied.yaml").exists()


def test_loss_guard_blocks_unprofitable_purchase(tmp_path):
    sup, e, fp, wallet = make(tmp_path)
    e.cfg.general["demo"] = False
    e.cfg.safety["min_profit_pct"] = 3
    o = {"price": 60.0, "channel": "funpay"}
    assert "без прибыли" in e._loss_problem(o, 50, 0.9) or "нет курса" in e._loss_problem(o, 50, 0.9)
    assert e._loss_problem({"price": 500.0, "channel": "funpay"}, 50, 0.1) == ""


def test_second_instance_on_same_db_refuses_to_start(tmp_path):
    from starsbot.core.engine import Engine
    from starsbot.crm.db import Database
    db = Database(tmp_path / "s.sqlite3")
    a = Engine.__new__(Engine)
    b = Engine.__new__(Engine)
    for x, lid in ((a, "pc:1:a"), (b, "pc:2:b")):
        x.db, x.lease_id = db, lid
    assert a.take_lease() is None                  # первый берёт базу
    assert b.take_lease()["id"] == "pc:1:a"        # второй видит живого владельца
    assert a.take_lease() is None                  # продление своей аренды
    a.release_lease()
    assert b.take_lease() is None                  # после выхода первого — можно
    db.kv_set("engine_lease", {"id": "pc:2:b", "pid": 2, "ts": time.time() - 999})
    assert a.take_lease() is None and db.kv_get("engine_lease")["id"] == "pc:1:a"   # зависшая аренда истекает


def test_remote_inbox_runs_commands(tmp_path):
    from starsbot.core.engine import Engine
    from starsbot.crm.db import Database
    (tmp_path / "data").mkdir()
    e = Engine.__new__(Engine)
    e.db = Database(tmp_path / "data" / "s.sqlite3")
    seen = []

    async def handle(cmd, args):
        seen.append((cmd, args))
        return "ok"
    e.handle_command = handle
    (tmp_path / "data" / "remote_cmd.txt").write_text("# test\n/approve AB12\nlots_off\n", encoding="utf-8")
    asyncio.run(e.remote_inbox())
    assert seen == [("approve", ["AB12"]), ("lots_off", [])]
    assert not (tmp_path / "data" / "remote_cmd.txt").exists()
    assert "approve AB12" in (tmp_path / "data" / "remote_cmd.out.txt").read_text(encoding="utf-8")
