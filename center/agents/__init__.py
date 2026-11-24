"""Агенты центра: отдельный раздел панели (/agents) для фоновых помощников.

Как подключить своего агента (пример — center/agents/channel.py):

    from center.agents import Agent, register

    @register
    class TrendsAgent(Agent):
        id = "trends"                       # ключ настроек agents.<id> в center.yaml и путь /api/agents/<id>
        title = "Тренды подарков и скинов"
        description = "Что сейчас растёт в цене"

        async def run_once(self) -> str:    # один проход; вернуть короткий итог для журнала
            self.add_item("finding", "Рост подарка X", "текст...", key="trend:x:2026-11-25")
            return "найдено 1"

        def status_extra(self) -> dict:     # необязательно: свои поля в карточке агента
            return {}

        def routes(self) -> list:           # необязательно: свои маршруты, принято под /api/agents/<id>/...
            return []

и импортировать модуль в AGENT_MODULES ниже. Общие вещи уже есть у базового класса:
- запуск и остановка кнопкой в панели; при старте центра агенты сами НЕ запускаются (правило FrayZ),
  кроме случая agents.<id>.autostart: true в center.yaml;
- цикл run_once раз в agents.<id>.interval_minutes и кнопка «Запустить проход сейчас»;
- общая таблица agent_items в center.sqlite3: черновики, находки, их статусы (self.add_item / self.items);
- журнал: self.log(...) пишет в события центра с источником agent:<id>.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from typing import ClassVar

log = logging.getLogger("center.agents")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,              -- у агента канала: news | season | promo | milestone | achievement | chatter | manual
    key TEXT,                        -- ключ от повторов: одна новость или один повод — один черновик
    status TEXT NOT NULL,            -- draft | approved | published | rejected | failed | info
    title TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    source_url TEXT DEFAULT '',
    issues TEXT DEFAULT '[]',        -- JSON: что нашёл фильтр конфиденциального
    meta TEXT DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    scheduled_at REAL,               -- одобрено «на время»: публикуется не раньше
    decided_at REAL,
    published_at REAL,
    message_id INTEGER,
    error TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_items_key ON agent_items(agent, key);
CREATE INDEX IF NOT EXISTS idx_agent_items_status ON agent_items(agent, status, created_at);
CREATE TABLE IF NOT EXISTS agent_kv (agent TEXT NOT NULL, k TEXT NOT NULL, v TEXT, PRIMARY KEY(agent, k));
"""

REGISTRY: dict[str, type["Agent"]] = {}
AGENT_MODULES = ("center.agents.channel",)      # модули с агентами; импорт регистрирует их


def register(cls: type["Agent"]) -> type["Agent"]:
    REGISTRY[cls.id] = cls
    return cls


class Store:
    """agent_items и agent_kv в общей базе центра. Потокобезопасно (как EventStore)."""

    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._lock = threading.Lock()

    def exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def close(self) -> None:
        with self._lock:
            self.db.close()


def _item(row: dict) -> dict:
    row = dict(row)
    for k, d in (("issues", []), ("meta", {})):
        try:
            row[k] = json.loads(row.get(k) or "null") or d
        except json.JSONDecodeError:
            row[k] = d
    return row


