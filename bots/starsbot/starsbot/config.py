"""Загрузка конфигурации: YAML + подстановка ${ENV} + значения по умолчанию."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

DENOMINATIONS = (50, 75, 100, 150, 250, 350, 500)
GIFT_DENOMINATIONS = (13, 21, 43, 85, 50, 100, 150, 250, 350, 500)

DEFAULTS: dict[str, Any] = {
    "general": {
        "dry_run": True, "demo": False, "language": "ru",
        "database": "data/starsbot.sqlite3", "log_file": "data/starsbot.log", "log_level": "INFO",
        "autostart": False, "backup_keep_days": 14,
    },
    "messages": {"ru": {}, "en": {}},
    "funpay": {
        "golden_key": "", "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
        "poll_interval": 6,
        "node_id": 2418,                 # «Telegram → Звёзды» (funpay.com/lots/2418/)
        "game_id": 0,                    # id игры для поднятия лотов (0 — не поднимать)
        # Лоты: один лот на номинал. offer_id — id лота из ссылки funpay.com/lots/offer?id=…
        # position — желаемое место лота в списке раздела (1 — первый). 0 — как в настройках RTBA
        "lots": [{"stars": n, "offer_id": 0, "position": 0} for n in DENOMINATIONS],
        "order_match_regex": r"(?i)(зв[её]зд|stars|⭐)",
        "order_exclude_regex": r"(?i)(подарк|gift|с заходом|вход на аккаунт)",
        "auto_raise": True, "raise_interval_minutes": 240,
        "stock_sync": True,              # «Наличие» лотов = сколько раз хватит баланса кошелька
        "max_display_amount": 200,
        "deactivate_when_empty": True,
        "presale_replies": True,         # отвечать на вопросы о звёздах в чатах без заказа
        "coord_db": "",                  # общий файл координации с другими ботами (обычно задаёт Центр)
        # Автовыставление лотов: бот сам создаёт недостающие лоты по образцу вашего лота (копия полей формы)
        "auto_create_lots": False,
        "lot_price_per_star": 1.6,       # стартовая цена нового лота, ₽ за звезду (дальше её ведёт RTBA)
        "lot_description": "⚡ Автовыдача 1–5 минут. Укажите @username — звёзды придут на аккаунт.",
        "gift_lot_description": "🎁 Автовыдача подарками 1–5 минут. Укажите @username — подарки придут на аккаунт, "
                                "их можно конвертировать в звёзды.",
    },
    # Выдача звёзд подарками Telegram (раздел FunPay «Звёзды → Подарком»)
    "gifts": {
        "enabled": False,
        "api_id": 0, "api_hash": "",     # свои ключи приложения: my.telegram.org → API development tools
        "phone": "",
        "session_file": "data/telegram.session",
        # номинал лота = сколько звёзд получит покупатель, конвертировав подарки (13 = 🧸15, 21 = 🌹25, 43 = 🎂50, 85 = 🏆100)
        "lots": [{"stars": n, "offer_id": 0, "position": 0} for n in GIFT_DENOMINATIONS],
        "order_regex": r"(?i)(подарк|gift)",
        "mode": "convert",               # convert — по сумме конвертации, price — по цене подарков
        "per_gift_penalty": 3,           # ⭐ «штрафа» за каждый лишний подарок — меньше подарков ценой мелкой переплаты
        "allowed_gift_ids": [],          # только эти подарки (пусто — любые обычные, не лимитированные)
        "max_gifts_per_order": 40,
        "hide_sender_name": False,
        "gift_message": "Заказ #{order_id}. Спасибо за покупку! ⭐",   # подпись к подарку (без ссылок и контактов)
        "pause_between_gifts": 1.5,
        "stock_sync": True, "max_display_amount": 200, "deactivate_when_empty": True,
        "low_stars_alert": 500,
        "pilot_orders": 1,               # первые N боевых заказов подарками — после одобрения
        "max_order_stars": 0,            # набор дороже N ⭐ — с одобрения (0 — без лимита)
        # Автопополнение звёзд аккаунта через Fragment (покупка самому себе тем же кошельком TON)
        "topup": {"enabled": False, "min_stars": 1000, "amount": 2500, "username": "",
                  "cooldown_minutes": 20,     # пауза после пополнения: звёзды от Fragment приходят не мгновенно
                  "max_per_day": 10000},      # не больше N ⭐ пополнений за сутки (0 — без лимита)
    },
    # Автономный цикл: напоминания о подтверждении, файлы для поддержки, вывод с FunPay на кошелёк
    "autopilot": {
        "enabled": False,
        "confirm_reminder_hours": 24,    # через сколько часов после выдачи вежливо напомнить о подтверждении (0 — нет)
        "support_after_hours": 72,       # через сколько часов без подтверждения готовить файл для поддержки FunPay
        "support_folder": "data/support",
        "withdraw": {
            "enabled": False,
            "min_wallet_ton": 5.0,       # выводить, когда на кошельке меньше N TON
            "min_funpay_rub": 1000.0,    # и на балансе FunPay не меньше N ₽
            "keep_rub": 0.0,             # оставлять на FunPay
            "max_per_day_rub": 20000.0,
            "method": "",                # способ вывода из списка FunPay (выбирается в панели)
            "wallet": "",                # куда: адрес кошелька (пусто — адрес кошелька бота)
            "require_approval": True,    # каждый вывод — после кнопки в Telegram/панели
        },
    },
    "fragment": {
        "cookies": "",                   # stel_ssid=…; stel_dt=…; stel_token=…; stel_ton_token=…
        "user_agent": "",                # User-Agent браузера, из которого взяты cookie (пусто — как у FunPay)
        "min_interval": 1.0,
        "max_ton_per_star": 0.0,         # предохранитель: не покупать дороже (0 — без ограничения)
        "max_cost_share_pct": 100.0,     # не покупать, если себестоимость > X% от суммы заказа (защита от убытка)
        "session_check_minutes": 10,     # проверять сессию Fragment перед заказом, если с прошлой проверки прошло больше
        "hide_lots_when_down": True,     # скрывать лоты, пока сессия Fragment истекла или нет KYC
        "cookies_set_at": 0,             # когда вставлены cookies (ставится автоматически)
        "session_warn_days": 14,         # напомнить обновить cookies заранее, после стольких дней (0 — не напоминать)
    },
    "ton": {
        "mnemonic": "", "address": "", "version": "v5r1",   # Tonkeeper по умолчанию создаёт W5 (v5r1)
        "mnemonic_type": "auto",         # auto | bip39 (12 слов Tonkeeper) | ton (24 слова) — auto подбирает сам
        "toncenter_endpoint": "https://toncenter.com/api/v2", "toncenter_api_key": "",
        "low_balance_ton": 3.0,          # предупреждать, когда на кошельке меньше
        "fee_reserve_ton": 0.1,          # запас на комиссию сети при расчёте наличия
    },
    "rates": {
        "source": "auto",                # auto — курс TON/RUB из сети (CoinGecko), manual — только ton_rub
        "ton_rub": 0.0,                  # курс вручную / запасной
        "refresh_minutes": 15,
    },
    "deal": {
        "max_username_attempts": 3, "reminder_after_minutes": 10, "admin_alert_after_minutes": 60,
        "purchase_retry_limit": 3,
        "purchase_pause_seconds": 3,     # пауза между покупками (антифрод Fragment)       # повторов покупки, когда транзакция гарантированно не исполнилась
        "confirm_wait_minutes": 15,      # сколько ждать подтверждения транзакции в блокчейне
        "auto_refund_bad_recipient": False,   # авто-возврат, если username так и не удалось получить
        "auto_refund_no_funds": False,   # авто-возврат, если на кошельке нет денег
        "message_rate_limit_seconds": 2,
        "review_reply": True,            # отвечать на отзывы 4–5★ (на 1–3★ — только алерт, отвечаете вы)
    },
    "risk": {
        "enabled": True, "manual_review_score": 60, "block_score": 90, "large_order_stars": 1500,
        "max_orders_per_buyer_per_day": 6, "max_recipients_per_buyer_per_day": 3,
        "blacklist_funpay_users": [], "blacklist_usernames": [],
    },
    # предохранители денег в боевом режиме (в dry-run и демо не действуют)
    "safety": {
        "pilot_orders": 3,              # первые N боевых покупок — только после /approve продавца
        "daily_ton_limit": 0.0,         # не тратить больше N TON за сутки без одобрения (0 — без лимита)
        "max_order_ton": 0.0,           # заказ дороже N TON — только после одобрения (0 — без лимита)
    },
    "rtba": {
        "enabled": False, "interval_seconds": 300, "strategy": "top1", "target_position": 1,
        "undercut_abs": 0.5, "undercut_pct": 0.0, "min_margin_pct": 8.0,
        "floor_price_per_star": 0.0, "ceiling_price_per_star": 0.0, "min_change": 0.3,
        "max_updates_per_hour": 6, "ignore_competitors": [], "competitor_min_amount": 1,
        "competitor_only_online": False, "outlier_iqr_factor": 1.5, "method_filter": "username",
        "raise_gap_pct": 3.0,
        "fallback_ton_per_star": 0.0,    # цена звезды в TON, пока не было ни одной покупки
    },
    "economics": {
        "funpay_fee_pct": 0.0,           # комиссия FunPay, если она вычитается из вашей цены лота
        "withdraw_fee_pct": 3.0,         # комиссия вывода денег с FunPay (зависит от способа)
        "withdraw_fee_fixed_rub": 0.0,
        "exchange_spread_pct": 2.0,      # насколько дороже биржевого курса вы покупаете TON (P2P, обменник)
        "deposit_fee_ton": 0.0,          # комиссия за перевод TON на кошелёк бота (за одно пополнение)
        "typical_topup_ton": 50.0,       # обычный размер пополнения — чтобы разложить комиссию на звёзды
        "gas_per_purchase_ton": 0.006,   # комиссия сети за одну покупку (уточняется по факту)
        "balance_mismatch_alert_ton": 1.0,   # тревога, если баланс расходится с учётом больше чем на столько
    },
    "notifications": {
        "telegram": {"enabled": False, "bot_token": "", "admin_chat_ids": [], "commands": True},
        "min_level": "INFO", "throttle_seconds": 300, "daily_report_hour": 10,
    },
    "dashboard": {"enabled": True, "host": "127.0.0.1", "port": 8083, "token": "",
                  "operator_token": "", "viewer_token": ""},
    "sla": {"order_total_minutes": 60},
    # Второй канал продаж: Ozon (кабинет «Цифровые товары», работа через Seller API).
    # Покупатель получает от Ozon код активации и вводит его вместе с @username в боте активации.
    "ozon": {
        "enabled": False,
        "client_id": "", "api_key": "",          # кабинет Ozon → Настройки → Seller API (ключ кабинета цифровых товаров)
        "api_base": "https://api-seller.ozon.ru",
        "poll_interval": 30,             # опрос новых отправлений, сек
        "lookback_hours": 72,            # за какой период читать отправления
        # Товары: артикул (offer_id) карточки Ozon → сколько звёзд в одной штуке
        "products": [{"stars": n, "offer_id": ""} for n in DENOMINATIONS],
        "code_prefix": "TGS",            # коды вида TGS-ABCD-EFGH-JKMN
        "stock_sync": True,              # остатки на Ozon = сколько раз хватит баланса кошелька
        "max_stock": 100,
        "commission_pct": 10.0,          # комиссия Ozon с цены (игровая валюта — 10%, прочие цифровые — 15%)
        "withdraw_fee_pct": 0.0,         # Ozon платит на расчётный счёт, обычно без комиссии
        "activation_bot": {"enabled": True, "bot_token": ""},   # отдельный Telegram-бот для покупателей
        "activation_web": {"enabled": False, "host": "0.0.0.0", "port": 8084, "public_url": ""},
        "messages": {},                  # свои тексты для покупателей Ozon (ключи как в starsbot/ozon/texts.py)
    },
}

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


class Section(dict):
    """dict с доступом через точку: cfg.steam.username."""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as e:  # pragma: no cover
            raise AttributeError(item) from e

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value


def _wrap(obj: Any) -> Any:
    if isinstance(obj, dict):
        return Section({k: _wrap(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_wrap(v) for v in obj]
    return obj


def _expand_env(obj: Any) -> Any:
    if isinstance(obj, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    return obj


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Section:
    data: dict = {}
    if path and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    merged = _merge(DEFAULTS, _expand_env(data))
    if overrides:
        merged = _merge(merged, overrides)
    return _wrap(merged)


def validate_config(cfg: Section) -> list[str]:
    """Возвращает список проблем конфигурации (пустой — всё ок)."""
    problems: list[str] = []
    if cfg.general.demo:
        return problems
    if not cfg.funpay.golden_key:
        problems.append("funpay.golden_key не задан")
    # лоты без offer_id — не ошибка: бот выставит их сам при запуске или по кнопке «Лоты → Выставить недостающие»
    for lot in cfg.funpay.lots or []:
        if lot.get("offer_id") and int(lot.get("stars") or 0) < 50:
            problems.append(f"лот {lot.get('offer_id')}: Fragment продаёт от 50 звёзд")
    if not cfg.fragment.cookies:
        problems.append("fragment.cookies не заданы (сессия fragment.com с подключённым кошельком)")
    from starsbot.ton.mnemonic import MnemonicError, check_words, normalize_words
    try:
        check_words(normalize_words(str(cfg.ton.mnemonic or "")))
    except MnemonicError as e:
        problems.append(f"ton.mnemonic: {e}")
    if cfg.ton.version not in ("v4r2", "v5r1"):
        problems.append("ton.version: поддерживаются v4r2 и v5r1")
    if cfg.dashboard.enabled and len(str(cfg.dashboard.token)) < 12:
        problems.append("dashboard.token должен быть не короче 12 символов")
    if cfg.rates.source == "manual" and float(cfg.rates.ton_rub or 0) <= 0:
        problems.append("rates.ton_rub: при ручном курсе укажите курс TON в рублях")
    problems += validate_ozon(cfg)
    return problems


def validate_ozon(cfg: Section) -> list[str]:
    oz = cfg.get("ozon") or {}
    if not oz.get("enabled"):
        return []
    out = []
    if not str(oz.get("client_id") or "").strip() or not str(oz.get("api_key") or "").strip():
        out.append("ozon: укажите Client-Id и Api-Key Seller API")
    if not any(p.get("offer_id") and p.get("stars") for p in (oz.get("products") or [])):
        out.append("ozon.products: укажите артикул (offer_id) хотя бы одного товара со звёздами")
    bot, web = oz.get("activation_bot") or {}, oz.get("activation_web") or {}
    if not ((bot.get("enabled") and bot.get("bot_token")) or web.get("enabled")):
        out.append("ozon: включите бот активации (токен @BotFather) или страницу активации — иначе покупатель не "
                   "сможет ввести код")
    if bot.get("enabled") and bot.get("bot_token") and \
            str(bot.get("bot_token")) == str(cfg.notifications.telegram.bot_token or ""):
        out.append("ozon.activation_bot: нужен отдельный бот, не тот, что для уведомлений продавцу")
    return out
