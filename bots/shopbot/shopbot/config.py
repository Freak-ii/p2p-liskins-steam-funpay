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
    },
    # ---------------------------------------------------------------- выкуп у клиентов
    "buyback": {
        "fee_pct": 3,                     # комиссия магазина при продаже звёзд/подарков клиентом (прозрачная)
        "stars": {"enabled": True, "min": 100, "max": 50000, "usd_per_star": 0.0115},   # курс выкупа до комиссии
        "gifts": {"enabled": True, "pay_stars": True, "pay_crypto": True,
                  "gift_account": "",     # @ аккаунта, на который клиент передаёт подарок
                  "unique_manual_quote": True},
        "daily_cap_usdt": 300,
        "payout_asset": "USDT",
    },
    # ---------------------------------------------------------------- оплата
    "payments": {
        "order_ttl_minutes": 30,
        # Карта / СБП. provider: yookassa (эквайринг, нужен ИП/самозанятость) или P2P-шлюз для физлиц:
        # crystalpay | aaio | freekassa (см. payments/gateways.py). Шлюз копит рубли у себя, вывод в USDT на хаб
        # TON возвращает их в круг. fee_pct — комиссия клиенту: приём шлюза + его вывод в USDT + запас.
        # Выключено, пока нет подключённого шлюза: в меню клиента не показывается.
        "card": {"enabled": False, "provider": "yookassa", "fee_pct": 3.5, "ttl_minutes": 30,
                 "title": "Картой / СБП",
                 "yookassa": {"shop_id": "", "secret_key": "", "test": True, "return_url": "",
                              "send_receipt": False, "vat_code": 1, "tax_system_code": 0},
                 "crystalpay": {"login": "", "secret": "", "salt": ""},
                 "aaio": {"merchant_id": "", "secret1": "", "api_key": ""},
                 "freekassa": {"shop_id": "", "api_key": "", "method_id": 0}},
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
        # Telegram Wallet (@wallet) через Wallet Pay: счёт в USD по курсу USDT + fee_pct (комиссия Wallet Pay
        # и запас на вывод). Нужен магазин Wallet Pay и ключ api_key. Выключено, пока ключа нет.
        "tgwallet": {"enabled": False, "api_key": "", "fee_pct": 2.0, "ttl_minutes": 60, "return_url": ""},
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
             "skins_sell": "lisbot_market_csgo", "card_route": "p2p_usdt_bybit",
             "sweep_gateway_rub": 5000, "tgwallet_hold_hours": 48},
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
    "referral": {
        "enabled": True,
        "reward_pct": 1.5,                # от суммы покупки приглашённого → бонусный баланс пригласившего
        "max_share_of_margin_pct": 40,    # не больше этой доли нашей маржи по заказу
        "friend_discount_pct": 3,         # скидка на первый заказ приглашённого
        "friend_discount_max_rub": 100,
    },
}

SECRETS = [("telegram", "bot_token"), ("payments.card.yookassa", "secret_key"),
           ("payments.tgwallet", "api_key"), ("payments.card.crystalpay", "secret"), ("payments.card.crystalpay", "salt"),
           ("payments.card.aaio", "secret1"), ("payments.card.aaio", "api_key"), ("payments.card.freekassa", "api_key"),
           ("payments.crypto.cryptopay", "token"), ("suppliers.stars", "token"), ("suppliers.premium", "token"),
           ("suppliers.gifts", "token"), ("suppliers.steam_keys", "token"),
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
