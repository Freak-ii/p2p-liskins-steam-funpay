"""Описание полей настроек для формы в приложении (подписи, типы, подсказки)."""
from __future__ import annotations

# (раздел, ключ, подпись, тип, подсказка). Типы: bool int float str secret list choice:a|b|c
SETTINGS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("general", [
        ("dry_run", "Тестовый режим (dry-run)", "bool", "Бот читает FunPay и Fragment и ведёт CRM, но ничего не отправляет и не покупает"),
        ("autostart", "Запускать бота при открытии приложения", "bool", ""),
        ("backup_keep_days", "Хранить резервные копии базы, дней", "int", "Копия раз в сутки в data/backups"),
        ("language", "Язык сообщений покупателю по умолчанию", "choice:ru|en", "Бот переключится на English, если покупатель пишет на английском"),
        ("log_level", "Подробность журнала", "choice:INFO|WARNING|DEBUG", ""),
    ]),
    ("funpay", [
        ("golden_key", "golden_key (cookie FunPay)", "secret", "Браузер → funpay.com → DevTools → Application → Cookies → golden_key"),
        ("poll_interval", "Опрос FunPay, сек", "int", "Не ставьте меньше 4"),
        ("node_id", "Раздел FunPay (node_id)", "int", "Telegram → Звёзды = 2418"),
        ("game_id", "id игры для поднятия лотов", "int", "0 — не поднимать"),
        ("order_match_regex", "Какие заказы обрабатывать (regex по описанию)", "str", ""),
        ("order_exclude_regex", "Какие заказы не трогать (regex)", "str", "Например, «Подарком» и «С заходом на аккаунт»"),
        ("auto_raise", "Автоподнятие лотов", "bool", ""),
        ("raise_interval_minutes", "Интервал поднятия, мин", "int", ""),
        ("stock_sync", "«Наличие» лотов по балансу кошелька", "bool", "Лот скрывается, когда TON не хватает на номинал"),
        ("max_display_amount", "Максимум показываемого наличия", "int", ""),
        ("deactivate_when_empty", "Скрывать лот, когда баланса не хватает", "bool", ""),
        ("presale_replies", "Отвечать на вопросы о звёздах без заказа", "bool", "Один ответ в чат раз в 6 часов"),
        ("auto_create_lots", "Выставлять недостающие лоты автоматически", "bool", "По образцу вашего лота или пустой форме раздела"),
        ("lot_price_per_star", "Стартовая цена нового лота, ₽ за звезду", "float", "Дальше цену ведёт RTBA / позиция"),
        ("lot_description", "Описание новых лотов «По username»", "str", "Без ссылок и контактов"),
        ("gift_lot_description", "Описание новых лотов «Подарком»", "str", ""),
    ]),
    ("gifts", [
        ("enabled", "Выдача подарками Telegram", "bool", "Заказы раздела «Звёзды → Подарком»"),
        ("api_id", "api_id Telegram", "int", "my.telegram.org → API development tools"),
        ("api_hash", "api_hash Telegram", "secret", ""),
        ("phone", "Номер аккаунта", "str", "Вход — в разделе «Юзеры / Подарки»"),
        ("mode", "Подбор подарков", "choice:convert|price", "convert — по сумме конвертации (13/21/43/85), price — по цене"),
        ("per_gift_penalty", "«Штраф» за каждый подарок, ⭐", "float", "Больше — меньше подарков в наборе"),
        ("max_gifts_per_order", "Подарков в заказе, максимум", "int", ""),
        ("gift_message", "Подпись к подарку", "str", "{order_id}, {buyer}; без ссылок"),
        ("hide_sender_name", "Скрыть имя отправителя", "bool", ""),
        ("pause_between_gifts", "Пауза между подарками, сек", "float", ""),
        ("stock_sync", "Наличие лотов «Подарком» по балансу звёзд", "bool", ""),
        ("deactivate_when_empty", "Выключать лот, когда звёзд не хватает", "bool", ""),
        ("max_display_amount", "Максимум показываемого наличия", "int", ""),
        ("low_stars_alert", "Предупреждать, когда звёзд меньше", "int", ""),
        ("pilot_orders", "Пилот: первые N заказов подарками — с одобрения", "int", "0 — выключено"),
        ("max_order_stars", "Набор дороже, ⭐ — с одобрения", "int", "0 — без лимита"),
        ("allowed_gift_ids", "Только эти подарки (id)", "list", "Пусто — любые обычные"),
    ]),
    ("gifts.topup", [
        ("enabled", "Автопополнение звёзд аккаунта через Fragment", "bool", "Покупка самому себе с кошелька TON"),
        ("min_stars", "Пополнять, когда свободных звёзд меньше", "int", ""),
        ("amount", "Покупать за раз, ⭐", "int", "Fragment продаёт от 50"),
        ("username", "@username аккаунта", "str", "Пусто — из подключённого аккаунта"),
        ("cooldown_minutes", "Пауза после пополнения, мин", "int", "Звёзды от Fragment приходят не мгновенно"),
        ("max_per_day", "Не больше пополнений в сутки, ⭐", "int", "0 — без лимита"),
    ]),
    ("autopilot", [
        ("enabled", "Автопилот", "bool", "Напоминания о подтверждении, файлы для поддержки, вывод с FunPay"),
        ("confirm_reminder_hours", "Напомнить о подтверждении через, ч", "float", "0 — не напоминать"),
        ("support_after_hours", "Файл в поддержку FunPay через, ч", "float", ""),
        ("support_folder", "Папка файлов для поддержки", "str", ""),
    ]),
    ("autopilot.withdraw", [
        ("enabled", "Вывод с FunPay на кошелёк автоматически", "bool", ""),
        ("min_wallet_ton", "Когда на кошельке меньше, GRAM (TON)", "float", ""),
        ("min_funpay_rub", "И на балансе FunPay не меньше, ₽", "float", ""),
        ("keep_rub", "Оставлять на FunPay, ₽", "float", ""),
        ("max_per_day_rub", "Не больше в сутки, ₽", "float", ""),
        ("method", "Способ вывода (значение из списка FunPay)", "str", "Выбирается в разделе «Крипта»"),
        ("wallet", "Куда выводить", "str", "Пусто — кошелёк бота"),
        ("require_approval", "Каждый вывод — после кнопки", "bool", ""),
    ]),
    ("fragment", [
        ("cookies", "Cookie Fragment", "secret", "stel_ssid=…; stel_dt=…; stel_token=…; stel_ton_token=… — из браузера, где на fragment.com подключён кошелёк бота и пройден KYC"),
        ("session_warn_days", "Напомнить обновить cookies через, дней", "int", "Сессия Fragment живёт 2–4 недели; 0 — не напоминать"),
        ("session_check_minutes", "Проверять сессию перед заказом, если прошло больше, мин", "int", ""),
        ("hide_lots_when_down", "Скрывать лоты, пока сессия истекла или нет KYC", "bool", ""),
        ("max_ton_per_star", "Не покупать дороже, TON за звезду", "float", "0 — без ограничения"),
        ("max_cost_share_pct", "Не покупать, если себестоимость больше, % суммы заказа", "float", "Защита от продажи в убыток"),
        ("min_interval", "Пауза между запросами к Fragment, сек", "float", ""),
    ]),
    ("ton", [
        ("mnemonic", "Сид-фраза Tonkeeper (12 или 24 слова)", "secret", "Tonkeeper → Настройки → Резервная копия. Хранится только в config.yaml на этом компьютере. Используйте отдельный кошелёк только для бота"),
        ("address", "Адрес кошелька бота", "str", "Можно не заполнять — бот вычислит адрес из сид-фразы. Если заполнен — тот же кошелёк, что подключён к Fragment"),
        ("version", "Версия кошелька", "choice:v5r1|v4r2", "Tonkeeper: Настройки → Активные адреса (W5 = v5r1). Если адрес указан, версия определится сама"),
        ("mnemonic_type", "Формат сид-фразы", "choice:auto|bip39|ton", "auto — бот сам подберёт: 12 слов Tonkeeper обычно BIP39, 24 слова — TON"),
        ("toncenter_endpoint", "toncenter API", "str", ""),
        ("toncenter_api_key", "Ключ toncenter API", "secret", "Необязательно, без ключа — 1 запрос в секунду (@tonapibot)"),
        ("low_balance_ton", "Предупреждать при балансе меньше, TON", "float", ""),
        ("fee_reserve_ton", "Запас на комиссию сети, TON", "float", ""),
    ]),
    ("rates", [
        ("source", "Курс TON/RUB", "choice:auto|manual", "auto — CoinGecko, manual — только значение ниже"),
        ("ton_rub", "Курс вручную / запасной, ₽", "float", ""),
        ("refresh_minutes", "Обновлять курс, мин", "int", ""),
    ]),
    ("economics", [
        ("funpay_fee_pct", "Комиссия FunPay с вашей цены, %", "float", "Обычно 0: комиссию платит покупатель сверху"),
        ("withdraw_fee_pct", "Комиссия вывода с FunPay, %", "float", "Зависит от способа вывода"),
        ("withdraw_fee_fixed_rub", "Фикс. комиссия за один вывод, ₽", "float", ""),
        ("exchange_spread_pct", "Покупаете TON дороже биржи на, %", "float", "Используется, пока нет записей о пополнениях"),
        ("deposit_fee_ton", "Комиссия перевода TON на кошелёк, TON", "float", ""),
        ("typical_topup_ton", "Обычное пополнение, TON", "float", ""),
        ("gas_per_purchase_ton", "Комиссия сети за покупку, TON", "float", ""),
        ("balance_mismatch_alert_ton", "Тревога при расхождении баланса с учётом, TON", "float", ""),
    ]),
    ("deal", [
        ("max_username_attempts", "Попыток прислать username", "int", "После — заказ уходит продавцу"),
        ("reminder_after_minutes", "Напомнить про username через, мин", "int", ""),
        ("admin_alert_after_minutes", "Позвать продавца, если покупатель молчит, мин", "int", ""),
        ("purchase_retry_limit", "Повторов покупки", "int", "Только когда транзакция гарантированно не исполнилась"),
        ("purchase_pause_seconds", "Пауза между покупками, сек", "float", ""),
        ("confirm_wait_minutes", "Ждать подтверждения сети, мин", "int", ""),
        ("auto_refund_bad_recipient", "Авто-возврат, если username так и не получен", "bool", ""),
        ("auto_refund_no_funds", "Авто-возврат, если не хватает TON", "bool", ""),
        ("message_rate_limit_seconds", "Пауза между сообщениями в чат, сек", "float", ""),
    ]),
    ("risk", [
        ("enabled", "Риск-система включена", "bool", ""),
        ("manual_review_score", "Порог ручной проверки", "int", ""),
        ("block_score", "Порог блокировки", "int", ""),
        ("large_order_stars", "Крупный заказ — от, звёзд", "int", ""),
        ("max_orders_per_buyer_per_day", "Заказов от покупателя в сутки (норма)", "int", ""),
        ("max_recipients_per_buyer_per_day", "Разных получателей у покупателя в сутки (норма)", "int", ""),
        ("blacklist_funpay_users", "Чёрный список ников FunPay", "list", "По одному в строке"),
        ("blacklist_usernames", "Чёрный список username Telegram", "list", "По одному в строке"),
    ]),
    ("safety", [
        ("pilot_orders", "Пилот: первые N боевых покупок — с одобрения", "int",
         "Бот готовит покупку и ждёт /approve, пока не пройдут N реальных оплат. 0 — выключено"),
        ("daily_ton_limit", "Лимит трат за сутки, TON", "float", "Сверх лимита — только с одобрения. 0 — без лимита"),
        ("max_order_ton", "Заказ дороже, TON — с одобрения", "float", "0 — без ограничения"),
    ]),
    ("rtba", [
        ("enabled", "RTBA (автоцена) включена", "bool", ""),
        ("interval_seconds", "Проверять рынок каждые, сек", "int", ""),
        ("strategy", "Стратегия", "choice:top1|topN|median", "top1 — дешевле лидера, topN — встать на позицию N, median — медиана"),
        ("target_position", "Позиция для topN", "int", ""),
        ("undercut_abs", "Шаг ниже конкурента, ₽", "float", ""),
        ("undercut_pct", "Шаг ниже конкурента, %", "float", ""),
        ("min_margin_pct", "Минимальная маржа над себестоимостью, %", "float", ""),
        ("floor_price_per_star", "Жёсткий минимум, ₽ за звезду", "float", "0 — только по себестоимости"),
        ("ceiling_price_per_star", "Потолок, ₽ за звезду", "float", "0 — без потолка"),
        ("fallback_ton_per_star", "Цена звезды в TON до первой покупки", "float", ""),
        ("min_change", "Не менять цену меньше чем на, ₽", "float", ""),
        ("max_updates_per_hour", "Изменений цены лота в час, максимум", "int", ""),
        ("competitor_min_amount", "Игнорировать лоты с наличием меньше", "int", ""),
        ("competitor_only_online", "Учитывать только продавцов онлайн", "bool", ""),
        ("ignore_competitors", "Игнорировать продавцов", "list", "По одному нику в строке"),
        ("method_filter", "Способ получения у конкурентов (regex)", "str", "username — сравниваемся только с «По username»"),
    ]),
    ("notifications.telegram", [
        ("enabled", "Уведомления в Telegram", "bool", ""),
        ("bot_token", "Токен бота (@BotFather)", "secret", "Отдельный бот, не тот, что у MannBot: иначе команды будут конфликтовать"),
        ("admin_chat_ids", "chat_id администраторов", "list", "По одному в строке (узнать: @userinfobot)"),
        ("commands", "Управление командами из Telegram", "bool", ""),
    ]),
    ("notifications", [
        ("min_level", "Минимальный уровень уведомлений", "choice:INFO|WARNING|CRITICAL", ""),
        ("throttle_seconds", "Повторять одинаковый алерт не чаще, сек", "int", ""),
        ("daily_report_hour", "Час ежедневного отчёта", "int", ""),
    ]),
    ("dashboard", [
        ("operator_token", "Токен оператора", "secret", "Заказы, чат, клиенты, пауза. Без настроек и денег. Не короче 12 символов"),
        ("viewer_token", "Токен наблюдателя", "secret", "Только просмотр. Не короче 12 символов"),
    ]),
    ("sla", [
        ("order_total_minutes", "Заказ должен быть выдан за, мин", "int", ""),
    ]),
    ("ozon", [
        ("enabled", "Продавать на Ozon", "bool", "Кабинет «Цифровые товары» с доступом к Seller API"),
        ("client_id", "Client-Id", "str", "Кабинет Ozon → Настройки → Seller API"),
        ("api_key", "Api-Key", "secret", "Ключ с правами на отправления и товары кабинета цифровых товаров"),
        ("poll_interval", "Проверять новые заказы Ozon, сек", "int", ""),
        ("code_prefix", "Префикс кодов активации", "str", "Коды вида TGS-ABCD-EFGH-JKMN"),
        ("stock_sync", "Остатки на Ozon по балансу кошелька", "bool", "Товар уходит в 0, когда TON не хватает"),
        ("max_stock", "Максимальный остаток на Ozon", "int", ""),
        ("commission_pct", "Комиссия Ozon, %", "float", "Игровая валюта — 10%, прочие цифровые товары — 15%"),
        ("withdraw_fee_pct", "Комиссия вывода с Ozon, %", "float", "Обычно 0"),
    ]),
    ("ozon.activation_bot", [
        ("enabled", "Бот активации в Telegram", "bool", "Покупатель присылает боту код и @username"),
        ("bot_token", "Токен бота активации (@BotFather)", "secret",
         "Отдельный бот, не тот, что для уведомлений: его будут видеть покупатели"),
    ]),
    ("ozon.activation_web", [
        ("enabled", "Страница активации на сайте", "bool", "Нужен публичный адрес (VPS или туннель)"),
        ("host", "Адрес страницы", "str", "0.0.0.0 — доступна извне"),
        ("port", "Порт страницы", "int", ""),
        ("public_url", "Публичная ссылка", "str", "Для инструкции в карточке, например https://stars.example.ru"),
    ]),
]

