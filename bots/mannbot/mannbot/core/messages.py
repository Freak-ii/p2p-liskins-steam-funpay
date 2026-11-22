"""Шаблоны сообщений покупателю (ru/en).

Правила FunPay, которые учтены в текстах:
  • п. 2.1.2 — подтверждение заказа просим ТОЛЬКО после фактической выдачи ключей;
  • п. 1.1   — никаких контактов (Telegram/Discord и т. п.), всё общение только в чате FunPay;
  • п. 1.12  — никаких ссылок на файлообменники; единственные ссылки — steamcommunity.com;
  • п. 2.1.4 / 3.1.2 — вопросы покупателя не игнорируются: на непонятное сообщение
    бот отвечает подсказкой и зовёт продавца.
"""
from __future__ import annotations

import re

TEMPLATES: dict[str, dict[str, str]] = {
    "ru": {
        "greeting": (
            "Здравствуйте, {buyer}! Спасибо за оплату заказа #{order_id} 🎉\n"
            "📦 Товар: Mann Co. Supply Crate Key × {keys}\n"
            "🤖 Выдача автоматическая — через обмен в Steam, обычно 1–3 минуты.\n\n"
            "👉 Отправьте в этот чат вашу ССЫЛКУ НА ОБМЕН Steam. Она выглядит так:\n"
            "https://steamcommunity.com/tradeoffer/new/?partner=123456789&token=AbCdEfGh\n"
            "Где взять: Steam → Инвентарь → Предложения обмена → «Кто может отправлять мне "
            "предложения обмена?» → блок «Сторонние сайты» (steamcommunity.com/my/tradeoffers/privacy).\n\n"
            "⚠️ Перед отправкой проверьте:\n"
            "• на аккаунте нет ограничений на обмен, инвентарь TF2 не переполнен;\n"
            "• подключён мобильный Steam Guard (не менее 7 дней) — иначе Steam задержит обмен до 15 дней.\n\n"
            "🛡 Безопасность: обмен придёт ТОЛЬКО от аккаунта «{bot_name}»{bot_level} с комментарием "
            "«FunPay #{order_id}». Мы НИКОГДА не просим ваши предметы, логин, коды Steam Guard или API-ключ. "
            "Любые другие «боты» — мошенники.\n"
            "Пожалуйста, подтверждайте заказ только после того, как ключи окажутся у вас в инвентаре."
        ),
        "link_received": "✅ Ссылка получена, проверяю аккаунт {partner} и готовлю обмен на {keys} ключ(а/ей)…",
        "bad_data": (
            "❗ {buyer}, с вашими данными проблема: {reason}.\n"
            "Обмен по таким данным отправить невозможно — нужна НАСТОЯЩАЯ рабочая ссылка на обмен "
            "от вашего аккаунта Steam.\n\n"
            "Повторяю, как её получить:\n"
            "Steam → Инвентарь → Предложения обмена → «Кто может отправлять мне предложения обмена?» → "
            "скопируйте ссылку из блока «Сторонние сайты» ЦЕЛИКОМ (с partner= и token=).\n"
            "Пример: https://steamcommunity.com/tradeoffer/new/?partner=123456789&token=AbCdEfGh\n"
            "Попытка {attempt} из {max_attempts}."
        ),
        "bad_data_final": (
            "⛔ {buyer}, корректную ссылку на обмен получить не удалось ({reason}). "
            "Я передал заказ #{order_id} продавцу — он ответит вам здесь, в чате, в ближайшее время. "
            "Пожалуйста, не открывайте спор: мы решим вопрос."
        ),
        "trade_hold": (
            "⏳ На вашем аккаунте Steam действует задержка обмена {days} дн. — у вас не подключён мобильный "
            "Steam Guard (или подключён менее 7 дней назад). При такой задержке ключи придут только через "
            "{days} дн.\n{policy_text}"
        ),
        "trade_hold_fix": (
            "Подключите мобильный аутентификатор Steam Guard, подождите 7 дней и пришлите ссылку снова — "
            "или напишите «!отправить», если согласны получить ключи с задержкой."
        ),
        "trade_hold_send": "Обмен всё равно будет отправлен — ключи станут доступны после окончания задержки.",
        "trade_hold_manual": "Продавец получил уведомление и свяжется с вами здесь, в чате.",
        "offer_sent": (
            "📨 Обмен отправлен! {keys} × Mann Co. Supply Crate Key.\n"
            "Принять: https://steamcommunity.com/tradeoffer/{offer_id}/\n"
            "Отправитель: «{bot_name}», комментарий: «FunPay #{order_id}». Проверьте, что в обмене вы НИЧЕГО "
            "не отдаёте — только получаете ключи.\n"
            "Предложение действует {ttl} ч."
        ),
        "success": (
            "🎉 Сделка #{order_id} успешно завершена — {keys} × Mann Co. Supply Crate Key уже в вашем "
            "инвентаре Steam!\n\n"
            "Огромное спасибо, что выбрали нас, {buyer} ❤️\n"
            "Пожалуйста, подтвердите выполнение заказа: кнопка «Подтвердить выполнение заказа» на странице "
            "funpay.com/orders/{order_id}/ — и, если не сложно, оставьте отзыв. Это очень помогает нам.\n"
            "Будем рады видеть вас снова — ключи всегда в наличии! 🔑"
        ),
        "escrow": (
            "✅ Обмен принят, но Steam удерживает ключи из-за задержки обмена на вашем аккаунте. Они "
            "автоматически поступят в инвентарь после окончания задержки. Я напишу, когда всё будет готово."
        ),
        "declined": (
            "↩️ Обмен по заказу #{order_id} был {what}. Ключи по-прежнему ждут вас.\n"
            "Напишите «!повтор», чтобы получить новый обмен, или пришлите новую ссылку на обмен."
        ),
        "api_scam_warning": (
            "🚨 ВНИМАНИЕ! Наш обмен по заказу #{order_id} был отменён НЕ нами. Так бывает, когда мошенники "
            "получили доступ к вашему Steam API-ключу и подменяют обмены.\n"
            "1) Срочно отзовите ключ: steamcommunity.com/dev/apikey\n"
            "2) Смените пароль Steam и завершите все сессии (Steam Guard → «Выйти на всех устройствах»).\n"
            "3) Не принимайте обмены от аккаунтов, кроме «{bot_name}».\n"
            "После этого напишите «!повтор» — мы отправим обмен заново."
        ),
        "resend_limit": "Лимит повторных отправок исчерпан. Продавец уже уведомлён и ответит здесь, в чате.",
        "out_of_stock": (
            "⚠️ {buyer}, ключи временно закончились на складе. Продавец уже уведомлён и выдаст ваш заказ "
            "#{order_id} в ближайшее время — или оформит возврат, если вы предпочтёте."
        ),
        "awaiting_approval": (
            "🔎 Заказ #{order_id} проходит стандартную проверку безопасности — обычно это занимает несколько "
            "минут. Обмен придёт сразу после неё."
        ),
        "reminder": (
            "👋 {buyer}, напоминаю: для получения {keys} ключ(а/ей) по заказу #{order_id} пришлите ссылку на "
            "обмен Steam (https://steamcommunity.com/tradeoffer/new/?partner=...&token=...)."
        ),
        "status": "ℹ️ Заказ #{order_id}: {status_text}",
        "help": (
            "Команды: «!статус» — состояние заказа, «!повтор» — отправить обмен заново, "
            "«!продавец» — позвать продавца. Чтобы получить ключи, просто пришлите ссылку на обмен Steam."
        ),
        "call_seller": "🙋 Продавец уведомлён и ответит вам здесь, в чате, как можно скорее.",
        "off_platform": (
            "🙏 По правилам FunPay мы не можем общаться или проводить сделки вне площадки и не обмениваемся "
            "контактами. Всё решаем здесь, в чате заказа."
        ),
        "unknown": (
            "Не совсем понял сообщение 🙂 Для выдачи ключей по заказу #{order_id} нужна ссылка на обмен Steam. "
            "Если есть вопрос — напишите «!продавец», и продавец ответит."
        ),
        "refunded": "💸 По заказу #{order_id} оформлен возврат средств. Обмен Steam по нему отменён.",
        "completed_thanks": "🙏 Спасибо за подтверждение заказа #{order_id}! Хорошей игры и до новых встреч!",
        "review_thanks": "⭐ Спасибо за отзыв! Нам очень приятно 😊",
        "presale_keys": (
            "Здравствуйте! 👋 Ключи Mann Co. Supply Crate Key в наличии{stock_hint}, выдача автоматическая "
            "через обмен Steam, обычно 1–3 минуты, 24/7.\n"
            "Как купить: выберите количество в лоте и оплатите, затем пришлите в этот чат ссылку на обмен Steam "
            "(steamcommunity.com/my/tradeoffers/privacy). Если остался вопрос, напишите его здесь: продавец ответит."
        ),
    },
    "en": {
        "greeting": (
            "Hi {buyer}! Thank you for paying for order #{order_id} 🎉\n"
            "📦 Item: Mann Co. Supply Crate Key × {keys}\n"
            "🤖 Delivery is automatic via a Steam trade offer, usually 1–3 minutes.\n\n"
            "👉 Please send your Steam TRADE URL to this chat. It looks like:\n"
            "https://steamcommunity.com/tradeoffer/new/?partner=123456789&token=AbCdEfGh\n"
            "Where: Steam → Inventory → Trade Offers → \"Who can send me Trade Offers?\" → \"Third-Party Sites\".\n\n"
            "⚠️ Make sure your account has no trade restrictions, and Steam Guard Mobile Authenticator has been "
            "active for 7+ days, otherwise Steam will hold the trade for up to 15 days.\n\n"
            "🛡 The offer will come ONLY from \"{bot_name}\"{bot_level} with the message \"FunPay #{order_id}\". "
            "We NEVER ask for your items, login, Steam Guard codes or API key.\n"
            "Please confirm the order only after the keys are in your inventory."
        ),
        "link_received": "✅ Trade URL received, checking account {partner} and preparing {keys} key(s)…",
        "bad_data": (
            "❗ {buyer}, there is a problem with your data: {reason}.\n"
            "We need your REAL working Steam trade URL (with partner= and token=).\n"
            "Steam → Inventory → Trade Offers → \"Who can send me Trade Offers?\" → \"Third-Party Sites\".\n"
            "Example: https://steamcommunity.com/tradeoffer/new/?partner=123456789&token=AbCdEfGh\n"
            "Attempt {attempt} of {max_attempts}."
        ),
        "bad_data_final": ("⛔ {buyer}, we could not get a valid trade URL ({reason}). The seller has been "
                           "notified and will reply here shortly. Please don't open a dispute — we'll sort it out."),
        "trade_hold": ("⏳ Your Steam account has a {days}-day trade hold (no Mobile Authenticator for 7+ days).\n"
                       "{policy_text}"),
        "trade_hold_fix": ("Enable the Steam Guard Mobile Authenticator, wait 7 days and resend the URL — or reply "
                           "\"!send\" if you accept the delay."),
        "trade_hold_send": "We'll send the offer anyway — the keys will arrive after the hold ends.",
        "trade_hold_manual": "The seller has been notified and will reply here.",
        "offer_sent": ("📨 Trade offer sent! {keys} × Mann Co. Supply Crate Key.\n"
                       "Accept: https://steamcommunity.com/tradeoffer/{offer_id}/\n"
                       "Sender: \"{bot_name}\", message: \"FunPay #{order_id}\". Make sure you give NOTHING away.\n"
                       "The offer is valid for {ttl} h."),
        "success": ("🎉 Order #{order_id} is complete — {keys} × Mann Co. Supply Crate Key are in your Steam "
                    "inventory!\n\nThank you so much, {buyer} ❤️\nPlease confirm the order with the \"Confirm order "
                    "fulfillment\" button on funpay.com/orders/{order_id}/ and leave a review if you can. "
                    "See you again! 🔑"),
        "escrow": ("✅ The trade was accepted, but Steam holds the keys due to your trade hold. They will arrive "
                   "automatically once it ends."),
        "declined": ("↩️ The trade offer for order #{order_id} was {what}. Reply \"!resend\" to get a new one or "
                     "send a new trade URL."),
        "api_scam_warning": ("🚨 WARNING! Our trade offer for #{order_id} was cancelled NOT by us — your Steam API "
                             "key may be compromised. Revoke it at steamcommunity.com/dev/apikey, change your "
                             "password and deauthorize all devices. Only accept offers from \"{bot_name}\". Then "
                             "reply \"!resend\"."),
        "resend_limit": "Resend limit reached. The seller has been notified and will reply here.",
        "out_of_stock": ("⚠️ {buyer}, keys are temporarily out of stock. The seller has been notified and will "
                         "deliver order #{order_id} shortly, or issue a refund if you prefer."),
        "awaiting_approval": "🔎 Order #{order_id} is passing a standard security check. The offer will follow.",
        "reminder": ("👋 {buyer}, to receive {keys} key(s) for order #{order_id}, please send your Steam trade URL."),
        "status": "ℹ️ Order #{order_id}: {status_text}",
        "help": ("Commands: \"!status\", \"!resend\", \"!seller\". To receive keys just send your Steam trade URL."),
        "call_seller": "🙋 The seller has been notified and will reply here as soon as possible.",
        "off_platform": ("🙏 Under FunPay rules we can't communicate or trade outside the platform or exchange "
                         "contacts. Let's sort everything out here."),
        "unknown": ("I didn't quite get that 🙂 To deliver order #{order_id} I need your Steam trade URL. "
                    "Type \"!seller\" if you have a question."),
        "refunded": "💸 Order #{order_id} has been refunded. The Steam offer has been cancelled.",
        "completed_thanks": "🙏 Thank you for confirming order #{order_id}! Have fun!",
        "review_thanks": "⭐ Thank you for the review! 😊",
        "presale_keys": ("Hi! 👋 Mann Co. Supply Crate Keys are in stock{stock_hint}, delivered automatically via a "
                         "Steam trade offer, usually in 1–3 minutes, 24/7. Pick the quantity, pay for the offer and "
                         "send your Steam trade URL here (steamcommunity.com/my/tradeoffers/privacy)."),
    },
}

