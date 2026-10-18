import asyncio
import json
import sqlite3

from center.app import Center
from center.coordinator import StubBackend
from tests.test_crm import DAY, NOW, seed_crm


def _orders(home, rows):
    """rows: (order_id, buyer_id, username, price, days_ago, status)."""
    con = sqlite3.connect(home / "mannbot" / "data" / "mannbot.sqlite3")
    for oid, bid, name, price, ago, st in rows:
        t = NOW - ago * DAY
        delivered = st in ("completed", "delivered")
        con.execute("INSERT INTO orders(order_id,buyer_username,buyer_id,description,quantity,keys_qty,price,currency,status,"
                    "created_at,paid_at,delivered_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, name, bid, "Mann Co. Supply Crate Key", 1, 1, price, "₽", st, t, t,
                     t + 600 if delivered else None, t + 3600 if st == "completed" else None))
    con.commit()
    con.close()


def test_segments(tmp_path):
    seed_crm(tmp_path)
    _orders(tmp_path, [
        ("B1", 20, "vip", 3000, 5, "completed"), ("B2", 20, "vip", 3000, 2, "completed"),
        ("C1", 21, "regular", 200, 20, "completed"), ("C2", 21, "regular", 200, 10, "completed"),
        ("D1", 22, "newbie", 200, 3, "completed"),
        ("E1", 23, "sleepy", 900, 70, "completed"), ("E2", 23, "sleepy", 900, 45, "completed"),
        ("F1", 24, "gone", 200, 120, "completed"),
        ("G1", 25, "waiting", 200, 0.1, "awaiting_link"),
    ])
    c = Center(tmp_path)
    crm = c.crm
    seg = {r["username"]: r["segment"]["key"] for r in crm.customers(now=NOW)["rows"]}
    assert seg == {"vip": "vip", "regular": "regular", "newbie": "new", "sleepy": "sleeping", "gone": "lost",
                   "waiting": "prospect", "buyer": "risky", "twin": "prospect"}
    d = crm.customers(segment="sleeping", now=NOW)
    assert d["total"] == 1 and d["segments"]["vip"] == 1 and d["rows"][0]["segment"]["why"].startswith("последняя покупка 45")
    c.cfg.save({"crm": {"sleep_days": 60}})                                    # пороги из center.yaml
    assert crm.customer_brief(23)["segment"]["key"] == "regular"
    c.cfg.save({"crm": {"sleep_days": 30}})

    b = crm.segments_brief(NOW)
    assert b["counts"]["sleeping"] == 1 and b["sleeping_top"][0]["username"] == "sleepy" and b["sleeping_spent"] == 1800
    assert b["new_7d"] == 3 and b["repeat_rate"] == 66.7                     # vip, newbie, buyer; 4 из 6 купили ещё раз
    lines = crm.segment_lines(NOW)
    assert "VIP 1, постоянных 1" in lines[0] and "Уснули 1" in lines[1] and "sleepy" in lines[1]

    async def go():
        report = await c.monitor.weekly_report()
        assert "Уснули 1" in report
        f = await c.coordinator.facts()
        assert f["customers"]["counts"]["sleeping"] == 1
        res, _ = await StubBackend().complete(f"<facts>{json.dumps(f)}</facts>", {})
        assert any("уснувших" in r["title"] for r in res["recommendations"])
    asyncio.run(go())
    c.events.close()


def test_winback_text_for_sleeping(tmp_path):
    seed_crm(tmp_path)
    _orders(tmp_path, [("E1", 23, "sleepy", 900, 70, "completed"), ("E2", 23, "sleepy", 900, 45, "completed"),
                       ("D1", 22, "newbie", 200, 3, "completed")])
    c = Center(tmp_path)
    card = c.crm.customer(23)
    assert card["segment"]["key"] == "sleeping" and card["winback"].startswith("Здравствуйте, sleepy!")
    assert "winback" not in c.crm.customer(22)
    c.cfg.save({"crm": {"winback_text": "Привет, {name}, скидка 5%"}})
    assert c.crm.customer(23)["winback"] == "Привет, sleepy, скидка 5%"
    c.events.close()


def test_cohorts(tmp_path):
    import time as _t
    seed_crm(tmp_path)                                    # заказы seed_crm — больше года назад относительно now ниже
    lt = _t.localtime(NOW + 400 * DAY)
    now = _t.mktime((lt.tm_year, lt.tm_mon, 15, 12, 0, 0, 0, 0, -1))
    m = lambda back: (NOW - now) / DAY + back * 30.4    # noqa: E731 — «дней назад от NOW» для заказа back месяцев до now
    _orders(tmp_path, [
        ("K1", 30, "a", 500, m(2), "completed"), ("K2", 30, "a", 300, m(1), "completed"),   # когорта −2, вернулся через 1
        ("K3", 31, "b", 200, m(2), "completed"),                                           # когорта −2, не вернулся
        ("K4", 32, "c", 400, m(2), "refunded"), ("K5", 32, "c", 100, m(0), "completed"),    # возврат не в счёт: когорта 0
    ])
    c = Center(tmp_path)
    d = c.crm.cohorts(6, now=now)
    rows = {r["month"]: r for r in d["rows"]}
    assert len(d["rows"]) == 6
    first = d["rows"][-3]
    assert first["new"] == 2 and first["retention"] == [50.0, 0.0] and first["repeat"] == 50.0 and first["ltv"] == 500.0
    assert d["rows"][-1]["new"] == 1 and d["rows"][-1]["retention"] == []
    assert d["rows"][-2]["new"] == 0 and d["rows"][-2]["retention"] == [None]
    assert sum(r["new"] for r in rows.values()) == 3


def test_customers_csv_export(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from center.web import make_app
    seed_crm(tmp_path)
    _orders(tmp_path, [("S1", 40, "csvbuyer", 700, 3, "completed"), ("S2", 40, "csvbuyer", 300, 1, "completed")])
    c = Center(tmp_path)
    rows = c.crm.customer_export_rows(q="csvbuyer")
    assert rows[0][:3] == ["ID FunPay", "Ник", "Сегмент"] and len(rows) == 2
    assert rows[1][0] == 40 and rows[1][1] == "csvbuyer" and rows[1][3] == 2 and rows[1][6] == 1000

    async def go():
        token = c.cfg.ensure_token()
        async with TestClient(TestServer(make_app(c))) as cl:
            await cl.get(f"/login?token={token}", allow_redirects=False)
            r = await cl.get("/api/crm/customers.csv?q=csvbuyer")
            assert r.status == 200 and "crm-customers-" in r.headers["Content-Disposition"]
            text = (await r.read()).decode("utf-8-sig")
            assert "csvbuyer" in text and text.count("\n") >= 2
    asyncio.run(go())
    c.events.close()
