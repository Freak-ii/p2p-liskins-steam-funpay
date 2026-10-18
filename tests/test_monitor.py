import asyncio
import sqlite3
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from center.app import Center
from center.monitor import in_quiet_hours
from center.web import make_app
from center.workers import RUNNING


def _center(tmp_path, monitor=None):
    c = Center(tmp_path)
    if monitor:
        c.cfg.save({"monitor": monitor})
    sent = []

    async def fake_send(text):
        sent.append(text)
        return True, "sent"
    c.monitor.send_telegram = fake_send
    return c, sent


def test_quiet_hours_parsing():
    noon = time.mktime((2026, 10, 17, 12, 0, 0, 0, 0, -1))
    night = time.mktime((2026, 10, 17, 2, 30, 0, 0, 0, -1))
    assert in_quiet_hours("23:00-08:00", night) and not in_quiet_hours("23:00-08:00", noon)
    assert in_quiet_hours("11:00-13:00", noon) and not in_quiet_hours("", noon) and not in_quiet_hours("bad", noon)


def test_dedup_and_quiet_hours(tmp_path):
    async def go():
        c, sent = _center(tmp_path, {"dedup_minutes": 15})
        m = c.monitor
        assert await m.notify("lisbot:worker.exit", "warn", "упал") == "sent"
        assert await m.notify("lisbot:worker.exit", "warn", "упал") == "duplicate"
        assert await m.notify("mannbot:worker.exit", "warn", "другой") == "sent"
        m._last_sent["lisbot:worker.exit"] -= 16 * 60
        assert await m.notify("lisbot:worker.exit", "warn", "снова") == "sent"
        assert "ещё 1 похожих" in sent[-1]
        lt = time.localtime()
        c.cfg.save({"monitor": {"quiet_hours": f"{lt.tm_hour:02d}:00-{(lt.tm_hour + 1) % 24:02d}:00"}})
        assert await m.notify("k1", "warn", "ночью") == "held"
        assert await m.notify("k2", "critical", "срочно") == "sent"      # критичное проходит и в тихие часы
        n = len(sent)
        await m._flush_held()
        assert len(sent) == n                                             # тихие часы ещё идут
        c.cfg.save({"monitor": {"quiet_hours": ""}})
        await m._flush_held()
        assert "За тихие часы (1)" in sent[-1] and "ночью" in sent[-1]
        statuses = [x["status"] for x in m.notifications()]
        assert {"sent", "duplicate", "held"} <= set(statuses)
        c.events.close()
    asyncio.run(go())


def test_events_become_notifications(tmp_path):
    async def go():
        c, sent = _center(tmp_path)
        await c.monitor.start()
        c.events.add("lisbot", "critical", "worker.crashed", "LisBot падает сразу после запуска")
        c.events.add("lisbot", "info", "action", "пауза из панели")                     # не уведомляем
        c.events.add("mannbot", "warn", "worker.exit", "MannBot завершился с кодом 1")
        for _ in range(20):
            await asyncio.sleep(0.02)
        assert len(sent) == 2 and "LisBot падает" in sent[0] and "MannBot · FunPay" in sent[1]
        await c.monitor.stop()
        c.events.close()
    asyncio.run(go())


def test_unclean_stop_detected(tmp_path):
    async def go():
        c, _ = _center(tmp_path)
        c.events.add("center", "info", "center.start", "Центр запущен")    # «упал» без center.stop
        c.monitor.kv_set("last_alive", 1_700_000_000)
        c.events.close()
        c2, sent = _center(tmp_path)
        assert c2.monitor.unclean == 1_700_000_000
        await c2.monitor.start()
        await asyncio.sleep(0.05)
        assert any("завершился аварийно" in s for s in sent)
        await c2.monitor.stop()
        c2.events.add("center", "info", "center.stop", "Центр остановлен")
        c2.events.close()
        c3, _ = _center(tmp_path)
        assert c3.monitor.unclean is None
        c3.events.close()
    asyncio.run(go())


