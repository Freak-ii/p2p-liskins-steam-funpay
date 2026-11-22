import asyncio
import json
import sqlite3
import time

from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.coordinator import BackendError, StubBackend
from center.web import make_app
from center.workers import CRASHED, RUNNING
from test_analytics import seed

NOW = time.time()


def _center(tmp_path, **coord):
    c = Center(tmp_path)
    c.cfg.save({"analytics": {"usd_rub": 90}, **({"coordinator": coord} if coord else {})})
    sent, calls = [], []

    async def fake_send(text):
        sent.append(text)
        return True, "sent"
    c.monitor.send_telegram = fake_send
    for name in ("lisbot", "mannbot"):
        w = c.workers[name]
        w.state = RUNNING

        async def control(action, _n=name):
            calls.append((_n, action))
            return "готово"
        w.control = control

    async def lis_settings(changes):
        calls.append(("lisbot", "settings", changes))
        return "настройки сохранены"
    c.coordinator._lis_settings = lis_settings
    return c, sent, calls


def _spend(tmp_path, usd, minutes_ago=10):
    con = sqlite3.connect(tmp_path / "lisbot" / "data" / "bot.sqlite")
    t = int((NOW - minutes_ago * 60) * 1000)
    con.execute("INSERT INTO items(name,mode,status,dry_run,buy_price_usd,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("Spike", "rtba", "bought", 0, usd, t, t))
    con.commit()
    con.close()


def test_permission_tiers(tmp_path):
    c, _, _ = _center(tmp_path, permissions={"change_limits": "auto", "withdraw": "propose", "restart_bot": "auto",
                                             "pause_buying": "propose", "ack_alerts": "off", "set_live_mode": "manual"})
    co = c.coordinator
    assert co.tier("change_limits") == "confirm"      # лимиты нельзя опустить ниже подтверждения
    assert co.tier("withdraw") == "manual"            # вывод денег — только вручную
    assert co.tier("restart_bot") == "propose"        # auto разрешён только паузам
    assert co.tier("pause_buying") == "propose"       # а саму паузу можно ослабить до предложения
    assert co.tier("ack_alerts") == "off"
    assert co.tier("set_live_mode") == "manual"       # строже можно
    c2, _, _ = _center(tmp_path / "b")
    assert c2.coordinator.tier("pause_buying") == "auto" and c2.coordinator.tier("set_live_mode") == "confirm"
    c.events.close()
    c2.events.close()


def test_spend_spike_pauses_buying_itself(tmp_path):
    seed(tmp_path)
    _spend(tmp_path, 120.0)

    async def go():
        c, sent, calls = _center(tmp_path)
        co = c.coordinator
        found = await co.check()
        assert [a["code"] for a in found] == ["lis_spend_spike"]
        assert calls == [("lisbot", "buyPause")]
        act = co.decisions(kind="action")[0]
        assert act["status"] == "done" and act["tool"] == "pause_buying" and "$120.00" in act["reason"]
        assert "сделал сам" in sent[-1]
        assert any(e["kind"] == "coordinator.action" for e in c.events.recent(20))
        await co.check()                               # та же аномалия в пределах cooldown — ничего
        assert calls == [("lisbot", "buyPause")]
        c.events.close()
    asyncio.run(go())


def test_spike_when_already_paused_and_when_auto_disabled(tmp_path):
    seed(tmp_path)
    _spend(tmp_path, 120.0)

    async def go():
        c, sent, calls = _center(tmp_path)
        c.summaries["lisbot"] = {"bot_state": "running", "flags": ["закупка на паузе"]}
        await c.coordinator.check()
        assert calls == [] and "уже на паузе" in sent[-1]
        c.events.close()
        c2, sent2, calls2 = _center(tmp_path / "b", permissions={"pause_buying": "propose"})
        seed(tmp_path / "b")
        _spend(tmp_path / "b", 120.0)
        await c2.coordinator.check()
        assert calls2 == []
        p = c2.coordinator.decisions(kind="proposal", status="proposed")
        assert p and p[0]["tool"] == "pause_buying" and "Предлагаю" in sent2[-1]
        c2.events.close()
    asyncio.run(go())


def test_other_rules_only_propose(tmp_path):
    seed(tmp_path)
    con = sqlite3.connect(tmp_path / "lisbot" / "data" / "bot.sqlite")
    for i in range(4):
        t = int((NOW - 3600 * (i + 1)) * 1000)
        con.execute("INSERT INTO items(name,mode,status,dry_run,buy_price_usd,sold_at,sold_usd,profit_usd,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)", ("Loss", "rtba", "sold", 0, 1.0, t, 0.0, -3.0, t - 86400000, t))
    con.commit()
    con.close()
    con = sqlite3.connect(tmp_path / "mannbot" / "data" / "mannbot.sqlite3")
    for i in range(3):
        con.execute("INSERT INTO orders(order_id,status,created_at,price,keys_qty,quantity,currency) VALUES(?,?,?,?,?,?,?)",
                    (f"R{i}", "refunded", NOW - 600, 200, 1, 1, "₽"))
    con.commit()
    con.close()

    async def go():
        c, sent, calls = _center(tmp_path)
        c.workers["mannbot"].state = CRASHED
        found = {a["code"] for a in await c.coordinator.check()}
        assert found == {"lis_loss_streak", "mann_refunds", "mannbot_crashed"}
        assert calls == [("lisbot", "buyPause")]       # сам — только пауза закупок
        tools = {p["tool"] for p in c.coordinator.decisions(kind="proposal", status="proposed")}
        assert tools == {"pause_bot", "restart_bot"}
        c.events.close()
    asyncio.run(go())


class FakeAI:
    model = "fake"

    def __init__(self, data=None, error=None):
        self.data, self.error = data, error

    async def complete(self, user, schema):
        if self.error:
            raise BackendError(self.error)
        assert "<facts>" in user and json.loads(user.split("<facts>")[1].split("</facts>")[0])["bots"]
        return self.data, {"model": "fake"}


def test_ai_recommendations_never_execute_and_approval_rules(tmp_path):
    seed(tmp_path)

    async def go():
        c, sent, calls = _center(tmp_path)
        co = c.coordinator
        co.backend_override = FakeAI({"summary": "Всё спокойно.", "highlights": ["прибыль растёт"], "recommendations": [
            {"title": "Пауза закупок", "why": "тест", "tool": "pause_buying", "args_json": "{}"},
            {"title": "Поднять лимит", "why": "оборот растёт", "tool": "change_limits",
             "args_json": json.dumps({"changes": {"strategy.maxSpendPerDayUsd": 300}})},
            {"title": "Вывести прибыль", "why": "накопилось", "tool": "withdraw", "args_json": '{"amount": 5000}'},
            {"title": "Лимит через стратегию", "why": "обход", "tool": "change_strategy",
             "args_json": json.dumps({"changes": {"strategy.maxSpendPerDayUsd": 1e6}})},
            {"title": "Посмотреть отзывы", "why": "просто совет", "tool": "none", "args_json": "{}"},
        ]})
        b = await co.briefing()
        assert b["result"] == "Всё спокойно." and b["source"] == "ai"
        assert calls == []                                       # ИИ сам ничего не делает, даже auto-инструмент
        props = {p["tool"]: p for p in co.decisions(kind="proposal", status="proposed")}
        assert set(props) == {"pause_buying", "change_limits", "withdraw"}   # обход лимитов через стратегию отброшен
        assert props["change_limits"]["tier"] == "confirm" and props["withdraw"]["tier"] == "manual"
        assert "Брифинг координатора" in sent[-1] and "нужно подтверждение" in sent[-1]

        ok, _ = await co.decide(props["pause_buying"]["id"], True)
        assert ok and calls == [("lisbot", "buyPause")]
        ok, msg = await co.decide(props["change_limits"]["id"], True)
        assert not ok and "подтверждения" in msg and len(calls) == 1
        ok, _ = await co.decide(props["change_limits"]["id"], True, confirmed=True)
        assert ok and calls[-1] == ("lisbot", "settings", {"strategy.maxSpendPerDayUsd": 300})
        ok, msg = await co.decide(props["withdraw"]["id"], True, confirmed=True)
        assert ok and "вручную" in msg and co.get(props["withdraw"]["id"])["status"] == "manual"
        ok, msg = await co.decide(props["withdraw"]["id"], True, confirmed=True)
        assert not ok and "закрыто" in msg

        co.backend_override = FakeAI(error="Claude API недоступен (сеть)")
        a = await co.ask("Как дела?")
        assert a["source"] == "stub" and a["data"]["meta"]["error"] == "Claude API недоступен (сеть)"
        c.events.close()
    asyncio.run(go())


def test_proposals_expire_and_dedupe(tmp_path):
    async def go():
        c, _, calls = _center(tmp_path)
        co = c.coordinator
        p1 = await co.act_or_propose("restart_bot", {"bot": "lisbot"}, reason="r", source="ai")
        p2 = await co.act_or_propose("restart_bot", {"bot": "lisbot"}, reason="r", source="ai")
        assert p1["id"] == p2["id"]
        co.clock = lambda: time.time() + 25 * 3600
        ok, msg = await co.decide(p1["id"], True)
        assert not ok and "expired" in msg and calls == []
        c.events.close()
    asyncio.run(go())


def test_stub_briefing_and_api(tmp_path):
    seed(tmp_path)

    async def go():
        c, sent, calls = _center(tmp_path)
        c.cfg.save({"coordinator": {"ai": {"api_key": "sk-ant-test-123456789"}}})
        assert c.coordinator.status()["mode"]            # без пакета anthropic — не падает, а пишет причину
        c.coordinator.backend_override = StubBackend()
        client = TestClient(TestServer(make_app(c)))
        await client.start_server()
        h = {"Authorization": f"Bearer {c.cfg.ensure_token()}"}
        s = await (await client.get("/api/coordinator/settings", headers=h)).json()
        assert s["ai"]["api_key_set"] and s["ai"]["api_key_hint"] == "…6789" and "sk-ant" not in json.dumps(s)
        r = await client.post("/api/coordinator/settings", headers=h, json={"permissions": {"withdraw": "auto"}})
        assert r.status == 200
        s = await (await client.get("/api/coordinator/settings", headers=h)).json()
        assert {p["tool"]: p["tier"] for p in s["permissions"]}["withdraw"] == "manual"
        assert (await client.post("/api/coordinator/settings", headers=h, json={"permissions": {"x": "auto"}})).status == 400

        r = await (await client.post("/api/coordinator/briefing", headers=h, json={"send": True})).json()
        assert r["ok"] and "Базовая сводка без ИИ" in r["briefing"]["result"]
        assert any("За 7 дней" in x for x in r["briefing"]["data"]["highlights"])
        assert "Брифинг координатора" in sent[-1]
        st = await (await client.get("/api/coordinator/status", headers=h)).json()
        assert st["briefing"]["id"] == r["briefing"]["id"] and st["journal"]
        props = st["proposals"]
        r = await (await client.post("/api/coordinator/ask", headers=h, json={"question": "Что с прибылью?"})).json()
        assert r["ok"] and "ИИ выключен" in r["answer"]["result"]
        r = await client.post("/api/coordinator/ask", headers=h, json={"question": " "})
        assert r.status == 400
        r = await (await client.post("/api/coordinator/check", headers=h)).json()
        assert r["ok"]
        if props:
            r = await client.post(f"/api/coordinator/decisions/{props[0]['id']}/reject", headers=h)
            assert r.status == 200
        assert (await client.post("/api/coordinator/decisions/99999/approve", headers=h)).status == 409
        await client.close()
        c.events.close()
    asyncio.run(go())


def test_stuck_orders_and_buy_stall_only_notify(tmp_path):
    seed(tmp_path)
    con = sqlite3.connect(tmp_path / "mannbot" / "data" / "mannbot.sqlite3")
    con.execute("INSERT INTO orders(order_id,buyer_username,status,created_at,paid_at,offer_sent_at,price,keys_qty,quantity,currency) "
                "VALUES('S1','slow','offer_sent',?,?,?,200,1,1,'₽')", (NOW - 4 * 3600, NOW - 4 * 3600, NOW - 3 * 3600))
    con.execute("INSERT INTO orders(order_id,buyer_username,status,created_at,price,keys_qty,quantity,currency) "
                "VALUES('S2','check','pending_approval',?,200,1,1,'₽')", (NOW - 2 * 3600,))
    con.execute("INSERT INTO orders(order_id,buyer_username,status,created_at,offer_sent_at,price,keys_qty,quantity,currency) "
                "VALUES('S3','fresh','offer_sent',?,?,200,1,1,'₽')", (NOW - 1800, NOW - 1800))
    con.commit()
    con.close()
    con = sqlite3.connect(tmp_path / "lisbot" / "data" / "bot.sqlite")
    for i in range(10):                                     # 10 покупок за неделю, последняя — 30 ч назад
        t = int((NOW - 30 * 3600 - i * 12 * 3600) * 1000)
        con.execute("INSERT INTO items(name,mode,status,dry_run,buy_price_usd,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    ("Old", "rtba", "listed", 0, 1.0, t, t))
    con.commit()
    con.close()

    async def go():
        c, sent, calls = _center(tmp_path)
        assert "lis_buy_stall" not in {a["code"] for a in c.coordinator.detect()}   # режим неизвестен — молчим
        c.summaries["lisbot"] = {"dry_run": False}
        found = {a["code"]: a for a in await c.coordinator.check()}
        assert {"mann_stuck", "lis_buy_stall"} <= set(found)
        assert "S1" in found["mann_stuck"]["reason"] and "S2" in found["mann_stuck"]["reason"]
        assert "S3" not in found["mann_stuck"]["reason"] and found["mann_stuck"]["title"].startswith("MannBot: 2")
        assert "30 ч назад" in found["lis_buy_stall"]["reason"]
        assert [r["order_id"] for r in c.coordinator.stuck_orders()] == ["S1", "S2"]
        (text, _), = await c.tg.handle_command("/orders")
        assert "S1" in text and "трейд не принят" in text and "ждёт вашей проверки" in text
        assert calls == [] and not c.coordinator.decisions(kind="proposal", status="proposed")   # ничего не делает сам
        assert any("застряли" in t for t in sent) and any("перестал закупать" in t for t in sent)
        n = len(sent)
        await c.coordinator.check()
        assert len(sent) == n                               # повтор не раньше cooldown
        c.summaries["lisbot"] = {"dry_run": False, "paused": True}
        assert "lis_buy_stall" not in {a["code"] for a in c.coordinator.detect()}   # на паузе — это нормально
        c.events.close()
    asyncio.run(go())


def test_losing_item_type_recommendation():
    async def go():
        f = {"bots": [], "lis_types_30d": [{"type": "Ножи", "sold": 6, "profit": -12.5, "roi": -2.1, "names": 2, "bought": 6},
                                           {"type": "Винтовки", "sold": 9, "profit": 4.0, "roi": 3.0, "names": 3, "bought": 9},
                                           {"type": "Наклейки", "sold": 2, "profit": -1.0, "roi": -5.0, "names": 1, "bought": 2}]}
        res, _ = await StubBackend().complete(f"<facts>{json.dumps(f)}</facts>", {})
        titles = [r["title"] for r in res["recommendations"]]
        assert "Пересмотреть закупку LisBot: ножи" in titles
        assert not any("винтовки" in t or "наклейки" in t for t in titles)     # в плюсе / мало продаж
    asyncio.run(go())


def test_raise_min_roi_recommendation():
    async def go():
        cut = [{"min_roi": 0.08, "deals": 20, "profit": 40.0, "losers": 5, "roi": 4.0, "current": True},
               {"min_roi": 0.1, "deals": 16, "profit": 46.0, "losers": 1, "roi": 6.0, "current": False},
               {"min_roi": 0.12, "deals": 10, "profit": 44.0, "losers": 0, "roi": 7.0, "current": False},
               {"min_roi": 0.15, "deals": 19, "profit": 90.0, "losers": 0, "roi": 9.0, "current": False}]   # отсекает 1 — мало
        res, _ = await StubBackend().complete(f"<facts>{json.dumps({'bots': [], 'lis_roi_cut_30d': cut})}</facts>", {})
        r = next(r for r in res["recommendations"] if r["title"].startswith("Поднять минимальный ROI"))
        assert r["title"] == "Поднять минимальный ROI LisBot до 10%" and r["tool"] == "change_strategy"
        assert json.loads(r["args_json"]) == {"changes": {"strategy.minRoi": 0.1}} and "-6.00 $" in r["why"]
        cut[1]["profit"] = cut[2]["profit"] = 30.0                       # выше порог — меньше прибыли: не советуем
        res, _ = await StubBackend().complete(f"<facts>{json.dumps({'bots': [], 'lis_roi_cut_30d': cut})}</facts>", {})
        assert not any(r["title"].startswith("Поднять минимальный ROI") for r in res["recommendations"])
    asyncio.run(go())


def test_restock_and_key_bridge_recommendations():
    async def go():
        inv = {"mannbot": {"level": "low", "stock": 2, "days_left": 1.0, "restock": 5, "target_days": 14, "bridge_coming": 3},
               "bridge": {"enabled": True, "failed": 2}}
        res, _ = await StubBackend().complete(f"<facts>{json.dumps({'bots': [], 'inventory': inv})}</facts>", {})
        recs = {r["title"]: r for r in res["recommendations"]}
        why = recs["Пополнить склад ключей MannBot"]["why"]
        assert "Докупите 5 ключей" in why and "Ещё 3 ключей едут по мосту" in why and "включите мост" not in why
        assert "accept_gifts_from" in recs["Проверить приём ключей MannBot"]["why"]
        inv["bridge"] = {"enabled": False, "failed": 0}
        inv["mannbot"]["bridge_coming"] = 0
        res, _ = await StubBackend().complete(f"<facts>{json.dumps({'bots': [], 'inventory': inv})}</facts>", {})
        recs = {r["title"]: r for r in res["recommendations"]}
        assert "«Мост ключей» и покупку ключей" in recs["Пополнить склад ключей MannBot"]["why"]
        assert "Проверить приём ключей MannBot" not in recs
    asyncio.run(go())


def test_ack_alerts_accepts_every_center_bot():
    """Сводка предлагает «Разобрать тревоги StarsBot» — проверка аргументов не должна это отклонять."""
    from center.coordinator import TOOLS, ToolError
    check = TOOLS["ack_alerts"][2]
    for bot in ("lisbot", "mannbot", "starsbot", "tggift", "shopbot"):
        assert check({"bot": bot}) == {"bot": bot}
    import pytest
    with pytest.raises(ToolError):
        check({"bot": "nope"})
