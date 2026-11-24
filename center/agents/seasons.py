"""Календарь сезонных поводов для канала: праздники, распродажи Steam, дни Telegram.

Агент канала заранее (agents.channel.season_lead_days) кладёт черновик к каждому поводу. Даты распродаж Steam
плавают из года в год — у них стоит примерная дата и пометка «проверить даты».
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Season:
    id: str
    title: str
    month: int
    day: int
    idea: str                 # о чём пост (подсказка для текста)
    approx: bool = False      # дата примерная (распродажи Steam)
    lead_days: int | None = None   # своё упреждение; None — общее season_lead_days


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    while d.weekday() != weekday:
        d -= dt.timedelta(days=1)
    return d


FIXED: list[Season] = [
    Season("new_year", "Новый год", 12, 31, "новогодний пост: подарки Telegram друзьям, Premium в подарок, "
           "итоги года магазина в общих словах, пожелания", lead_days=10),
    Season("christmas", "Рождество", 1, 7, "тёплое поздравление, подарки близким"),
    Season("valentine", "День святого Валентина", 2, 14, "подарки Telegram для пар, романтичные подарки", lead_days=5),
    Season("feb23", "23 февраля", 2, 23, "поздравление, Premium или подарок в подарок"),
    Season("mar8", "8 Марта", 3, 8, "поздравление, подборка красивых подарков Telegram", lead_days=5),
    Season("april1", "1 апреля", 4, 1, "лёгкий шуточный пост и мини-конкурс"),
    Season("tg_birthday", "День рождения Telegram", 8, 14, "поздравление Telegram, немного истории, розыгрыш"),
    Season("sept1", "1 сентября", 9, 1, "возвращение после лета, напоминание о реферальной программе"),
    Season("halloween", "Хэллоуин", 10, 31, "жуткий тематический пост, тыквы, «страшно выгодно», конкурс", lead_days=4),
    Season("singles_day", "Распродажа 11.11", 11, 11, "короткая акция на сутки"),
    Season("steam_spring", "Весенняя распродажа Steam", 3, 14, "напоминание про ключи и игры", approx=True),
    Season("steam_summer", "Летняя распродажа Steam", 6, 25, "самая большая распродажа года, ключи и игры", approx=True),
    Season("steam_autumn", "Осенняя распродажа Steam", 11, 25, "напоминание про ключи и игры", approx=True),
    Season("steam_winter", "Зимняя распродажа Steam", 12, 18, "игры в подарок к праздникам", approx=True),
]


def occurrences(year: int) -> list[tuple[Season, dt.date]]:
    out = [(s, dt.date(year, s.month, s.day)) for s in FIXED]
    bf = _last_weekday(year, 11, 4)                       # последняя пятница ноября
    out.append((Season("black_friday", "Чёрная пятница", 11, bf.day, "скидка по промокоду на несколько дней, "
                       "бонус к первой покупке", lead_days=4), bf))
    out.append((Season("cyber_monday", "Киберпонедельник", 12, 1, "последний день акций Чёрной пятницы", lead_days=1),
                bf + dt.timedelta(days=3)))
    return out


def upcoming(today: dt.date, horizon_days: int = 60) -> list[dict]:
    """Ближайшие поводы (для панели): от сегодня на horizon_days вперёд."""
    out = []
    for year in (today.year, today.year + 1):
        for s, d in occurrences(year):
            delta = (d - today).days
            if 0 <= delta <= horizon_days:
                out.append({"id": s.id, "title": s.title, "date": d.isoformat(), "in_days": delta, "approx": s.approx,
                            "idea": s.idea})
    return sorted(out, key=lambda x: x["date"])


def due(today: dt.date, lead_days: int) -> list[tuple[Season, dt.date]]:
    """Поводы, к которым пора готовить черновик: до даты осталось не больше упреждения (и дата ещё не прошла)."""
    out = []
    for year in (today.year, today.year + 1):
        for s, d in occurrences(year):
            lead = s.lead_days if s.lead_days is not None else lead_days
            if 0 <= (d - today).days <= lead:
                out.append((s, d))
    return out
