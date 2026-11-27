"""Тексты бота и канала. Стиль — как у витрин Tribute: короткие блоки, жирный заголовок, понятная выгода,
одна главная кнопка. Все комиссии называются цифрами, без мелкого шрифта."""
from __future__ import annotations

WELCOME = (
    "✨ <b>{shop}</b>\n"
    "Звёзды, подарки и Premium для Telegram — за пару минут, без входа в аккаунт.\n\n"
    "⭐ <b>Звёзды</b> — от 50 шт., приходят через Fragment\n"
    "🎁 <b>Подарки</b> — отправим любому @username\n"
    "💎 <b>Premium</b> — 3, 6 или 12 месяцев\n"
    "🎮 <b>Steam</b> — ключи и пополнение баланса\n"
    "🛡 <b>VPN и подписки</b> — ключ для Happ, Spotify, Claude/ChatGPT, карты App Store\n"
    "💸 <b>Продать</b> — звёзды или подарки за крипту\n\n"
    "Оплата криптой на кошелёк TON, звёздами, подарками или скинами Steam. Все комиссии видны до оплаты."
)
PRODUCT_WORDS = {"stars": "звёзды", "gift": "подарки", "premium": "Premium", "steam_keys": "ключи Steam"}
PRODUCT_LINE = {"stars": "⭐ <b>Звёзды</b>", "gift": "🎁 <b>Подарки</b>", "premium": "💎 <b>Premium</b>",
                "steam_keys": "🎮 <b>Steam</b>"}
PRODUCT_BUTTON = {"stars": ("⭐ Звёзды", "buy:stars"), "gift": ("🎁 Подарки", "buy:gift"),
                  "premium": ("💎 Premium", "buy:premium"), "steam_keys": ("🔑 Ключи TF2", "buy:steam_keys")}
STEAM_TOPUP_BUTTON = ("💳 Пополнить Steam", "sv:steam_topup")   # услуга «Пополнение Steam» прямо с главного экрана


def goods_text(hidden: set[str] | frozenset = frozenset(), *, capital: bool = True) -> str:
    """«Звёзды, подарки и Premium» — только то, что сейчас продаётся (Пульт Орбиты центра)."""
    words = [w for p, w in PRODUCT_WORDS.items() if p not in hidden] or ["товары для Telegram"]
    text = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " и " + words[-1]
    return text[:1].upper() + text[1:] if capital else text


def strip_lines(text: str, hidden: set[str] | frozenset, lines: dict[str, str]) -> str:
    """Убрать строки текста, которые начинаются с маркера снятого товара."""
    marks = [lines[p] for p in hidden if p in lines]
    return "\n".join(ln for ln in text.split("\n") if not any(ln.startswith(m) for m in marks))


def welcome(shop: str, hidden: set[str] | frozenset = frozenset()) -> str:
    text = WELCOME.format(shop=shop)
    if not hidden:
        return text
    text = strip_lines(text, hidden, PRODUCT_LINE)
    tg = [p for p in ("stars", "gift", "premium") if p not in hidden]
    head = goods_text(hidden | {"steam_keys"}) + " для Telegram" if tg else goods_text(hidden)
    return text.replace("Звёзды, подарки и Premium для Telegram", head, 1)


def menu_buttons(hidden: set[str] | frozenset = frozenset(), *, steam_topup: bool = False) -> list:
    goods = [PRODUCT_BUTTON[p] for p in PRODUCT_BUTTON if p not in hidden]
    rows = [goods[i:i + 2] for i in range(0, len(goods), 2)]
    if steam_topup:
        rows.append([STEAM_TOPUP_BUTTON])
    return rows + MENU_BUTTONS[2:]


