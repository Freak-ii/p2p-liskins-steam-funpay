"""SQLite-хранилище: CRM заказов, клиенты, события, резервы ключей, аналитика."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from mannbot.core.models import OrderStatus

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    buyer_username TEXT, buyer_id INTEGER, chat_id TEXT,
    description TEXT, quantity INTEGER, keys_qty INTEGER,
    price REAL, currency TEXT,
    status TEXT NOT NULL, fp_status TEXT DEFAULT 'paid', lot_offer_id TEXT,
    trade_url TEXT, steam_id64 TEXT, partner_name TEXT, escrow_days INTEGER DEFAULT 0,
    offer_id TEXT, link_attempts INTEGER DEFAULT 0, resends INTEGER DEFAULT 0,
    risk_score INTEGER DEFAULT 0, risk_reasons TEXT DEFAULT '[]', approved INTEGER DEFAULT 0,
    reminded INTEGER DEFAULT 0, admin_alerted INTEGER DEFAULT 0, lang TEXT DEFAULT 'ru',
    review_stars INTEGER, notes TEXT DEFAULT '', fail_reason TEXT DEFAULT '',
    created_at REAL, paid_at REAL, link_received_at REAL, offer_sent_at REAL,
    delivered_at REAL, completed_at REAL, last_buyer_msg_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_id);
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, ts REAL, kind TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id);
CREATE TABLE IF NOT EXISTS customers (
    funpay_id INTEGER PRIMARY KEY, username TEXT, first_seen REAL, last_seen REAL,
    orders_count INTEGER DEFAULT 0, completed_count INTEGER DEFAULT 0, refunded_count INTEGER DEFAULT 0,
    total_spent REAL DEFAULT 0, keys_bought INTEGER DEFAULT 0, steam_ids TEXT DEFAULT '[]',
    blacklisted INTEGER DEFAULT 0, notes TEXT DEFAULT '', tags TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, order_id TEXT,
    direction TEXT, text TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
CREATE TABLE IF NOT EXISTS chat_state (chat_id TEXT PRIMARY KEY, last_message_id INTEGER);
CREATE TABLE IF NOT EXISTS offers (
    offer_id TEXT PRIMARY KEY, order_id TEXT, state INTEGER, asset_ids TEXT,
    created_at REAL, updated_at REAL, canceled_by_bot INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reservations (asset_id TEXT PRIMARY KEY, order_id TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, offer_id TEXT, old_price REAL, new_price REAL,
    best_competitor REAL, competitors INTEGER, position INTEGER, reason TEXT, applied INTEGER
);
CREATE TABLE IF NOT EXISTS competitor_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS stock_history (ts REAL, available INTEGER, reserved INTEGER);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, code TEXT, text TEXT,
    order_id TEXT, acknowledged INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recommendations (
    code TEXT PRIMARY KEY, ts REAL, severity TEXT, text TEXT, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS metrics (ts REAL, name TEXT, value REAL);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(name, ts);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

ORDER_COLUMNS = {
    "buyer_username", "buyer_id", "chat_id", "description", "quantity", "keys_qty", "price",
    "currency", "status", "fp_status", "lot_offer_id", "trade_url", "steam_id64", "partner_name", "escrow_days", "offer_id",
    "link_attempts", "resends", "risk_score", "risk_reasons", "approved", "reminded",
    "admin_alerted", "lang", "review_stars", "notes", "fail_reason", "created_at", "paid_at",
    "link_received_at", "offer_sent_at", "delivered_at", "completed_at", "last_buyer_msg_at",
}


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------ helpers
    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
        row = self.one(sql, params)
        return default if row is None or row[0] is None else row[0]

    def exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    # ------------------------------------------------------------ kv
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.exec("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))

    # ------------------------------------------------------------ orders
    def get_order(self, order_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM orders WHERE order_id=?", (order_id,))

    def order_exists(self, order_id: str) -> bool:
        return self.get_order(order_id) is not None

    def create_order(self, **fields: Any) -> None:
        now = time.time()
        fields.setdefault("created_at", now)
        fields.setdefault("paid_at", now)
        fields.setdefault("status", OrderStatus.NEW.value)
        fields["updated_at"] = now
        cols = ["order_id"] + [k for k in fields if k in ORDER_COLUMNS or k == "updated_at"]
        vals = [fields.pop("order_id")] + [fields[k] for k in cols[1:]]
        self.exec(f"INSERT OR IGNORE INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)

    def update_order(self, order_id: str, **fields: Any) -> None:
        bad = set(fields) - ORDER_COLUMNS
        if bad:
            raise ValueError(f"unknown order columns: {bad}")
        if "status" in fields and isinstance(fields["status"], OrderStatus):
            fields["status"] = fields["status"].value
        fields["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        self.exec(f"UPDATE orders SET {sets} WHERE order_id=?", [*fields.values(), order_id])

    def orders_by_status(self, *statuses: OrderStatus) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.q(f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY created_at",
                      [s.value for s in statuses])

    def active_orders(self) -> list[sqlite3.Row]:
        finals = [s.value for s in OrderStatus if s.is_final]
        return self.q(f"SELECT * FROM orders WHERE status NOT IN ({','.join('?' * len(finals))}) "
                      "ORDER BY created_at", finals)

    def active_order_for_chat(self, chat_id: str) -> sqlite3.Row | None:
        """Самый ранний незавершённый заказ в чате (у покупателя может быть несколько)."""
        rows = [r for r in self.q("SELECT * FROM orders WHERE chat_id=? ORDER BY created_at", (chat_id,))
                if OrderStatus(r["status"]).is_active and r["status"] != OrderStatus.DELIVERED.value]
        return rows[0] if rows else None

    def add_event(self, order_id: str, kind: str, **data: Any) -> None:
        self.exec("INSERT INTO order_events(order_id,ts,kind,data) VALUES(?,?,?,?)",
                  (order_id, time.time(), kind, json.dumps(data, ensure_ascii=False, default=str)))

    def events(self, order_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM order_events WHERE order_id=? ORDER BY id", (order_id,))

    # ------------------------------------------------------------ customers
    def touch_customer(self, funpay_id: int, username: str, *, new_order_total: float = 0.0,
                       keys: int = 0) -> None:
        now = time.time()
        self.exec("INSERT OR IGNORE INTO customers(funpay_id,username,first_seen,last_seen) VALUES(?,?,?,?)",
                  (funpay_id, username, now, now))
        if new_order_total or keys:
            self.exec("UPDATE customers SET username=?, last_seen=?, orders_count=orders_count+1, "
                      "total_spent=total_spent+?, keys_bought=keys_bought+? WHERE funpay_id=?",
                      (username, now, new_order_total, keys, funpay_id))
        else:
            self.exec("UPDATE customers SET username=?, last_seen=? WHERE funpay_id=?", (username, now, funpay_id))

    def customer(self, funpay_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM customers WHERE funpay_id=?", (funpay_id,))

    def customer_add_steam_id(self, funpay_id: int, steam_id: str) -> None:
        row = self.customer(funpay_id)
        if not row:
            return
        ids = set(json.loads(row["steam_ids"] or "[]"))
        ids.add(steam_id)
        self.exec("UPDATE customers SET steam_ids=? WHERE funpay_id=?", (json.dumps(sorted(ids)), funpay_id))

    def customer_incr(self, funpay_id: int, column: str) -> None:
        assert column in ("completed_count", "refunded_count")
        self.exec(f"UPDATE customers SET {column}={column}+1 WHERE funpay_id=?", (funpay_id,))

    def set_blacklist(self, funpay_id: int, value: bool, note: str = "") -> None:
        self.exec("UPDATE customers SET blacklisted=?, notes=CASE WHEN ?<>'' THEN ? ELSE notes END "
                  "WHERE funpay_id=?", (int(value), note, note, funpay_id))

    def update_customer(self, funpay_id: int, *, notes: str | None = None, tags: str | None = None) -> bool:
        """Заметки и теги покупателя (правятся из панели MannBot и из CRM центра). False — нет такого покупателя."""
        fields = {k: v for k, v in (("notes", notes), ("tags", tags)) if v is not None}
        if not self.customer(funpay_id):
            return False
        if fields:
            self.exec(f"UPDATE customers SET {','.join(f'{k}=?' for k in fields)} WHERE funpay_id=?",
                      [*fields.values(), funpay_id])
        return True

    def customers_with_steam_id(self, steam_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM customers WHERE steam_ids LIKE ?", (f'%"{steam_id}"%',))

    # ------------------------------------------------------------ messages
    def log_message(self, chat_id: str, order_id: str | None, direction: str, text: str) -> None:
        self.exec("INSERT INTO messages(chat_id,order_id,direction,text,ts) VALUES(?,?,?,?,?)",
                  (chat_id, order_id, direction, text, time.time()))

    def last_seen_message(self, chat_id: str) -> int:
        return int(self.scalar("SELECT last_message_id FROM chat_state WHERE chat_id=?", (chat_id,), 0))

    def set_last_seen_message(self, chat_id: str, message_id: int) -> None:
        self.exec("INSERT INTO chat_state(chat_id,last_message_id) VALUES(?,?) ON CONFLICT(chat_id) "
                  "DO UPDATE SET last_message_id=MAX(last_message_id, excluded.last_message_id)",
                  (chat_id, message_id))

    # ------------------------------------------------------------ offers / reservations
    def record_offer(self, offer_id: str, order_id: str, state: int, asset_ids: list[str]) -> None:
        now = time.time()
        self.exec("INSERT OR REPLACE INTO offers(offer_id,order_id,state,asset_ids,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?)", (offer_id, order_id, state, json.dumps(asset_ids), now, now))

    def update_offer_state(self, offer_id: str, state: int, canceled_by_bot: bool | None = None) -> None:
        if canceled_by_bot is None:
            self.exec("UPDATE offers SET state=?, updated_at=? WHERE offer_id=?", (state, time.time(), offer_id))
        else:
            self.exec("UPDATE offers SET state=?, updated_at=?, canceled_by_bot=? WHERE offer_id=?",
                      (state, time.time(), int(canceled_by_bot), offer_id))

    def get_offer(self, offer_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM offers WHERE offer_id=?", (offer_id,))

    def known_offer_ids(self) -> set[str]:
        return {r["offer_id"] for r in self.q("SELECT offer_id FROM offers")}

    def reserve_assets(self, order_id: str, asset_ids: list[str]) -> None:
        now = time.time()
        self.conn.executemany("INSERT OR REPLACE INTO reservations(asset_id,order_id,ts) VALUES(?,?,?)",
                              [(a, order_id, now) for a in asset_ids])

    def release_assets(self, order_id: str) -> None:
        self.exec("DELETE FROM reservations WHERE order_id=?", (order_id,))

    def reserved_asset_ids(self) -> set[str]:
        return {r["asset_id"] for r in self.q("SELECT asset_id FROM reservations")}

    def keys_committed(self) -> int:
        """Ключи, обещанные активным заказам, но ещё не отправленные трейдом."""
        waiting = (OrderStatus.NEW, OrderStatus.AWAITING_LINK, OrderStatus.PENDING_APPROVAL,
                   OrderStatus.READY_TO_SEND, OrderStatus.MANUAL)
        marks = ",".join("?" * len(waiting))
        return int(self.scalar(f"SELECT SUM(keys_qty) FROM orders WHERE status IN ({marks})",
                               [s.value for s in waiting], 0))

    # ------------------------------------------------------------ analytics/alerts
    def metric(self, name: str, value: float) -> None:
        self.exec("INSERT INTO metrics(ts,name,value) VALUES(?,?,?)", (time.time(), name, value))

    def add_alert(self, level: str, code: str, text: str, order_id: str | None = None) -> int:
        cur = self.exec("INSERT INTO alerts(ts,level,code,text,order_id) VALUES(?,?,?,?,?)",
                        (time.time(), level, code, text, order_id))
        return int(cur.lastrowid)

    def set_recommendation(self, code: str, severity: str, text: str) -> bool:
        """Возвращает True, если рекомендация новая (нужно уведомить)."""
        row = self.one("SELECT active FROM recommendations WHERE code=?", (code,))
        self.exec("INSERT INTO recommendations(code,ts,severity,text,active) VALUES(?,?,?,?,1) "
                  "ON CONFLICT(code) DO UPDATE SET ts=excluded.ts, severity=excluded.severity, "
                  "text=excluded.text, active=1", (code, time.time(), severity, text))
        return row is None or not row["active"]

    def clear_recommendation(self, code: str) -> None:
        self.exec("UPDATE recommendations SET active=0 WHERE code=?", (code,))
