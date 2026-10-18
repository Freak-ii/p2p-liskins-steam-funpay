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
