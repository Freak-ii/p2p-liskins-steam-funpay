import asyncio
import json
import sqlite3
import time

import yaml
from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.demo import lisbot_schema
from center.web import make_app

NOW = time.time()
DAY = 86400
ms = lambda t: int(t * 1000)  # noqa: E731


def seed(home, *, stock=10, sold_keys=(4, 3)):
    lis = home / "lisbot" / "data"
    lis.mkdir(parents=True)
    con = sqlite3.connect(lis / "bot.sqlite")
    con.executescript(lisbot_schema())
    items = [  # name, status, buy, expected, days ago, relists, dry
        ("AK", "listed", 10.0, 12.0, 2, 0, 0),             # рынок 11.5 → +1.5
        ("AWP", "ready", 50.0, 55.0, 30, 1, 0),           # рынок 45 → −5, долго лежит
        ("Old", "protected", 20.0, 22.0, 5, 0, 0),        # оценка устарела → прогноз при покупке 22
        ("Howl", "listed", 100.0, 110.0, 4, 5, 0),        # нет оценки → прогноз, лимит перевыставлений
        ("Sold", "sold", 5.0, 6.0, 9, 0, 0),              # продан — не склад
        ("Dry", "listed", 5.0, 6.0, 1, 0, 1),             # тестовый — не склад
    ]
    for n, st, buy, exp, ago, rel, dry in items:
        con.execute("INSERT INTO items(name, status, dry_run, buy_price_usd, expected_net_usd, relists, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)", (n, st, dry, buy, exp, rel, ms(NOW - ago * DAY), ms(NOW)))
    for n, ts, net in (("AK", NOW - 3600, 11.5), ("AWP", NOW - 3600, 45.0), ("Old", NOW - 10 * DAY, 5.0)):
        con.execute("INSERT INTO valuations(name, ts, ok, net_usd, trend) VALUES(?,?,1,?,-0.02)", (n, ms(ts), net))
    con.execute("INSERT INTO kv VALUES('fx', ?, 0)", (json.dumps({"rate": 90.0, "ts": ms(NOW)}),))
    con.commit()
    con.close()

    mann = home / "mannbot"
    (mann / "data").mkdir(parents=True)
    (mann / "config.yaml").write_text(yaml.safe_dump({"rtba": {"cost_per_key": 150}}), encoding="utf-8")
    con = sqlite3.connect(mann / "data" / "mannbot.sqlite3")
    con.executescript("""
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE orders (order_id TEXT PRIMARY KEY, keys_qty INTEGER, status TEXT, delivered_at REAL);
    """)
    for i, k in enumerate(sold_keys):
        con.execute("INSERT INTO orders VALUES(?,?,?,?)", (f"A{i}", k, "completed", NOW - (13 - i) * DAY))
    con.execute("INSERT INTO orders VALUES('R', 50, 'refunded', ?)", (NOW - DAY,))
    con.execute("INSERT INTO kv VALUES('last_stock', ?)", (str(stock),))
    con.commit()
    con.close()


def test_lisbot_stock_valued_and_flagged(tmp_path):
    seed(tmp_path)
    c = Center(tmp_path)
    inv = c.inventory.lisbot()
    assert inv["n"] == 4 and inv["cost"] == 180.0
    by = {i["name"]: i for i in inv["items"]}
    assert by["AK"]["value"] == 11.5 and by["AK"]["pnl"] == 1.5 and by["AK"]["value_source"] == "рынок"
    assert by["AWP"]["flags"] == ["долго лежит", "в минусе"] and by["AWP"]["pnl_pct"] == -10.0
    assert by["Old"]["value"] == 22.0 and by["Old"]["value_source"] == "прогноз при покупке"
    assert by["Howl"]["flags"] == ["лимит перевыставлений"]
    assert inv["pnl"] == 1.5 - 5 + 2 + 10 and inv["losing"] == 1 and inv["old"] == 1 and inv["old_days"] == 21
    assert inv["items"][0]["name"] == "AWP"                           # проблемные и убыточные — сверху
    assert [b["n"] for b in inv["buckets"]] == [3, 0, 0, 1]
    c.cfg.save({"inventory": {"lis_old_days": 3}})
    assert c.inventory.lisbot()["old"] == 3
    c.events.close()