REASONS = {
    "ru": {
        "no_link": "в сообщении нет ссылки на обмен",
        "profile_link": "вы прислали ссылку на профиль, а нужна ссылка на ОБМЕН",
        "no_token": "в ссылке нет параметра token — скопируйте её полностью",
        "bad_token": "ссылка обрезана или повреждена",
        "steamid_only": "SteamID недостаточно — нужна ссылка на обмен",
        "other_steam_link": "это другая ссылка Steam, а нужна ссылка на обмен",
        "foreign_link": "ссылка ведёт на сторонний сайт — нужна ссылка steamcommunity.com на обмен",
        "bad_format": "ссылка в неверном формате",
        "invalid_token": "ссылка устарела или недействительна (возможно, вы её сбросили — скопируйте новую)",
        "trade_banned": "на аккаунте Steam запрет на обмен",
        "cannot_trade": "Steam не разрешает отправить обмен этому аккаунту (ограничение аккаунта, "
                        "непривязан Steam Guard или переполнен инвентарь)",
        "inventory_full": "ваш инвентарь TF2 переполнен — освободите место",
        "inventory_private": "инвентарь скрыт настройками приватности",
        "self": "это ссылка нашего собственного аккаунта",
        "blacklisted": "этот аккаунт Steam не может быть использован для получения заказа",
        "network": "Steam временно не отвечает — повторим автоматически",
        "unknown": "Steam отклонил данные",
    },
    "en": {
        "no_link": "there is no trade URL in your message",
        "profile_link": "that's a profile link, but we need a TRADE URL",
        "no_token": "the URL has no token parameter — copy it fully",
        "bad_token": "the URL is cut or damaged",
        "steamid_only": "a SteamID is not enough — we need a trade URL",
        "other_steam_link": "that's a different Steam link, we need the trade URL",
        "foreign_link": "that link points to a third-party site",
        "bad_format": "the URL has a wrong format",
        "invalid_token": "the trade URL is outdated or invalid (copy a fresh one)",
        "trade_banned": "the Steam account is trade banned",
        "cannot_trade": "Steam doesn't allow sending a trade to this account",
        "inventory_full": "your TF2 backpack is full",
        "inventory_private": "your inventory is private",
        "self": "that's our own account's URL",
        "blacklisted": "this Steam account can't be used for this order",
        "network": "Steam is temporarily unavailable — we'll retry",
        "unknown": "Steam rejected the data",
    },
}

