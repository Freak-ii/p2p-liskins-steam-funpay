"""Настройки ShopBot: значения по умолчанию + config.yaml + разовый config.patch.yaml (как у StarsBot).

Секреты (токен бота, ключи ЮKassa и Crypto Pay, токены поставщиков) живут только в config.yaml на компьютере
пользователя или в переменных окружения SHOPBOT_<SECTION>_<KEY>; в репозиторий не попадают.
"""
from __future__ import annotations

import copy
import os
import secrets
from pathlib import Path
from typing import Any

import yaml

from shopbot.services_catalog import ITEMS as SERVICE_ITEMS

DEFAULTS: dict[str, Any] = {
    "general": {
        "shop_name": "Stars Market",
        "demo": False,
        "dry_run": True,             # бой без денег: платежи создаются, выдача не идёт (пока владелец не включит)
        "support_username": "",      # @ поддержки, показывается клиентам
        "currency": "RUB",
    },
    "dashboard": {"token": "", "operator_token": "", "viewer_token": ""},
    "telegram": {
        "bot_token": "",             # бот магазина (НЕ тот же, что у StarsBot/MannBot: иначе getUpdates 409)
        "bot_username": "",
        "admin_chat_ids": [],        # кому слать тревоги RTBA и заявки на ручное подтверждение
        "api": "https://api.telegram.org",
    },
    # Мини-приложение (окно магазина, как у BotFather), см. shopbot/miniapp.py:
    # url — постоянный https-адрес страницы (GitHub Pages); пусто → запуск с ПК через туннель Cloudflare
    # (нужен cloudflared.exe), адрес меняется при каждом запуске, кнопку «Магазин» бот обновляет сам.
    "miniapp": {"url": "", "menu_text": "Магазин", "tunnel": "cloudflared", "port": 8807, "cloudflared": ""},
    "channel": {
        "enabled": False,            # публикация в канал только после явного включения владельцем
        "chat_id": "",               # @channel или -100…; бот должен быть админом канала
        "autoupdate_price_post": True,
        "price_post_message_id": 0,
        "reprice_threshold_pct": 3,  # обновлять пост с ценами, когда цена сдвинулась больше чем на N %
        # пост «Открыть магазин» (без цен) с кнопкой мини-приложения: опубликовать и закрепить при запуске
        "open_post": {"auto": False, "pin": True},
    },
    # ---------------------------------------------------------------- товары
    "catalog": {
        "stars": {"enabled": True, "denominations": [50, 75, 100, 150, 250, 350, 500, 1000], "min": 50,
                  "max": 20000, "margin_pct": 12, "min_margin_pct": 4, "undercut_funpay_pct": 5,
                  "funpay_ref_rub_per_star": 1.55},   # цена покупателя на FunPay (3-е место), из StarsBot RTBA
        "gifts": {"enabled": True, "margin_pct": 15, "min_margin_pct": 5,
                  "items": [
                      {"id": "bear", "emoji": "🧸", "title": "Мишка", "stars": 15, "convert": 13},
                      {"id": "heart", "emoji": "💝", "title": "Сердце", "stars": 15, "convert": 13},
                      {"id": "gift", "emoji": "🎁", "title": "Подарок", "stars": 25, "convert": 21},
                      {"id": "rose", "emoji": "🌹", "title": "Роза", "stars": 25, "convert": 21},
                      {"id": "cake", "emoji": "🎂", "title": "Торт", "stars": 50, "convert": 43},
                      {"id": "flowers", "emoji": "💐", "title": "Букет", "stars": 50, "convert": 43},
                      {"id": "rocket", "emoji": "🚀", "title": "Ракета", "stars": 50, "convert": 43},
                      {"id": "champagne", "emoji": "🍾", "title": "Шампанское", "stars": 50, "convert": 43},
                      {"id": "cup", "emoji": "🏆", "title": "Кубок", "stars": 100, "convert": 85},
                      {"id": "ring", "emoji": "💍", "title": "Кольцо", "stars": 100, "convert": 85},
                      {"id": "diamond", "emoji": "💎", "title": "Алмаз", "stars": 100, "convert": 85},
                  ]},
        "premium": {"enabled": True, "margin_pct": 8, "min_margin_pct": 3, "months": [3, 6, 12]},
        "steam_keys": {"enabled": True, "margin_pct": 6, "min_margin_pct": 2, "max_qty": 50,
                       "title": "Ключ Mann Co. (TF2)",
                       "steam_value_usd": 1.87},   # ≈ сколько ключ даёт на балансе Steam после продажи на ТП (оценка)
        # Сервисы (shopbot/services.py): VPN, карты App Store США, Claude/ChatGPT через карту, Spotify, Steam.
        # prefer_auto_within_pct — склад/своя панель берутся, даже если дороже перепродажи не больше чем на N %;
        # max_over_market_pct — вариант закрывается RTBA, если наша цена дороже рынка (market_ref_rub) на N %.
        "services": {"enabled": True, "margin_pct": 12, "min_margin_pct": 4, "prefer_auto_within_pct": 5,
                     "stock_low": 3, "max_over_market_pct": 5, "items": SERVICE_ITEMS},
    },
    # ---------------------------------------------------------------- выкуп у клиентов
    "buyback": {
        "fee_pct": 3,                     # комиссия магазина при продаже звёзд/подарков клиентом (прозрачная)
        # Выкуп звёзд. auto: на beat_pct выше официального вывода Telegram (ref, 0.013 $ после 21 дня ожидания),
        # но не выше ценности звезды для нас (use — цена на Fragment, 0.015 $) минус min_spread_pct.
        # Звёзды приходят на баланс бота (XTR) и тратятся на подарки клиентам (поставщик gifts с bot_balance).
        # Запас ≥ max_stock_stars — курс падает до вывода −2%, чтобы не копить звёзды без сбыта.
        # fixed: usd_per_star и отдельная комиссия fee_pct (как раньше).
        "stars": {"enabled": True, "min": 100, "max": 50000, "mode": "auto", "usd_per_star": 0.0115,
                  "ref_usd_per_star": 0.013, "beat_pct": 1.0, "use_usd_per_star": 0.015, "min_spread_pct": 6.0,
                  "max_stock_stars": 20000, "full_stock_cut_pct": 2.0},
        "gifts": {"enabled": True, "pay_stars": True, "pay_crypto": True,
                  "gift_account": "",     # @ аккаунта, на который клиент передаёт подарок
                  "unique_manual_quote": True},
        "daily_cap_usdt": 300,
        "payout_asset": "USDT",
    },
    # ---------------------------------------------------------------- оплата
    "payments": {
        "order_ttl_minutes": 30,
        # Карта — заготовка под эквайринг (ЮKassa сейчас; другой провайдер — новым классом в payments).
        # Выключена, пока нет договора: в меню клиента не показывается.
        # provider: yookassa (ИП/самозанятость) | crystalpay | aaio (P2P-шлюзы для физлиц: KYC по паспорту,
        # комиссия 5–12 % — fee_pct ставьте по тарифу из кабинета шлюза + ~1 % на вывод USDT; см. README).
        "card": {"enabled": False, "provider": "yookassa", "fee_pct": 3.5,
                 "crystalpay": {"login": "", "secret": "", "redirect_url": ""},   # старое место ключей кассы
                 "aaio": {"merchant_id": "", "secret1": "", "api_key": ""},
                 "yookassa": {"shop_id": "", "secret_key": "", "test": True, "return_url": "",
                              "send_receipt": False, "vat_code": 1, "tax_system_code": 0}},
        # Касса CrystalPAY (provider: crystalpay у карты): СБП по счёту кассы. СБП касса получает только
        # со статусом «Профессиональный»; пока его нет, RTBA держит «Картой / СБП» закрытым сам.
        # sbp_method: auto — найти включённый метод СБП по method/list, или точный ключ метода из кассы.
        # subtract_from: balance — комиссию кассы платим мы (она в payments.card.fee_pct), amount — покупатель.
        # callback_url — только если панель доступна из интернета (…/api/pay/crystalpay); иначе опрос статуса.
        "crystalpay": {"login": "", "secret": "", "salt": "", "sbp_method": "auto", "subtract_from": "balance",
                       "redirect_url": "", "callback_url": ""},
        "crypto": {"enabled": True, "provider": "cryptopay", "fee_pct": 2, "min_fee_pct": 2,
                   "assets": ["USDT", "TON"],
                   "cryptopay": {"token": "", "testnet": True}},
        "stars": {"enabled": True, "rub_per_star": 1.25},   # оплата звёздами Telegram (XTR) — кроме покупки звёзд
        # Крипта на общий кошелёк TON (хаб казначейства StarsBot) — деньги сразу в круге, без Crypto Pay.
        # Комиссии отдельной строкой нет: спред заложен в курс обмена (курс Bybit − spread_pct %).
        # spread_pct держим минимальным, но не ниже min_spread_pct (покрывает сеть + обмен USDT→TON на Bybit).
        "wallet": {"enabled": True, "address": "", "assets": ["USDT", "TON"], "spread_pct": 3.0,
                   "min_spread_pct": 1.0, "toncenter_api_key": "", "ttl_minutes": 60},
        # Подарками Telegram: клиент передаёт подарки на аккаунт приёма; засчитываем по цене продажи подарка
        # Telegram (convert, ⭐) × курс звезды − haircut_pct. Подарки уходят в круг: в запас TG Gift или в звёзды.
        "gift": {"enabled": True, "account": "", "haircut_pct": 5.0, "ttl_minutes": 60},
        # Скинами Steam: обмен на trade_url магазина, оценка по Market.CSGO − haircut_pct; скины продаются
        # LisBot/Market.CSGO → USDT → хаб. Пока подтверждает оператор.
        "skins": {"enabled": True, "trade_url": "", "haircut_pct": 12.0, "ttl_minutes": 120,
                  "market": "market.csgo.com"},
        "balance": {"enabled": True},
    },
    # Круг денег: куда уходит каждый приём (см. shopbot/loop.py). Ничего не двигает само — ведёт учёт и
    # подсказывает следующий шаг, чтобы приём не лежал активом.
    "loop": {"xtr_hold_days": 21, "sweep_cryptobot_usdt": 50, "gift_use": "tggift_stock",
             "skins_sell": "lisbot_market_csgo", "card_route": "p2p_usdt_bybit"},
    "rates": {
        "source": "bybit",                # bybit | fixed
        "usdt_rub": 86.8,                 # запасной курс (ваш P2P, см. starsbot economics)
        "ton_usdt": 3.1,
        "max_age_minutes": 10,            # старше — оплата криптой закрывается RTBA
        "refresh_seconds": 120,
    },
    # ---------------------------------------------------------------- поставщики экосистемы
    "suppliers": {
        "stars": {"kind": "http", "url": "http://127.0.0.1:8803", "token": ""},     # StarsBot /api/supply
        "premium": {"kind": "http", "url": "http://127.0.0.1:8803", "token": ""},   # StarsBot (starsbot.premium)
        "gifts": {"kind": "http", "url": "http://127.0.0.1:8805", "token": ""},     # TG Gift (профиль giftshop)
        # MannBot /api/supply: ключи TF2 обменом Steam (kind: manual — выдача вручную по cost_rub)
        "steam_keys": {"kind": "http", "url": "http://127.0.0.1:8801", "token": "", "cost_rub": 165},
    },
    # Источники сервисов. panel — свой VPN-сервер (Marzban или 3x-ui): cost_month_rub — аренда VPS в месяц,
    # capacity — сколько ключей держит сервер (себестоимость ключа = cost_month_rub / capacity × месяцы).
    # resale_sla_minutes — за сколько оператор обещает выдать перепродажу; дольше — тревога и возврат на баланс.
    "services": {
        "secret_key": "",            # ключ шифрования логинов/паролей покупателей (создаётся сам)
        # Свои VPN-серверы: блок на каждый сервер печатает bots/shopbot/vpn/install-3xui.sh. Ключ создаётся
        # на всех сразу (один UUID), подписка — с первого. kind: 3xui | marzban.
        "panels": [],
        # общая экономика серверов (и старый формат одного сервера: url/username/password здесь же)
        # Aeza NLs-1 (1 ядро, 2 ГБ, 1 Гбит/с, безлимит) — 5.93 €/мес ≈ 560 ₽; ~100 ключей на сервер
        "panel": {"kind": "3xui", "url": "", "username": "", "password": "", "inbound_tags": [],
                  "inbound_id": 1, "sub_base": "", "flow": "xtls-rprx-vision", "limit_ip": 3, "data_limit_gb": 0,
                  "cost_month_rub": 560, "capacity": 100},
        "resale_sla_minutes": 120,
        "learned_cost_days": 14,   # цена закупки = медиана 3 последних реальных закупок за N дней (0 — выкл)
        # Свои семьи Яндекс Плюс (shopbot/family.py): правила сортировки аккаунтов, куда можно добавлять людей.
        "family": {
            "seats_per_account": 3,    # Плюсом делятся с 3 близкими (владелец — наш, его место не продаём)
            "reserve_days": 3,         # Плюс аккаунта должен быть оплачен на срок места + столько дней
            "seat_cooldown_days": 1,   # после исключения участника место «остывает» столько дней
            "max_adds_30d": 8,         # не больше стольких новых людей в аккаунт за 30 дней (меньше риск бана)
            "max_risk": 2,             # аккаунты с риском ≥ этого (ставит оператор) не получают новых людей
            "vacancy_pct": 15,         # надбавка к себестоимости места за простой свободных мест
            "invite_ttl_hours": 24,    # ссылки-приглашения из пула старше этого не выдаются
        },
    },
    # ---------------------------------------------------------------- RTBA: контроль каждого пункта
    "rtba": {
        "interval_seconds": 30,
        "fail_window_minutes": 60,
        "fail_threshold": 3,              # столько сбоев поставщика за окно — пункт закрывается
        "max_delivery_retries": 5,        # после стольких неудачных передач поставщику — возврат на баланс
        "reopen_after_minutes": 15,       # автоматическая повторная проверка закрытого пункта
        "user_daily_rub": 30000,
        "new_user_daily_rub": 5000,       # первые сутки клиента
        "max_order_rub": 25000,
        "auto_payout_usdt": 20,           # выплаты выше — только после подтверждения
        "payout_reserve_usdt": 10,        # неснижаемый остаток на балансе выплат
        "min_quote_margin_rub": 3,
    },
    # Лояльность: уровень по обороту за window_days (покупки + выкуп). Скидка уровня и приглашения вместе не
    # опускают маржу заказа ниже min_margin_after_pct. Тег CRM «tier:<id>» даёт уровень вручную (переговоры с китом).
    "loyalty": {
        "enabled": True, "window_days": 30, "min_margin_after_pct": 2.0,
        "tiers": [
            {"id": "base", "title": "Клиент", "min_rub": 0, "discount_pct": 0, "buyback_bonus_pct": 0, "limit_mult": 1},
            {"id": "silver", "title": "Серебро", "min_rub": 15000, "discount_pct": 1, "buyback_bonus_pct": 0.5,
             "limit_mult": 1.5},
            {"id": "gold", "title": "Золото", "min_rub": 50000, "discount_pct": 2, "buyback_bonus_pct": 1,
             "limit_mult": 3},
            {"id": "whale", "title": "Кит", "min_rub": 150000, "discount_pct": 3, "buyback_bonus_pct": 1.5,
             "limit_mult": 6},
        ],
    },
    # Отзывы: после выдачи бот просит оценку 1–5 и пару слов. Бонус — только за первый отзыв к заказу от
    # bonus_min_order_rub; отзывы с оценкой ≥ publish_min_rating попадают в черновики поста канала (публикует владелец).
    "reviews": {"enabled": True, "bonus_rub": 5, "bonus_min_order_rub": 300, "publish_min_rating": 5,
                "ask_delay_minutes": 0},
    # Антискам: новые продавцы и всплески — на ручную проверку; самоприглашения не вознаграждаются;
    # бонусы не выводятся и покрывают не больше bonus_pay_share_pct заказа.
    "antiscam": {"new_seller_hours": 72, "new_seller_min_deals": 1, "new_seller_daily_usdt": 15,
                 "seller_daily_usdt": 150, "ref_daily_cap_rub": 500, "bonus_pay_share_pct": 30,
                 "max_recipients_per_day": 5},
    # Метки трафика: t.me/<бот>?start=src_<метка> (можно src_<метка>__stars); первая метка держится window_days.
    "sources": {"window_days": 30},
    "referral": {
        "enabled": True,
        "reward_pct": 1.5,                # от суммы покупки приглашённого → бонусный баланс пригласившего
        "max_share_of_margin_pct": 40,    # не больше этой доли нашей маржи по заказу
        "friend_discount_pct": 3,         # скидка на первый заказ приглашённого
        "friend_discount_max_rub": 100,
    },
}