def test_bot_alerts_forwarded_once(tmp_path):
    db = tmp_path / "lisbot" / "data" / "bot.sqlite"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts INTEGER, level TEXT, code TEXT, message TEXT, acked INTEGER)")
    con.execute("INSERT INTO alerts VALUES (1, ?, 'critical', 'old', 'старая тревога', 0)", (int(time.time() * 1000),))
    con.commit()

    async def go():
        c, sent = _center(tmp_path, {"dedup_minutes": 0})
        c.monitor._init_alert_marks()
        assert await c.monitor.check_bot_alerts() == 0                  # старые тревоги не пересылаем
        now = int(time.time() * 1000)
        con.execute("INSERT INTO alerts VALUES (2, ?, 'critical', 'balance', 'баланс ниже лимита', 0)", (now,))
        con.execute("INSERT INTO alerts VALUES (3, ?, 'warn', 'slow', 'медленно', 0)", (now,))
        con.commit()
        assert await c.monitor.check_bot_alerts() == 1
        assert "баланс ниже лимита" in sent[-1] and "balance" in sent[-1]
        assert await c.monitor.check_bot_alerts() == 0
        text = c.monitor.daily_report()
        assert "Отчёт центра за сутки" in text and "critical 2" in text and "не подтверждено" in text
        c.events.close()
    asyncio.run(go())


class _FakeProc:
    returncode = None


def test_sample_history_and_unresponsive(tmp_path):
    async def go():
        c, sent = _center(tmp_path, {"unresponsive_after": 2})

        async def idle():
            await asyncio.Event().wait()
        c.monitor._sample_loop = idle                                    # проверки вызываем вручную
        await c.monitor.start()
        w = c.workers["mannbot"]
        w.proc, w.state, w._want_running = _FakeProc(), RUNNING, True
        answers = iter([True, False, False, False, True])

        async def health():
            return next(answers)
        w.health = health
        for _ in range(5):
            await c.monitor.sample()
        await asyncio.sleep(0.05)
        h = c.monitor.history(1)["bots"]["mannbot"]
        assert abs(h["uptime"] - 0.4) < 1e-9 and h["unresponsive"] == 1 and h["latency_avg"] is not None
        assert any("не отвечает 2 проверки" in s for s in sent) and any("снова отвечает" in s for s in sent)
        assert c.monitor.history(1)["bots"]["lisbot"]["uptime"] is None   # не запускался — не считается простоем
        await c.monitor.stop()
        c.events.close()
    asyncio.run(go())


def test_heartbeat(tmp_path):
    async def go():
        hits = []

        async def ping(request):
            hits.append(request.path)
            return web.Response(text="OK")
        app = web.Application()
        app.router.add_get("/{tail:.*}", ping)
        srv = TestServer(app)
        await srv.start_server()
        url = f"http://127.0.0.1:{srv.port}/abc"
        c, _ = _center(tmp_path, {"heartbeat": {"url": url, "fail_when_bot_down": True}})
        await c.monitor.heartbeat()
        assert hits == ["/abc"] and c.monitor.heartbeat_state["ok"]
        c.workers["lisbot"]._want_running = True                        # бот должен работать, но не работает
        await c.monitor.heartbeat()
        assert hits[-1] == "/abc/fail"
        await srv.close()
        await c.monitor.heartbeat()
        assert c.monitor.heartbeat_state["ok"] is False
        c.events.close()
    asyncio.run(go())


