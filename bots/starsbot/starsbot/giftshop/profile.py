"""Настройки профиля giftshop (бот TG Gift) поверх общих настроек StarsBot."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger("giftshop")

# Обычные подарки Telegram (не лимитированные). emoji — как в каталоге Telegram, stars — цена подарка.
# Каталог Telegram иногда меняется: бот сверяет лоты с живым каталогом аккаунта и не продаёт пропавшие подарки.
DEFAULT_SHOP_LOTS = [
    {"emoji": "🧸", "name": "Мишка", "name_en": "Teddy Bear", "stars": 15},
    {"emoji": "💝", "name": "Сердце", "name_en": "Heart", "stars": 15},
    {"emoji": "🎁", "name": "Коробка", "name_en": "Gift Box", "stars": 25, "match": r"коробк|gift\s*box"},
    {"emoji": "🌹", "name": "Роза", "name_en": "Rose", "stars": 25},
    {"emoji": "🎂", "name": "Торт", "name_en": "Cake", "stars": 50},
    {"emoji": "💐", "name": "Букет", "name_en": "Bouquet", "stars": 50},
    {"emoji": "🚀", "name": "Ракета", "name_en": "Rocket", "stars": 50},
    {"emoji": "🍾", "name": "Шампанское", "name_en": "Champagne", "stars": 50},
    {"emoji": "🏆", "name": "Кубок", "name_en": "Trophy", "stars": 100},
    {"emoji": "💍", "name": "Кольцо", "name_en": "Ring", "stars": 100},
    {"emoji": "💎", "name": "Алмаз", "name_en": "Diamond", "stars": 100},
]
for _l in DEFAULT_SHOP_LOTS:
    _l.update(offer_id=0, position=3, price=0, sell=True)

PROFILE_DEFAULTS: dict = {
    "general": {"profile": "giftshop"},
    "funpay": {
        "node_id": 3064,                 # «Telegram → Подарки» (funpay.com/lots/3064/)
        "lots": [],                      # лотов звёзд у TG Gift нет
        "order_match_regex": r"(?!x)x",  # заказы звёзд — не наши (их ведёт StarsBot)
        "foreign_subcategory_regex": r"(?i)зв[её]зды|\bstars\b",
        "auto_create_lots": False, "stock_sync": False,
    },
    "gifts": {
        "enabled": True, "lots": [], "pilot_orders": 0, "max_order_stars": 1000,
        "gift_message": "Заказ #{order_id}. Спасибо за покупку! 🎁",
        "low_stars_alert": 150,
        "topup": {"enabled": False},
    },
    "giftshop": {"lots": DEFAULT_SHOP_LOTS},
    "rtba": {"enabled": True, "strategy": "topN", "target_position": 3, "min_margin_pct": 5.0,
             "method_filter": "giftshop", "undercut_abs": 0.3, "min_change": 0.2, "competitor_min_amount": 1},
    "safety": {"pilot_orders": 0},
    "fragment": {"hide_lots_when_down": False, "session_warn_days": 0},
    "dashboard": {"port": 8805},
    "notifications": {"prefix": "🎁 TG Gift"},
    "supply": {"enabled": False},        # TG Gift звёзды не покупает — просит их у StarsBot
}


def validate_giftshop(cfg) -> list[str]:
    problems: list[str] = []
    if not cfg.funpay.golden_key:
        problems.append("funpay.golden_key не задан (возьмётся у StarsBot, если он настроен в Центре)")
    g = cfg.get("gifts") or {}
    if not int(g.get("api_id") or 0) or not str(g.get("api_hash") or ""):
        problems.append("gifts.api_id / gifts.api_hash: ключи приложения Telegram (my.telegram.org → API development "
                        "tools) для аккаунта, с которого отправляются подарки")
    if cfg.dashboard.enabled and len(str(cfg.dashboard.token)) < 12:
        problems.append("dashboard.token должен быть не короче 12 символов")
    return problems


def inherit_shared(cfg, home: Path) -> list[str]:
    """Пустые golden_key и Telegram-уведомления берём из настроек StarsBot (тот же аккаунт FunPay, тот же продавец).
    Команды Telegram у TG Gift при этом выключены: один токен опрашивать двум программам нельзя (ошибка 409)."""
    src = str((cfg.get("giftshop") or {}).get("share_from") or "")
    if not src:
        return []
    path = Path(src) if Path(src).is_absolute() else (Path(home) / src)
    try:
        other = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
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
    if taken:
        log.info("TG Gift: из настроек StarsBot взяты %s", ", ".join(taken))
    return taken
