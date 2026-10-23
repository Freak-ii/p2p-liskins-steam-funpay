"""Координация нескольких ботов на одном аккаунте FunPay (MannBot, StarsBot, …).

Общий SQLite-файл, путь которого всем ботам передаёт Центр (переменная FUNPAY_COORD_DB):

* «захват» чата: бот, у которого в чате активный заказ, отмечает чат своим; остальные боты в этот
  чат не пишут (иначе, например, MannBot отвечал бы «не делимся контактами» на @username для звёзд);
* «захват» заказа: заказ обрабатывает только один бот, даже если описание подошло обоим;
* общий ограничитель частоты запросов к FunPay: суммарно все боты не чаще min_interval.

Файл модуля одинаковый у всех ботов (копия). Без пути координация выключена и бот работает как раньше.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_claims (chat_id TEXT PRIMARY KEY, bot TEXT, order_id TEXT, until REAL);
CREATE TABLE IF NOT EXISTS order_claims (order_id TEXT PRIMARY KEY, bot TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS gate (name TEXT PRIMARY KEY, next_ts REAL);
"""


class Coordinator:
    def __init__(self, path: str | Path | None, bot: str, *, min_interval: float = 0.4):
        self.bot = bot
        self.min_interval = float(min_interval)
        self.path = str(path) if path else ""
        if self.path:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with self._conn() as c:
                c.executescript(SCHEMA)

    @classmethod
    def from_env(cls, bot: str, configured: str | None = None, **kw) -> "Coordinator":
        return cls(configured or os.environ.get("FUNPAY_COORD_DB") or None, bot, **kw)

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        con.execute("PRAGMA busy_timeout=5000")
        return con

    # ------------------------------------------------------------------ чаты
    def claim_chat(self, chat_id: str | None, order_id: str, hours: float = 72) -> None:
        if not (self.enabled and chat_id):
            return
        with self._conn() as c:
            c.execute("INSERT INTO chat_claims(chat_id,bot,order_id,until) VALUES(?,?,?,?) ON CONFLICT(chat_id) DO "
                      "UPDATE SET bot=excluded.bot, order_id=excluded.order_id, until=excluded.until",
                      (str(chat_id), self.bot, order_id, time.time() + hours * 3600))

    def release_chat(self, chat_id: str | None, order_id: str | None = None) -> None:
        if not (self.enabled and chat_id):
            return
        with self._conn() as c:
            if order_id:
                c.execute("DELETE FROM chat_claims WHERE chat_id=? AND bot=? AND order_id=?",
                          (str(chat_id), self.bot, order_id))
            else:
                c.execute("DELETE FROM chat_claims WHERE chat_id=? AND bot=?", (str(chat_id), self.bot))

    def chat_owner(self, chat_id: str | None) -> str | None:
        """Какой бот сейчас ведёт заказ в этом чате (None — никто)."""
        if not (self.enabled and chat_id):
            return None
        with self._conn() as c:
            row = c.execute("SELECT bot FROM chat_claims WHERE chat_id=? AND until>?", (str(chat_id), time.time())
                            ).fetchone()
        return row[0] if row else None

    def chat_is_foreign(self, chat_id: str | None) -> bool:
        owner = self.chat_owner(chat_id)
        return owner is not None and owner != self.bot

    # ------------------------------------------------------------------ заказы
    def claim_order(self, order_id: str) -> bool:
        """True — заказ наш (или только что стал нашим), False — его уже взял другой бот."""
        if not self.enabled:
            return True
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO order_claims(order_id,bot,ts) VALUES(?,?,?)",
                      (order_id, self.bot, time.time()))
            row = c.execute("SELECT bot FROM order_claims WHERE order_id=?", (order_id,)).fetchone()
        return bool(row) and row[0] == self.bot

    def order_owner(self, order_id: str) -> str | None:
        if not self.enabled:
            return None
        with self._conn() as c:
            row = c.execute("SELECT bot FROM order_claims WHERE order_id=?", (order_id,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ частота запросов
    def _reserve_slot(self, name: str) -> float:
        """Резервирует следующий свободный слот для запроса; возвращает, сколько секунд подождать."""
        now = time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT next_ts FROM gate WHERE name=?", (name,)).fetchone()
            slot = max(now, float(row[0]) if row else 0.0)
            c.execute("INSERT INTO gate(name,next_ts) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET next_ts=excluded.next_ts",
                      (name, slot + self.min_interval))
            c.execute("COMMIT")
        return slot - now

    async def wait_turn(self, name: str = "funpay") -> None:
        if not self.enabled:
            return
        try:
            wait = await asyncio.to_thread(self._reserve_slot, name)
        except sqlite3.Error:
            return                          # координация не должна останавливать торговлю
        if wait > 0:
            await asyncio.sleep(min(wait, 10))
