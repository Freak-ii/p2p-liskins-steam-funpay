import asyncio

from center.app import Center


def _center(tmp_path, *, own=True):
    c = Center(tmp_path)
    if own:
        c.monitor.save_settings({**c.monitor.settings(), "telegram_token": "1:abc", "telegram_chat_id": "42"})
    calls = []

    async def fake_api(token, method, payload, timeout=15):
        calls.append((method, payload))
        if method == "getUpdates":
            return {"ok": True, "result": c._updates}
        return {"ok": True}
    c.tg._api = fake_api
    c._updates = []
    return c, calls


def _msg(uid, text, chat=42):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def _cb(uid, data, chat=42):
    return {"update_id": uid, "callback_query": {"id": f"q{uid}", "data": data,
                                                  "message": {"chat": {"id": chat}, "message_id": 7}}}


def test_commands_only_from_allowed_chat(tmp_path):
    async def go():
        c, calls = _center(tmp_path)
        c._updates = [_msg(1, "/status"), _msg(2, "/status", chat=999), _msg(3, "/help"), _msg(4, "/pause"),
                      _msg(5, "/pause lisbot"), _msg(6, "/stock"), _msg(7, "/report"), _msg(8, "привет")]
        assert await c.tg.poll_once() == 8
        sent = [p for m, p in calls if m == "sendMessage"]
        assert all(p["chat_id"] == "42" for p in sent) and len(sent) == 6        # чужой чат и не-команды без ответа
        assert "LisBot" in sent[0]["text"] and "/proposals" in sent[1]["text"]
        assert "Укажите бота" in sent[2]["text"]
        assert "✗" in sent[3]["text"]                                           # бот не запущен — честная ошибка
        assert "отчёт" in sent[5]["text"].lower()
        assert c.monitor.kv_get("tg_offset") == "9"
        assert c.tg.state["active"]
        assert any(e["kind"] == "telegram.command" for e in c.events.recent(10))
        c.events.close()
    asyncio.run(go())


def test_lisbot_token_is_not_polled(tmp_path):
    async def go():
        c, calls = _center(tmp_path, own=False)
        c.workers["lisbot"].secrets = lambda: {"TELEGRAM_BOT_TOKEN": "9:lis", "TELEGRAM_CHAT_ID": "42"}
        assert c.monitor.telegram_target()[2] == "LisBot"
        assert await c.tg.poll_once() == -1 and not calls
        assert "свой Telegram-бот" in c.tg.state["info"]
        c.cfg.save({"monitor": {"telegram": {"commands": False}}})
        c2, _ = _center(tmp_path / "b")
        c2.cfg.save({"monitor": {"telegram": {"commands": False}}})
        assert await c2.tg.poll_once() == -1
        c.events.close()
        c2.events.close()
    asyncio.run(go())