SECRETS = [("telegram", "bot_token"), ("payments.card.yookassa", "secret_key"),
           ("payments.crypto.cryptopay", "token"),
           ("payments.card.crystalpay", "secret"), ("payments.crystalpay", "secret"), ("payments.crystalpay", "salt"), ("payments.card.aaio", "secret1"), ("payments.card.aaio", "api_key"), ("suppliers.stars", "token"), ("suppliers.premium", "token"),
           ("suppliers.gifts", "token"), ("suppliers.steam_keys", "token"),
           ("services", "secret_key"), ("services.panel", "password"),
           ("dashboard", "token"), ("dashboard", "operator_token"), ("dashboard", "viewer_token")]


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def dig(d: dict, path: str, default: Any = None) -> Any:
    node: Any = d
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


class Config:
    def __init__(self, home: Path, *, demo: bool = False, overrides: dict | None = None):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "config.yaml"
        self.demo = demo
        self.overrides = overrides or {}
        self.data: dict = {}
        self.reload()

    def reload(self) -> None:
        user: dict = {}
        if self.path.exists():
            user = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        patch = self.home / "config.patch.yaml"
        if patch.exists():                       # разовая правка без секретов, как у StarsBot
            try:
                user = merge(user, yaml.safe_load(patch.read_text(encoding="utf-8")) or {})
                self._write(user)
                patch.replace(patch.with_suffix(".applied.yaml"))
            except (OSError, yaml.YAMLError):
                pass
        data = merge(DEFAULTS, user)
        for sec, key in SECRETS:                # переменные окружения сильнее файла
            env = "SHOPBOT_" + (sec.replace(".", "_") + "_" + key).upper()
            if os.environ.get(env):
                node = data
                for part in sec.split("."):
                    node = node.setdefault(part, {})
                node[key] = os.environ[env]
        data = merge(data, self.overrides)
        if self.demo:
            data["general"]["demo"] = True
            data["payments"]["card"]["enabled"] = True       # демо показывает все способы, включая карту
        if not data["dashboard"].get("token"):
            data["dashboard"]["token"] = secrets.token_urlsafe(18)
            self.save({"dashboard": {"token": data["dashboard"]["token"]}})
        self.data = data

    def _write(self, user: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("# Настройки ShopBot (секреты не публикуйте)\n" +
                       yaml.safe_dump(user, allow_unicode=True, sort_keys=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def save(self, changes: dict) -> None:
        user = {}
        if self.path.exists():
            user = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self._write(merge(user, changes))
        if self.data:
            self.data = merge(self.data, changes)

    def get(self, path: str, default: Any = None) -> Any:
        return dig(self.data, path, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def is_demo(self) -> bool:
        return bool(self.data["general"].get("demo"))
