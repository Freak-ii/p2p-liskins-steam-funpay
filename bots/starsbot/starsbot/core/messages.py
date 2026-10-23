"""Шаблоны сообщений покупателю (ru/en) и распознавание его сообщений.

Правила FunPay, которые учтены в текстах:
  • п. 2.1.2 — подтверждение заказа просим ТОЛЬКО после фактической оплаты звёзд на Fragment;
  • п. 1.1   — username нужен только как данные для зачисления (так устроен раздел «По username»),
    в Telegram мы покупателю не пишем и контактами не обмениваемся, всё общение — в чате FunPay;
  • п. 1.12  — никаких сторонних ссылок в сообщениях;
  • п. 2.1.4 / 3.1.2 — вопросы покупателя не игнорируются: на любое сообщение есть ответ,
    непонятное пересылается продавцу;
  • п. 1.8 / 1.9 — без спама: ограничение частоты сообщений и одно напоминание.
"""
from __future__ import annotations

import re

TEMPLATES: dict[str, dict[str, str]] = {
    "ru": {
        "greeting_need_username": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "⭐ Товар: {stars} Telegram Stars\n"
            "🤖 Выдача автоматическая через официальную платформу Telegram — Fragment, обычно 1–5 минут.\n\n"
            "👉 Пришлите в этот чат ваш username в Telegram, например: @username\n{last_hint}"
            "Найти его: Telegram → Настройки → «Имя пользователя». Звёзды придут на этот аккаунт.\n\n"
            "ℹ️ Username нужен только для зачисления звёзд: мы не пишем вам в Telegram, всё общение — здесь, "
            "в чате FunPay. Пожалуйста, подтверждайте заказ только после того, как звёзды придут."
        ),
        "greeting_with_username": (
            "Здравствуйте, {buyer}! {returning}Спасибо за оплату заказа #{order_id} 🎉\n"
            "⭐ {stars} Telegram Stars → @{username}{name_part}\n"
            "🤖 Отправляю автоматически через официальную платформу Telegram — Fragment, обычно 1–5 минут.\n"
            "Если username указан с ошибкой, сразу напишите правильный в этот чат.\n"
            "Пожалуйста, подтверждайте заказ только после того, как звёзды придут."
        ),
        "username_ok": "✅ Аккаунт найден: @{username}{name_part}. Отправляю {stars} ⭐…",
        "bad_username": (
            "❗ {buyer}, {reason}.\n"
            "Пришлите, пожалуйста, ваш username в Telegram ещё раз — латиницей, как в настройках Telegram "
            "(например, @username). Попытка {attempt} из {max_attempts}."
        ),
        "bad_username_final": (
            "⛔ {buyer}, не получилось найти аккаунт для зачисления ({reason}). Я передал заказ #{order_id} "
            "продавцу — он ответит вам здесь, в чате, в ближайшее время. Пожалуйста, не открывайте спор: решим вопрос."
        ),
        "delivered": (
            "🎉 Готово! {stars} ⭐ Telegram Stars отправлены на @{username} (заказ #{order_id}).\n"
            "Звёзды зачисляются в течение пары минут: Telegram → Настройки → «Мои звёзды».\n\n"
            "Огромное спасибо за покупку, {buyer} ❤️\n"
            "Пожалуйста, подтвердите выполнение заказа кнопкой «Подтвердить выполнение заказа» на странице "
            "заказа — и, если не сложно, оставьте отзыв. Это очень помогает нам!\n"
            "Будем рады видеть вас снова ⭐"
        ),
        "slow_network": (
            "⏳ Платёж по заказу #{order_id} уже отправлен, сеть TON подтверждает его чуть дольше обычного. "
            "Звёзды придут автоматически в ближайшие минуты, я напишу."
        ),
        "awaiting_approval": (
            "🔎 Заказ #{order_id} проходит стандартную проверку безопасности — обычно это несколько минут. "
            "Звёзды будут отправлены сразу после неё."
        ),
        "delay": (
            "⚠️ {buyer}, выдача заказа #{order_id} ненадолго задерживается по техническим причинам. Продавец уже "
            "уведомлён и выдаст звёзды в ближайшее время — или оформит возврат, если вы предпочтёте."
        ),
        "reminder": (
            "👋 {buyer}, напоминаю: чтобы получить {stars} ⭐ по заказу #{order_id}, пришлите ваш username в "
            "Telegram (например, @username)."
        ),
        "status": "ℹ️ Заказ #{order_id}: {status_text}",
        "help": (
            "Команды: «!статус» — состояние заказа, «!продавец» — позвать продавца. Чтобы получить звёзды, просто "
            "пришлите ваш username в Telegram (например, @username)."
        ),
        "call_seller": "🙋 Продавец уведомлён и ответит вам здесь, в чате, как можно скорее.",
        "off_platform": (
            "🙏 По правилам FunPay мы не общаемся и не проводим сделки вне площадки и не обмениваемся контактами. "
            "Все вопросы решаем здесь, в чате заказа."
        ),
        "unknown": (
            "Не совсем понял сообщение 🙂 Для выдачи звёзд по заказу #{order_id} нужен ваш username в Telegram "
            "(например, @username). Если есть вопрос — напишите «!продавец», и продавец ответит."
        ),
        "after_delivery": (
            "Звёзды по заказу #{order_id} уже отправлены на @{username} ✅ Если они не появились в течение 10 минут, "
            "откройте Telegram → Настройки → «Мои звёзды» или напишите «!продавец»."
        ),
        "change_username_late": (
            "Звёзды по заказу #{order_id} уже оплачены на @{username} — изменить получателя после отправки нельзя. "
            "Если это ошибка, напишите «!продавец»."
        ),
        "refunded": "💸 По заказу #{order_id} оформлен возврат средств.",
        "completed_thanks": "🙏 Спасибо за подтверждение заказа #{order_id}! Будем рады видеть вас снова ⭐",
        "presale": (
            "Здравствуйте! 👋 Telegram Stars выдаются автоматически: выберите лот нужного номинала "
            "({denominations} ⭐), оплатите и укажите ваш username — звёзды придут за 1–5 минут через официальную "
            "платформу Telegram (Fragment). Если остались вопросы — напишите, продавец ответит здесь."
        ),
        "faq_gift": (
            "Звёзды зачисляются напрямую на аккаунт по username (не подарком). Можно указать username друга — "
            "звёзды придут ему."
        ),
        "faq_safe": (
            "Это безопасно: звёзды покупаются на официальной платформе Telegram — Fragment. Нам нужен только ваш "
            "username, никакие коды, пароли и вход в аккаунт не нужны. Никому их не сообщайте!"
        ),
        "faq_time": "Обычно звёзды приходят за 1–5 минут после оплаты и получения username. {status_hint}",
    },
    "en": {
        "greeting_need_username": (
            "Hi {buyer}! {returning}Thank you for paying for order #{order_id} 🎉\n"
            "⭐ Item: {stars} Telegram Stars\n"
            "🤖 Delivery is automatic via Telegram's official platform Fragment, usually 1–5 minutes.\n\n"
            "👉 Please send your Telegram username to this chat, e.g. @username\n{last_hint}"
            "Find it in Telegram → Settings → Username. The stars will be credited to that account.\n\n"
            "ℹ️ We only need the username to credit the stars; we won't message you in Telegram. Please confirm "
            "the order only after the stars arrive."
        ),
        "greeting_with_username": (
            "Hi {buyer}! {returning}Thank you for paying for order #{order_id} 🎉\n"
            "⭐ {stars} Telegram Stars → @{username}{name_part}\n"
            "🤖 Sending automatically via Fragment, usually 1–5 minutes. If the username is wrong, send the correct "
            "one right away. Please confirm the order only after the stars arrive."
        ),
        "username_ok": "✅ Account found: @{username}{name_part}. Sending {stars} ⭐…",
        "bad_username": ("❗ {buyer}, {reason}.\nPlease send your Telegram username again (Latin letters, e.g. "
                         "@username). Attempt {attempt} of {max_attempts}."),
        "bad_username_final": ("⛔ {buyer}, we couldn't find the account ({reason}). The seller has been notified "
                               "and will reply here shortly. Please don't open a dispute — we'll sort it out."),
        "delivered": ("🎉 Done! {stars} ⭐ Telegram Stars have been sent to @{username} (order #{order_id}).\n"
                      "They arrive within a couple of minutes: Telegram → Settings → My Stars.\n\n"
                      "Thank you so much, {buyer} ❤️ Please confirm the order on FunPay and leave a review if you "
                      "can. See you again ⭐"),
        "slow_network": ("⏳ The payment for #{order_id} is sent; the TON network is confirming it a bit slower than "
                         "usual. The stars will arrive shortly."),
        "awaiting_approval": "🔎 Order #{order_id} is passing a standard security check. The stars will follow.",
        "delay": ("⚠️ {buyer}, delivery of #{order_id} is briefly delayed for technical reasons. The seller has been "
                  "notified and will deliver shortly or refund if you prefer."),
        "reminder": "👋 {buyer}, to receive {stars} ⭐ for order #{order_id}, please send your Telegram username.",
        "status": "ℹ️ Order #{order_id}: {status_text}",
        "help": "Commands: \"!status\", \"!seller\". To receive the stars just send your Telegram username.",
        "call_seller": "🙋 The seller has been notified and will reply here as soon as possible.",
        "off_platform": ("🙏 Under FunPay rules we can't communicate or trade outside the platform or exchange "
                         "contacts. Let's sort everything out here."),
        "unknown": ("I didn't quite get that 🙂 To deliver order #{order_id} I need your Telegram username "
                    "(e.g. @username). Type \"!seller\" if you have a question."),
        "after_delivery": ("The stars for #{order_id} have already been sent to @{username} ✅ If they don't show up "
                           "within 10 minutes, type \"!seller\"."),
        "change_username_late": ("The stars for #{order_id} have already been paid to @{username}; the recipient "
                                 "can't be changed now. Type \"!seller\" if this is a mistake."),
        "refunded": "💸 Order #{order_id} has been refunded.",
        "completed_thanks": "🙏 Thank you for confirming order #{order_id}!",
        "presale": ("Hi! 👋 Telegram Stars are delivered automatically: pick the lot ({denominations} ⭐), pay and "
                    "send your username — the stars arrive in 1–5 minutes via Fragment. Any questions — write here."),
        "faq_gift": "Stars are credited directly to the account by username (not as a gift).",
        "faq_safe": ("It's safe: the stars are bought on Telegram's official platform Fragment. We only need your "
                     "username — never share codes or passwords with anyone."),
        "faq_time": "Usually the stars arrive within 1–5 minutes after payment and username. {status_hint}",
    },
}

