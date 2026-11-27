"""Загрузка конфигурации: YAML + подстановка ${ENV} + значения по умолчанию."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "general": {
        "dry_run": True, "demo": False, "language": "ru",
        "database": "data/mannbot.sqlite3", "log_file": "data/mannbot.log", "log_level": "INFO",
        "autostart": False,
    },
    "messages": {"ru": {}, "en": {}},
    "funpay": {
        # false — режим «Только поставщик»: MannBot снабжает ключами Telegram-магазин (ShopBot), а на FunPay не
        # заходит (свой лот выключается один раз при запуске; golden_key не нужен)
        "enabled": True,
        "golden_key": "", "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
        "poll_interval": 6, "lots": [], "order_match_regex": r"(?i)(mann\s*co|ключ|key)",
        "auto_raise": True, "raise_interval_minutes": 240, "stock_sync": True,
        "max_display_amount": 500, "deactivate_when_empty": True,
        "coord_db": "",                  # общий файл координации с StarsBot (обычно задаёт Центр)
        "presale_replies": True,         # вопрос о ключах без заказа — один раз ответить шаблоном
        # golden_key и Telegram-уведомления берутся у StarsBot (тот же аккаунт FunPay), если свои пусты
        "share_from": "../starsbot/config.yaml",
        # лот без offer_id: найти свой лот с ключами в разделе или выставить новый (выключенным в тестовом режиме)
        "auto_create_lot": True,
        "lot_title_ru": "🔑 Ключ от ящика Манн Ко | Mann Co. Supply Crate Key | Автовыдача 24/7 🤖",
        "lot_title_en": "🔑 Mann Co. Supply Crate Key | Instant auto delivery 24/7 🤖",
        "lot_desc_ru": ("Ключ от ящика Манн Ко (Mann Co. Supply Crate Key), Team Fortress 2.\n"
                        "Выдача автоматическая через обмен Steam, обычно 1–3 минуты, круглосуточно.\n\n"
                        "Как получить: оплатите нужное количество и пришлите в чат ссылку на обмен Steam "
                        "(Steam → Инвентарь → Предложения обмена → «Кто может отправлять мне предложения обмена?»).\n"
                        "Нужен мобильный Steam Guard не менее 7 дней, иначе Steam задержит обмен.\n"
                        "Мы никогда не просим ваши предметы, логин, коды или API-ключ. Обмен приходит "
                        "с комментарием «FunPay заказ #номер»."),
        # FunPay не принимает короткое английское описание: текст должен быть таким же полным, как русский
        "lot_desc_en": ("Mann Co. Supply Crate Key, Team Fortress 2.\n"
                        "Delivery is automatic via a Steam trade offer, usually within 1–3 minutes, 24/7.\n\n"
                        "How to receive: pay for the number of keys you need and send your Steam trade URL to the "
                        "chat (Steam → Inventory → Trade Offers → \"Who can send me Trade Offers?\").\n"
                        "Your account must have the Steam Guard Mobile Authenticator for at least 7 days, otherwise "
                        "Steam will hold the trade.\n"
                        "We never ask for your items, login, codes or API key. Accept only a trade offer in which "
                        "you give nothing and receive the keys."),
        "lot_start_price": 0,            # цена нового лота, ₽ (0 — пол RTBA или 150)
    },
    "steam": {
        "username": "", "password": "", "shared_secret": "", "identity_secret": "", "steam_id": "",
        "login_secure_cookie": "", "api_key": "", "key_market_hash_name": "Mann Co. Supply Crate Key",
        "appid": 440, "contextid": "2",
        # комментарий к трейду; пусто — без комментария (свой трейд после сбоя бот найдёт по получателю и ключам)
        "offer_message": "",
        "offer_ttl_hours": 12, "min_seconds_between_offers": 8, "max_keys_per_offer": 100,
        "confirm_poll_seconds": 5,
    },
    "deal": {
        "max_link_attempts": 3, "reminder_after_minutes": 15, "admin_alert_after_minutes": 120,
        "max_resends": 2, "trade_hold_policy": "require_fix", "cancel_offer_on_refund": True,
        "auto_refund_out_of_stock": False, "message_rate_limit_seconds": 2,
        # проблема с выдачей (Steam не входит, трейд не создаётся/не подтверждается, сбой выдачи) → лоты скрываются,
        # пока проблема не решится (Steam сам вернулся) или продавец не скажет /lots_on
        "hold_lots_on_problems": True,
    },
    "risk": {
        "enabled": True, "manual_review_score": 60, "block_score": 90, "large_order_keys": 20,
        "max_orders_per_buyer_per_day": 5, "blacklist_funpay_users": [], "blacklist_steam_ids": [],
    },
    "rtba": {
        "enabled": False, "interval_seconds": 300, "strategy": "top1", "target_position": 1,
        "undercut_abs": 0.5, "undercut_pct": 0.0, "cost_per_key": 0.0, "min_margin_pct": 5.0,
        "floor_price": 0.0, "ceiling_price": 0.0, "funpay_fee_pct": 0.0, "min_change": 0.3,
        "max_updates_per_hour": 6, "ignore_competitors": [], "competitor_min_amount": 1,
        "competitor_only_online": False, "outlier_iqr_factor": 1.5,
        "lot_filter_regex": r"(?i)(mann\s*co.*key|ключ)", "scarcity_stock_threshold": 5,
        "scarcity_premium_pct": 5.0, "raise_gap_pct": 3.0,
    },
    "security": {"cancel_foreign_offers": False, "decline_incoming_requests": True, "inventory_drop_alert": True,
                 "accept_gifts_from": []},
    "notifications": {
        "telegram": {"enabled": False, "bot_token": "", "admin_chat_ids": [], "commands": True},
        "min_level": "INFO", "throttle_seconds": 300, "daily_report_hour": 10,
    },
    "dashboard": {"enabled": True, "host": "127.0.0.1", "port": 8080, "token": ""},
    # Поставка ключей Telegram-магазину ShopBot (/api/supply/*): токен ≥16 символов, такой же, как
    # suppliers.steam_keys.token у ShopBot; пусто — API выключен
    "supply": {"token": ""},
    "sla": {"link_wait_minutes": 120, "offer_accept_hours": 12, "order_total_hours": 24},
    "ai": {
        "enabled": False, "backend": "claude", "api_key": "", "model": "claude-opus-5-5", "effort": "low",
        "mode": "suggest", "auto_topics": ["order_status", "trade_link_help", "delivery_help"],
        "min_confidence": 0.8, "seller_tone": "Вежливо, коротко, на «вы», можно один эмодзи",
        "max_reply_chars": 700, "history_messages": 12, "max_calls_per_chat_hour": 6,
        "draft_ttl_minutes": 60, "timeout_seconds": 40, "server_fallback": True,
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


def inherit_shared(cfg: Section, home: str | Path) -> list[str]:
    """Пустые golden_key и Telegram-уведомления берём у StarsBot: тот же аккаунт FunPay и тот же продавец.
    Команды Telegram при этом выключены: один токен опрашивать двум программам нельзя (ошибка 409)."""
    src = str(cfg.funpay.get("share_from") or "")
    if not src or cfg.general.demo:
        return []
    path = Path(src) if Path(src).is_absolute() else (Path(home) / src)
    try:
        other = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    other = _expand_env(other)
    taken = []
    fp = other.get("funpay") or {}
    if not cfg.funpay.golden_key and fp.get("golden_key"):
        cfg.funpay.golden_key = str(fp["golden_key"])
        if fp.get("user_agent"):
            cfg.funpay.user_agent = str(fp["user_agent"])
        taken.append("golden_key")
    tg_other = ((other.get("notifications") or {}).get("telegram") or {})
    tg = cfg.notifications.telegram
    if not tg.get("bot_token") and tg_other.get("bot_token"):
        tg.bot_token = str(tg_other["bot_token"])
        tg.admin_chat_ids = list(tg_other.get("admin_chat_ids") or [])
        tg.enabled = bool(tg_other.get("enabled", True))
        tg.commands = False
        taken.append("уведомления Telegram")
    return taken


def funpay_enabled(cfg) -> bool:
    """Продаёт ли MannBot на FunPay. false — режим «Только поставщик» (mannbot/funpay/off.py)."""
    return bool((cfg.get("funpay") or {}).get("enabled", True))


def validate_config(cfg: Section) -> list[str]:
    """Возвращает список проблем конфигурации (пустой — всё ок)."""
    problems: list[str] = []
    if cfg.general.demo:
        return problems
    fp_on = funpay_enabled(cfg)
    if fp_on and not cfg.funpay.golden_key:
        problems.append("funpay.golden_key не задан (или выключите FunPay: режим «Только поставщик»)")
    if fp_on and not cfg.funpay.get("auto_create_lot") and \
            (not cfg.funpay.lots or not any(l.get("offer_id") for l in cfg.funpay.lots)):
        problems.append("funpay.lots: укажите хотя бы один offer_id лота с ключами")
    st = cfg.steam
    if not st.login_secure_cookie and not (st.username and st.password and st.shared_secret):
        problems.append("steam: нужен login_secure_cookie или username+password+shared_secret")
    if not st.identity_secret:
        problems.append("steam.identity_secret не задан — трейды нельзя подтвердить автоматически")
    if cfg.dashboard.enabled and len(str(cfg.dashboard.token)) < 12:
        problems.append("dashboard.token должен быть не короче 12 символов")
    if cfg.rtba.enabled and cfg.rtba.cost_per_key <= 0 and cfg.rtba.floor_price <= 0:
        problems.append("rtba: задайте cost_per_key или floor_price, иначе нет нижней границы цены")
    return problems