MENU_BUTTONS = [
    [("⭐ Звёзды", "buy:stars"), ("🎁 Подарки", "buy:gift")],
    [("💎 Premium", "buy:premium"), ("🔑 Ключи TF2", "buy:steam_keys")],
    [("🛡 VPN и подписки", "buy:service")],
    [("💸 Продать", "sell"), ("👛 Баланс", "balance")],
    [("🤝 Пригласить друга", "ref"), ("ℹ️ Комиссии", "fees")],
    [("🧾 Мои заказы", "orders"), ("🏅 Мой уровень", "level")],
    [("🛟 Поддержка", "support")],
]
SERVICES_HEAD = ("🛡 <b>VPN и подписки</b>\nПеред оплатой покажем, как именно вы получите товар — "
                 "оплачивайте, только если всё подходит.\n\n")
CLOSED = "⏸ Этот раздел сейчас недоступен — мы уже чиним. Загляните чуть позже или выберите другое."
ASK_STARS = "⭐ <b>Сколько звёзд?</b>\nВыберите или пришлите число от {lo} до {hi}."
ASK_RECIPIENT = ("👤 <b>Кому отправить?</b>\nПришлите @username получателя "
                 "или нажмите «Себе».")
ASK_TRADE = ("🔗 Пришлите вашу <b>ссылку на обмен Steam</b>\n"
             "(Steam → Инвентарь → Предложения обмена → Кто может отправлять мне предложения)")
ASK_KEYS = ("🔑 <b>{title}</b>\nКлюч продаётся на Торговой площадке Steam ≈ за <b>{usd:g} $</b> "
            "(после комиссии Steam) — так пополняется баланс.\nСколько ключей? (1–{hi})")
NEED_USERNAME = "Не вижу @username. Пришлите его в виде @name (латиница, 5–32 символа)."
CHOOSE_METHOD = "💳 <b>Как оплатить?</b>\n\n{lines}"
QUOTE_HEAD = "🧾 <b>{title}</b>\nПолучатель: <b>{to}</b>\n\n"
POST_OPEN = ("⚡ <b>{shop}</b>\n"
             "Telegram Stars, подарки, Premium и ключи Steam с автовыдачей за пару минут.\n\n"
             "Нажмите кнопку ниже — магазин откроется прямо здесь.")
PAY_WALLET = ("💎 Переведите <b>{amount}</b> на кошелёк магазина (сеть TON):\n<code>{address}</code>\n"
              "Комментарий к переводу: <code>{comment}</code>\n\n"
              "Сумма и комментарий должны совпасть — по ним платёж найдётся сам, обычно за 1–2 минуты. "
              "Счёт действует {ttl} мин.")
PAY_GIFT = ("🎁 Передайте подарки на аккаунт <b>{account}</b> на сумму от <b>{stars} ⭐</b> по цене продажи "
            "(её видно в подарке: «Продать за N ⭐»).\nВ подписи укажите <code>{oid}</code>. "
            "Как только подарки придут, заказ выдастся. Ждём {ttl} мин.")
PAY_SKINS = ("🎮 Отправьте предложение обмена со скинами CS2/Dota 2/TF2 на сумму от <b>{usd:g} $</b> по ценам "
             "{market}:\n{trade}\nВ сообщении к обмену укажите <code>{oid}</code>. "
             "После принятия обмена заказ выдастся. Ждём {ttl} мин.")
PAY_LINK = "Счёт на <b>{amount}</b> действует {ttl} мин.\nПосле оплаты товар придёт автоматически."
FEES = (
    "ℹ️ <b>Комиссии и курсы — честно</b>\n\n"
    "💳 Карта (ЮKassa): <b>+{card:g}%</b> — эквайринг банка\n"
    "🪙 Крипта: курс Bybit сейчас — USDT <b>{usdt} ₽</b>, TON <b>{ton} ₽</b>; конвертация <b>+{crypto:g}%</b>\n"
    "⭐ Звёзды Telegram: 1 ⭐ = <b>{xtr:g} ₽</b>, без комиссии\n"
    "👛 Баланс магазина: без комиссии\n\n"
    "💸 <b>Продажа нам</b>: комиссия <b>{fee:g}%</b>, звёзды — только за крипту, подарки — за звёзды или крипту.\n"
    "🤝 Рефералка: <b>{ref:g}%</b> от покупок друга на ваш бонусный баланс, другу — −{disc:g}% на первый заказ.\n\n"
    "Перед оплатой вы всегда видите разбивку: цена товара, скидка, комиссия, курс и итог."
)
SELL_MENU = ("💸 <b>Продать магазину</b>\n\n⭐ Звёзды → USDT (курс {usd:g} $ за звезду, комиссия {fee:g}%)\n"
             "🎁 Подарок → звёзды или USDT (по цене продажи Telegram, комиссия {fee:g}%)\n\n"
             "Выплаты в USDT — в @CryptoBot на ваш Telegram, звёзды — на звёздный баланс магазина.")
