"""Настройки центра: center.yaml в домашней папке центра.

Структура домашней папки (по умолчанию ./center-data рядом с репозиторием):
    center.yaml          — порт панели, токен входа, какие боты запускать
    center.sqlite3       — журнал событий центра (запуски, падения, перезапуски)
    logs/<бот>.log       — вывод каждого бота
    lisbot/              — данные LisBot: config.json, secrets.env, data/
    mannbot/             — домашняя папка MannBot: config.yaml, data/
"""
from __future__ import annotations

import copy
import os
import secrets
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
    },
    "workers": {
        "mannbot": {"enabled": True, "autostart": True, "port": 8801, "python": ""},
        "lisbot": {"enabled": True, "autostart": True, "port": 8802, "node": ""},
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
    },
    "monitor": {                     # center/monitor.py
        "sample_seconds": 60,        # как часто проверять, отвечают ли боты (история и «бот не отвечает»)
        "unresponsive_after": 3,     # столько неудачных проверок подряд = «бот не отвечает»
        "history_days": 30,
        "telegram": {
            "enabled": True,
            "token": "",             # пусто — берутся токен и chat id из подключений LisBot
            "chat_id": "",           # можно несколько через запятую
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
            "cooldown_hours": 6,     # одна и та же аномалия не чаще
        },
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def default_home() -> Path:
    if os.environ.get("CENTER_HOME"):
        return Path(os.environ["CENTER_HOME"]).expanduser().resolve()
    return ROOT / "center-data"


class CenterConfig:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "center.yaml"
        user = yaml.safe_load(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.user: dict = user or {}
        self.data: dict = _merge(DEFAULTS, self.user)

    def __getitem__(self, key: str):
        return self.data[key]

    def worker(self, name: str) -> dict:
        return self.data["workers"].get(name, {})

    def save(self, updates: dict) -> None:
        self.user = _merge(self.user, updates)
        self.data = _merge(DEFAULTS, self.user)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(self.user, allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def ensure_token(self) -> str:
        token = str(self.data["center"].get("token") or "")
        if len(token) < 16:
            token = secrets.token_urlsafe(24)
            self.save({"center": {"token": token}})
        return token
