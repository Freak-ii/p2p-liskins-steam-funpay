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
        "golden_key": "", "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
        "poll_interval": 6, "lots": [], "order_match_regex": r"(?i)(mann\s*co|ключ|key)",
        "auto_raise": True, "raise_interval_minutes": 240, "stock_sync": True,
        "max_display_amount": 500, "deactivate_when_empty": True,
        "coord_db": "",                  # общий файл координации с StarsBot (обычно задаёт Центр)
    },
    "steam": {
        "username": "", "password": "", "shared_secret": "", "identity_secret": "", "steam_id": "",
        "login_secure_cookie": "", "api_key": "", "key_market_hash_name": "Mann Co. Supply Crate Key",
        "appid": 440, "contextid": "2",
        "offer_message": "FunPay заказ #{order_id} · {qty} key(s) · спасибо за покупку!",
        "offer_ttl_hours": 12, "min_seconds_between_offers": 8, "max_keys_per_offer": 100,
        "confirm_poll_seconds": 5,
    },
    "deal": {
        "max_link_attempts": 3, "reminder_after_minutes": 15, "admin_alert_after_minutes": 120,
        "max_resends": 2, "trade_hold_policy": "require_fix", "cancel_offer_on_refund": True,
        "auto_refund_out_of_stock": False, "message_rate_limit_seconds": 2,
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


def validate_config(cfg: Section) -> list[str]:
    """Возвращает список проблем конфигурации (пустой — всё ок)."""
    problems: list[str] = []
    if cfg.general.demo:
        return problems
    if not cfg.funpay.golden_key:
        problems.append("funpay.golden_key не задан")
    if not cfg.funpay.lots or not any(l.get("offer_id") for l in cfg.funpay.lots):
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
