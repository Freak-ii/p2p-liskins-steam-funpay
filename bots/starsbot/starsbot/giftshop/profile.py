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
    _l.update(kind="gift", offer_id=0, position=3, price=0, sell=True)

# «Звёзды → Подарком» (funpay.com/lots/2418/): номинал = звёзд у покупателя после конвертации подарков.
# Все номиналы раздела — от 13 до 2500 (13 = 🧸15, 21 = 🌹25, 43 = 🎂50, 85 = 🏆100).
TOPUP_DENOMINATIONS = (13, 21, 43, 50, 75, 85, 100, 150, 200, 250, 300, 350, 500, 1000, 2500)
DEFAULT_TOPUP_LOTS = [{"stars": n, "offer_id": 0, "position": 3, "price": 0, "sell": True}
                      for n in TOPUP_DENOMINATIONS]

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
    "giftshop": {
        "lots": DEFAULT_SHOP_LOTS,
        "topup_lots": DEFAULT_TOPUP_LOTS,
        # раздел «Звёзды → Подарком» (раньше вёл StarsBot): тот же аккаунт подарков, звёзды от StarsBot
        "topup": {"enabled": True, "node_id": 2418, "method_filter": "подарк"},
        # коллекционные (NFT) подарки: покупка на Fragment (кошелёк StarsBot) или на рынке Telegram — что дешевле
        "nft": {"enabled": True},
        # подбор NFT-лотов раздела «Подарки» по ликвидности, марже и сезону (ведёт RTBA)
        "assortment": {"enabled": True},
    },
    "rtba": {"enabled": True, "strategy": "topN", "target_position": 3, "min_margin_pct": 5.0,
             "method_filter": "giftshop", "undercut_abs": 0.3, "min_change": 0.2, "competitor_min_amount": 1},
    "safety": {"pilot_orders": 0},
    "fragment": {"hide_lots_when_down": False, "session_warn_days": 0, "keepalive_minutes": 0},
    "dashboard": {"port": 8805},
    "notifications": {"prefix": "🎁 TG Gift"},
    "supply": {"enabled": False},        # TG Gift звёзды не покупает — просит их у StarsBot
}


def validate_giftshop(cfg) -> list[str]:
    problems: list[str] = []
    from starsbot.config import funpay_enabled
    if funpay_enabled(cfg) and not cfg.funpay.golden_key:
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
    gl = cfg.get("giftshop")
    old = [l for l in ((other.get("gifts") or {}).get("lots") or []) if l.get("offer_id")]
    if gl is not None and old and not any(l.get("offer_id") for l in (gl.get("topup_lots") or [])):
        # лоты «Подарком», которые раньше вёл StarsBot, — берём себе (те же offer_id, второй лот не выставляем)
        by_stars = {int(l["stars"]): l for l in old if l.get("stars")}
        lots = [dict(l) for l in (gl.get("topup_lots") or DEFAULT_TOPUP_LOTS)]
        for l in lots:
            src_lot = by_stars.pop(int(l["stars"]), None)
            if src_lot:
                l["offer_id"] = int(src_lot["offer_id"])
        for n, src_lot in sorted(by_stars.items()):
            lots.append({"stars": n, "offer_id": int(src_lot["offer_id"]), "position": 3, "price": 0, "sell": True})
        gl["topup_lots"] = lots
        taken.append("лоты «Подарком»")
    if taken:
        log.info("TG Gift: из настроек StarsBot взяты %s", ", ".join(taken))
    return taken
