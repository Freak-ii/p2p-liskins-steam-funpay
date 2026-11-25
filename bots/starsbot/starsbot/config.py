"""Загрузка конфигурации: YAML + подстановка ${ENV} + значения по умолчанию."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

# Номиналы «По username», которые есть в списке «Количество» раздела FunPay (проверено 2026-11-21)
DENOMINATIONS = (50, 75, 100, 150, 200, 250, 300, 350, 500, 1000, 2500)
# Fragment продаёт любое количество от 50 звёзд; этих номиналов в списке FunPay нет — лот выставляется через
# «Другое количество» с числом звёзд (так продают 750, 1500, 5000 и т. д. другие продавцы)
CUSTOM_DENOMINATIONS = (750, 1500, 2000, 3000, 5000, 10000)
PREMIUM_MONTHS = (3, 6, 12)                              # сроки, которые Fragment дарит (fragment.com/premium/gift)
GIFT_DENOMINATIONS = (13, 21, 43, 85, 50, 100, 150, 250, 350, 500)

DEFAULTS: dict[str, Any] = {
    "general": {
        "dry_run": True, "demo": False, "language": "ru",
        "database": "data/starsbot.sqlite3", "log_file": "data/starsbot.log", "log_level": "INFO",
        "autostart": False, "backup_keep_days": 14,
        "profile": "stars",              # stars — StarsBot (звёзды через Fragment), giftshop — TG Gift (подарки)
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
        "lots": [{"stars": n, "offer_id": 0, "position": 0} for n in DENOMINATIONS] +
                [{"stars": n, "offer_id": 0, "position": 0, "custom": True} for n in CUSTOM_DENOMINATIONS],
        "order_match_regex": r"(?i)(зв[её]зд|stars|⭐)",
        "order_exclude_regex": r"(?i)(подарк|gift|с заходом|вход на аккаунт)",
        "auto_raise": True, "raise_interval_minutes": 240,
        "stock_sync": True,              # «Наличие» лотов = сколько раз хватит баланса кошелька
        "max_display_amount": 200,
        "deactivate_when_empty": True,
        "presale_replies": True,         # ответить шаблоном тому, кто пишет без заказа (один раз на чат)
        "coord_db": "",                  # общий файл координации с другими ботами (обычно задаёт Центр)
        # заказы этих подразделов не наши, даже если в описании есть число звёзд: «Подарки» ведёт бот TG Gift
        "foreign_subcategory_regex": r"(?i)подарки|\bgifts\b",
        # Автовыставление лотов: бот сам создаёт недостающие лоты по образцу вашего лота (копия полей формы)
        "auto_create_lots": False,
        "lot_price_per_star": 1.6,       # стартовая цена нового лота, ₽ за звезду (дальше её ведёт RTBA)
        "lot_description": "⚡ Автовыдача 1–5 минут. Укажите @username — звёзды придут на аккаунт.",
        "manage_lot_texts": True,        # бот сам обновляет тексты карточек (выключите, если правите их на FunPay)
        "lot_texts": {},                 # ручные тексты карточки «По username»: summary_ru, desc_ru, summary_en, desc_en
        "gift_lot_texts": {},            # то же для «Подарком»; {stars} — номинал
        "gift_lot_description": "🎁 Автовыдача подарками 1–5 минут. Укажите @username — подарки придут на аккаунт, "
                                "их можно конвертировать в звёзды.",
    },
    # Выдача звёзд подарками Telegram (раздел FunPay «Звёзды → Подарком»)
    "gifts": {
        "enabled": False,
        # раздел «Звёзды → Подарком» ведёт бот TG Gift (свой Telegram-аккаунт, звёзды от StarsBot): у StarsBot эта
        # выдача выключена, его старые лоты подарками выключаются, пока TG Gift не возьмёт их себе
        "moved_to_tggift": True,
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
    # Продажа Telegram Premium: покупка подарком на Fragment тем же кошельком TON, продажа на FunPay
    # (раздел «Telegram → Premium», funpay.com/lots/1391/), GGSEL (ggsel.premium_products) и в магазине (supply).
    "premium": {
        "enabled": False,
        "node_id": 1391,
        # Лоты: один лот на срок. offer_id — id лота FunPay; position — место в списке раздела (0 — по марже)
        "lots": [{"months": m, "offer_id": 0, "position": 0} for m in PREMIUM_MONTHS],
        "order_regex": r"(?i)(premium|премиум)",
        "order_exclude_regex": r"(?i)(с заходом|со входом|вход на аккаунт|через вход|логин и пароль)",
        "stock_sync": True, "max_display_amount": 50, "deactivate_when_empty": True,
        "price_mode": "rtba",            # rtba — по рынку (позиция лота, не ниже пола), margin — себестоимость + маржа
        "margin_pct": 8.0,               # цена в режиме margin: маржа с цены лота, %
        "min_margin_pct": 3.0,           # пол цены и предохранитель покупки: не меньше N% чистой прибыли
        "target_position": 3,            # позиция лота в режиме rtba, если у лота не задана своя
        "undercut_abs": 1.0,             # насколько дешевле конкурента на позиции, ₽ (цена покупателя)
        "min_change": 2.0, "max_updates_per_hour": 4, "interval_seconds": 300,
        "ignore_competitors": [],
        "competitor_regex": r"(?i)(подар|gift|username|юзернейм|без входа|fragment)",   # такие же лоты, без входа
        # себестоимость в TON, пока не было ни одной котировки Fragment (обновляется после первой покупки/котировки)
        "fallback_ton": {"3": 0.0, "6": 0.0, "12": 0.0},
        "quote_username": "",            # свой username: раз в quote_minutes бот берёт котировку Fragment (без оплаты)
        "quote_minutes": 30,
        "pilot_orders": 1,               # первые N боевых заказов Premium — после одобрения
        "lot_texts": {},                 # ручные тексты карточки: summary_ru, desc_ru, summary_en, desc_en ({months})
    },
    # Снабжение звёздами других ботов (TG Gift): они пишут заявку в общий файл координации, StarsBot покупает
    # звёзды на Fragment на @username их Telegram-аккаунта тем же безопасным путём, что и обычные заказы
    "supply": {
        "enabled": True,
        "token": "",                     # ключ API /api/supply/* для Telegram-магазина ShopBot (≥16 символов)
        "max_per_topup": 2500,           # не больше N ⭐ за одну поставку
        "max_per_day": 5000,             # и N ⭐ за сутки на один аккаунт (0 — без лимита)
        "cooldown_minutes": 20,          # пауза после поставки: звёзды от Fragment приходят за несколько минут
        "min_wallet_reserve_stars": 500, # не снабжать, если после поставки кошелька хватит меньше чем на N ⭐ своих заказов
    },
    # NFT-подарки для TG Gift: StarsBot покупает коллекционный подарок на fragment.com/gifts своим кошельком TON и
    # отправляет его покупателю в Telegram по заданию TG Gift (см. starsbot/nft_supply.py)
    "nft_supply": {
        "enabled": True,
        "max_ton_per_item": 60.0,        # дороже N TON за подарок не покупать, что бы ни просил TG Gift
        "max_ton_per_day": 200.0,        # всего на NFT-подарки за сутки, TON
        "transfer_max_ton": 0.6,         # перевод подарка в Telegram (газ и сбор Fragment) не дороже N TON
        "expected_transfer_ton": 0.15,   # оценка стоимости перевода для цен TG Gift
        "wallet_reserve_ton": 2.0,       # после покупки на кошельке остаётся N TON на заказы звёзд
        "transfer_wait_minutes": 30,     # ждать появления купленного подарка на кошельке до перевода
    },
    # Бот TG Gift (профиль giftshop): продажа подарков Telegram в разделе FunPay «Подарки»
    "giftshop": {
        # Лоты: один лот на подарок. emoji/stars — подарок из каталога Telegram (цена в звёздах), name — название
        # в карточке. offer_id — id лота FunPay (0 — бот выставит сам), position — место в списке (0 — как в RTBA),
        # price — своя цена лота, ₽ (0 — ведёт RTBA), sell — продавать ли этот подарок
        "lots": [],
        "subcategory_regex": r"(?i)подарк|gift",
        "rub_per_star": 1.35,            # себестоимость звезды, пока не было поставок от StarsBot (₽)
        "min_margin_pct": 5.0,           # цена лота не ниже себестоимости подарка + N%
        "start_markup_pct": 30.0,        # стартовая цена нового лота: себестоимость + N%
        "auto_create_lots": True,        # выставлять лоты подарков с sell=true, у которых нет offer_id
        "lot_texts": {},                 # свои тексты карточки: summary_ru, desc_ru, summary_en, desc_en ({gift}, {stars})
        "share_from": "../starsbot/config.yaml",   # взять golden_key и Telegram-уведомления у StarsBot, если свои пусты
        "supply": {
            "enabled": True,             # просить у StarsBot звёзды на аккаунт подарков
            "min_stars": 300,            # когда свободных звёзд меньше — заявка на пополнение
            "amount": 1000,              # сколько просить за раз
        },
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
    # Казначейство: все балансы в одном месте, автопополнение кошелька бота с Bybit/хаба, советы по выводу
    "treasury": {
        "enabled": False,
        "auto": True,                    # двигать деньги самому в пределах лимитов; False — каждый шаг по кнопке
        "interval_seconds": 120,
        "burn_days": 7,                  # за сколько дней считать расход TON
        "fallback_burn_rub_day": 60000,  # расход, пока статистики мало (звёзды ~30к + подарки ~30к ₽/сутки)
        "usdt_rub": 86.8,                # цена USDT на P2P с переводом (если не записаны свои покупки)
        "daily_report_hour": 10,
        "working": {"refill_below_hours": 8, "refill_to_hours": 24},   # кошелёк бота: запас в часах расхода
        "hub": {
            "enabled": False,            # отдельный кошелёк с основным запасом TON (вторая сид-фраза)
            "mnemonic": "", "address": "", "version": "v5r1", "mnemonic_type": "auto",
            "refill_below_hours": 12, "keep_hours": 48,
        },
        "bybit": {
            "enabled": False,
            "api_key": "", "api_secret": "",
            "base_url": "https://api.bybit.com",   # для других регионов: api.bybit.kz, api.bybit.eu, api.bytick.com
            "symbol": "TONUSDT", "withdraw_chain": "TON",
            "max_slippage_pct": 0.5,     # покупать не дороже ask + 0.5%
            "min_withdraw_ton": 20,      # выводить партиями не меньше (комиссия вывода фиксированная)
            "max_withdraw_ton_day": 3000,
            "max_buy_usdt_day": 5000,
            "keep_usdt": 0,
        },
        "p2p": {"trigger_days": 1.5, "target_days": 3, "min_buy_rub": 25000, "cooldown_hours": 20},
        "router": {
            "enabled": True,
            "min_batch_rub": 20000,      # выводить только крупными суммами
            "max_batch_rub": 150000,     # не больше за один вывод на карту
            "cooldown_hours": 24,        # не чаще одного вывода на карту за столько часов
            "max_per_day": 1,
            "keep_rub": 0,
            "sources": [],               # переопределения: [{key: funpay, fee_pct: 6, margin_pct: 1, ...}]
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
        "usdt_rub": 0.0,                 # почём вы покупаете USDT, ₽ (0 — живой курс Rapira)
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
        "confirm_reminder_minutes": 10,  # одно напоминание подтвердить заказ через N мин после выдачи (0 — не напоминать)
        # подтверждение получателя покупателем до выдачи (доказательство для арбитража):
        # off — не спрашивать; new_or_big — новым покупателям и от confirm_username_min_stars; always — всегда
        "confirm_username": "new_or_big",
        "confirm_username_min_stars": 1000,
        "confirm_username_timeout_minutes": 15,   # покупатель молчит N мин после вопроса →
        "confirm_username_timeout_action": "deliver",   # deliver — выдать (он предупреждён), manual — продавцу
    },
    "evidence": {
        "enabled": True,                 # снимок доказательств по каждому заказу сразу после выдачи
        "folder": "data/evidence",       # снимки <заказ>.json и пакеты evidence_*.zip
        "auto_reply_arbitration": True,  # арбитраж FunPay просит доказательства → бот сам отвечает в чат заказа
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
        "min_profit_pct": 3.0,          # не покупать без одобрения, если чистая прибыль заказа меньше N% (0 — выкл)
        "price_guard": True,            # поднимать цену лота, если она ниже безубытка + min_margin_pct из RTBA
        "price_guard_minutes": 10,      # как часто проверять цены своих лотов
    },
    "rtba": {
        "enabled": False, "interval_seconds": 300, "strategy": "top1", "target_position": 1,
        "buyer_markup_pct": 14.06,       # сколько FunPay добавляет к цене лота для покупателя (91.25 при лоте 80)
        "undercut_abs": 0.5, "undercut_pct": 0.0, "min_margin_pct": 8.0,
        "floor_price_per_star": 0.0, "ceiling_price_per_star": 0.0, "min_change": 0.3,
        "max_updates_per_hour": 6, "ignore_competitors": [], "competitor_min_amount": 1,
        "competitor_only_online": False, "outlier_iqr_factor": 1.5, "method_filter": "username",
        "raise_gap_pct": 3.0,
        "fallback_ton_per_star": 0.0,    # цена звезды в TON, пока не было ни одной покупки
        # пресет цен (starsbot/core/strategy.py): custom — позиция лота + min_margin_pct, как раньше;
        # turnover / balanced / margin — сегменты номиналов; auto — переключение по лимиту карты
        "preset": "custom",
        "segments": [],                  # свои сегменты для custom: [{from, to, position, max_position, margin_pct}]
        "competitor_min_reviews": 0,     # не равняться на продавцов с меньшим числом отзывов
        "competitor_min_rating": 0.0,    # и с рейтингом ниже (в таблице FunPay только целые звёзды)
        "spread_buffer": {"enabled": False, "min_gap_rub": 5.0, "min_gap_pct": 1.5, "max_gap_pct": 10.0,
                          "offset_rub": 0.05},
        "night": {"enabled": False, "start_hour": 1, "end_hour": 8, "utc_offset_hours": 3, "floor_bonus_pct": 2.0},
        "card_limit": {"daily_rub": 0.0, "monthly_rub": 0.0, "balanced_from": 0.7, "margin_from": 1.0,
                       "no_limit_preset": "balanced"},
    },
    "economics": {
        "funpay_fee_pct": 0.0,           # комиссия FunPay, если она вычитается из вашей цены лота
        "withdraw_fee_pct": 3.0,         # комиссия вывода денег с FunPay (зависит от способа)
        "withdraw_fee_fixed_rub": 0.0,
        "cost_rate_rub": 0.0,            # >0 — считать себестоимость по этому курсу ₽/TON вместо журнала пополнений
        "cost_basis": "live",            # live — себестоимость по цене следующей закупки; topups — по журналу пополнений
        "star_usd": 0.015,               # цена звезды на Fragment в долларах (0 — брать из прошлых покупок в TON)
        "exchange_spread_pct": 2.0,      # насколько дороже биржевого курса вы покупаете TON (P2P, обменник)
        "deposit_fee_ton": 0.0,          # комиссия за перевод TON на кошелёк бота (за одно пополнение)
        "typical_topup_ton": 50.0,       # обычный размер пополнения — чтобы разложить комиссию на звёзды
        "gas_per_purchase_ton": 0.001,   # комиссия сети за одну покупку (факт 103 заказов — 0.00053)
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
        "min_margin_pct": 3.0,           # пол маржи: панель подсветит цену ниже, рекомендованная цена — не ниже
    },
    # Третий канал: Avito (безопасная сделка для цифровых товаров, с 30.04.2026). Покупатель нажимает «Купить»,
    # платит Авито, бот видит оплату в чате объявления, просит @username и покупает звёзды на Fragment.
    # Нужен доступ к API Авито (Профиль → Для профессионалов → API) с правом на мессенджер.
    "avito": {
        "enabled": False,
        "client_id": "", "client_secret": "",    # avito.ru/professionals/api
        "user_id": 0,                    # номер профиля; 0 — узнать автоматически (/core/v1/accounts/self)
        "api_base": "https://api.avito.ru",
        "poll_interval": 20,             # опрос чатов, сек (вебхук не нужен — работает с домашнего ПК)
        # Объявления: id объявления → номинал. price — ваша цена, ₽ (не опустится ниже пола маржи)
        "products": [{"stars": n, "item_id": 0, "price": 0} for n in DENOMINATIONS],
        # Признаки оплаты в системных сообщениях чата (регулярные выражения, без учёта регистра)
        "paid_patterns": ["оплатил", "оплачен", "заказ оформлен", "новый заказ"],
        "done_patterns": ["подтвердил получение", "заказ завершён", "заказ завершен"],
        "cancel_patterns": ["отменил заказ", "заказ отменён", "заказ отменен", "возврат"],
        "presale_replies": True,         # отвечать на вопросы до оплаты (цена, как купить)
        "presale_cooldown_minutes": 60,  # не чаще раза в N минут в одном чате
        "auto_complete_hours": 48,       # «выдан» → «завершён», если Авито не прислал подтверждение покупателя
        "commission_pct": 10.0,          # комиссия Авито за безопасную сделку, % (уточните в кабинете)
        "withdraw_fee_pct": 0.0,
        "min_margin_pct": 3.0,           # маржа не ниже, % от цены
        "price_mode": "fixed",           # fixed — ваша цена (но не ниже пола), margin — по целевой марже
        "target_margin_pct": 8.0,
        "price_round": 1,                # шаг цены, ₽ (округление вверх)
        "price_sync": True,              # бот сам меняет цену объявлений через API
        "price_interval": 900,           # как часто пересчитывать цены, сек
        "messages": {},                  # свои тексты (ключи как в starsbot/avito/texts.py)
    },
    # Четвёртый канал: Wildberries, категория «Цифровые товары» (ключи активации). Площадку WB Digital закрыли
    # 01.12.2025; на основной витрине категория пока в пилоте для части продавцов, открытого API выдачи нет.
    # Поэтому бот выпускает пул кодов активации, вы загружаете их в кабинет WB файлом, а покупатель вводит код
    # и @username в боте активации — как на Ozon.
    "wb": {
        "enabled": False,
        "products": [{"stars": n, "nm_id": "", "price": 0} for n in DENOMINATIONS],   # nm_id — артикул WB
        "code_prefix": "TGW",            # коды вида TGW-ABCD-EFGH-JKMN (не путаются с кодами Ozon)
        "pool_warn_low": 2,              # предупредить, когда невыданных кодов номинала осталось меньше
        "commission_pct": 5.0,           # комиссия WB для ключей активации, % (уточните в кабинете)
        "withdraw_fee_pct": 0.0,
        "min_margin_pct": 3.0,
        "price_mode": "fixed", "target_margin_pct": 8.0, "price_round": 1,
        "activation_bot": {"enabled": True, "bot_token": ""},   # отдельный бот для покупателей WB
        "messages": {},
    },
    # Третий канал продаж: маркетплейс GGSEL (ggsel.net, Seller API). Покупатель указывает @username в поле
    # заказа при оформлении (или пишет его в чат заказа), дальше — тот же путь, что у FunPay: Fragment → TON.
    "ggsel": {
        "enabled": False,
        "seller_id": "", "api_key": "",          # кабинет GGSEL → Настройки → API (ID продавца и ключ API)
        "api_base": "https://seller.ggsel.com",
        "poll_interval": 20,             # опрос новых продаж и чатов, сек
        "refresh_seconds": 120,          # как часто перечитывать статус незакрытых счетов
        "sales_top": 50,                 # сколько последних продаж читать за опрос
        # Товары: ID товара на GGSEL → сколько звёзд в одной штуке (цена — для ручного режима цен)
        "products": [{"stars": n, "offer_id": "", "price": 0} for n in DENOMINATIONS],
        # Telegram Premium на GGSEL (нужен включённый раздел premium): ID товара → срок в месяцах
        "premium_products": [{"months": m, "offer_id": "", "price": 0} for m in PREMIUM_MONTHS],
        "stock_sync": True,              # остаток товара на GGSEL = сколько раз хватит баланса кошелька
        "max_stock": 100,
        "price_mode": "margin",          # margin — цена = себестоимость + маржа по курсу TON; manual — цены из таблицы
        "margin_pct": 6.0,               # желаемая маржа с цены, %
        "min_margin_pct": 3.0,           # ниже — товар скрывается (остаток 0) и приходит алерт
        "hide_below_min_margin": True,
        "price_push_minutes": 10,        # не чаще, чем раз в N минут менять цену на GGSEL
        "commission_pct": 10.0,          # комиссия GGSEL с цены — уточните в кабинете для вашей категории
        "withdraw_fee_pct": 3.0,         # комиссия вывода с GGSEL (зависит от способа: карта, USDT и т.д.)
        "sla_warn_minutes": 30,          # алерт, если заказ не выдан
        "sla_critical_hours": 6,         # срочный алерт: после 12 ч GGSEL вправе вернуть деньги сам (п. 7.5)
        "confirm_reminder_hours": 24,    # одно напоминание подтвердить получение (0 — не напоминать)
        "messages": {},                  # свои тексты для покупателей GGSEL (ключи как в starsbot/ggsel/texts.py)
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


def profile_of(data: dict | None, overrides: dict | None = None) -> str:
    """Профиль бота: из настроек, из переменной STARSBOT_PROFILE (её ставит запуск с --profile) или stars."""
    for src in (overrides or {}, data or {}):
        p = ((src.get("general") or {}) if isinstance(src, dict) else {}).get("profile")
        if p:
            return str(p)
    return os.environ.get("STARSBOT_PROFILE") or "stars"


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Section:
    data: dict = {}
    if path and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    base = DEFAULTS
    profile = profile_of(data, overrides)
    if profile == "giftshop":
        from starsbot.giftshop.profile import PROFILE_DEFAULTS
        base = _merge(DEFAULTS, PROFILE_DEFAULTS)
    merged = _merge(base, _expand_env(data))
    if overrides:
        merged = _merge(merged, overrides)
    return _wrap(merged)


def validate_config(cfg: Section) -> list[str]:
    """Возвращает список проблем конфигурации (пустой — всё ок)."""
    problems: list[str] = []
    if cfg.general.demo:
        return problems
    if cfg.general.get("profile") == "giftshop":
        from starsbot.giftshop.profile import validate_giftshop
        return validate_giftshop(cfg)
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
    problems += validate_premium(cfg)
    problems += validate_ozon(cfg)
    problems += validate_ggsel(cfg)
    problems += validate_avito(cfg)
    problems += validate_wb(cfg)
    return problems


def validate_premium(cfg: Section) -> list[str]:
    pr = cfg.get("premium") or {}
    if not pr.get("enabled"):
        return []
    out = []
    for lot in pr.get("lots") or []:
        if int(lot.get("months") or 0) not in PREMIUM_MONTHS:
            out.append(f"premium.lots: срок {lot.get('months')} мес. — Fragment дарит только 3, 6 и 12 месяцев")
    if str(pr.get("price_mode") or "rtba") not in ("rtba", "margin", "manual"):
        out.append("premium.price_mode: rtba, margin или manual")
    return out


def validate_ggsel(cfg: Section) -> list[str]:
    gg = cfg.get("ggsel") or {}
    if not gg.get("enabled"):
        return []
    out = []
    if not str(gg.get("seller_id") or "").strip().isdigit() or not str(gg.get("api_key") or "").strip():
        out.append("ggsel: укажите ID продавца (число) и ключ API GGSEL")
    if not any(str(p.get("offer_id") or "").strip().isdigit() and (p.get("stars") or p.get("months"))
               for p in (gg.get("products") or []) + (gg.get("premium_products") or [])):
        out.append("ggsel.products: укажите ID товара GGSEL хотя бы для одного номинала")
    if str(gg.get("price_mode") or "manual") not in ("manual", "margin"):
        out.append("ggsel.price_mode: manual или margin")
    if float(gg.get("min_margin_pct") or 0) > float(gg.get("margin_pct") or 0) and gg.get("price_mode") == "margin":
        out.append("ggsel.margin_pct должна быть не меньше min_margin_pct")
    return out


def validate_avito(cfg: Section) -> list[str]:
    av = cfg.get("avito") or {}
    if not av.get("enabled"):
        return []
    out = []
    if not str(av.get("client_id") or "").strip() or not str(av.get("client_secret") or "").strip():
        out.append("avito: укажите client_id и client_secret (Авито → Для профессионалов → API)")
    if not any(p.get("item_id") and p.get("stars") for p in (av.get("products") or [])):
        out.append("avito.products: укажите id хотя бы одного объявления со звёздами")
    return out


def validate_wb(cfg: Section) -> list[str]:
    wb = cfg.get("wb") or {}
    if not wb.get("enabled"):
        return []
    out = []
    bot = wb.get("activation_bot") or {}
    if not (bot.get("enabled") and bot.get("bot_token")):
        out.append("wb: укажите токен бота активации (@BotFather) — без него покупатель не введёт код")
    tokens = {str(cfg.notifications.telegram.bot_token or ""),
              str(((cfg.get("ozon") or {}).get("activation_bot") or {}).get("bot_token") or "")} - {""}
    if bot.get("bot_token") and str(bot.get("bot_token")) in tokens:
        out.append("wb.activation_bot: нужен отдельный бот (не уведомлений и не бот активации Ozon)")
    return out


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