def test_proposal_buttons_and_confirm_tier(tmp_path):
    async def go():
        c, calls = _center(tmp_path)
        co = c.coordinator
        p = await co.act_or_propose("pause_bot", {"bot": "mannbot"}, reason="тест", source="test")
        lim = await co.act_or_propose("change_limits", {"changes": {"strategy.maxSpendPerDayUsd": 50}}, reason="r", source="test")
        live = await co.act_or_propose("set_live_mode", {"bot": "lisbot", "live": True}, reason="r", source="test")
        assert c.tg.keyboard(p) and c.tg.keyboard(lim) and c.tg.keyboard(live) is None
        assert co._buttons(p)["inline_keyboard"][0][0]["callback_data"] == f"co:a:{p['id']}"
        c._updates = [_msg(1, "/proposals")]
        await c.tg.poll_once()
        sent = [x for m, x in calls if m == "sendMessage"]
        assert len(sent) == 3 and sum(1 for x in sent if "reply_markup" in x) == 2
        assert any("только в панели" in x["text"] for x in sent)
        calls.clear()
        c._updates = [_cb(2, f"co:r:{p['id']}"), _cb(3, f"co:a:{lim['id']}"), _cb(4, f"co:a:{live['id']}"),
                      _cb(5, "co:a:999"), _cb(6, f"co:a:{lim['id']}", chat=5)]
        await c.tg.poll_once()
        edits = [x for m, x in calls if m == "editMessageText"]
        assert len(edits) == 4 and "Отклонено" in edits[0]["text"] and co.get(p["id"])["status"] == "rejected"
        assert "Точно выполнить" in edits[1]["text"] and edits[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"co:c:{lim['id']}"
        assert co.get(lim["id"])["status"] == "proposed"                         # первое нажатие ничего не делает
        assert "только в панели" in edits[2]["text"] and co.get(live["id"])["status"] == "proposed"
        assert "не найдено" in edits[3]["text"]
        calls.clear()
        c._updates = [_cb(7, f"co:c:{lim['id']}")]
        await c.tg.poll_once()
        assert co.get(lim["id"])["status"] == "failed"                            # LisBot не запущен — выполнение честно не удалось
        c.events.close()
    asyncio.run(go())


def test_goal_command(tmp_path):
    async def go():
        c, calls = _center(tmp_path)
        c._updates = [_msg(1, "/goal")]
        await c.tg.poll_once()
        assert "Цель задаётся в панели" in calls[-1][1]["text"]
        c.analytics.save_goal(50000)
        c._updates = [_msg(2, "/goal")]
        await c.tg.poll_once()
        assert "из 50 000 ₽" in calls[-1][1]["text"]
        c._updates = [_msg(3, "/ready")]
        await c.tg.poll_once()
        assert "не задан ключ API LIS-SKINS" in calls[-1][1]["text"]
        c.events.close()
    asyncio.run(go())


def test_errors_command_and_daily_report(tmp_path):
    import time

    async def go():
        c, calls = _center(tmp_path)
        c._updates = [_msg(1, "/errors")]
        await c.tg.poll_once()
        assert "Steam</b>: ошибок нет" in calls[-1][1]["text"]
        w = c.workers["mannbot"]
        for i in range(3):
            w.seq += 1
            w.lines.append({"n": w.seq, "ts": time.time() - 60, "stream": "stderr",
                            "text": f"2026-10-18 10:00:0{i} ERROR   funpay   <timeout> order {i}"})
        c._updates = [_msg(2, "/errors")]
        await c.tg.poll_once()
        text = calls[-1][1]["text"]
        assert "FunPay</b>: 3 ошибок" in text and "3× " in text and "&lt;timeout&gt;" in text
        assert "Ошибок в выводе: 3" in c.monitor.daily_report()
        c.events.close()
    asyncio.run(go())


def test_today_command(tmp_path):
    import sqlite3
    import time
    from tests.test_analytics import seed
    seed(tmp_path)
    lt = time.localtime(time.time() + 30 * 86400)      # через месяц: сделки seed() не попадают ни в сегодня, ни во вчера
    noon = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))
    con = sqlite3.connect(tmp_path / "lisbot" / "data" / "bot.sqlite")
    for name, sold, profit in (("T1", noon - 3600, 2.0), ("Y1", noon - 86400 - 3600, 1.0), ("Y2", noon - 86400 + 3600, 5.0)):
        ms = int(sold * 1000)
        con.execute("INSERT INTO items(name,mode,status,dry_run,buy_price_usd,expected_net_usd,received_at,sold_at,sold_usd,"
                    "profit_usd,created_at,updated_at) VALUES(?,'rtba','sold',0,10,12,?,?,?,?,?,?)",
                    (name, ms - 9 * 86400000, ms, 10 + profit, profit, ms - 10 * 86400000, ms))
    con.commit()
    con.close()

    async def go():
        c, calls = _center(tmp_path)
        c.monitor.clock = lambda: noon
        c._updates = [_msg(1, "/today")]
        await c.tg.poll_once()
        text = calls[-1][1]["text"]
        # курс seed — 90 ₽/$: сегодня +2 $ = 180 ₽, вчера к полудню +1 $ = 90 ₽ (Y2 продан вчера после полудня — не в счёт)
        assert "Сегодня до 12:00" in text and "Прибыль 180 ₽</b> (вчера 90)" in text and "Сделок 1 (вчера 1)" in text
        c.events.close()
    asyncio.run(go())
