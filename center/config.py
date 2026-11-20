"""Настройки центра: center.yaml в домашней папке центра.

Структура домашней папки (по умолчанию ./center-data рядом с репозиторием):
    center.yaml          — порт панели, токен входа, какие боты запускать
    center.sqlite3       — журнал событий центра (запуски, падения, перезапуски)
    logs/<бот>.log       — вывод каждого бота
    lisbot/              — данные LisBot: config.json, secrets.env, data/
    mannbot/             — домашняя папка MannBot: config.yaml, data/
    starsbot/            — домашняя папка StarsBot: config.yaml, data/
    shared/              — общий файл координации ботов одного аккаунта FunPay
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
        # autostart — запускать ли бота вместе с центром (меняется кнопкой в панели центра)
        "mannbot": {"enabled": True, "autostart": False, "port": 8801, "python": ""},
        "lisbot": {"enabled": True, "autostart": False, "port": 8802, "node": ""},
        # процесс StarsBot = его панель; торговля начнётся только после мастера настройки (кошелёк TON, Fragment)
        "starsbot": {"enabled": True, "autostart": True, "port": 8803, "python": ""},
    },
    "restart": {
        "backoff_start": 2,          # первая пауза перед перезапуском, с
        "backoff_max": 300,
        "fast_fail_seconds": 20,     # падение раньше этого срока считается «быстрым»
        "max_fast_fails": 5,         # после стольких быстрых падений подряд — не перезапускать
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