REASONS = {
    "ru": {
        "bad_format": "это не похоже на username Telegram",
        "not_found": "аккаунт с таким username не найден в Telegram",
        "cannot_receive": "на этот аккаунт нельзя отправить звёзды (например, это бот или канал)",
        "blacklisted": "этот аккаунт не может быть использован для получения заказа",
        "no_username": "в сообщении нет username",
        "unknown": "Fragment не принял username",
    },
    "en": {
        "bad_format": "that doesn't look like a Telegram username",
        "not_found": "no Telegram account with this username",
        "cannot_receive": "stars can't be sent to this account (a bot or channel?)",
        "blacklisted": "this account can't be used for this order",
        "no_username": "there is no username in your message",
        "unknown": "Fragment rejected the username",
    },
}

STATUS_TEXT = {
    "ru": {
        "new": "обрабатывается", "awaiting_username": "ждём ваш username в Telegram",
        "pending_approval": "проходит проверку безопасности", "ready": "в очереди на отправку, это 1–2 минуты",
        "purchasing": "звёзды покупаются на Fragment", "sent": "платёж отправлен, сеть подтверждает",
        "delivered": "звёзды отправлены ✅", "completed": "завершён ✅", "refunded": "оформлен возврат",
        "manual": "передан продавцу, он ответит здесь", "blocked": "передан продавцу",
    },
    "en": {
        "new": "processing", "awaiting_username": "waiting for your Telegram username",
        "pending_approval": "security check", "ready": "queued, 1–2 minutes", "purchasing": "buying on Fragment",
        "sent": "payment sent, confirming", "delivered": "stars sent ✅", "completed": "completed ✅",
        "refunded": "refunded", "manual": "handed to the seller", "blocked": "handed to the seller",
    },
}

