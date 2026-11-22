import asyncio

from test_engine import boot, make

from starsbot.config import load_config, validate_config
from starsbot.core.models import OrderStatus as S
from starsbot.ozon.tgbot import ActivationBot
from starsbot.wb import texts as T

WB = {"enabled": True, "products": [{"stars": n, "nm_id": f"nm{n}", "price": p} for n, p in ((100, 190), (250, 460))]}


def setup(tmp_path, **wb):
    sup, e, fp, wallet = make(tmp_path, wb={**WB, **wb})
    ch = e.channels["wb"]
    e.cfg.rtba.fallback_ton_per_star = 0.0115
    ch.bot = ActivationBot("", ch, prefix="TGW", texts=T)
    e.fragment.users.update({"maria_tg": "Мария", "oleg_tg": "Олег"})
    return sup, e, ch, wallet


async def settle(e, wallet):
    for _ in range(3):
        while not e.queue.empty():
            oid = e.queue.get_nowait()
            e._queued.discard(oid)
            await e.purchase(oid)
            wallet.tick()
            await e.watch_purchases()
        wallet.tick()
        await e.watch_purchases()


def test_pool_issue_export_redeem(tmp_path):
    sup, e, ch, wallet = setup(tmp_path)
    batch, codes = ch.issue(100, 3)
    assert all(c.startswith("TGW-") for c in codes) and ch.pool() == {100: 3}
    csv_text = ch.export_csv(batch)
    assert csv_text.splitlines()[0] == "code;stars;nm_id;batch" and codes[0] in csv_text and "nm100" in csv_text

    async def go():
        await boot(e)
        r1 = await ch.bot.handle(1, codes[0])                 # код, потом username
        r2 = await ch.bot.handle(1, "@maria_tg")
        await settle(e, wallet)
        r3 = await ch.bot.handle(2, f"{codes[1]} @oleg_tg")   # одним сообщением
        await settle(e, wallet)
        r4 = await ch.bot.handle(3, codes[0])                 # повторная активация того же кода
        return r1, r2, r3, r4
    r1, r2, r3, r4 = asyncio.run(go())
    assert "100 ⭐" in r1 and "Wildberries" not in r1
    assert "уже активирован" in r4 and "@maria_tg" in r4
    orders = sup.db.q("SELECT * FROM orders WHERE channel='wb' ORDER BY tg_username")
    assert [o["tg_username"] for o in orders] == ["maria_tg", "oleg_tg"]
    assert all(o["status"] == S.COMPLETED.value and o["price"] >= 190 for o in orders)
    assert ch.pool() == {100: 1}
    assert any("Wildberries" in t for _, t in ch.bot.sent)


def test_void_and_bad_codes(tmp_path):
    sup, e, ch, wallet = setup(tmp_path)
    batch, codes = ch.issue(250, 2)
    assert ch.void(batch=batch) == 2

    async def go():
        await boot(e)
        return await ch.bot.handle(5, codes[0]), await ch.bot.handle(5, "TGW-AAAA-BBBB-CCCC")
    a, b = asyncio.run(go())
    assert "аннулирован" in a and "не найден" in b
    assert not sup.db.q("SELECT * FROM orders WHERE channel='wb'")


def test_ozon_codes_not_accepted_by_wb(tmp_path):
    sup, e, ch, wallet = setup(tmp_path)
    assert not ch.check_code("TGS-ABCD-EFGH-JKMN").ok


def test_validation(tmp_path):
    cfg = load_config(None, overrides={"wb": {"enabled": True}})
    assert any(p.startswith("wb:") for p in validate_config(cfg))
