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
