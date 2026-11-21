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
CREATE TABLE IF NOT EXISTS presale_claims (chat_id TEXT PRIMARY KEY, bot TEXT, until REAL);
CREATE TABLE IF NOT EXISTS supply_requests (account TEXT PRIMARY KEY, bot TEXT, username TEXT, balance INTEGER,
    committed INTEGER, need INTEGER, min_stars INTEGER, note TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS supply_log (id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, username TEXT, stars INTEGER,
    order_id TEXT, state TEXT, cost_rub REAL, ton REAL, supplier TEXT, note TEXT, created_at REAL, updated_at REAL);
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

    def claim_presale(self, chat_id: str | None, hours: float = 6) -> bool:
        """Ответ на вопрос в чате без заказа: отвечает один бот (кто первый взял чат), остальные молчат."""
        if not (self.enabled and chat_id):
            return True
        now = time.time()
        try:
            with self._conn() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT bot, until FROM presale_claims WHERE chat_id=?", (str(chat_id),)).fetchone()
                if row and row[0] != self.bot and float(row[1] or 0) > now:
                    c.execute("COMMIT")
                    return False
                c.execute("INSERT INTO presale_claims(chat_id,bot,until) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE "
                          "SET bot=excluded.bot, until=excluded.until", (str(chat_id), self.bot, now + hours * 3600))
                c.execute("COMMIT")
        except sqlite3.Error:
            return True
        return True

    # ------------------------------------------------------------------ снабжение звёздами
    # Бот, которому нужны звёзды на Telegram-аккаунт (TG Gift), пишет заявку; StarsBot покупает звёзды на Fragment
    # на @username этого аккаунта и ведёт журнал поставок. Файл общий, поэтому боты не знают портов друг друга.
    def supply_request(self, account: str, *, username: str, balance: int, committed: int, need: int,
                       min_stars: int, note: str = "") -> None:
        if not self.enabled:
            return
        with self._conn() as c:
            c.execute("INSERT INTO supply_requests(account,bot,username,balance,committed,need,min_stars,note,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET bot=excluded.bot, "
                      "username=excluded.username, balance=excluded.balance, committed=excluded.committed, "
                      "need=excluded.need, min_stars=excluded.min_stars, note=excluded.note, updated_at=excluded.updated_at",
                      (str(account), self.bot, username, int(balance), int(committed), int(need), int(min_stars), note,
                       time.time()))

    def supply_requests(self, max_age: float = 900) -> list[dict]:
        if not self.enabled:
            return []
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM supply_requests WHERE updated_at>=?", (time.time() - max_age,)).fetchall()
        return [dict(r) for r in rows]

    def supply_log_add(self, account: str, username: str, stars: int, order_id: str, note: str = "") -> None:
        if not self.enabled:
            return
        now = time.time()
        with self._conn() as c:
            c.execute("INSERT INTO supply_log(account,username,stars,order_id,state,supplier,note,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?)", (str(account), username, int(stars), order_id, "pending", self.bot,
                                                    note, now, now))

    def supply_log_update(self, order_id: str, state: str, *, cost_rub: float | None = None, ton: float | None = None,
                          note: str | None = None) -> None:
        if not self.enabled:
            return
        with self._conn() as c:
            c.execute("UPDATE supply_log SET state=?, cost_rub=COALESCE(?,cost_rub), ton=COALESCE(?,ton), "
                      "note=COALESCE(?,note), updated_at=? WHERE order_id=?",
                      (state, cost_rub, ton, note, time.time(), order_id))

    def supply_history(self, account: str | None = None, limit: int = 20) -> list[dict]:
        if not self.enabled:
            return []
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            if account:
                rows = c.execute("SELECT * FROM supply_log WHERE account=? ORDER BY id DESC LIMIT ?",
                                 (str(account), limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM supply_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

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
