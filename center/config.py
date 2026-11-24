"""Настройки центра: center.yaml в домашней папке центра.

Структура домашней папки (по умолчанию ./center-data рядом с репозиторием):
    center.yaml          — порт панели, токен входа, какие боты запускать
    center.sqlite3       — журнал событий центра (запуски, падения, перезапуски)
    logs/<бот>.log       — вывод каждого бота
    lisbot/              — данные LisBot: config.json, secrets.env, data/
    mannbot/             — домашняя папка MannBot: config.yaml, data/
    starsbot/            — домашняя папка StarsBot: config.yaml, data/
    tggift/              — домашняя папка TG Gift (StarsBot в профиле giftshop): config.yaml, data/
    shared/              — общий файл координации ботов одного аккаунта FunPay
"""
from __future__ import annotations

import copy
import io
import os
import secrets
import time
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent          # корень репозитория
BOTS_DIR = ROOT / "bots"

DEFAULTS: dict = {
    "center": {
        "host": "127.0.0.1",
        "port": 8800,
        "token": "",
        "poll_seconds": 10,          # как часто опрашивать ботов для сводки
        "update_check": True,        # раз в сутки смотреть, не вышла ли новая версия центра на GitHub
    },
    "workers": {
        # autostart — запускать ли бота вместе с центром (меняется кнопкой в панели центра)
        "mannbot": {"enabled": True, "autostart": False, "port": 8801, "python": ""},
        "lisbot": {"enabled": True, "autostart": False, "port": 8802, "node": ""},
        # процесс StarsBot = его панель; торговля начнётся только после мастера настройки (кошелёк TON, Fragment)
        "starsbot": {"enabled": True, "autostart": True, "port": 8803, "python": ""},
        # Telegram-магазин: пока выключен до настройки (токен бота, оплата, токены поставщиков)
        "shopbot": {"enabled": True, "autostart": False, "port": 8806, "python": ""},
        # TG Gift (подарки Telegram на FunPay): выключен до настройки аккаунта подарков (api_id/api_hash, вход)
        "tggift": {"enabled": True, "autostart": False, "port": 8805, "python": ""},
    },
    "restart": {
        "backoff_start": 2,          # первая пауза перед перезапуском, с
        "backoff_max": 300,
        "fast_fail_seconds": 20,     # падение раньше этого срока считается «быстрым»
        "max_fast_fails": 5,         # после стольких быстрых падений подряд — не перезапускать
    },
    "analytics": {
        "usd_rub": "auto",           # число — фиксированный курс; auto — курс LisBot (Steam в ₽) или ЦБ РФ
        "fallback_usd_rub": 95,      # если курс узнать неоткуда (нет сети)
        "month_goal_rub": 0,         # цель прибыли центра на месяц, ₽; 0 — не задана
    },
    "budget": {
        "total_usd": 0,              # общий капитал центра, $; 0 — не задан. LisBot получает его минус склад MannBot
        "plan": {},                  # желаемые лимиты ботов (ключи center/rtba.py LIMITS); применяются кнопкой в панели
    },
    "monitor": {                     # center/monitor.py
        "sample_seconds": 60,        # как часто проверять, отвечают ли боты (история и «бот не отвечает»)
        "unresponsive_after": 3,     # столько неудачных проверок подряд = «бот не отвечает»
        "restart_unresponsive_after": 10,   # столько подряд — бот завис, центр перезапускает его (не чаще раза в час); 0 — нет
        "history_days": 30,
        "events_days": 180,          # журнал событий и решений координатора хранится столько дней; 0 — всегда
        "disk": {"warn_gb": 2},
        "errors": {"per_hour": 20},  # столько строк с ошибками в выводе бота за час — уведомление (раз в 6 ч); 0 — нет      # свободного места меньше — уведомление (меньше половины — критическое); 0 — не следить
        "telegram": {
            "enabled": True,
            "token": "",             # пусто — берутся токен и chat id из подключений LisBot
            "chat_id": "",           # можно несколько через запятую
            "commands": True,        # команды центра в Telegram (только со своим ботом центра, center/tgcontrol.py)
        },
        "dedup_minutes": 15,         # одинаковое уведомление не чаще раза за столько минут
        "quiet_hours": "",           # например "23:00-08:00": некритичное копится и приходит одним сообщением
        "quiet_allow_critical": True,
        "bot_alert_levels": ["critical"],   # какие тревоги ботов пересылать (у ботов бывают и свои уведомления)
        "heartbeat": {
            "url": "",               # healthchecks.io и подобные: центр пингует, сервис пишет, если пинги пропали
            "seconds": 60,
            "fail_when_bot_down": False,    # слать <url>/fail, пока включённый бот не работает
        },
        "daily_report": {"enabled": True, "time": "09:00"},
        "weekly_report": {"enabled": True, "weekday": 0, "time": "09:00"},   # 0 — понедельник
        "monthly_report": {"enabled": True, "time": "09:00"},   # 1-го числа: итоги прошлого месяца
    },
    "crm": {                         # сегменты покупателей FunPay, center/crm.py
        "sleep_days": 30,            # нет покупок дольше — «уснул» (стоит написать)
        "lost_days": 90,             # дольше — «ушёл»
        "vip_spent_rub": 5000,       # VIP: потратил столько ₽ или сделал vip_orders покупок
        "vip_orders": 5,
        # текст, который предлагается отправить уснувшему покупателю в чат FunPay; {name} — ник покупателя
        "winback_text": "Здравствуйте, {name}! Давно вас не было. Ключи Mann Co. Supply Crate Key снова в наличии "
                        "и выдаются автоматически за пару минут после оплаты. Будем рады новому заказу 🙂",
    },
    "steam": {                       # center/steamshare.py
        "shared_account": True,      # LisBot и MannBot на одном аккаунте Steam: MannBot записывает свои обмены в центр,
                                     # LisBot не считает их взломом. False — у ботов разные аккаунты (старый режим)
    },
    "inventory": {                   # center/inventory.py
        "lis_old_days": 0,           # предмет LisBot «долго лежит» после стольких дней; 0 — как sell.maxHoldDaysAlert LisBot
        "mann_days_warn": 3,         # ключей MannBot меньше чем на столько дней продаж — уведомление
        "mann_rate_days": 14,        # скорость продаж ключей — за столько последних дней
        "mann_target_days": 14,      # «докупить N ключей»: чтобы склада хватило на столько дней продаж
        "lis_balance_warn_usd": 10,  # баланс LIS-SKINS ниже этого в боевом режиме — уведомление; 0 — не следить
        "check_seconds": 900,
    },
    "backup": {                      # center/backup.py
        "enabled": True,
        "time": "04:00",             # ежедневная копия в это время (или при первом запуске после него)
        "keep": 14,                  # сколько копий каждого вида хранить
        "dir": "",                   # пусто — center-data/backups; можно папку Яндекс Диска / Google Drive
    },
    "coordinator": {                 # center/coordinator.py
        "enabled": True,
        "check_seconds": 60,         # как часто сторож аномалий смотрит на ботов
        "proposal_ttl_hours": 24,    # неодобренное предложение после этого срока устаревает
        "ai": {
            "enabled": True,
            "api_key": "",           # ключ Claude API; пусто — брифинги без ИИ (по правилам)
            "model": "claude-opus-5-5",
            "effort": "medium",
        },
        "briefing": {"enabled": True, "time": "09:05", "telegram": True},
        "permissions": {},           # инструмент: auto | propose | confirm | manual | off (см. coordinator.TOOLS)
        "anomaly": {
            "spend_hour_factor": 3,  # траты LisBot за час больше среднего часа за неделю во столько раз…
            "spend_hour_min_usd": 30,     # …и не меньше этой суммы — пауза закупок
            "loss_streak": 4,        # столько продаж LisBot подряд в минус…
            "loss_streak_min_usd": 5,     # …на общую сумму не меньше — пауза закупок
            "refunds_24h": 3,        # столько возвратов MannBot за сутки — предложить паузу
            "item_loss_sales": 3,    # предмет LisBot продан столько раз за item_loss_days…
            "item_loss_days": 30,
            "item_loss_min_usd": 3,  # …с общим убытком не меньше — предложить выключить его закупку
            "mann_stuck_hours": 2,   # оплаченный заказ MannBot без выдачи дольше — сообщить
            "mann_approval_hours": 1,     # заказ ждёт вашей проверки дольше — сообщить
            "lis_stall_hours": 24,   # LisBot в боевом режиме не покупал дольше (а обычно 1+ в день) — сообщить; 0 — нет
            "lis_drop_pct": 15,      # купленный предмет LisBot подешевел по рынку больше чем на столько % — сообщить; 0 — нет
            "cooldown_hours": 6,     # одна и та же аномалия не чаще
        },
    },
    "agents": {                      # раздел «Агенты» (center/agents/): при старте центра агенты сами не запускаются
        "channel": {                 # center/agents/channel.py — посты для Telegram-канала магазина через одобрение
            "enabled": True,
            "autostart": False,      # true — запускать вместе с центром (по умолчанию только кнопкой)
            "interval_minutes": 60,  # как часто делать проход, пока агент запущен
            "publish_enabled": False,     # без этого одобренные посты в канал не уходят
            "chat_id": "",           # пусто — channel.chat_id из настроек ShopBot (@DPBFastchange)
            "bot_token": "",         # пусто — токен бота ShopBot (бот должен быть админом канала)
            "bot_username": "",      # пусто — из ShopBot; нужен для кнопки «Открыть магазин»
            "shop_name": "",         # пусто — general.shop_name из ShopBot
            "button": True,          # кнопка «Открыть магазин» под постом
            "ai": True,              # тексты пишет Claude (ключ координатора); без ключа — шаблоны
            "sources": [],           # публичные каналы для новостей: @name или t.me/name
            "keywords": ["звёзд", "звезд", "stars", "premium", "премиум", "подар", "gift", "nft", "steam", "telegram",
                         "телеграм", "обновлен"],
            "news_max_age_hours": 24,
            "news_per_run": 2,
            "news_per_day": 4,
            "season_lead_days": 3,   # за сколько дней до праздника готовить черновик (у некоторых поводов своё)
            "seasons_off": [],       # id поводов, которые не нужны (см. center/agents/seasons.py)
            "promo_lead_days": 1,    # анонс акции за столько дней до начала
            "promos": [],            # [{title, text, code, discount_pct, start: ГГГГ-ММ-ДД, end: ГГГГ-ММ-ДД}]
            "achievements": [],      # новости проекта своими словами: «добавили оплату по СБП» — по посту на пункт
            "share_milestones": True,     # пост на круглое число выполненных заказов (без денег)
            "milestones": [100, 500, 1000, 2500, 5000, 10000],
            "chatter_every_days": 3, # обычный пост (советы, безопасность, вопросы), если других постов не было; 0 — нет
            "allow_prices": False,   # суммы и цены в постах агента (по умолчанию нельзя)
            "forbidden_words": [],   # свои запрещённые слова в дополнение к встроенным
            "notify_drafts": True,   # сообщать в Telegram центра о новых черновиках
        },
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


FREE_FORM = {("budget", "plan"), ("coordinator", "permissions"), ("workers",)}


def check_config(user: dict, base: dict = DEFAULTS, path: tuple = ()) -> list[str]:
    """Опечатки в center.yaml: неизвестные ключи (центр их молча не видит) и значения не того типа."""
    out = []
    for k, v in (user or {}).items():
        here = (*path, k)
        if k not in base:
            out.append(f"center.yaml: неизвестная настройка {'.'.join(map(str, here))} — опечатка? Центр её не использует")
            continue
        d = base[k]
        if isinstance(d, dict):
            if not isinstance(v, dict):
                out.append(f"center.yaml: {'.'.join(here)} должно быть разделом настроек, а не значением")
            elif d and here not in FREE_FORM and here[:1] not in FREE_FORM:
                out += check_config(v, d, here)
        elif isinstance(d, bool):
            if not isinstance(v, bool):
                out.append(f"center.yaml: {'.'.join(here)} должно быть true или false, а не «{v}»")
        elif isinstance(d, (int, float)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                out.append(f"center.yaml: {'.'.join(here)} должно быть числом, а не «{v}»")
    return out


def default_home() -> Path:
    if os.environ.get("CENTER_HOME"):
        return Path(os.environ["CENTER_HOME"]).expanduser().resolve()
    return ROOT / "center-data"


# Демо живёт рядом с боевым центром на том же компьютере: свои порты (боевые 8800–8803 могут быть заняты
# работающими ботами) и автозапуск всех ботов — демо нужно, чтобы посмотреть всё сразу.
DEMO_DEFAULTS: dict = {
    "center": {"port": 18800, "update_check": False},
    "workers": {
        "mannbot": {"autostart": True, "port": 18801},
        "lisbot": {"autostart": True, "port": 18802},
        "starsbot": {"autostart": True, "port": 18803},
        "shopbot": {"autostart": True, "port": 18806},
        "tggift": {"autostart": True, "port": 18805},
    },
}


class CenterConfig:
    def __init__(self, home: Path, *, demo: bool = False):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "center.yaml"
        self.problems: list[tuple[str, str]] = []   # (уровень, текст) — центр запишет их в журнал при запуске
        self.defaults = _merge(DEFAULTS, DEMO_DEFAULTS) if demo else DEFAULTS
        self.user: dict = self._load()
        self.data: dict = _merge(self.defaults, self.user)
        self.problems += [("warn", p) for p in check_config(self.user)]

    def _load(self) -> dict:
        """center.yaml с ошибкой не должен мешать запуску: испорченный файл откладывается в сторону, настройки
        берутся из последней резервной копии (или по умолчанию), а в журнал пишется, что случилось."""
        if not self.path.exists():
            return {}
        try:
            user = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            if not isinstance(user, dict):
                raise yaml.YAMLError("в файле должен быть словарь настроек")
            return user
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            err = str(e).splitlines()[0] if str(e) else type(e).__name__
            mark = getattr(e, "problem_mark", None)
            where = f" (строка {mark.line + 1})" if mark is not None else ""
        broken = self.path.with_name(f"center.yaml.broken-{time.strftime('%Y%m%d-%H%M%S')}")
        os.replace(self.path, broken)
        user, src = {}, ""
        for z in sorted((self.home / "backups").glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                with zipfile.ZipFile(z) as zf:
                    cand = yaml.safe_load(io.TextIOWrapper(zf.open("center.yaml"), encoding="utf-8")) or {}
                if isinstance(cand, dict):
                    user, src = cand, z.name
                    break
            except (KeyError, OSError, zipfile.BadZipFile, yaml.YAMLError, UnicodeDecodeError):
                continue
        if src:
            self.path.write_text(yaml.safe_dump(user, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.problems.append(("critical", f"center.yaml с ошибкой{where}: {err}. Файл сохранён как {broken.name}; "
                              + (f"настройки восстановлены из резервной копии {src}" if src else
                                 "центр запущен с настройками по умолчанию") + ". Исправьте ошибку и верните файл."))
        return user

    def __getitem__(self, key: str):
        return self.data[key]

    def worker(self, name: str) -> dict:
        return self.data["workers"].get(name, {})

    def save(self, updates: dict) -> None:
        self.user = _merge(self.user, updates)
        self.data = _merge(self.defaults, self.user)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(self.user, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def ensure_token(self) -> str:
        token = str(self.data["center"].get("token") or "")
        if len(token) < 16:
            token = secrets.token_urlsafe(24)
            self.save({"center": {"token": token}})
        return token
