"""Фильтр конфиденциального для постов в публичный канал.

Каждый черновик агента (и каждая ручная правка) проходит через check(): если в тексте есть то, что нельзя
показывать покупателям, черновик получает список проблем и не может быть одобрен, пока их не убрать.

Что нельзя: как и где закупается товар (площадки, кошельки, биржи), внутренние цифры (наценка, спред, прибыль,
себестоимость, оборот, балансы), служебные слова системы ботов (RTBA, арбитраж, сид-фраза, токены).
Цены и суммы по умолчанию тоже нельзя (allow_prices) — цены в канале публикует сам ShopBot своим постом.
Проценты разрешены только те, что совпадают со скидками заведённых акций.
"""
from __future__ import annotations

import re

# (регулярное выражение, почему нельзя). Регистр не важен; \b у кириллицы в Python работает.
RULES: list[tuple[str, str]] = [
    (r"спред\w*", "спред — внутренняя цифра"),
    (r"марж\w*", "маржа — внутренняя цифра"),
    (r"наценк\w*", "наценка — внутренняя цифра"),
    (r"прибыл\w*|профит\w*|\bprofit\w*", "прибыль — внутренняя цифра"),
    (r"себестоим\w*|закупочн\w*|закупк\w*|закупа\w*|закуп\b", "закупка и себестоимость — схема работы"),
    (r"выручк\w*|оборот\w*|\bturnover\b|\brevenue\b", "выручка и оборот — внутренние цифры"),
    (r"баланс\w* (?:бот|кошел|магазин)\w*", "балансы — внутренние цифры"),
    (r"арбитраж\w*|перепрода\w*|\bp2p\b|п2п", "схема перепродажи"),
    (r"\brtba\b|ртба", "внутренняя система ботов"),
    (r"fragment(?:\.com)?\b|фрагмент\w*", "площадка закупки"),
    (r"funpay|фанпей|фанпэй|ggsel|ггсел|lis-?skins|лис-?скинс|market\.csgo|маркет ?ксго|avito|авито|"
     r"wildberries|вайлдберриз|\bozon\b|озон", "другие площадки, где работают боты"),
    (r"bybit|байбит|\bokx\b|binance|бинанс|tonkeeper|тонкипер|crypto ?pay|криптобот|@wallet\b|@cryptobot\b",
     "кошельки и биржи — схема движения денег"),
    (r"сид-?фраз\w*|seed\b|мнемони\w*|приватн\w* ключ\w*|api[- ]?key|api[- ]?ключ\w*|токен\w* бот\w*",
     "секреты"),
    (r"lisbot|mannbot|starsbot|shopbot|tggift|tg gift бот|центр перепродажи", "внутренние названия ботов"),
    (r"дешевле (?:чем )?(?:в |у )?(?:telegram|телеграм|официальн\w*|fragment|фрагмент\w*)",
     "сравнение с официальной ценой Telegram"),
]

MONEY = re.compile(r"\d[\d\s.,]*\s?(?:₽|руб\w*|р\.|\$|usd\w*|usdt|ton\b|тон\b|€|eur\w*|грам\w*|gram\b)", re.I)
PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s?%")


def check(text: str, *, allow_prices: bool = False, allowed_percents: set[float] | None = None,
          extra_words: list[str] | None = None) -> list[str]:
    """Список проблем текста (пустой — можно публиковать). Каждая проблема: «что найдено: почему нельзя»."""
    issues: list[str] = []
    low = text.lower()
    seen: set[str] = set()
    for pattern, why in RULES:
        m = re.search(pattern, low, re.I)
        if m and why not in seen:
            seen.add(why)
            issues.append(f"«{m.group(0)}»: {why}")
    for w in extra_words or []:
        w = str(w).strip().lower()
        if w and w in low:
            issues.append(f"«{w}»: слово из вашего списка запрещённых")
    if not allow_prices:
        m = MONEY.search(text)
        if m:
            issues.append(f"«{m.group(0).strip()}»: суммы и цены в постах агента выключены (agents.channel.allow_prices)")
    allowed = allowed_percents or set()
    for m in PERCENT.finditer(text):
        try:
            v = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if v not in allowed:
            issues.append(f"«{m.group(0)}»: процент не совпадает со скидкой ни одной акции")
            break
    return issues


def safe_html(text: str) -> str:
    """Оставить в посте только разметку, которую понимает Telegram (b, i, u, s, a, code, blockquote)."""
    text = re.sub(r"<(?!/?(?:b|i|u|s|code|blockquote|a)(?:\s+href=\"https?://[^\"<>]+\")?\s*>)[^>]*>", "", text)
    return text.strip()
