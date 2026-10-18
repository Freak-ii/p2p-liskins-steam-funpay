import asyncio
import time

from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.web import make_app
from tests.test_analytics import seed


def test_weekly_report_compares_weeks(tmp_path):
    seed(tmp_path)

    async def go():
        c = Center(tmp_path)
        text = await c.monitor.weekly_report()
        # за неделю: 2 продажи LisBot (+3 $ = 270 ₽) и 2 заказа MannBot (+90 ₽); прошлая неделя пустая
        assert "Сделок 4" in text and "Прибыль 360 ₽" in text and "к прошлой" not in text
        assert "AK-47 | Redline — +$3.00 за 2 шт" in text
        assert "Склад MannBot: 10 ключей" in text
        token = c.cfg.ensure_token()
        async with TestClient(TestServer(make_app(c))) as cl:
            await cl.get(f"/login?token={token}", allow_redirects=False)
            r = await (await cl.get("/api/monitor/report?period=week")).json()
            assert "Неделя центра" in r["text"]
        c.events.close()
    asyncio.run(go())


def test_weekly_due_on_configured_weekday(tmp_path):
    c = Center(tmp_path)
    monday_10 = time.mktime((2026, 10, 19, 10, 0, 0, 0, 0, -1))
    c.monitor.clock = lambda: monday_10
    assert c.monitor.weekly_due()
    c.monitor.kv_set("weekly_report_date", "2026-10-19")
    assert not c.monitor.weekly_due()
    c.monitor.clock = lambda: monday_10 + 86400                     # вторник
    assert not c.monitor.weekly_due()
    c.cfg.save({"monitor": {"weekly_report": {"weekday": 1}}})
    assert c.monitor.weekly_due()
    c.events.close()


def test_month_goal_progress_and_forecast(tmp_path):
    import datetime as dt
    seed(tmp_path)

    async def go():
        c = Center(tmp_path)
        now = time.time()
        g = await c.analytics.goal(now)
        assert g["goal"] is None and g["progress"] is None
        start = dt.datetime.fromtimestamp(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        expected = sum(p for p, t in ((180.0, now - 2 * 86400), (90.0, now - 86400), (60.0, now - 86400), (30.0, now - 2 * 86400))
                       if t >= start)
        assert abs(g["profit"] - expected) < 1                          # LisBot +2/+1 $ ×90, MannBot 60/30 ₽
        c.analytics.save_goal(10000)
        g = await c.analytics.goal(now)
        assert g["goal"] == 10000 and g["progress"] == round(g["profit"] / 100, 1)
        assert g["forecast"] >= g["profit"]
        token = c.cfg.ensure_token()
        async with TestClient(TestServer(make_app(c))) as cl:
            await cl.get(f"/login?token={token}", allow_redirects=False)
            assert (await cl.post("/api/analytics/goal", json={"goal": -5})).status == 400
            r = await (await cl.post("/api/analytics/goal", json={"goal": 20000})).json()
            assert r["goal"] == 20000
        assert "Цель месяца" in await c.monitor.weekly_report()
        c.events.close()
    asyncio.run(go())


def test_monthly_report_and_due(tmp_path):
    seed(tmp_path)
    now = time.time()
    lt = time.localtime(now)
    y, m = (lt.tm_year, lt.tm_mon + 1) if lt.tm_mon < 12 else (lt.tm_year + 1, 1)
    first_10 = time.mktime((y, m, 1, 10, 0, 0, 0, 0, -1))           # 1-е число следующего месяца, 10:00

    if lt.tm_mday <= 2:                                              # сделки сида могли попасть в позапрошлый месяц
        return

    async def go():
        c = Center(tmp_path)
        c.monitor.clock = lambda: first_10
        c.analytics.save_goal(300)
        text = await c.monitor.monthly_report()
        assert "Итоги месяца" in text and "Сделок 4" in text and "Прибыль 360 ₽" in text
        assert "✅ Цель месяца 300 ₽: выполнена" in text and "Аптайм за 30 дней" in text
        c.analytics.save_goal(1000)
        assert "❌ Цель месяца 1 000 ₽: не выполнена" in (await c.monitor.monthly_report()).replace(" ", " ")
        token = c.cfg.ensure_token()
        async with TestClient(TestServer(make_app(c))) as cl:
            await cl.get(f"/login?token={token}", allow_redirects=False)
            r = await (await cl.get("/api/monitor/report?period=month")).json()
            assert "Итоги месяца" in r["text"]
        assert c.monitor.monthly_due()
        c.monitor.kv_set("monthly_report_month", time.strftime("%Y-%m", time.localtime(first_10)))
        assert not c.monitor.monthly_due()
        c.monitor.clock = lambda: first_10 + 86400                   # 2-е число
        c.monitor.kv_set("monthly_report_month", "")
        assert not c.monitor.monthly_due()
        c.events.close()
    asyncio.run(go())
