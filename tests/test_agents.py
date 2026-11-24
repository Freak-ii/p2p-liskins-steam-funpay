import asyncio
import datetime as dt
import sqlite3
import time

import yaml
from aiohttp.test_utils import TestClient, TestServer

from center.agents import guard, news, seasons
from center.app import Center
from center.web import make_app

PAGE = """
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="durov/400">
<div class="tgme_widget_message_text js-message_text" dir="auto">Telegram выпустил новые <b>подарки</b> к Новому году.<br/>Их можно
купить за Stars и подарить друзьям.</div><time datetime="{now}" class="time">12:00</time></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="durov/401">
<div class="tgme_widget_message_text js-message_text">Про котиков, ничего интересного для магазина, просто длинный текст тут.</div>
<time datetime="{now}"></time></div></div>
"""


def test_guard_blocks_confidential():
    assert guard.check("Привет! Новые подарки уже в боте 🎁") == []
    bad = guard.check("Покупаем звёзды на Fragment, наш спред 3% и прибыль растёт, баланс кошелька Tonkeeper")
    text = " ".join(bad)
    assert "площадка закупки" in text and "спред" in text and "прибыль" in text and "кошельки" in text
    assert guard.check("Звёзды всего за 150 ₽")                       # суммы по умолчанию нельзя
    assert guard.check("Звёзды всего за 150 ₽", allow_prices=True) == []
    assert guard.check("Скидка 10% по промокоду") and not guard.check("Скидка 10%", allowed_percents={10.0})
    assert guard.check("тут слово секрет", extra_words=["секрет"])
    assert guard.check("Дешевле чем в Telegram!")
    assert guard.safe_html('<b>ok</b><script>x</script><a href="https://t.me/x">l</a>') == '<b>ok</b>x<a href="https://t.me/x">l</a>'


def test_seasons_black_friday_and_due():
    occ = {s.id: d for s, d in seasons.occurrences(2026)}
    assert occ["black_friday"] == dt.date(2026, 11, 27) and occ["cyber_monday"] == dt.date(2026, 11, 30)
    due = {s.id for s, _ in seasons.due(dt.date(2026, 11, 24), 3)}
    assert "black_friday" in due and "steam_autumn" in due and "new_year" not in due
    assert "new_year" in {s.id for s, _ in seasons.due(dt.date(2026, 12, 22), 3)}        # у Нового года 10 дней
    up = seasons.upcoming(dt.date(2026, 12, 25), 20)
    assert [u["id"] for u in up][:2] == ["new_year", "christmas"]


def test_news_parse():
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    posts = news.parse(PAGE.replace("{now}", now))
    assert [p["post"] for p in posts] == ["durov/400", "durov/401"]
    assert posts[0]["url"] == "https://t.me/durov/400" and "подарки" in posts[0]["text"] and "<b>" not in posts[0]["text"]
    assert news.channel_name("https://t.me/s/durov") == "durov" == news.channel_name("@durov")


def _center(tmp_path, shop_token="123:abc"):
    (tmp_path / "shopbot").mkdir(parents=True)
    (tmp_path / "shopbot" / "config.yaml").write_text(yaml.safe_dump({
        "general": {"shop_name": "DPB Fastchange"},
        "telegram": {"bot_token": shop_token, "bot_username": "DPBFastchange_bot"},
        "channel": {"chat_id": "@DPBFastchange"}}), encoding="utf-8")
    return Center(tmp_path)


