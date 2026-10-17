"""Описание полей настроек для формы в приложении (подписи, типы, подсказки)."""
from __future__ import annotations

# (раздел, ключ, подпись, тип, подсказка). Типы: bool int float str secret list choice:a|b|c
SETTINGS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("general", [
        ("dry_run", "Тестовый режим (dry-run)", "bool", "Бот читает FunPay/Steam и ведёт CRM, но ничего не отправляет"),
        ("autostart", "Запускать бота при открытии приложения", "bool", ""),
        ("language", "Язык сообщений покупателю по умолчанию", "choice:ru|en", "Бот переключится на English, если покупатель пишет на английском"),
        ("log_level", "Подробность журнала", "choice:INFO|WARNING|DEBUG", ""),
    ]),
    ("funpay", [
        ("golden_key", "golden_key (cookie FunPay)", "secret", "Браузер → funpay.com → DevTools → Application → Cookies → golden_key"),
        ("poll_interval", "Опрос FunPay, сек", "int", "Не ставьте меньше 4"),
        ("order_match_regex", "Какие заказы обрабатывать (regex по описанию)", "str", ""),
        ("auto_raise", "Автоподнятие лотов", "bool", ""),
        ("raise_interval_minutes", "Интервал поднятия, мин", "int", ""),
        ("stock_sync", "Синхронизировать наличие лота с инвентарём", "bool", ""),
        ("max_display_amount", "Максимум показываемого наличия", "int", ""),
        ("deactivate_when_empty", "Скрывать лот, когда ключей нет", "bool", ""),
    ]),
    ("steam", [
        ("username", "Логин Steam", "str", ""),
        ("password", "Пароль Steam", "secret", ""),
        ("shared_secret", "shared_secret (из maFile)", "secret", "Для кодов Steam Guard"),
        ("identity_secret", "identity_secret (из maFile)", "secret", "Для подтверждения трейдов"),
        ("steam_id", "SteamID64 бота", "str", ""),
        ("login_secure_cookie", "Cookie steamLoginSecure (вместо пароля)", "secret", "Необязательно"),
        ("api_key", "Steam Web API key", "secret", "Необязательно — если статусы трейдов не читаются"),
        ("offer_message", "Комментарий к трейду", "str", "Переменные: {order_id}, {qty}"),
        ("offer_ttl_hours", "Отменять непринятый трейд через, ч", "int", ""),
        ("min_seconds_between_offers", "Пауза между трейдами, сек", "int", ""),
        ("max_keys_per_offer", "Максимум ключей в одном трейде", "int", ""),
    ]),
    ("deal", [
        ("max_link_attempts", "Попыток прислать данные", "int", "После — заказ уходит продавцу"),
        ("reminder_after_minutes", "Напомнить про ссылку через, мин", "int", ""),
        ("admin_alert_after_minutes", "Позвать продавца, если покупатель молчит, мин", "int", ""),
        ("max_resends", "Повторных отправок трейда", "int", ""),
        ("trade_hold_policy", "Если у покупателя задержка обмена", "choice:require_fix|warn_and_send|manual",
         "require_fix — попросить Steam Guard (или «!отправить»), warn_and_send — отправить, manual — спросить вас"),
        ("cancel_offer_on_refund", "Отменять трейд при возврате денег", "bool", ""),
        ("auto_refund_out_of_stock", "Авто-возврат, если ключей нет", "bool", ""),
        ("message_rate_limit_seconds", "Пауза между сообщениями в чат, сек", "float", ""),
    ]),
    ("risk", [
        ("enabled", "Риск-система включена", "bool", ""),
        ("manual_review_score", "Порог ручной проверки", "int", ""),
        ("block_score", "Порог блокировки", "int", ""),
        ("large_order_keys", "Крупный заказ — от, ключей", "int", ""),
        ("max_orders_per_buyer_per_day", "Заказов от покупателя в сутки (норма)", "int", ""),
        ("blacklist_funpay_users", "Чёрный список ников FunPay", "list", "По одному в строке"),
        ("blacklist_steam_ids", "Чёрный список SteamID64", "list", "По одному в строке"),
    ]),
    ("rtba", [
        ("enabled", "RTBA (автоцена) включена", "bool", ""),
        ("interval_seconds", "Проверять рынок каждые, сек", "int", ""),
        ("strategy", "Стратегия", "choice:top1|topN|median", "top1 — дешевле лидера, topN — встать на позицию N, median — медиана"),
        ("target_position", "Позиция для topN", "int", ""),
        ("undercut_abs", "Шаг ниже конкурента, ₽", "float", ""),
        ("undercut_pct", "Шаг ниже конкурента, %", "float", ""),
        ("cost_per_key", "Себестоимость ключа, ₽", "float", "Обязательно для пола цены"),
        ("min_margin_pct", "Минимальная маржа, %", "float", ""),
        ("floor_price", "Жёсткий минимум цены, ₽", "float", "0 — только по марже"),
        ("ceiling_price", "Потолок цены, ₽", "float", ""),
        ("funpay_fee_pct", "Комиссия, вычитаемая из вашей цены, %", "float", ""),
        ("min_change", "Не менять цену меньше чем на, ₽", "float", ""),
        ("max_updates_per_hour", "Изменений цены в час, максимум", "int", ""),
        ("competitor_min_amount", "Игнорировать лоты с наличием меньше", "int", ""),
        ("competitor_only_online", "Учитывать только продавцов онлайн", "bool", ""),
        ("ignore_competitors", "Игнорировать продавцов", "list", "По одному нику в строке"),
        ("lot_filter_regex", "Фильтр лотов-ключей (regex)", "str", ""),
        ("scarcity_stock_threshold", "Малый остаток — меньше, ключей", "int", ""),
        ("scarcity_premium_pct", "Надбавка при малом остатке, %", "float", ""),
    ]),
    ("security", [
        ("cancel_foreign_offers", "Отменять трейды, созданные не ботом", "bool", "Выключите, если сами торгуете с аккаунта"),
        ("decline_incoming_requests", "Отклонять входящие трейды, просящие ваши предметы", "bool", ""),
        ("inventory_drop_alert", "Тревога при пропаже ключей", "bool", ""),
    ]),
    ("notifications.telegram", [
        ("enabled", "Уведомления в Telegram", "bool", ""),
        ("bot_token", "Токен бота (@BotFather)", "secret", ""),
        ("admin_chat_ids", "chat_id администраторов", "list", "По одному в строке (узнать: @userinfobot)"),
        ("commands", "Управление командами из Telegram", "bool", ""),
    ]),
    ("notifications", [
        ("min_level", "Минимальный уровень уведомлений", "choice:INFO|WARNING|CRITICAL", ""),
        ("throttle_seconds", "Повторять одинаковый алерт не чаще, сек", "int", ""),
        ("daily_report_hour", "Час ежедневного отчёта", "int", ""),
    ]),
    ("sla", [
        ("link_wait_minutes", "Ожидание ссылки, мин", "int", ""),
        ("offer_accept_hours", "Ожидание принятия трейда, ч", "int", ""),
        ("order_total_hours", "Заказ должен закрыться за, ч", "int", ""),
    ]),
]