def test_monitor_api(tmp_path):
    async def go():
        c, sent = _center(tmp_path)
        token = c.cfg.ensure_token()
        async with TestClient(TestServer(make_app(c))) as cl:
            await cl.get(f"/login?token={token}", allow_redirects=False)
            s = await (await cl.get("/api/monitor/settings")).json()
            assert s["telegram_source"] == "не настроен" and s["daily_report_time"] == "09:00"
            r = await cl.post("/api/monitor/settings", json={**s, "telegram_token": "123:abc", "telegram_chat_id": "42",
                                                               "quiet_hours": "23:00-07:30", "bot_alert_levels": "critical, warn"})
            assert (await r.json())["ok"]
            s = await (await cl.get("/api/monitor/settings")).json()
            assert s["telegram_token"] == "" and s["telegram_token_set"] and s["telegram_source"] == "центр"
            assert s["bot_alert_levels"] == ["critical", "warn"] and s["quiet_hours"] == "23:00-07:30"
            r = await cl.post("/api/monitor/settings", json={**s, "quiet_hours": "ночью"})
            assert r.status == 400
            assert c.monitor.telegram_target()[0] == "123:abc"            # пустое поле токена не стёрло сохранённый
            assert (await (await cl.post("/api/monitor/test")).json())["ok"]
            assert "Проверка уведомлений" in sent[-1]
            rep = await (await cl.get("/api/monitor/report")).json()
            assert "LisBot" in rep["text"]
            assert (await cl.post("/api/monitor/report")).status == 200
            st = await (await cl.get("/api/monitor/status")).json()
            assert st["telegram"]["ok"] and len(st["notifications"]) == 2
            h = await (await cl.get("/api/monitor/history?hours=24")).json()
            assert len(h["bots"]["lisbot"]["buckets"]) == 96
            r = await cl.post("/api/alerts/ack", json={"source": "lisbot", "id": 1})
            assert r.status == 409                                        # бот не запущен
            assert (await cl.post("/api/alerts/ack-all")).status == 409
        c.events.close()
    asyncio.run(go())


def test_telegram_falls_back_to_lisbot(tmp_path):
    c, _ = _center(tmp_path)
    c.workers["lisbot"].save_secrets({"TELEGRAM_BOT_TOKEN": "9:x", "TELEGRAM_CHAT_ID": "1, 2"})
    assert c.monitor.telegram_target() == ("9:x", ["1", "2"], "LisBot")
    c.events.close()


def test_bot_ack_calls_bot_api(tmp_path):
    async def go():
        got = []

        async def lis_ack(request):
            got.append(("lis", request.headers.get("Authorization"), await request.json()))
            return web.json_response({"ok": True})

        async def mann_ack(request):
            got.append(("mann", request.cookies.get("mb_token"), await request.json()))
            return web.json_response({"ok": True})
        app = web.Application()
        async def route(request):
            return await (lis_ack(request) if "Authorization" in request.headers else mann_ack(request))
        app.router.add_post("/api/alerts/ack", route)
        srv = TestServer(app)
        await srv.start_server()
        c, _ = _center(tmp_path)
        for w in c.workers.values():
            w.port, w.state = srv.port, RUNNING
        assert (await c.monitor.ack("lisbot", 7))[0]
        assert (await c.monitor.ack("mannbot", None))[0]
        assert got[0][0] == "lis" and got[0][1].startswith("Bearer") and got[0][2] == {"id": 7}
        assert got[1][0] == "mann" and got[1][2] == {"all": True}
        assert any(e["kind"] == "alert.ack" for e in c.events.recent(10))
        await srv.close()
        c.events.close()
    asyncio.run(go())


def test_hung_bot_is_restarted_once_an_hour(tmp_path):
    async def go():
        c, sent = _center(tmp_path, {"unresponsive_after": 2, "restart_unresponsive_after": 3})
        w = c.workers["lisbot"]
        w.proc, w.state, w._want_running = _FakeProc(), RUNNING, True
        restarts = []

        async def health():
            return False

        async def restart(reason="панель"):
            restarts.append(reason)
            return "перезапущен"
        w.health, w.restart = health, restart
        for _ in range(3):
            await c.monitor.sample()
        assert restarts == ["бот завис"]
        kinds = [e["kind"] for e in c.events.recent(10)]
        assert "monitor.auto_restart" in kinds and "monitor.unresponsive" in kinds
        for _ in range(6):                                          # после перезапуска опять завис — второй раз за час нельзя
            await c.monitor.sample()
        assert restarts == ["бот завис"]
        c.monitor.kv_set("auto_restart:lisbot", time.time() - 3700)
        await c.monitor.sample()
        assert len(restarts) == 2
        c.events.close()
    asyncio.run(go())