def test_mannbot_days_left_and_low_stock_event(tmp_path):
    seed(tmp_path, stock=1)
    c = Center(tmp_path)
    m = c.inventory.mannbot()
    # 7 ключей за 13 дней (первая выдача 13 дней назад), возврат не считается
    assert m["sold_keys"] == 7 and m["rate_days"] == 13.0 and m["per_day"] == round(7 / 13, 2)
    assert m["days_left"] == round(1 / (7 / 13), 1) and m["level"] == "low" and m["value"] == 150.0
    assert m["restock"] == 7 and m["restock_cost"] == 1050.0              # 7/13 в день × 14 дн − 1 на складе
    msg = c.inventory.check()
    assert "докупить 7 ключей (~1 050 ₽) на 14 дн продаж" in msg and c.inventory.check() is None   # не чаще раза в сутки
    ev = c.events.recent(5)[0]
    assert ev["kind"] == "inventory.low_stock" and ev["source"] == "mannbot"
    assert any("Склад MannBot" in x and x.startswith("⚠") for x in c.inventory.report_lines())
    assert "Склад MannBot" in c.monitor.daily_report()
    c.events.close()


def test_mannbot_no_forecast_without_a_day_of_history(tmp_path):
    seed(tmp_path, stock=1, sold_keys=())
    con = sqlite3.connect(tmp_path / "mannbot" / "data" / "mannbot.sqlite3")
    con.execute("INSERT INTO orders VALUES('N', 30, 'completed', ?)", (NOW - 3600,))   # демо: много продаж за час
    con.commit()
    con.close()
    m = Center(tmp_path).inventory.mannbot()
    assert m["sold_keys"] == 30 and m["per_day"] == 0 and m["days_left"] is None and m["level"] == "ok"
    assert m["restock"] is None


def test_inventory_api_and_coordinator(tmp_path):
    seed(tmp_path, stock=50)

    async def go():
        c = Center(tmp_path)
        assert c.inventory.mannbot()["level"] == "ok" and c.inventory.check() is None
        token = c.cfg.ensure_token()
        async with TestClient(TestServer(make_app(c))) as cl:
            await cl.get(f"/login?token={token}", allow_redirects=False)
            r = await (await cl.get("/api/inventory")).json()
            assert r["lisbot"]["n"] == 4 and r["mannbot"]["stock"] == 50
            assert r["total"]["USD"]["pnl"] == 8.5 and r["total"]["RUB"]["pnl"] == 765.0
        f = await c.coordinator.facts()
        assert f["inventory"]["lisbot"]["losing"] == 1 and f["inventory"]["lisbot"]["flagged_items"][0]["name"] == "AWP"
        b = await c.coordinator.briefing()
        assert any("залежавшиеся" in d["title"] for d in c.coordinator.decisions(20, kind="proposal")), b
        c.events.close()
    asyncio.run(go())


def test_low_lis_balance_only_in_live_mode(tmp_path):
    seed(tmp_path)
    con = sqlite3.connect(tmp_path / "lisbot" / "data" / "bot.sqlite")
    con.execute("INSERT INTO kv VALUES('balance.last', ?, 0)", (json.dumps({"balance": 4.5, "ts": ms(NOW)}),))
    con.commit()
    con.close()
    c = Center(tmp_path)
    c.summaries["lisbot"] = {"dry_run": True}
    assert not c.inventory.lis_balance()["low"] and c.inventory.check_balance() is None
    c.summaries["lisbot"] = {"dry_run": False}
    assert c.inventory.check_balance() and c.inventory.check_balance() is None    # раз в сутки
    ev = c.events.recent(3)[0]
    assert ev["kind"] == "inventory.low_balance" and ev["source"] == "lisbot"
    assert any("Баланс LIS-SKINS $4.50" in x for x in c.inventory.report_lines())
    c.events.close()


def test_market_drop_rule(tmp_path):
    seed(tmp_path)
    c = Center(tmp_path)
    assert "lis_market_drop" not in {a["code"] for a in c.coordinator.detect()}    # AWP −10% — меньше порога 15%
    c.cfg.save({"coordinator": {"anomaly": {"lis_drop_pct": 8}}})
    an = next(a for a in c.coordinator.detect() if a["code"] == "lis_market_drop")
    assert an["tool"] is None and "AWP -10%" in an["reason"] and "−$5.00" in an["reason"]
    assert "Old" not in an["reason"]                                               # устаревшая оценка не считается
    c.events.close()