class Agent:
    id: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    default_interval_minutes: ClassVar[float] = 60

    def __init__(self, center, store: Store):
        self.center = center
        self.store = store
        self._task: asyncio.Task | None = None
        self._busy = asyncio.Lock()
        self.last_run: float | None = None
        self.last_result = ""
        self.last_error = ""

    # ------------------------------------------------------------ настройки и журнал
    @property
    def cfg(self) -> dict:
        try:
            return dict((self.center.cfg["agents"] or {}).get(self.id) or {})
        except KeyError:
            return {}

    def save_cfg(self, changes: dict) -> None:
        self.center.cfg.save({"agents": {self.id: changes}})

    def log(self, level: str, text: str) -> None:
        self.center.events.add(f"agent:{self.id}", level, f"agent.{self.id}", text)

    def kv_get(self, k: str, default=None):
        rows = self.store.q("SELECT v FROM agent_kv WHERE agent=? AND k=?", (self.id, k))
        return json.loads(rows[0]["v"]) if rows else default

    def kv_set(self, k: str, v) -> None:
        self.store.exec("INSERT INTO agent_kv(agent,k,v) VALUES(?,?,?) ON CONFLICT(agent,k) DO UPDATE SET v=excluded.v",
                        (self.id, k, json.dumps(v, ensure_ascii=False)))

    # ------------------------------------------------------------ элементы (черновики, находки)
    def add_item(self, kind: str, title: str, text: str, *, key: str | None = None, status: str = "draft",
                 source_url: str = "", issues: list | None = None, meta: dict | None = None) -> int | None:
        """Новый элемент; None — элемент с таким key уже был (повтор не создаётся)."""
        now = time.time()
        try:
            cur = self.store.exec(
                "INSERT INTO agent_items(agent,kind,key,status,title,text,source_url,issues,meta,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.id, kind, key, status, title[:200], text, source_url, json.dumps(issues or [], ensure_ascii=False),
                 json.dumps(meta or {}, ensure_ascii=False), now, now))
        except sqlite3.IntegrityError:
            return None
        return int(cur.lastrowid)

    def has_key(self, key: str) -> bool:
        return bool(self.store.q("SELECT 1 FROM agent_items WHERE agent=? AND key=?", (self.id, key)))

    def item(self, iid: int) -> dict | None:
        rows = self.store.q("SELECT * FROM agent_items WHERE agent=? AND id=?", (self.id, iid))
        return _item(rows[0]) if rows else None

    def items(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status:
            rows = self.store.q("SELECT * FROM agent_items WHERE agent=? AND status=? ORDER BY id DESC LIMIT ?",
                                (self.id, status, limit))
        else:
            rows = self.store.q("SELECT * FROM agent_items WHERE agent=? ORDER BY id DESC LIMIT ?", (self.id, limit))
        return [_item(r) for r in rows]

    def update_item(self, iid: int, **fields) -> None:
        if not fields:
            return
        for k in ("issues", "meta"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.store.exec(f"UPDATE agent_items SET {cols} WHERE agent=? AND id=?", (*fields.values(), self.id, iid))

    def counts(self) -> dict:
        rows = self.store.q("SELECT status, COUNT(*) n FROM agent_items WHERE agent=? GROUP BY status", (self.id,))
        return {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------ жизненный цикл
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def interval(self) -> float:
        try:
            return max(1.0, float(self.cfg.get("interval_minutes") or self.default_interval_minutes)) * 60
        except (TypeError, ValueError):
            return self.default_interval_minutes * 60

    async def start(self, why: str = "панель центра") -> str:
        if self.running:
            return "уже работает"
        if not self.cfg.get("enabled", True):
            return "выключен в center.yaml (agents.%s.enabled)" % self.id
        self._task = asyncio.create_task(self._loop(), name=f"agent-{self.id}")
        self.log("info", f"{self.title}: запущен ({why})")
        return "запущен"

    async def stop(self, why: str = "панель центра") -> str:
        if not self.running:
            return "уже остановлен"
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None
        self.log("info", f"{self.title}: остановлен ({why})")
        return "остановлен"

    async def _loop(self) -> None:
        while True:
            await self.run_now()
            await asyncio.sleep(self.interval)

    async def run_now(self) -> str:
        if self._busy.locked():
            return "проход уже идёт"
        async with self._busy:
            self.last_run = time.time()
            try:
                self.last_result = await self.run_once()
                self.last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — агент не должен ронять центр
                log.exception("agent %s failed", self.id)
                self.last_error = f"{type(e).__name__}: {e}"
                self.last_result = ""
                self.log("warn", f"{self.title}: проход не удался — {self.last_error}")
            return self.last_result or self.last_error

    async def run_once(self) -> str:  # pragma: no cover - переопределяют наследники
        raise NotImplementedError

    def status_extra(self) -> dict:
        return {}

    def routes(self) -> list:
        return []

    def status(self) -> dict:
        return {"id": self.id, "title": self.title, "description": self.description,
                "enabled": bool(self.cfg.get("enabled", True)), "running": self.running,
                "interval_minutes": self.interval / 60, "last_run": self.last_run, "last_result": self.last_result,
                "last_error": self.last_error, "counts": self.counts(), **self.status_extra()}


class Agents:
    """Все агенты центра. Создаётся в Center.__init__, маршруты — center/agents/web.py."""

    def __init__(self, center):
        import importlib
        for m in AGENT_MODULES:
            importlib.import_module(m)
        self.center = center
        self.store = Store(center.home / "center.sqlite3")
        self.agents: dict[str, Agent] = {aid: cls(center, self.store) for aid, cls in REGISTRY.items()}

    def get(self, aid: str) -> Agent | None:
        return self.agents.get(aid)

    async def start(self) -> None:
        """При старте центра агенты сами не запускаются; только тот, кому явно включили autostart."""
        for a in self.agents.values():
            if a.cfg.get("autostart") is True and a.cfg.get("enabled", True):
                await a.start("автозапуск агента (agents.%s.autostart)" % a.id)

    async def stop(self) -> None:
        for a in self.agents.values():
            if a.running:
                await a.stop("завершение центра")
        self.store.close()

    def overview(self) -> list[dict]:
        return [a.status() for a in self.agents.values()]
