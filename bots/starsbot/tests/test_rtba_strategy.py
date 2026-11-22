"""Пресеты RTBA: сегменты номиналов, фильтр стакана, спред-буфер, ночной коэффициент, авто по лимиту карты."""
import asyncio
from datetime import datetime

from aiohttp.test_utils import TestClient, TestServer

from starsbot.app.launcher import DEMO_OVERRIDES
from starsbot.app.supervisor import Supervisor
from starsbot.config import load_config
from starsbot.core import strategy as strat
from starsbot.core.models import CompetitorLot
from starsbot.core.rtba import RTBAEngine, filter_competitors
from starsbot.web.server import make_app


def cfg(**rt):
    return load_config(None, {"rtba": {"enabled": True, "buyer_markup_pct": 0, "undercut_abs": 0.01, "min_change": 0.01,
                                       "competitor_min_amount": 1, **rt}}).rtba


def lot(i, seller, price, stars=500, reviews=100, rating=5.0, online=True):
    return CompetitorLot(offer_id=str(i), seller=seller, price=price, amount=10, online=online, rating=rating,
                         reviews=reviews, description=f"{stars} звёзд, По username",
                         fields={"quantity": f"{stars} звёзд", "method": "по username"})


MSK_NIGHT = datetime(2026, 11, 20, 3, 0, tzinfo=strat.MSK).timestamp()
MSK_DAY = datetime(2026, 11, 20, 14, 0, tzinfo=strat.MSK).timestamp()


def test_segments_by_denomination_and_auto_by_card_limit():
    rt = cfg(preset="balanced")
    small, mid, big = (strat.resolve(rt, n, now=MSK_DAY) for n in (50, 250, 1000))
    assert small["overrides"]["target_position"] == 3 and small["margin_pct"] == 0
    assert (mid["overrides"]["target_position"], mid["overrides"]["max_position"], mid["margin_pct"]) == (2, 4, 2.5)
    assert big["overrides"]["target_position"] == 1 and big["margin_pct"] == 5
    assert mid["overrides"]["competitor_min_reviews"] == 10 and mid["overrides"]["competitor_only_online"]
    rt = cfg(preset="auto", card_limit={"daily_rub": 10000})
    assert strat.auto_preset(rt, strat.usage(rt, 2000, 0)) == "turnover"
    assert strat.auto_preset(rt, strat.usage(rt, 8000, 0)) == "balanced"
    assert strat.auto_preset(rt, strat.usage(rt, 10500, 0)) == "margin"
    assert strat.auto_preset(cfg(preset="auto"), strat.usage(cfg(), 99999, 0)) == "balanced"   # лимит не задан
    # custom — прежнее поведение: никаких сегментов, только маржа из настроек
    c = strat.resolve(cfg(min_margin_pct=3), 500, now=MSK_DAY)
    assert c["effective"] == "custom" and c["overrides"] == {} and c["margin_pct"] == 3


def test_night_bonus_moscow_time():
    rt = cfg(preset="balanced", night={"enabled": True})
    assert strat.resolve(rt, 250, now=MSK_NIGHT)["margin_pct"] == 4.5
    assert strat.resolve(rt, 250, now=MSK_DAY)["margin_pct"] == 2.5
    assert strat.resolve(cfg(min_margin_pct=3, night={"enabled": True}), 50, now=MSK_NIGHT)["margin_pct"] == 5


def test_competitor_filter_skips_newcomers_and_offline():
    market = [lot(1, "new", 740, reviews=0, rating=None), lot(2, "low", 741, reviews=50, rating=3.0),
              lot(3, "off", 742, online=False), lot(4, "A", 745), lot(5, "B", 750)]
    rt = cfg(competitor_only_online=True, competitor_min_reviews=10, competitor_min_rating=4)
    assert [l.seller for l in filter_competitors(market, rt, 500, set(), "me")] == ["A", "B"]


def test_spread_buffer_takes_gap_to_next_competitor():
    market = [lot(1, "A", 745), lot(2, "B", 748), lot(3, "C", 750), lot(4, "D", 770), lot(5, "E", 771)]
    rt = cfg(spread_buffer={"enabled": True})
    eng = RTBAEngine(rt)
    kw = dict(offer_id="me", stars=500, own_offer_ids={"me"}, own_username="me", current_lot_price=800,
              cost_per_star_rub=1.0)
    # без запаса мест: 3-е место — чуть дешевле 3-го конкурента
    d = eng.decide(market, overrides={"strategy": "topN", "target_position": 3, "max_position": 3}, **kw)
    assert d.target_lot_price == 749.99 and d.position == 3
    # можно до 4-го: разрыв 750→770 = 20 ₽ ≥ max(5 ₽, 1.5%) → встаём на 770 − 0.05
    d = eng.decide(market, overrides={"strategy": "topN", "target_position": 3, "max_position": 4}, **kw)
    assert d.target_lot_price == 769.95 and d.position == 4 and "спред" in d.reason
    # мелкий разрыв (2 ₽ на 500⭐) не стоит места в выдаче
    tight = [lot(1, "A", 745), lot(2, "B", 748), lot(3, "C", 750), lot(4, "D", 752)]
    d = eng.decide(tight, overrides={"strategy": "topN", "target_position": 3, "max_position": 4}, **kw)
    assert d.position == 3


def test_engine_floor_and_loss_check_follow_segment(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=DEMO_OVERRIDES)
    eng = sup.engine
    eng.cfg.rtba["preset"] = "balanced"
    assert eng.lot_strategy(50)["margin_pct"] == 0
    assert eng.lot_strategy(1000)["margin_pct"] == 5
    eng.cfg.rtba["preset"] = "auto"
    eng.cfg.rtba["card_limit"]["daily_rub"] = 1000.0
    eng._usage_cache = None
    eng.db.exec("INSERT INTO orders(order_id,status,price,channel,paid_at,stars) VALUES('X','completed',1200,'funpay',?,1000)",
                (__import__("time").time(),))
    used, day, _ = eng.turnover_usage()
    assert day == 1200 and used == 1.2 and eng.lot_strategy(250)["effective"] == "margin"


def test_pricing_page_switches_preset_live(tmp_path):
    sup = Supervisor(tmp_path, demo=True, overrides=DEMO_OVERRIDES)

    async def go():
        client = TestClient(TestServer(make_app(sup)))
        await client.start_server()
        try:
            await client.get(f"/login?token={sup.ensure_token()}")
            r = await client.get("/pricing")
            assert r.status == 200 and "Стратегия цен" in await r.text()
            r = await client.post("/pricing/strategy", data={"preset": "auto", "daily_rub": "15000", "night": "on",
                                                             "spread": "on"})
            assert r.status == 200
            assert sup.engine.cfg.rtba.preset == "auto" and sup.engine.cfg.rtba.card_limit.daily_rub == 15000
            assert sup.engine.cfg.rtba.night.enabled and sup.cfg.rtba.preset == "auto"
            assert "Авто по лимиту карты" in await (await client.get("/pricing")).text()
            out = await sup.engine.handle_command("rtba_preset", ["margin"])
            assert "Маржа" in out and sup.engine.cfg.rtba.preset == "margin"
        finally:
            await client.close()

    asyncio.run(go())
