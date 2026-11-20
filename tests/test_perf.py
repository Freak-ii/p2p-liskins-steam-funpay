"""Производительность центра на большой истории: кэш чтения баз ботов, выгрузка в один проход,
тяжёлые запросы панели не подвешивают центр."""
import asyncio
import sqlite3
import time

from aiohttp.test_utils import TestClient, TestServer

from center import analytics
from center.app import Center
from center.web import make_app
from tests.test_crm import seed_crm

NOW = time.time()
DAY = 86400


def add_lis_sales(home, n: int) -> None:
    con = sqlite3.connect(home / "lisbot" / "data" / "bot.sqlite")
    ms = lambda t: int(t * 1000)  # noqa: E731
    con.executemany("INSERT INTO items(name,mode,status,dry_run,buy_price_usd,expected_net_usd,received_at,sold_at,sold_usd,"
                    "profit_usd,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(f"Case {i}", "scan", "sold", 0, 1.0, 1.2, ms(NOW - 5 * DAY), ms(NOW - DAY + i), 1.2, 0.2,
                      ms(NOW - 6 * DAY), ms(NOW)) for i in range(n)])
    con.commit()
    con.close()


def test_cache_sees_new_bot_writes(tmp_path):
    seed_crm(tmp_path)
    center = Center(tmp_path)
    a, crm = center.analytics, center.crm
    assert len(a.lis_deals()) == 3 and crm.deals(bot="lisbot")["total"] == 4
    assert a.lis_deals() is not a.lis_deals()                 # наружу — копия списка, общий кэш не испортить
    add_lis_sales(tmp_path, 2)
    assert len(a.lis_deals()) == 5 and crm.deals(bot="lisbot")["total"] == 6
    assert len(a.lis_deals(NOW - 1.5 * DAY)) == 3             # отсечка по дате продажи поверх кэша


def test_export_is_whole_selection_in_one_pass(tmp_path, monkeypatch):
    seed_crm(tmp_path)
    add_lis_sales(tmp_path, 2500)
    center = Center(tmp_path)
    crm = center.crm
    calls = []
    orig = crm._lis_deals
    monkeypatch.setattr(crm, "_lis_deals", lambda: calls.append(1) or orig())
    rows = crm.export_rows(bot="lisbot")
    assert len(rows) == 1 + 2504                              # заголовок + все сделки, а не страница в 1000
    assert crm.deals(bot="lisbot", limit=1000, offset=2000)["total"] == 2504
    assert len(calls) == 1                                    # база прочитана один раз, дальше кэш


def test_heavy_requests_do_not_freeze_center(tmp_path, monkeypatch):
    seed_crm(tmp_path)

    async def go():
        center = Center(tmp_path)
        orig = center.crm.export_rows

        def slow(**kw):
            time.sleep(2.0)                                   # как разбор годовой истории
            return orig(**kw)

        monkeypatch.setattr(center.crm, "export_rows", slow)
        h = {"Authorization": f"Bearer {center.cfg.ensure_token()}"}
        async with TestClient(TestServer(make_app(center))) as c:
            t0 = time.perf_counter()
            heavy = asyncio.ensure_future(c.get("/api/crm/deals.csv", headers=h))
            await asyncio.sleep(0.1)                          # выгрузка уже идёт
            r = await c.get("/api/ping")
            assert r.status == 200 and time.perf_counter() - t0 < 1.2    # центр отвечает, не дожидаясь выгрузки
            r = await heavy
            assert r.status == 200 and "Бот" in await r.text()

    asyncio.run(go())


def test_cache_is_bounded_and_expires(tmp_path, monkeypatch):
    db = tmp_path / "x.sqlite"
    sqlite3.connect(db).close()
    n = []
    build = lambda: n.append(1) or len(n)  # noqa: E731
    assert analytics._cached(("t",), db, build) == 1
    assert analytics._cached(("t",), db, build) == 1
    monkeypatch.setattr(analytics, "CACHE_TTL", 0.0)
    assert analytics._cached(("t",), db, build) == 2
    for i in range(40):
        analytics._cached(("t", i), db, build)
    assert len(analytics._cache) <= 33
