"""Орбита: что сейчас снято с продажи — решение Пульта Орбиты в Центре, которое читает каждый бот.

Один и тот же файл лежит в центре и в каждом Python-боте (center/orbit_gate.py, starsbot/orbit_gate.py,
mannbot/orbit_gate.py, shopbot/orbit_gate.py — тест следит, чтобы копии не разошлись).

Центр пишет <папка данных центра>/shared/orbit.json и передаёт ботам путь в CENTER_ORBIT, а имя бота —
в CENTER_ORBIT_BOT. Бот перечитывает файл, когда он меняется (смотрит время изменения не чаще раза в
несколько секунд), поэтому закрытие позиции действует на ходу, без перезапуска ботов и центра.

Без файла (бот запущен отдельно от центра, старый центр) всё открыто — поведение как раньше.
Ключи у каждого бота свои (stars, premium, keys, buy.stars …): какие ключи закрыты, центр решает сам
по каталогу позиций (center/orbit.py), бот только спрашивает blocked("ключ").
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ENV_PATH = "CENTER_ORBIT"
ENV_BOT = "CENTER_ORBIT_BOT"


class OrbitGate:
    def __init__(self, bot: str = "", path: str | Path | None = None, *, ttl: float = 3.0, clock=time.time):
        self.bot = os.environ.get(ENV_BOT) or bot
        raw = path if path is not None else os.environ.get(ENV_PATH, "")
        self.path = Path(raw) if raw else None
        self.ttl = ttl
        self.clock = clock
        self._data: dict = {}
        self._mtime: float | None = None
        self._checked = 0.0

    # ------------------------------------------------------------------ чтение
    def refresh(self, force: bool = False) -> dict:
        now = self.clock()
        if self.path is None or (not force and now - self._checked < self.ttl):
            return self._data
        self._checked = now
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._data, self._mtime = {}, None       # файла нет — всё открыто
            return self._data
        if mtime == self._mtime:
            return self._data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._data, self._mtime = data, mtime
        except (OSError, ValueError):
            pass                                     # файл пишется прямо сейчас — оставляем прошлое решение
        return self._data

    @property
    def enabled(self) -> bool:
        return self.path is not None

    @property
    def version(self) -> int:
        return int(self.refresh().get("version") or 0)

    def closed(self) -> set[str]:
        return set((self.refresh().get("closed") or {}).get(self.bot) or [])

    def blocked(self, *keys: str) -> bool:
        """True, если хоть один из ключей снят с орбиты для этого бота."""
        c = self.closed()
        return any(k in c for k in keys if k)

    def reason(self, key: str) -> str:
        why = (self.refresh().get("reasons") or {}).get(self.bot) or {}
        return str(why.get(key) or "позиция снята с продажи в Пульте Орбиты")

    # ------------------------------------------------------------------ подтверждение
    def ack(self, applied: dict, version: int | None = None) -> None:
        """Бот сообщает центру, что сделал по последнему решению: {ключ: "ok" | "manual: …" | "error: …"}."""
        if self.path is None:
            return
        d = self.path.parent / "orbit-ack"
        try:
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / f"{self.bot}.json.tmp"
            tmp.write_text(json.dumps({"bot": self.bot, "version": self.version if version is None else version,
                                       "ts": self.clock(), "pid": os.getpid(), "applied": applied},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, d / f"{self.bot}.json")
        except OSError:
            pass


class OrbitTracker:
    """Помогает боту применять решения один раз: changes() отдаёт (закрылось, открылось) с прошлого вызова."""

    def __init__(self, gate: OrbitGate):
        self.gate = gate
        self.seen_version: int | None = None
        self.seen_closed: set[str] = set()

    def changes(self) -> tuple[set[str], set[str]] | None:
        v = self.gate.version
        if v == self.seen_version:
            return None
        now = self.gate.closed()
        closed, opened = now - self.seen_closed, self.seen_closed - now
        if self.seen_version is None:
            closed, opened = now, set()          # после запуска бота закрытое закрываем ещё раз — на всякий случай
        self.seen_version, self.seen_closed = v, now
        return closed, opened
