"""Разбор данных заказа на звёзды: количество звёзд в лоте и @username получателя."""
from __future__ import annotations

import re

STARS_RE = re.compile(r"(\d[\d  ]{0,6})\s*(?:зв[её]зд\w*|stars?\b|⭐)", re.I)
STARS_AFTER_RE = re.compile(r"(?:stars?|зв[её]зд\w*)\D{0,3}(\d{2,6})\b", re.I)

# Telegram: 5–32 символа (коллекционные с Fragment — от 4), латиница/цифры/_, начинается с буквы
USERNAME_RE = re.compile(r"^[a-zA-Z](?:[a-zA-Z0-9_]{2,30})[a-zA-Z0-9]$")
_MENTION_RE = re.compile(r"(?:^|[\s(:,;«\"'])@([a-zA-Z][a-zA-Z0-9_]{2,31})\b")
_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/([a-zA-Z][a-zA-Z0-9_]{2,31})\b", re.I)
_BARE_RE = re.compile(r"^\s*@?([a-zA-Z][a-zA-Z0-9_]{2,31})\s*[.!]?\s*$")

# Ключи параметров заказа FunPay, в которых может лежать username покупателя
USERNAME_PARAM_RE = re.compile(r"(user\s*name|юзернейм|юзер|ник|логин|telegram|телеграм|получател|аккаунт|account|login)",
                               re.I)
# Слова, которые похожи на username, но им не являются
STOP_WORDS = {"stars", "telegram", "funpay", "username", "hello", "privet", "spasibo", "thanks", "thank", "admin",
              "seller", "status", "help", "support", "premium", "gift", "podarok", "ok", "okay", "yes", "net"}


def stars_from_text(text: str) -> int | None:
    """«50 звёзд, По username» → 50; «Telegram Stars 250» → 250."""
    t = text or ""
    for rx in (STARS_RE, STARS_AFTER_RE):
        m = rx.search(t)
        if m:
            n = int(re.sub(r"\D", "", m.group(1)) or 0)
            if n > 0:
                return n
    return None


def normalize_username(value: str) -> str | None:
    """Приводит «@Name», «t.me/Name», « name » к «Name». None — если это не похоже на username."""
    v = (value or "").strip()
    m = _LINK_RE.search(v)
    if m:
        v = m.group(1)
    v = v.lstrip("@").strip().strip(".,;:!?\"'«»()[]")
    if not USERNAME_RE.match(v) or "__" in v:
        return None
    if v.lower() in STOP_WORDS:
        return None
    return v


def username_from_text(text: str, *, allow_bare: bool = True) -> str | None:
    """Ищет username в сообщении покупателя: @name, ссылка t.me/name или сообщение из одного слова."""
    t = text or ""
    for rx in (_LINK_RE, _MENTION_RE):
        m = rx.search(t)
        if m:
            u = normalize_username(m.group(1))
            if u:
                return u
    if allow_bare:
        m = _BARE_RE.match(t)
        if m:
            return normalize_username(m.group(1))
    return None


def username_from_params(params: dict[str, str]) -> str | None:
    """Username из параметров заказа FunPay (поле, которое покупатель заполняет при оплате)."""
    for key, value in params.items():
        if USERNAME_PARAM_RE.search(key):
            u = username_from_text(value) or normalize_username(value)
            if u:
                return u
    return None


def looks_like_username_attempt(text: str) -> bool:
    """Покупатель явно пытался прислать username, но с ошибкой (кириллица, пробелы, короткий)."""
    t = (text or "").strip()
    if "@" in t or "t.me/" in t.lower():
        return True
    return bool(re.fullmatch(r"@?[\wа-яА-ЯёЁ]{2,40}", t)) and not t.isdigit()