def test_sell_time_forecast(tmp_path):
    seed(tmp_path)
    con = sqlite3.connect(tmp_path / "lisbot" / "data" / "bot.sqlite")
    for name, hold, ago in (("AK", 3, 5), ("AK", 5, 20), ("X", 10, 30), ("Y", 12, 40)):   # AK продаётся за 3–5 дн
        con.execute("INSERT INTO items(name, status, dry_run, buy_price_usd, created_at, sold_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                    (name, "sold", 0, 1.0, ms(NOW - (ago + hold) * DAY), ms(NOW - ago * DAY), ms(NOW)))
    con.commit()
    con.close()
    c = Center(tmp_path)
    inv = c.inventory.lisbot()
    by = {i["name"]: i for i in inv["items"]}
    assert by["AK"]["sell_in"] == 3.0                        # медиана AK 5 дн, лежит 2 дн
    assert by["Old"]["sell_in"] == 5.0                       # своей истории нет — общая медиана 10 дн, лежит 5
    assert by["AWP"]["sell_in"] == 0.0                       # уже дольше обычного
    f = inv["forecast"]
    assert f["typical_days"] == 10.0 and f["n7"] == 4        # AK, Old, AWP и Howl (через 6 дн)
    assert any("Ожидается продаж LisBot за 7 дней" in x for x in c.inventory.report_lines())
    c.events.close()


def test_key_bridge_state_and_restock(tmp_path):
    seed(tmp_path, stock=1)
    (tmp_path / "lisbot" / "config.json").write_text(json.dumps({
        "steam": {"walletCurrencyCode": "RUB"},
        "keyBridge": {"enabled": True, "buy": {"enabled": True, "maxKeyPrice": 160, "keysPerDay": 5}}}), encoding="utf-8")
    state = {"buyOrder": {"id": "1"}, "totals": {"bought": 6, "spentCents": 90000, "sent": 5, "delivered": 2},
             "ownOffers": {"A": {"assetIds": ["1", "2"], "open": False, "finalState": "Accepted"},
                           "B": {"assetIds": ["3"], "open": True},
                           "C": {"assetIds": ["4", "5"], "open": False, "finalState": "Declined", "ts": ms(NOW - 3600)},
                           "D": {"assetIds": ["6"], "open": False, "finalState": "Expired", "ts": ms(NOW - 5 * DAY)}}}
    con = sqlite3.connect(tmp_path / "lisbot" / "data" / "bot.sqlite")
    con.execute("INSERT INTO kv VALUES('keybridge', ?, 0)", (json.dumps(state),))
    con.commit()
    con.close()
    c = Center(tmp_path)
    b = c.inventory.bridge()
    assert b["enabled"] and b["buying"] and b["buy_order"] and b["currency"] == "RUB"
    assert (b["bought"], b["delivered"], b["in_offers"], b["failed"]) == (6, 2, 1, 2)
    assert b["waiting"] == 3 and b["coming"] == 4 and b["avg_price"] == 150.0 and b["spent"] == 900.0
    assert b["per_wallet_rub"] is None                                      # нет продаж MannBot — нет оценки
    c.analytics.mann_deals = lambda since=0: [{"currency": "RUB", "qty": 2, "revenue": 400.0, "fee": 40.0},
                                              {"currency": "USD", "qty": 1, "revenue": 3.0, "fee": 0.0}]
    b = c.inventory.bridge()
    assert b["sell_net_per_key"] == 180.0 and b["per_wallet_rub"] == 1.2 and b["price_basis"] == "покупки"
    m = c.inventory.mannbot()
    assert m["bridge_coming"] == 4 and m["restock"] == 3                 # 7/13 × 14 − 1 на складе − 4 в пути
    assert "ещё 4 едут по мосту ключей" in c.inventory.check()
    assert any(x.startswith("Мост ключей: куплено 6, принято MannBot 2, в пути 4") for x in c.inventory.report_lines())
    c.events.close()


def test_key_bridge_off_by_default(tmp_path):
    seed(tmp_path)
    c = Center(tmp_path)
    b = c.inventory.bridge()
    assert not b["enabled"] and b["bought"] == 0 and b["coming"] == 0
    assert not any("Мост ключей" in x for x in c.inventory.report_lines())
    c.events.close()