# Пользовательские тексты из настроек приложения: {lang: {key: text}}
OVERRIDES: dict[str, dict[str, str]] = {}

# Переменные, доступные в шаблонах (для редактора сообщений)
TEMPLATE_VARS = {
    "order_id": "ABCD1234", "buyer": "Покупатель", "stars": 100, "username": "durov", "name_part": " (Pavel)",
    "reason": "аккаунт не найден", "attempt": 1, "max_attempts": 3, "status_text": "ждём username",
    "denominations": "50, 75, 100", "status_hint": "", "returning": "Рады видеть вас снова 🙌 ",
    "last_hint": "Прошлый заказ вы получали на @durov — если аккаунт тот же, просто пришлите его ещё раз.\n",
}


def set_overrides(data: dict | None) -> None:
    OVERRIDES.clear()
    for lang, items in (data or {}).items():
        if isinstance(items, dict):
            OVERRIDES[lang] = {k: str(v) for k, v in items.items() if k in TEMPLATES.get(lang, {}) and str(v).strip()}


def template(lang: str, key: str) -> str:
    lang = lang if lang in TEMPLATES else "ru"
    return OVERRIDES.get(lang, {}).get(key) or TEMPLATES[lang][key]


def validate_template(text: str) -> str | None:
    """None — шаблон корректен, иначе текст ошибки."""
    try:
        text.format(**TEMPLATE_VARS)
    except KeyError as e:
        return f"неизвестная переменная {{{e.args[0]}}}"
    except (ValueError, IndexError) as e:
        return f"ошибка в фигурных скобках: {e}"
    if contains_forbidden_link(text):
        return "в тексте есть ссылка на сторонний ресурс — по правилам FunPay (п. 1.12, 2.1.13) так нельзя"
    return None