def test_channel_agent_flow(tmp_path):
    async def go():
        center = _center(tmp_path)
        ch = center.agents.get("channel")
        assert ch is not None and not ch.running
        await center.agents.start()
        assert not ch.running                              # агенты сами не запускаются
        now = dt.datetime.now(dt.timezone.utc).isoformat()

        async def fetch(src):
            return news.parse(PAGE.replace("{now}", now))
        sent = []

        async def send(method, payload):
            sent.append((method, payload))
            return {"message_id": 77}, ""
        ch.fetch_override, ch.send_override = fetch, send
        ch.today_override = dt.date(2026, 11, 24)
        ch.save_settings({"sources": "@durov", "keywords": "подар", "promos": [
            {"title": "Чёрная пятница", "code": "BLACK", "discount_pct": 10, "start": "2026-11-25", "end": "2026-11-30"}],
            "achievements": "Добавили оплату по СБП"})
        # заказы магазина: порог 100 достигнут
        db = tmp_path / "shopbot" / "data"
        db.mkdir()
        con = sqlite3.connect(db / "shopbot.sqlite3")
        con.execute("CREATE TABLE orders (id TEXT, status TEXT)")
        con.executemany("INSERT INTO orders VALUES (?,?)", [(str(i), "delivered") for i in range(130)])
        con.commit()
        con.close()

        res = await ch.run_now()
        assert "новых черновиков" in res, res
        drafts = ch.items("draft")
        kinds = {d["kind"] for d in drafts}
        assert {"season", "promo", "news", "achievement", "milestone"} <= kinds, kinds
        assert len([d for d in drafts if d["kind"] == "news"]) == 1       # второй пост не прошёл по ключевому слову
        promo = next(d for d in drafts if d["kind"] == "promo")
        assert "BLACK" in promo["text"] and "10%" in promo["text"] and promo["issues"] == []
        ms = next(d for d in drafts if d["kind"] == "milestone")
        assert "100 выполненных заказов" in ms["text"]
        assert all(not d["issues"] for d in drafts), [d["issues"] for d in drafts]
        assert not [d for d in drafts if d["kind"] == "chatter"]          # были другие посты — чаттинг не нужен
        n = len(drafts)
        await ch.run_now()
        assert len(ch.items("draft")) == n                                 # повторов нет

        # плохой текст нельзя одобрить
        bad = ch.manual("Закупаем на Fragment со спредом", "плохой")
        try:
            await ch.approve(bad)
            raise AssertionError("должно было не пустить")
        except ValueError as e:
            assert "Fragment" in str(e) or "fragment" in str(e)
        it = ch.edit(bad, "Новые подарки уже в боте!")
        assert it["issues"] == []
        # одобрено, но публикация выключена → в канал ничего
        msg = await ch.approve(bad)
        assert "выключена" in msg and not sent and ch.item(bad)["status"] == "approved"
        ch.save_settings({"publish_enabled": True})
        msg = await ch.publish(bad)
        assert msg.startswith("опубликовано") and sent[0][1]["chat_id"] == "@DPBFastchange"
        assert sent[0][1]["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://t.me/DPBFastchange_bot?startapp"
        assert ch.item(bad)["status"] == "published" and ch.item(bad)["message_id"] == 77
        # одобрено на время → уходит проходом, когда время пришло
        later = promo["id"]
        await ch.approve(later, time.time() + 3600)
        assert ch.item(later)["status"] == "approved" and len(sent) == 1
        ch.update_item(later, scheduled_at=time.time() - 1)
        assert await ch.publish_due() == 1 and ch.item(later)["status"] == "published"
        # отклонение
        rid = drafts[0]["id"] if drafts[0]["id"] != later else drafts[1]["id"]
        assert ch.reject(rid) == "отклонено"
        center.events.close()
        await center.agents.stop()
    asyncio.run(go())


def test_chatter_when_quiet(tmp_path):
    async def go():
        center = _center(tmp_path)
        ch = center.agents.get("channel")
        ch.today_override = dt.date(2026, 7, 10)            # рядом нет поводов
        await ch.run_now()
        drafts = ch.items("draft")
        assert [d["kind"] for d in drafts] == ["chatter"] and not drafts[0]["issues"]
        await ch.run_now()
        assert len(ch.items("draft")) == 1                  # следующий — через chatter_every_days
        center.events.close()
        await center.agents.stop()
    asyncio.run(go())


def test_agents_web(tmp_path):
    async def go():
        center = _center(tmp_path)
        token = center.cfg.ensure_token()
        ch = center.agents.get("channel")
        ch.today_override = dt.date(2026, 7, 10)
        async with TestClient(TestServer(make_app(center))) as c:
            assert (await c.get("/api/agents")).status == 401
            await c.get(f"/login?token={token}", allow_redirects=False)
            assert (await c.get("/agents")).status == 200
            d = await (await c.get("/api/agents")).json()
            a = d["agents"][0]
            assert a["id"] == "channel" and a["channel"] == "@DPBFastchange" and a["token_set"] and not a["publish_enabled"]
            assert "bot_token" not in str(a) and "123:abc" not in str(d)          # токен в панель не уходит
            r = await (await c.post("/api/agents/channel/run")).json()
            assert r["ok"]
            items = (await (await c.get("/api/agents/channel/items?status=draft")).json())["items"]
            assert items
            r = await c.post("/api/agents/channel/items", json={"text": "Наша маржа огромная"})
            iid = (await r.json())["item"]["id"]
            r = await c.post(f"/api/agents/channel/items/{iid}/approve", json={})
            assert r.status == 409
            r = await c.post("/api/agents/channel/settings", json={"interval_minutes": 1})
            assert r.status == 400
            r = await c.post("/api/agents/channel/settings", json={"promos": [{"title": "X", "start": "завтра"}]})
            assert r.status == 400
            r = await (await c.post("/api/agents/channel/start")).json()
            assert r["agent"]["running"]
            r = await (await c.post("/api/agents/channel/stop")).json()
            assert not r["agent"]["running"]
            assert (await c.post("/api/agents/nope/start")).status == 404
            r = await (await c.post("/api/agents/channel/links", json={"tag": "Ivan YT!"})).json()
            assert r["bot"] == "https://t.me/DPBFastchange_bot?start=src_ivanyt"
            r = await (await c.get("/api/agents/channel/sources")).json()
            assert not r["ok"] and "ShopBot" in r["message"]                      # ShopBot не запущен
        assert Center(tmp_path).cfg["agents"]["channel"]["interval_minutes"] == 60
        center.events.close()
        await center.agents.stop()
    asyncio.run(go())