SECTION_TITLES = {
    "general": "Основное", "funpay": "FunPay", "steam": "Steam", "deal": "Сделка", "risk": "Риск-система",
    "rtba": "RTBA — автоцена", "security": "Безопасность аккаунта", "notifications.telegram": "Telegram",
    "notifications": "Уведомления", "sla": "Сроки (SLA)",
}

MESSAGE_TITLES = {
    "greeting": "После оплаты (инструкция)", "link_received": "Ссылка получена", "bad_data": "Проблема с данными",
    "bad_data_final": "Данные так и не получены", "trade_hold": "Задержка обмена", "trade_hold_fix": "— просьба включить Steam Guard",
    "trade_hold_send": "— отправим с задержкой", "trade_hold_manual": "— решит продавец", "offer_sent": "Трейд отправлен",
    "success": "Успешная сделка (благодарность + просьба подтвердить)", "escrow": "Трейд в удержании Steam",
    "declined": "Трейд отклонён/истёк", "api_scam_warning": "Предупреждение об API-скаме", "resend_limit": "Лимит повторов",
    "out_of_stock": "Нет ключей", "awaiting_approval": "Проверка безопасности", "reminder": "Напоминание",
    "status": "Ответ на !статус", "help": "Ответ на !помощь", "call_seller": "Продавец вызван",
    "off_platform": "Отказ от общения вне FunPay", "unknown": "Непонятное сообщение", "refunded": "Возврат",
    "completed_thanks": "Спасибо за подтверждение", "review_thanks": "Спасибо за отзыв",
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
