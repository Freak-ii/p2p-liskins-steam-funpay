"""Стратегии цен RTBA: пресеты «Оборот» / «Сегменты» / «Маржа» / «Авто по лимиту карты».

Пресет делит лоты на сегменты по номиналу и задаёт каждому:
  position      — целевое место среди отфильтрованных конкурентов;
  max_position  — до какого места можно опуститься ради свободного спреда (спред-буфер);
  margin_pct    — маржа над себестоимостью для пола цены (с учётом комиссий вывода).
Плюс фильтр стакана (онлайн, рейтинг, отзывы) — на кого вообще ориентироваться.

«Авто» нужен из-за потолка вывода: деньги с FunPay идут на карту с лимитом, а с карты — на покупку TON.
Пока оборот за день/месяц далёк от лимита, выгоден оборот (мелкая маржа × много сделок); когда лимит
почти выбран, лишние продажи только «съедают» TON, который нечем пополнить, — тогда берём маржу.

custom — прежнее поведение: позиция лота со страницы «Лоты», маржа rtba.min_margin_pct, без сегментов.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

FILTER_STRICT = {"competitor_only_online": True, "competitor_min_reviews": 10, "competitor_min_rating": 4.0}

PRESETS: dict[str, dict] = {
    "turnover": {
        "title": "Оборот",
        "about": "Выше в выдаче и минимальная маржа: максимум сделок, пока лимит карты не мешает",
        "filters": FILTER_STRICT,
        "segments": [
            {"from": 0, "to": 100, "position": 2, "max_position": 2, "margin_pct": 0.0},
            {"from": 101, "to": 500, "position": 2, "max_position": 3, "margin_pct": 1.0},
            {"from": 501, "to": 100000, "position": 1, "max_position": 2, "margin_pct": 1.5},
        ],
    },
    "balanced": {
        "title": "Сегменты",
        "about": "Мелкие — витрина и отзывы без убытка, средние — 2–4 место, крупные — прибыль",
        "filters": FILTER_STRICT,
        "segments": [
            {"from": 0, "to": 100, "position": 3, "max_position": 3, "margin_pct": 0.0},
            {"from": 101, "to": 500, "position": 2, "max_position": 4, "margin_pct": 2.5},
            {"from": 501, "to": 100000, "position": 1, "max_position": 3, "margin_pct": 5.0},
        ],
    },
    "margin": {
        "title": "Маржа",
        "about": "Ниже в выдаче, но каждая сделка приносит больше — когда упираемся в лимит карты",
        "filters": FILTER_STRICT,
        "segments": [
            {"from": 0, "to": 100, "position": 3, "max_position": 4, "margin_pct": 1.5},
            {"from": 101, "to": 500, "position": 3, "max_position": 5, "margin_pct": 3.5},
            {"from": 501, "to": 100000, "position": 2, "max_position": 4, "margin_pct": 6.0},
        ],
    },
}
PRESET_TITLES = {"custom": "Свои настройки", "auto": "Авто по лимиту карты",
                 **{k: v["title"] for k, v in PRESETS.items()}}
MSK = timezone(timedelta(hours=3))


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def is_night(rtba, now: float | None = None) -> bool:
    n = rtba.get("night") or {}
    if not n.get("enabled"):
        return False
    tz = timezone(timedelta(hours=_f(n.get("utc_offset_hours"), 3)))
    h = datetime.fromtimestamp(now or time.time(), tz).hour
    start, end = int(n.get("start_hour", 1)), int(n.get("end_hour", 8))
    return start <= h < end if start <= end else (h >= start or h < end)


def night_bonus(rtba, now: float | None = None) -> float:
    return _f((rtba.get("night") or {}).get("floor_bonus_pct"), 2.0) if is_night(rtba, now) else 0.0


def turnover(db, now: float | None = None) -> tuple[float, float]:
    """Выручка FunPay (цены лотов) за текущие сутки и месяц по МСК — то, что потом выводится на карту."""
    t = datetime.fromtimestamp(now or time.time(), MSK)
    day0 = t.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = day0.replace(day=1)
    sql = ("SELECT COALESCE(SUM(price),0) FROM orders WHERE COALESCE(channel,'funpay')='funpay' AND paid_at>=? "
           "AND status NOT IN ('refunded','blocked')")
    return (float(db.scalar(sql, (day0.timestamp(),), 0) or 0), float(db.scalar(sql, (month0.timestamp(),), 0) or 0))


def usage(rtba, day_rub: float, month_rub: float) -> float | None:
    """Доля выбранного лимита карты (максимум из дневного и месячного); None — лимиты не заданы."""
    lim = rtba.get("card_limit") or {}
    vals = []
    if _f(lim.get("daily_rub")) > 0:
        vals.append(day_rub / _f(lim.get("daily_rub")))
    if _f(lim.get("monthly_rub")) > 0:
        vals.append(month_rub / _f(lim.get("monthly_rub")))
    return max(vals) if vals else None


def auto_preset(rtba, used: float | None) -> str:
    lim = rtba.get("card_limit") or {}
    if used is None:
        return str(lim.get("no_limit_preset") or "balanced")
    if used >= _f(lim.get("margin_from"), 1.0):
        return "margin"
    if used >= _f(lim.get("balanced_from"), 0.7):
        return "balanced"
    return "turnover"


def segment_of(segments: list, stars: int) -> dict | None:
    for s in segments or []:
        if int(s.get("from", 0)) <= stars <= int(s.get("to", 10 ** 9)):
            return s
    return None


def resolve(rtba, stars: int, *, used: float | None = None, now: float | None = None) -> dict:
    """Настройки RTBA для лота: {preset, effective, title, segment, night, overrides} (overrides → RTBAEngine.decide)."""
    preset = str(rtba.get("preset") or "custom")
    eff = auto_preset(rtba, used) if preset == "auto" else preset
    bonus = night_bonus(rtba, now)
    out = {"preset": preset, "effective": eff, "title": PRESET_TITLES.get(eff, eff), "segment": None,
           "night": bonus > 0, "night_bonus": bonus, "overrides": {}}
    p = PRESETS.get(eff)
    if p is None and eff == "custom" and rtba.get("segments"):
        p = {"segments": rtba.get("segments"), "filters": {}}      # свои сегменты из config.yaml
    if p is None:
        out["effective"] = "custom"
        out["title"] = PRESET_TITLES["custom"]
        if bonus:
            out["overrides"]["min_margin_pct"] = _f(rtba.get("min_margin_pct")) + bonus
        out["margin_pct"] = _f(rtba.get("min_margin_pct")) + bonus
        return out
    seg = segment_of(p["segments"], stars) or {}
    margin = _f(seg.get("margin_pct"), _f(rtba.get("min_margin_pct"))) + bonus
    ov = dict(p.get("filters") or {})
    # фильтры из настроек сильнее пресета, если там задано строже
    for k in ("competitor_min_reviews", "competitor_min_rating"):
        if _f(rtba.get(k)) > _f(ov.get(k)):
            ov[k] = rtba.get(k)
    if rtba.get("competitor_only_online"):
        ov["competitor_only_online"] = True
    if seg:
        pos = max(1, int(seg.get("position", 3)))
        ov.update(strategy="topN", target_position=pos, max_position=max(pos, int(seg.get("max_position", pos))))
    ov["min_margin_pct"] = margin
    out.update(segment=seg or None, overrides=ov, margin_pct=margin)
    return out


def describe(rtba, used: float | None, now: float | None = None) -> list[dict]:
    """Таблица для панели: все пресеты с сегментами и какой сейчас действует."""
    eff = auto_preset(rtba, used) if str(rtba.get("preset") or "custom") == "auto" else str(rtba.get("preset"))
    rows = []
    for key, p in PRESETS.items():
        rows.append({"key": key, "title": p["title"], "about": p["about"], "active": key == eff,
                     "segments": p["segments"]})
    return rows