def render(lang: str, key: str, **kw) -> str:
    lang = lang if lang in TEMPLATES else "ru"
    try:
        return template(lang, key).format(**kw)
    except (KeyError, ValueError, IndexError):
        return TEMPLATES[lang][key].format(**kw)


def reason(lang: str, code: str, fallback: str = "") -> str:
    lang = lang if lang in REASONS else "ru"
    return REASONS[lang].get(code) or fallback or REASONS[lang]["unknown"]


def status_text(lang: str, status: str) -> str:
    lang = lang if lang in STATUS_TEXT else "ru"
    return STATUS_TEXT[lang].get(status, status)


# --- распознавание команд, вопросов и опасных тем
COMMANDS = {
    "status": ("!статус", "!status"),
    "help": ("!помощь", "!help", "!команды"),
    "seller": ("!продавец", "!seller", "!admin"),
}

# Слово «Telegram» здесь — название товара, поэтому само по себе не считается уводом с площадки.
OFF_PLATFORM_HINTS = ("discord", "дискорд", "whatsapp", "ватсап", "vk.com", "напрямую", "без фанпей", "без funpay",
                      "мимо фанпей", "в обход", "скинь контакт", "номер телефона", "добавь в друзья", "напиши мне в",
                      "пиши в лс", "пиши в тг", "напиши в тг", "свяжись со мной", "для связи", "дешевле напрямую")

FAQ = [
    ("faq_gift", re.compile(r"подар|gift", re.I)),
    ("faq_safe", re.compile(r"безопасн|не скам|не обман|scam|safe|законн|бан", re.I)),
    ("faq_time", re.compile(r"когда|скоро|долго|сколько ждать|how long|when", re.I)),
]

QUESTION_RE = re.compile(r"\?|^(как|когда|что|почему|зачем|сколько|где|можно|а если|how|when|what|why|can)\b", re.I)
STARS_TOPIC_RE = re.compile(r"зв[её]зд|stars?\b|⭐|старс", re.I)
_LINK_RE = re.compile(r"(https?://|www\.)\S+", re.I)


def detect_command(text: str) -> str | None:
    t = (text or "").strip().lower()
    for cmd, aliases in COMMANDS.items():
        if any(t.startswith(a) for a in aliases):
            return cmd
    return None


def detect_faq(text: str) -> str | None:
    for key, rx in FAQ:
        if rx.search(text or ""):
            return key
    return None


def is_question(text: str) -> bool:
    return bool(QUESTION_RE.search((text or "").strip()))


def is_off_platform_request(text: str) -> bool:
    t = f" {(text or '').lower()} "
    return any(h in t for h in OFF_PLATFORM_HINTS)


def contains_forbidden_link(text: str) -> bool:
    for m in _LINK_RE.finditer(text or ""):
        if "funpay.com" not in m.group(0).lower():
            return True
    return False


def detect_lang(text: str, default: str = "ru") -> str:
    if not text:
        return default
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    lat = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    if cyr == 0 and lat > 12 and "@" not in text:
        return "en"
    return default