STATUS_TEXT = {
    "ru": {
        "new": "обрабатывается", "awaiting_link": "ждём вашу ссылку на обмен Steam",
        "pending_approval": "проходит проверку безопасности", "ready_to_send": "готовим обмен",
        "offer_sent": "обмен отправлен — примите его в Steam", "escrow": "обмен принят, Steam удерживает ключи",
        "delivered": "ключи выданы ✅", "completed": "завершён ✅", "refunded": "оформлен возврат",
        "manual": "передан продавцу, он ответит здесь", "blocked": "передан продавцу",
    },
    "en": {
        "new": "processing", "awaiting_link": "waiting for your Steam trade URL",
        "pending_approval": "security check", "ready_to_send": "preparing the offer",
        "offer_sent": "offer sent — accept it in Steam", "escrow": "accepted, Steam holds the keys",
        "delivered": "keys delivered ✅", "completed": "completed ✅", "refunded": "refunded",
        "manual": "handed to the seller", "blocked": "handed to the seller",
    },
}


# Пользовательские тексты из настроек приложения: {lang: {key: text}}
OVERRIDES: dict[str, dict[str, str]] = {}

# Переменные, доступные в каждом шаблоне (для редактора сообщений)
TEMPLATE_VARS = {
    "order_id": "ABCD1234", "buyer": "Покупатель", "keys": 2, "bot_name": "MannBot", "bot_level": " (уровень 23)",
    "ttl": 12, "partner": "Player", "reason": "ссылка устарела", "attempt": 1, "max_attempts": 3, "days": 15,
    "policy_text": "…", "offer_id": "1234567890", "what": "отклонён", "status_text": "ждём ссылку",
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


# --- распознавание команд и опасных тем
COMMANDS = {
    "resend": ("!повтор", "!resend", "!retry"),
    "status": ("!статус", "!status"),
    "help": ("!помощь", "!help", "!команды"),
    "seller": ("!продавец", "!seller", "!admin"),
    "send_anyway": ("!отправить", "!send"),
}

OFF_PLATFORM_HINTS = ("telegram", "телеграм", "телега", "discord", "дискорд", "вк ", "vk.com", "t.me/",
                      "whatsapp", "ватсап", "напрямую", "без фанпей", "без funpay", "мимо фанпей", "в обход",
                      "скинь контакт", "номер телефона", "добавь в друзья")


# вопрос о ключах/TF2 в чате без заказа — отвечаем один раз шаблоном (звёзды и подарки ведёт StarsBot/TG Gift)
KEYS_TOPIC_RE = re.compile(r"ключ|key|манн|mann|tf\s*2|тф\s*2|team\s*fortress", re.I)


def detect_command(text: str) -> str | None:
    t = (text or "").strip().lower()
    for cmd, aliases in COMMANDS.items():
        if any(t.startswith(a) for a in aliases):
            return cmd
    return None


def is_off_platform_request(text: str) -> bool:
    t = f" {(text or '').lower()} "
    return any(h in t for h in OFF_PLATFORM_HINTS)


def detect_lang(text: str, default: str = "ru") -> str:
    if not text:
        return default
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    lat = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    if cyr == 0 and lat > 12 and "steamcommunity" not in text.lower():
        return "en"
    return default