SELL_STARS_ASK = "⭐ Сколько звёзд продаёте? От {lo} до {hi}."
SELL_GIFT_HOW = ("🎁 Передайте подарок <b>{gift}</b> аккаунту <b>{account}</b>\n"
                 "(профиль → подарок → «Передать»). В комментарии укажите код <code>{oid}</code>.\n"
                 "Как только подарок придёт, выплатим автоматически.\n\n{lines}")
BALANCE = ("👛 <b>Баланс</b>\n\n₽ основной: <b>{rub:.2f} ₽</b> (возвраты)\n🎁 бонусы: <b>{bonus:.2f} ₽</b> "
           "(рефералка, тратятся на покупки)\n⭐ звёзды: <b>{stars}</b> (вывод от 50 ⭐)\n\n<b>Последние операции</b>\n{ops}")
REF = ("🤝 <b>Приглашайте друзей</b>\n\nВаша ссылка:\n<code>{link}</code>\n\n"
       "• Вам — <b>{pct:g}%</b> от каждой покупки друга на бонусный баланс\n"
       "• Другу — <b>−{disc:g}%</b> на первый заказ (до {dmax:g} ₽)\n\n"
       "Приглашено: <b>{n}</b> · заработано: <b>{earned:.2f} ₽</b>")
SUPPORT = "🛟 Поддержка: {support}\nУкажите номер заказа — так ответим быстрее."
ORDERS_EMPTY = "Заказов пока нет — самое время начать ✨"

# ------------------------------------------------------------------ посты канала
POST_SHOWCASE = (
    "✨ <b>{shop}</b>\n\n"
    "Всё для Telegram в одном боте:\n"
    "⭐ Звёзды — от {stars_from} ₽ за 50 шт.\n"
    "🎁 Подарки друзьям — от {gift_from} ₽\n"
    "💎 Premium — от {prem_from} ₽\n"
    "🎮 Ключи для пополнения Steam\n\n"
    "⚡️ Выдача за 1–3 минуты, 24/7\n"
    "🔒 Без входа в ваш аккаунт — только @username\n"
    "🧾 Комиссии видны до оплаты: карта, крипта по курсу Bybit, звёзды\n\n"
    "👇 Выберите, что нужно"
)
SHOWCASE_LINE = {"stars": "⭐ Звёзды —", "gift": "🎁 Подарки", "premium": "💎 Premium —", "steam_keys": "🎮 Ключи"}
POST_PRICES = "📈 <b>Цены на сегодня</b>\n\n{rows}\n\nЦены обновляются автоматически. Комиссия оплаты — отдельной строкой при заказе."
POST_SELL = (
    "💸 <b>Продайте звёзды или подарки</b>\n\n"
    "⭐ Звёзды → USDT\n🎁 Подарки → звёзды или USDT\n\n"
    "Комиссия магазина — {fee:g}%, всё остальное — по курсу, который вы видите до сделки.\n"
    "Выплата в @CryptoBot сразу после получения."
)
POST_REF = (
    "🤝 <b>Зовите друзей — получайте {pct:g}%</b>\n\n"
    "С каждой покупки приглашённого — бонусы на ваш баланс. Другу — скидка {disc:g}% на первый заказ.\n"
    "Ссылка — в боте, раздел «Пригласить друга»."
)
