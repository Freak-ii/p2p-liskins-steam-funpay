"""Тексты для покупателя Ozon (бот активации и страница активации) и коды активации.

Отличия от FunPay: покупатель пишет не в чат площадки, а в бот активации — поэтому здесь нет просьб
«подтвердите заказ» (на Ozon заказ цифрового товара закрывается сам после передачи кода), а вместо
{buyer} — нейтральное обращение (Ozon не передаёт продавцу имя покупателя цифрового товара).
"""
from __future__ import annotations

import re
import secrets

# Без похожих символов (0/O, 1/I/L), чтобы код было легко перепечатать с почты
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"\b([A-Z0-9]{2,6}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})\b", re.I)


def new_code(prefix: str = "TGS") -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(12))
    prefix = re.sub(r"[^A-Z0-9]", "", str(prefix or "").upper())[:6]
    parts = [body[i:i + 4] for i in (0, 4, 8)]
    return "-".join(([prefix] if prefix else []) + parts)


def normalize_code(text: str) -> str:
    """«tgs-abcd efgh-JKMN-pqrs» → «TGSABCDEFGHJKMNPQRS» (ключ поиска в базе)."""
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def find_code(text: str) -> str | None:
    """Код активации в сообщении покупателя (с дефисами) или сообщение целиком из 12–18 букв/цифр."""
    m = CODE_RE.search(text or "")
    if m:
        return m.group(1).upper()
    t = re.sub(r"[\s-]", "", text or "")
    # без дефисов — только ЗАГЛАВНЫМИ (как в письме Ozon), чтобы не спутать с username вида durov2024abc
    if 12 <= len(t) <= 18 and t.isascii() and t.isalnum() and t == t.upper() and any(ch.isdigit() for ch in t):
        return t
    return None


TEMPLATES: dict[str, str] = {
    "greeting_need_username": (
        "Код принят ✅ Для зачисления {stars} ⭐ пришлите ваш username в Telegram, например: @username\n"
        "Найти его: Telegram → Настройки → «Имя пользователя»."
    ),
    "greeting_with_username": (
        "Код принят ✅ {stars} ⭐ Telegram Stars → @{username}{name_part}\n"
        "🤖 Отправляю автоматически через официальную платформу Telegram — Fragment, обычно 1–5 минут. "
        "Я напишу, когда звёзды будут зачислены."
    ),
    "username_ok": "✅ Аккаунт найден: @{username}{name_part}. Отправляю {stars} ⭐…",
    "bad_username": (
        "❗ {reason}.\nПришлите username ещё раз — латиницей, как в настройках Telegram (например, @username). "
        "Попытка {attempt} из {max_attempts}."
    ),
    "bad_username_final": (
        "⛔ Не получилось найти аккаунт для зачисления ({reason}). Код передан продавцу — напишите ему в чат "
        "заказа на Ozon, и он поможет с зачислением."
    ),
    "delivered": (
        "🎉 Готово! {stars} ⭐ Telegram Stars отправлены на @{username}.\n"
        "Звёзды появятся в течение пары минут: Telegram → Настройки → «Мои звёзды».\n\n"
        "Спасибо за покупку на Ozon ❤️ Будем рады вашему отзыву о товаре ⭐"
    ),
    "slow_network": "⏳ Платёж уже отправлен, сеть TON подтверждает его чуть дольше обычного. Звёзды придут автоматически.",
    "awaiting_approval": "🔎 Заказ проходит стандартную проверку безопасности — обычно несколько минут, затем звёзды "
                         "будут отправлены.",
    "delay": "⚠️ Зачисление ненадолго задерживается по техническим причинам. Продавец уже уведомлён и выдаст звёзды "
             "в ближайшее время. Если удобнее — напишите ему в чат заказа на Ozon.",
    "refunded": "💸 Заказ на Ozon отменён, код аннулирован. Деньги вернёт Ozon.",
    "call_seller": "🙋 Продавец уведомлён. Написать ему можно в чате заказа на Ozon.",
    "change_username_late": "Звёзды по этому коду уже оплачены на @{username} — изменить получателя нельзя.",
}

BOT = {
    "start": (
        "Здравствуйте! 👋 Это бот активации Telegram Stars, купленных на Ozon.\n\n"
        "1️⃣ Пришлите код активации из заказа Ozon (он есть на странице заказа и в письме), например: "
        "{example}\n2️⃣ Затем пришлите username аккаунта, на который зачислить звёзды, например: @username\n\n"
        "Можно одним сообщением: «{example} @username». Пароли и коды из Telegram не нужны — никому их не сообщайте."
    ),
    "need_username": "Код подходит ✅ Это {stars} ⭐ Telegram Stars.\nТеперь пришлите username получателя, например: "
                     "@username",
    "need_code": "Пришлите код активации из заказа Ozon (например, {example}), затем username получателя.",
    "bad_code": "❗ Код не найден. Проверьте, что он перепечатан без ошибок (буквы латиницей), и пришлите ещё раз.",
    "cancelled_code": "⛔ Этот код аннулирован: заказ на Ozon был отменён.",
    "not_ready": "Этот код ещё обрабатывается, попробуйте через минуту.",
    "too_many": "Слишком много неверных кодов. Попробуйте позже или напишите продавцу в чат заказа на Ozon.",
    "bad_format": "❗ Это не похоже на username Telegram. Пришлите username латиницей, например @username.",
    "already": "Код уже активирован: {stars} ⭐ → @{username}. Статус: {status_text}.",
    "status_none": "У вас пока нет активированных кодов. Пришлите код активации из заказа Ozon.",
    "help": "Пришлите код активации из заказа Ozon и username получателя. /status — статус ваших заказов.",
}