SECTION_TITLES = {
    "general": "Основное", "funpay": "FunPay", "fragment": "Fragment", "ton": "TON-кошелёк", "rates": "Курс TON",
    "economics": "Деньги: комиссии", "deal": "Сделка", "risk": "Риск-система",
    "safety": "Предохранители денег", "rtba": "RTBA — автоцена",
    "notifications.telegram": "Telegram", "notifications": "Уведомления", "dashboard": "Доступ к панели (роли)",
    "sla": "Сроки (SLA)", "ozon": "Ozon: продажи", "ozon.activation_bot": "Ozon: бот активации",
    "ozon.activation_web": "Ozon: страница активации",
    "gifts": "Юзеры / Подарки: выдача подарками", "gifts.topup": "Подарки: автопополнение звёзд",
    "autopilot": "Автопилот", "autopilot.withdraw": "Автопилот: вывод с FunPay",
}

MESSAGE_TITLES = {
    "greeting_need_username": "После оплаты: просьба прислать username",
    "greeting_with_username": "После оплаты: username указан при покупке",
    "username_ok": "Username проверен", "bad_username": "Неверный username", "bad_username_final": "Username так и не получен",
    "delivered": "Звёзды отправлены (благодарность + просьба подтвердить)", "slow_network": "Сеть подтверждает дольше обычного",
    "awaiting_approval": "Проверка безопасности", "delay": "Задержка выдачи", "reminder": "Напоминание про username",
    "status": "Ответ на !статус", "help": "Ответ на !помощь", "call_seller": "Продавец вызван",
    "off_platform": "Отказ от общения вне FunPay", "unknown": "Непонятное сообщение", "after_delivery": "Вопрос после выдачи",
    "change_username_late": "Смена username после оплаты", "refunded": "Возврат", "completed_thanks": "Спасибо за подтверждение",
    "presale": "Вопрос о звёздах без заказа", "faq_gift": "FAQ: подарком?", "faq_safe": "FAQ: это безопасно?",
    "faq_time": "FAQ: когда придут?", "faq_not_arrived": "FAQ: звёзды не пришли",
    "faq_refund": "FAQ: просьба о возврате/отмене", "faq_premium": "FAQ: про Telegram Premium",
    "faq_where": "FAQ: где посмотреть звёзды",
    "review_reply": "Ответ на отзыв 4–5★ (публичный, под отзывом)",
    "greeting_need_username_gift": "Подарки: после оплаты, просьба прислать username",
    "greeting_with_username_gift": "Подарки: после оплаты, username указан",
    "username_ok_gift": "Подарки: аккаунт найден", "delivered_gift": "Подарки отправлены (как конвертировать + подтвердить)",
    "faq_gift_gift": "Подарки: FAQ «как получить звёзды»", "after_delivery_gift": "Подарки: вопрос после выдачи",
    "confirm_reminder": "Напоминание подтвердить заказ (автопилот, одно)",
}


def get_value(cfg, section: str, key: str):
    node = cfg
    for part in section.split("."):
        node = node.get(part, {})
    return node.get(key)


def parse_form(form, section: str, fields) -> dict:
    """Значения формы → вложенный dict для Supervisor.save()."""
    out: dict = {}
    for key, _label, typ, _help in fields:
        name = f"{section}__{key}"
        if typ == "bool":
            val = form.get(name) == "on"
        elif name not in form:
            continue
        else:
            raw = str(form.get(name, "")).strip()
            if typ == "int":
                val = int(float(raw or 0))
            elif typ == "float":
                val = float(raw.replace(",", ".") or 0)
            elif typ == "list":
                items = [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()]
                val = [int(x) if x.lstrip("-").isdigit() and key == "admin_chat_ids" else x for x in items]
            else:
                val = raw
        node = out
        for part in section.split("."):
            node = node.setdefault(part, {})
        node[key] = val
    return out
