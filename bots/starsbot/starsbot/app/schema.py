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
        ("enabled", "Продавать на FunPay", "bool", "Выключите — режим «Только поставщик»: бот снабжает Telegram-магазин "
         "(ShopBot), GGSEL и другие включённые площадки, а на FunPay не заходит. Свои лоты FunPay он один раз "
         "выключит при запуске"),
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
        ("presale_replies", "Отвечать тем, кто пишет без заказа", "bool", "Один шаблонный ответ на чат (до следующего заказа)"),
        ("auto_create_lots", "Выставлять недостающие лоты автоматически", "bool", "По образцу вашего лота или пустой форме раздела"),
        ("lot_price_per_star", "Стартовая цена нового лота, ₽ за звезду", "float", "Дальше цену ведёт RTBA / позиция"),
        ("lot_description", "Описание новых лотов «По username»", "str", "Без ссылок и контактов"),
        ("gift_lot_description", "Описание новых лотов «Подарком»", "str", ""),
    ]),
    ("gifts", [
        ("enabled", "Выдача подарками Telegram", "bool", "Заказы раздела «Звёзды → Подарком»"),
        ("api_id", "api_id Telegram", "int", "my.telegram.org → API development tools"),
        ("api_hash", "api_hash Telegram", "secret", ""),
        ("phone", "Номер аккаунта", "str", "Вход — в разделе «Подарки»"),
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
        ("method", "Способ вывода (значение из списка FunPay)", "str", "Выбирается в разделе «Кошелёк и автопилот»"),
        ("wallet", "Куда выводить", "str", "Пусто — кошелёк бота"),
        ("require_approval", "Каждый вывод — после кнопки", "bool", ""),
    ]),
    ("fragment", [
        ("cookies", "Cookie Fragment", "secret", "stel_ssid=…; stel_dt=…; stel_token=…; stel_ton_token=… — из браузера, где на fragment.com подключён кошелёк бота и пройден KYC"),
        ("user_agent", "User-Agent браузера с cookie", "str", "Если Fragment отвечает «Access denied»: откройте в том же браузере whatsmyua.info и вставьте строку User-Agent. Пусто — как у FunPay"),
        ("session_warn_days", "Напомнить обновить cookies через, дней", "int", "Сессия Fragment живёт 2–4 недели; 0 — не напоминать"),
        ("session_check_minutes", "Проверять сессию перед заказом, если прошло больше, мин", "int", ""),
        ("keepalive_minutes", "Проверять сессию в простое, раз в мин", "int", "Истёкшая сессия скроет лоты до оплаты, а не после; новые cookie от Fragment сохраняются сами. 0 — выкл."),
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
        ("source", "Курс TON/RUB", "choice:auto|manual", "auto — биржа × P2P USDT (запас CoinGecko), manual — только значение ниже"),
        ("ton_rub", "Курс вручную / запасной, ₽", "float", ""),
        ("usdt_rub", "Почём покупаете USDT, ₽ (0 — живой курс)", "float", "Цена USDT на P2P с переводом"),
        ("refresh_minutes", "Обновлять курс, мин", "int", ""),
    ]),
    ("economics", [
        ("funpay_fee_pct", "Комиссия FunPay с вашей цены, %", "float", "Обычно 0: комиссию платит покупатель сверху"),
        ("withdraw_fee_pct", "Комиссия вывода с FunPay, %", "float", "Зависит от способа вывода"),
        ("withdraw_fee_fixed_rub", "Фикс. комиссия за один вывод, ₽", "float", ""),
        ("cost_rate_rub", "Курс TON для себестоимости, ₽ (0 — по пополнениям)", "float", "Например, цена следующей закупки"),
        ("cost_basis", "Себестоимость", "choice:live|topups", "live — по цене следующей закупки, topups — по средней цене пополнений"),
        ("star_usd", "Цена звезды на Fragment, $", "float", "0.015; 0 — брать из прошлых покупок"),
        ("exchange_spread_pct", "Покупаете TON дороже биржи на, %", "float", "Только для запасного курса CoinGecko (форекс): обычно 6–9"),
        ("trade_fee_pct", "Комиссия биржи за обмен USDT → GRAM, %", "float", "Bybit спот 0.1%"),
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
        ("confirm_reminder_minutes", "Напомнить подтвердить заказ через, мин", "int", "Одно напоминание после выдачи; 0 — не напоминать"),
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
        ("min_profit_pct", "Не покупать в минус: минимальная прибыль заказа, %", "float",
         "Если после комиссий и по текущему курсу прибыль меньше — покупка ждёт одобрения. 0 — выключено"),
        ("price_guard", "Защита цены: поднимать цену лота до безубытка + маржа", "bool",
         "Маржа — «Минимальная маржа» в разделе RTBA. Работает и при выключенном RTBA"),
        ("price_guard_minutes", "Проверять цены лотов каждые, мин", "int", ""),
    ]),
    ("rtba", [
        ("enabled", "RTBA (автоцена) включена", "bool", ""),
        ("interval_seconds", "Проверять рынок каждые, сек", "int", ""),
        ("strategy", "Стратегия", "choice:top1|topN|median", "top1 — дешевле лидера, topN — встать на позицию N, median — медиана"),
        ("target_position", "Позиция для topN", "int", ""),
        ("buyer_markup_pct", "Наценка FunPay для покупателя, %", "float",
         "Покупатель платит цену лота + столько процентов (лот 80 ₽ → покупатель 91,25 ₽ = 14,06%). Цены конкурентов в списке — уже с наценкой"),
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
        ("preset", "Пресет цен", "choice:custom|turnover|balanced|margin|auto",
         "custom — позиции со страницы «Лоты» и минимальная маржа выше; turnover — оборот; balanced — сегменты "
         "(мелкие в ноль, средние 2–4 место, крупные с маржой); margin — маржа; auto — по лимиту карты. Подробнее — «Цены»"),
        ("competitor_min_reviews", "Не равняться на продавцов, у которых отзывов меньше", "int",
         "Пресеты сами ставят 10. 0 — учитывать всех"),
        ("competitor_min_rating", "…и рейтинг ниже, ★", "float",
         "В таблице FunPay видны только целые звёзды: 4 — пропускать 1–3★, 5 — только пятёрки"),
    ]),
    ("rtba.card_limit", [
        ("daily_rub", "Лимит карты на вывод за сутки, ₽", "float", "0 — не задан. Для пресета auto"),
        ("monthly_rub", "Лимит карты за месяц, ₽", "float", "0 — не задан"),
        ("balanced_from", "Переходить на «Сегменты» с доли лимита", "float", "0.7 = 70% лимита"),
        ("margin_from", "Переходить на «Маржу» с доли лимита", "float", "1.0 = лимит выбран"),
        ("no_limit_preset", "Пресет auto, пока лимит не задан", "choice:turnover|balanced|margin", ""),
    ]),
    ("rtba.spread_buffer", [
        ("enabled", "Спред-буфер: уходить на место ниже, если следующий конкурент заметно дороже", "bool",
         "Только в пресетах, где у сегмента «до места» больше целевого"),
        ("min_gap_rub", "Минимальный разрыв, ₽", "float", "В ценах покупателя"),
        ("min_gap_pct", "…и не меньше, % от цены", "float", "Чтобы на 1000⭐ не терять место ради 5 ₽"),
        ("max_gap_pct", "Не прыгать, если разрыв больше, %", "float", "0 — без ограничения"),
        ("offset_rub", "Вставать дешевле следующего на, ₽", "float", ""),
    ]),
    ("rtba.night", [
        ("enabled", "Ночной коэффициент: поднимать пол цены ночью", "bool", ""),
        ("start_hour", "С, час", "int", "По времени ниже"),
        ("end_hour", "До, час", "int", ""),
        ("utc_offset_hours", "Часовой пояс, UTC+", "float", "3 — Москва"),
        ("floor_bonus_pct", "Надбавка к марже пола, %", "float", ""),
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
        ("withdraw_fee_pct", "Вывод и налог с Ozon, %", "float", "Налог (НПД 6%, УСН 6%) + комиссия вывода"),
        ("min_margin_pct", "Минимальная маржа, %", "float", "Панель подсветит цену ниже"),
    ]),
    ("ggsel", [
        ("enabled", "Продавать на GGSEL", "bool", "Маркетплейс ggsel.net, работа через Seller API"),
        ("seller_id", "ID продавца", "str", "Кабинет GGSEL → Настройки → API"),
        ("api_key", "Ключ API", "secret", "Там же. Ключ нужен с правами на продажи, чаты и товары"),
        ("poll_interval", "Проверять продажи и чаты GGSEL, сек", "int", ""),
        ("stock_sync", "Остатки на GGSEL по балансу кошелька", "bool", "Товар уходит в 0, когда TON не хватает"),
        ("max_stock", "Максимальный остаток на GGSEL", "int", ""),
        ("price_mode", "Цены на GGSEL", "str", "margin — себестоимость + маржа (сам следит за курсом TON); manual — "
         "цены из таблицы на странице GGSEL"),
        ("margin_pct", "Желаемая маржа, %", "float", "Для режима margin"),
        ("min_margin_pct", "Минимальная маржа, %", "float", "Ниже — товар скрывается и приходит алерт"),
        ("commission_pct", "Комиссия GGSEL, %", "float", "Уточните в кабинете для категории Telegram"),
        ("withdraw_fee_pct", "Комиссия вывода с GGSEL, %", "float", "Зависит от способа вывода"),
        ("sla_critical_hours", "Срочный алерт, если не выдан, ч", "float", "GGSEL требует выдать за 12 ч"),
        ("confirm_reminder_hours", "Напомнить подтвердить получение через, ч", "float", "0 — не напоминать"),
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
    ("avito", [
        ("enabled", "Продавать на Авито", "bool", "Безопасная сделка для цифровых товаров, чаты через API Авито"),
        ("client_id", "client_id", "str", "Авито → Для профессионалов → API"),
        ("client_secret", "client_secret", "secret", "Ключ API. Мессенджер API доступен не на всех тарифах"),
        ("user_id", "Номер профиля", "int", "0 — бот узнает сам"),
        ("poll_interval", "Проверять чаты, сек", "int", ""),
        ("presale_replies", "Отвечать до оплаты", "bool", "Как купить, номиналы; username можно прислать заранее"),
        ("presale_cooldown_minutes", "Автоответ до оплаты не чаще, мин", "int", "В одном чате"),
        ("commission_pct", "Комиссия Авито, %", "float", "За безопасную сделку — уточните в кабинете"),
        ("withdraw_fee_pct", "Вывод и налог с Авито, %", "float", "Налог (НПД 4–6%, УСН 6%) + комиссия вывода — иначе маржа завышена"),
        ("min_margin_pct", "Минимальная маржа, %", "float", "Цена объявления не опустится ниже"),
        ("price_sync", "Менять цены объявлений автоматически", "bool", ""),
        ("price_interval", "Пересчитывать цены, сек", "int", ""),
        ("auto_complete_hours", "Считать заказ завершённым через, ч", "int", "Если Авито не прислал подтверждение"),
    ]),
    ("wb", [
        ("enabled", "Продавать на Wildberries", "bool", "Категория «Цифровые товары»: коды активации из пула"),
        ("code_prefix", "Префикс кодов", "str", "Коды вида TGW-ABCD-EFGH-JKMN, не совпадает с Ozon"),
        ("pool_warn_low", "Предупредить, когда кодов номинала меньше", "int", ""),
        ("commission_pct", "Комиссия WB, %", "float", "Уточните в кабинете для ключей активации"),
        ("withdraw_fee_pct", "Вывод и налог с WB, %", "float", "Налог (НПД 6%, УСН 6%) + комиссия вывода"),
        ("min_margin_pct", "Минимальная маржа, %", "float", "Панель подскажет цену не ниже"),
    ]),
    ("wb.activation_bot", [
        ("enabled", "Бот активации в Telegram", "bool", "Покупатель присылает боту код и @username"),
        ("bot_token", "Токен бота активации WB (@BotFather)", "secret", "Отдельный бот: не уведомлений и не Ozon"),
    ]),
]

SECTION_TITLES = {
    "general": "Основное", "funpay": "FunPay", "fragment": "Fragment", "ton": "TON-кошелёк", "rates": "Курс TON",
    "economics": "Деньги: комиссии", "deal": "Сделка", "risk": "Риск-система",
    "safety": "Предохранители денег", "rtba": "RTBA — автоцена",
    "rtba.card_limit": "RTBA: лимит карты (пресет auto)", "rtba.spread_buffer": "RTBA: спред-буфер",
    "rtba.night": "RTBA: ночной коэффициент",
    "notifications.telegram": "Telegram", "notifications": "Уведомления", "dashboard": "Доступ к панели (роли)",
    "sla": "Сроки (SLA)", "ozon": "Ozon: продажи", "ozon.activation_bot": "Ozon: бот активации",
    "ozon.activation_web": "Ozon: страница активации", "ggsel": "GGSEL: продажи",
    "avito": "Авито: продажи", "wb": "Wildberries: продажи", "wb.activation_bot": "Wildberries: бот активации",
    "gifts": "Подарки: выдача подарками Telegram", "gifts.topup": "Подарки: автопополнение звёзд",
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
    "presale": "Вопрос о звёздах без заказа", "presale_hello": "«Вы тут?» без заказа", "faq_gift": "FAQ: подарком?", "faq_safe": "FAQ: это безопасно?",
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


# Раздел настроек нужен, только когда включена его площадка: остальные свёрнуты в «Выключено», чтобы не путаться.
# Значение — какая площадка: "funpay" — продажи на FunPay, иначе раздел config.yaml с флагом enabled.
SECTION_GATE = {
    "funpay": "funpay", "autopilot": "funpay", "autopilot.withdraw": "funpay", "rtba": "funpay",
    "rtba.card_limit": "funpay", "rtba.spread_buffer": "funpay", "rtba.night": "funpay",
    "gifts": "gifts", "gifts.topup": "gifts",
    "ozon": "ozon", "ozon.activation_bot": "ozon", "ozon.activation_web": "ozon", "ggsel": "ggsel",
    "avito": "avito", "wb": "wb", "wb.activation_bot": "wb",
}
GATE_TITLES = {"funpay": "FunPay", "gifts": "выдача подарками", "ozon": "Ozon", "ggsel": "GGSEL", "avito": "Авито",
               "wb": "Wildberries"}


def gate_on(cfg, gate: str) -> bool:
    if gate == "funpay":
        return bool((cfg.get("funpay") or {}).get("enabled", True))
    if gate == "gifts":     # подарки FunPay «Подарком» — часть продаж на FunPay
        return bool((cfg.get("funpay") or {}).get("enabled", True)) and bool((cfg.get("gifts") or {}).get("enabled"))
    return bool((cfg.get(gate) or {}).get("enabled"))


def split_sections(cfg, sections=None) -> tuple[list, list]:
    """(разделы включённых площадок, разделы выключенных). Выключенные форма всё равно отправляет (свёрнутыми),
    иначе флажки в них сохранились бы как «выкл»."""
    active, idle = [], []
    for section, fields in (sections if sections is not None else SETTINGS):
        gate = SECTION_GATE.get(section)
        (idle if gate and not gate_on(cfg, gate) else active).append((section, fields))
    return active, idle


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
