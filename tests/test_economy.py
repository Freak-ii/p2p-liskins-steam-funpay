import asyncio
import sqlite3
import time

import yaml
from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.economy import Economy, TonLots
from center.web import make_app

T0 = time.time() - 3 * 86400
CIRCLE = "fp_rub>card_rub>usdt>ton[stars@funpay]>fp_rub"


def _stars_db(home):
    d = home / "starsbot" / "data"
    d.mkdir(parents=True)
    (home / "starsbot" / "config.yaml").write_text(yaml.safe_dump({"economics": {"withdraw_fee_pct": 3.0}}), "utf-8")
    con = sqlite3.connect(d / "starsbot.sqlite3")
    # старая схема без части колонок (premium_months, gift_cost, payout): отчёт должен читать терпимо
    con.executescript("""
    CREATE TABLE orders (order_id TEXT PRIMARY KEY, description TEXT, stars INTEGER, price REAL, currency TEXT,
        status TEXT, fp_status TEXT, tg_username TEXT, ton_amount REAL, ton_rate REAL, cost_rub REAL, tx_hash TEXT,
        tx_fee_ton REAL, created_at REAL, sent_at REAL, delivered_at REAL, completed_at REAL, channel TEXT);
    CREATE TABLE cashflow (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, rub REAL, ton REAL, note TEXT);
    CREATE TABLE balance_history (ts REAL, ton REAL, ton_rub REAL, capacity_stars INTEGER);
    """)
    con.executemany("INSERT INTO cashflow (ts, kind, rub, ton, note) VALUES (?,?,?,?,?)",
                    [(T0, "topup", 900.0, 10.0, "bybit"), (T0 + 10, "topup", 1000.0, 10.0, "tg")])
    orders = [  # 3 продажи по 100 ₽ за 1.1 TON (рынок 80 ₽/TON), одна ждёт подтверждения, одна — возврат
        ("A", "completed", 100.0, 1.1), ("B", "completed", 100.0, 1.1), ("C", "delivered", 100.0, 1.1),
        ("D", "refunded", 100.0, 1.1), ("E", "awaiting_username", 100.0, None)]
    for i, (oid, st, price, ton) in enumerate(orders):
        ts = T0 + 100 + i
        con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, "50 звёзд", 50, price, "₽", st, "closed", "user", ton, 80.0 if ton else None,
                     ton * 80 if ton else None, "h" if ton else None, 0.01 if ton else None, ts, ts, ts,
                     ts + 50 if st == "completed" else None, "funpay"))
    con.executemany("INSERT INTO balance_history VALUES (?,?,?,?)",
                    [(T0 - 1, 0.0, 80, 0), (T0 + 200, 20 - 4 * 1.11, 80, 0)])
    con.commit()
    con.close()


def test_ton_lots_fifo():
    lots = TonLots([(2, 1000, 10), (1, 900, 10)], fallback_rate=50)
    assert round(lots.take(12, 80), 2) == 900 + 200           # сначала дешёвая партия 90 ₽/TON, потом 100 ₽/TON
    assert round(lots.left_ton, 6) == 8 and round(lots.left_rub, 2) == 800
    assert round(lots.take(10, 80), 2) == 800 + 2 * 50 and lots.unlotted_ton == 2


def test_report_stars(tmp_path):
    _stars_db(tmp_path)
    r = Economy(tmp_path).report(usd_rub=80)
    t = r["totals"]
    assert t["orders"] == 4                                        # продажи A, B, C и возврат D
    assert t["revenue"] == 300 and t["revenue_done"] == 200 and t["revenue_pending"] == 100
    assert t["cost"] + t["gas"] == round(4 * 1.11 * 90, 2)         # все 4.44 TON из первой партии по 90 ₽
    assert t["fx_loss"] == round(4 * 1.1 * 10, 2)                 # переплата 10 ₽/TON против рынка 80
    assert t["fee_estimate"] == 9.0 and t["net"] == round(300 - 4 * 1.11 * 90 - 9, 2)
    c = r["circles"][0]
    assert c["id"] == CIRCLE and c["net"] == t["net"]
    assert [l["kind"] for l in c["legs"]] == ["transfer", "purchase", "sale", "transfer"]
    assert round(sum(l["rub"] for l in c["legs"]), 2) == t["net"]   # шаги круга складываются в итог
    w = r["wallets"][0]
    assert w["diff"] == 0 and w["note"] == "сходится"
    assert any("дороже рынка" in x for x in r["warnings"]) and any("в минусе" in x for x in r["warnings"])
    assert r["positions"][-1]["position"] == "fp_hold" and r["positions"][-1]["value_rub"] == 100
    assert Economy(tmp_path).position_margin("stars", "funpay") == t["net_pct"]
    assert Economy(tmp_path).position_margin("mann_keys") is None


def test_empty_home(tmp_path):
    r = Economy(tmp_path).report()
    assert r["totals"]["revenue"] == 0 and r["circles"] == [] and r["warnings"] == []


def test_economy_web(tmp_path):
    async def go():
        _stars_db(tmp_path)
        center = Center(tmp_path)
        token = center.cfg.ensure_token()
        async with TestClient(TestServer(make_app(center))) as c:
            assert (await c.get("/api/economy/summary")).status == 401
            await c.get(f"/login?token={token}", allow_redirects=False)
            assert (await c.get("/economy")).status == 200
            d = await (await c.get("/api/economy/summary?days=30")).json()
            assert d["totals"]["revenue"] == 300 and d["circles"][0]["id"] == CIRCLE
            led = await (await c.get("/api/economy/ledger?kind=sale&status=pending")).json()
            assert led["total"] == 1 and led["ops"][0]["ref"] == "C"
            led = await (await c.get("/api/economy/ledger?q=refunded")).json()
            assert led["total"] == 0
            r = await c.get("/api/economy/export")
            body = (await r.read()).decode("utf-8-sig")
            assert r.status == 200 and body.startswith("Время;Бот") and "Продажа" in body
    asyncio.run(go())
