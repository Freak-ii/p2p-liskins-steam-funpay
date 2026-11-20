"""Журнал событий центра (center.sqlite3): запуски, остановки, падения ботов, действия из панели.

Это основа для мониторинга центра: всё, что происходит с воркерами, попадает сюда с уровнем
info / warn / critical, а панель и будущие уведомления читают отсюда.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source TEXT NOT NULL,        -- center | lisbot | mannbot
    level TEXT NOT NULL,         -- info | warn | critical
    kind TEXT NOT NULL,          -- worker.start, worker.exit, worker.crashed, action, ...
    message TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class EventStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.listeners: list = []      # вызываются с dict события после записи (мониторинг и др.)

    def add(self, source: str, level: str, kind: str, message: str, data: dict | None = None) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO events (ts, source, level, kind, message, data) VALUES (?,?,?,?,?,?)",
                (time.time(), source, level, kind, message, json.dumps(data, ensure_ascii=False) if data else None))
            self.db.commit()
            eid = int(cur.lastrowid)
        ev = {"id": eid, "ts": time.time(), "source": source, "level": level, "kind": kind, "message": message,
              "data": data}
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 — подписчик не должен ломать запись журнала
                pass
        return eid

    def recent(self, limit: int = 200, source: str | None = None, *, important: bool = False, q: str = "") -> list[dict]:
        """Последние события; important — только предупреждения и ошибки, q — поиск по тексту и виду события."""
        where, args = [], []
        if source:
            where.append("source=?")
            args.append(source)
        if important:
            where.append("level IN ('warn','warning','error','critical')")
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        args.append(20000 if q else limit)       # LIKE в SQLite не различает регистр только для латиницы — ищем в Python
        with self._lock:
            rows = [dict(r) for r in self.db.execute(sql, args).fetchall()]
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in (r["message"] or "").lower() or ql in (r["kind"] or "").lower()][:limit]
        return rows

    def prune(self, days: float, now: float | None = None) -> int:
        """Удалить события старше days дней (и решения координатора того же возраста, кроме неразобранных
        предложений). Журнал не растёт бесконечно; копии старых событий остаются в резервных копиях."""
        cut = (time.time() if now is None else now) - days * 86400
        with self._lock:
            n = self.db.execute("DELETE FROM events WHERE ts < ?", (cut,)).rowcount
            has = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coord_decisions'").fetchone()
            if has:
                n += self.db.execute("DELETE FROM coord_decisions WHERE ts < ? AND status <> 'proposed'", (cut,)).rowcount
            self.db.commit()
        return n

    def close(self) -> None:
        with self._lock:
            self.db.close()
