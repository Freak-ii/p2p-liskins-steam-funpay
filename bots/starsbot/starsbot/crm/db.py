"""SQLite-хранилище: CRM заказов, клиенты, события, покупки на Fragment, аналитика."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from starsbot.core.models import OrderStatus

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    buyer_username TEXT, buyer_id INTEGER, chat_id TEXT,
    description TEXT, quantity INTEGER, stars_per_unit INTEGER, stars INTEGER,
    price REAL, currency TEXT,
    status TEXT NOT NULL, fp_status TEXT DEFAULT 'paid', lot_offer_id TEXT,
    tg_username TEXT, tg_recipient TEXT, tg_name TEXT, username_source TEXT, username_attempts INTEGER DEFAULT 0,
    req_id TEXT, ton_amount REAL, ton_rate REAL, cost_rub REAL, tx_seqno INTEGER, tx_valid_until INTEGER,
    tx_msg_hash TEXT, tx_hash TEXT, tx_fee_ton REAL, fragment_ref TEXT, purchase_attempts INTEGER DEFAULT 0,
    risk_score INTEGER DEFAULT 0, risk_reasons TEXT DEFAULT '[]', approved INTEGER DEFAULT 0,
    reminded INTEGER DEFAULT 0, admin_alerted INTEGER DEFAULT 0, lang TEXT DEFAULT 'ru',
    review_stars INTEGER, notes TEXT DEFAULT '', fail_reason TEXT DEFAULT '',
    created_at REAL, paid_at REAL, username_at REAL, purchase_started_at REAL, sent_at REAL,
    delivered_at REAL, completed_at REAL, last_buyer_msg_at REAL, updated_at REAL,
    channel TEXT DEFAULT 'funpay', payout REAL,
    delivery TEXT DEFAULT 'fragment', gift_plan TEXT, gift_cost INTEGER, support_file TEXT, confirm_reminded INTEGER DEFAULT 0,
    premium_months INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_id);
CREATE INDEX IF NOT EXISTS idx_orders_chat ON orders(chat_id);
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, ts REAL, kind TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id);
CREATE TABLE IF NOT EXISTS customers (
    funpay_id INTEGER PRIMARY KEY, username TEXT, first_seen REAL, last_seen REAL,
    orders_count INTEGER DEFAULT 0, completed_count INTEGER DEFAULT 0, refunded_count INTEGER DEFAULT 0,
    total_spent REAL DEFAULT 0, stars_bought INTEGER DEFAULT 0, tg_usernames TEXT DEFAULT '[]',
    blacklisted INTEGER DEFAULT 0, notes TEXT DEFAULT '', tags TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, order_id TEXT,
    direction TEXT, text TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
CREATE TABLE IF NOT EXISTS chat_state (chat_id TEXT PRIMARY KEY, last_message_id INTEGER, presale_at REAL);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, req_id TEXT, stars INTEGER, recipient TEXT,
    dest TEXT, amount_nano INTEGER, seqno INTEGER, valid_until INTEGER, msg_hash TEXT, comment TEXT,
    state TEXT, tx_hash TEXT, error TEXT, created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_purchases_order ON purchases(order_id);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, offer_id TEXT, stars INTEGER, old_price REAL, new_price REAL,
    best_competitor REAL, competitors INTEGER, position INTEGER, floor REAL, reason TEXT, applied INTEGER
);
CREATE TABLE IF NOT EXISTS competitor_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, stars INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS balance_history (ts REAL, ton REAL, ton_rub REAL, capacity_stars INTEGER);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, code TEXT, text TEXT,
    order_id TEXT, acknowledged INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recommendations (
    code TEXT PRIMARY KEY, ts REAL, severity TEXT, text TEXT, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS metrics (ts REAL, name TEXT, value REAL);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(name, ts);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, role TEXT, action TEXT, target TEXT,
    result TEXT);
CREATE TABLE IF NOT EXISTS cashflow (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, rub REAL DEFAULT 0, ton REAL DEFAULT 0, note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS gift_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, seq INTEGER, gift_id INTEGER, stars INTEGER,
    convert_stars INTEGER, recipient_id INTEGER, state TEXT, error TEXT, created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_gift_sends_order ON gift_sends(order_id);
CREATE TABLE IF NOT EXISTS stars_history (ts REAL, stars INTEGER);
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, amount_rub REAL, method TEXT, wallet TEXT, state TEXT,
    detail TEXT, updated_at REAL
);
"""

ORDER_COLUMNS = {
    "buyer_username", "buyer_id", "chat_id", "description", "quantity", "stars_per_unit", "stars", "price",
    "currency", "status", "fp_status", "lot_offer_id", "tg_username", "tg_recipient", "tg_name", "username_source",
    "username_attempts", "req_id", "ton_amount", "ton_rate", "cost_rub", "tx_seqno", "tx_valid_until", "tx_msg_hash",
    "tx_hash", "tx_fee_ton", "fragment_ref", "purchase_attempts", "risk_score", "risk_reasons", "approved", "reminded",
    "admin_alerted", "lang", "review_stars", "notes", "fail_reason", "created_at", "paid_at", "username_at",
    "purchase_started_at", "sent_at", "delivered_at", "completed_at", "last_buyer_msg_at",
    "channel", "payout", "delivery", "gift_plan", "gift_cost", "support_file", "confirm_reminded",
    "premium_months",
}

# Колонки, добавленные после первых версий: в старых базах их создаёт миграция в Database.__init__
MIGRATIONS = {"orders": [("channel", "TEXT DEFAULT 'funpay'"), ("payout", "REAL"),
                          ("delivery", "TEXT DEFAULT 'fragment'"), ("gift_plan", "TEXT"), ("gift_cost", "INTEGER"),
                          ("support_file", "TEXT"), ("confirm_reminded", "INTEGER DEFAULT 0"),
                          ("premium_months", "INTEGER")]}


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols:
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_channel ON orders(channel)")
        self._lock = threading.RLock()
        self.listeners: list[Callable[[dict], None]] = []   # realtime: панель подписывается на события

    # ------------------------------------------------------------ helpers
    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
        row = self.one(sql, params)
        return default if row is None or row[0] is None else row[0]

    def exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def emit(self, event: dict) -> None:
        for fn in list(self.listeners):
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                pass

    def backup(self, folder: str | Path, keep: int = 14) -> Path | None:
        """Копия базы на сегодня (sqlite backup API — безопасно при работающем боте); старше keep копий — удаляются."""
        if self.path == ":memory:":
            return None
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{Path(self.path).stem}-{time.strftime('%Y%m%d')}.sqlite3"
        if dest.exists():
            return dest
        tmp = dest.with_suffix(".tmp")
        target = sqlite3.connect(str(tmp))
        try:
            with self._lock:
                self.conn.backup(target)
        finally:
            target.close()
        tmp.replace(dest)
        for old in sorted(folder.glob(f"{Path(self.path).stem}-*.sqlite3"))[:-keep]:
            old.unlink(missing_ok=True)
        return dest

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
        self.emit({"type": "order", "order_id": vals[0], "status": fields.get("status")})

    def update_order(self, order_id: str, **fields: Any) -> None:
        bad = set(fields) - ORDER_COLUMNS
        if bad:
            raise ValueError(f"unknown order columns: {bad}")
        if "status" in fields and isinstance(fields["status"], OrderStatus):
            fields["status"] = fields["status"].value
        fields["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        self.exec(f"UPDATE orders SET {sets} WHERE order_id=?", [*fields.values(), order_id])
        if "status" in fields:
            self.emit({"type": "order", "order_id": order_id, "status": fields["status"]})

    def orders_by_status(self, *statuses: OrderStatus) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.q(f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY created_at",
                      [s.value for s in statuses])

    def active_orders(self) -> list[sqlite3.Row]:
        finals = [s.value for s in OrderStatus if s.is_final]
        return self.q(f"SELECT * FROM orders WHERE status NOT IN ({','.join('?' * len(finals))}) "
                      "ORDER BY created_at", finals)

    def active_order_for_chat(self, chat_id: str) -> sqlite3.Row | None:
        """Самый ранний незавершённый и ещё не выданный заказ в чате (у покупателя их может быть несколько)."""
        rows = [r for r in self.q("SELECT * FROM orders WHERE chat_id=? ORDER BY created_at", (chat_id,))
                if OrderStatus(r["status"]).is_active and r["status"] != OrderStatus.DELIVERED.value]
        return rows[0] if rows else None

    def last_order_for_chat(self, chat_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM orders WHERE chat_id=? ORDER BY created_at DESC LIMIT 1", (chat_id,))

    def add_event(self, order_id: str, kind: str, **data: Any) -> None:
        self.exec("INSERT INTO order_events(order_id,ts,kind,data) VALUES(?,?,?,?)",
                  (order_id, time.time(), kind, json.dumps(data, ensure_ascii=False, default=str)))
        self.emit({"type": "event", "order_id": order_id, "kind": kind})

    def events(self, order_id: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM order_events WHERE order_id=? ORDER BY id", (order_id,))

    def stars_committed(self) -> int:
        """Звёзды, обещанные активным заказам, но ещё не оплаченные на Fragment."""
        waiting = (OrderStatus.NEW, OrderStatus.AWAITING_USERNAME, OrderStatus.PENDING_APPROVAL, OrderStatus.READY,
                   OrderStatus.PURCHASING)
        marks = ",".join("?" * len(waiting))
        return int(self.scalar(f"SELECT SUM(stars) FROM orders WHERE status IN ({marks}) AND "
                               "COALESCE(delivery,'fragment')='fragment'", [s.value for s in waiting], 0))

    def gift_stars_committed(self) -> int:
        """Звёзды аккаунта Telegram, которые уйдут на подарки активным заказам (по стоимости набора)."""
        waiting = (OrderStatus.NEW, OrderStatus.AWAITING_USERNAME, OrderStatus.PENDING_APPROVAL, OrderStatus.READY,
                   OrderStatus.PURCHASING)
        marks = ",".join("?" * len(waiting))
        return int(self.scalar(f"SELECT SUM(COALESCE(gift_cost, stars)) FROM orders WHERE status IN ({marks}) AND "
                               "delivery='gift'", [s.value for s in waiting], 0))

    # ------------------------------------------------------------ purchases
    def add_purchase(self, **fields: Any) -> int:
        now = time.time()
        fields.update(created_at=now, updated_at=now)
        cols = list(fields)
        cur = self.exec(f"INSERT INTO purchases({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [fields[c] for c in cols])
        return int(cur.lastrowid)

    def update_purchase(self, pid: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        self.exec(f"UPDATE purchases SET {sets} WHERE id=?", [*fields.values(), pid])

    def last_purchase(self, order_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM purchases WHERE order_id=? ORDER BY id DESC LIMIT 1", (order_id,))

    # ------------------------------------------------------------ customers
    def touch_customer(self, funpay_id: int, username: str, *, new_order_total: float = 0.0, stars: int = 0) -> None:
        now = time.time()
        self.exec("INSERT OR IGNORE INTO customers(funpay_id,username,first_seen,last_seen) VALUES(?,?,?,?)",
                  (funpay_id, username, now, now))
        if new_order_total or stars:
            self.exec("UPDATE customers SET username=?, last_seen=?, orders_count=orders_count+1, "
                      "total_spent=total_spent+?, stars_bought=stars_bought+? WHERE funpay_id=?",
                      (username, now, new_order_total, stars, funpay_id))
        else:
            self.exec("UPDATE customers SET username=?, last_seen=? WHERE funpay_id=?", (username, now, funpay_id))

    def customer(self, funpay_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM customers WHERE funpay_id=?", (funpay_id,))

    def customer_add_tg(self, funpay_id: int, tg_username: str) -> None:
        row = self.customer(funpay_id)
        if not row:
            return
        names = set(json.loads(row["tg_usernames"] or "[]"))
        names.add(tg_username.lower())
        self.exec("UPDATE customers SET tg_usernames=? WHERE funpay_id=?", (json.dumps(sorted(names)), funpay_id))

    def customer_incr(self, funpay_id: int, column: str) -> None:
        assert column in ("completed_count", "refunded_count")
        self.exec(f"UPDATE customers SET {column}={column}+1 WHERE funpay_id=?", (funpay_id,))

    def set_blacklist(self, funpay_id: int, value: bool, note: str = "") -> None:
        self.exec("UPDATE customers SET blacklisted=?, notes=CASE WHEN ?<>'' THEN ? ELSE notes END "
                  "WHERE funpay_id=?", (int(value), note, note, funpay_id))

    def set_customer_note(self, funpay_id: int, note: str, tags: str) -> None:
        self.exec("UPDATE customers SET notes=?, tags=? WHERE funpay_id=?", (note, tags, funpay_id))

    # ------------------------------------------------------------ messages
    def log_message(self, chat_id: str, order_id: str | None, direction: str, text: str) -> None:
        self.exec("INSERT INTO messages(chat_id,order_id,direction,text,ts) VALUES(?,?,?,?,?)",
                  (chat_id, order_id, direction, text, time.time()))
        self.emit({"type": "message", "chat_id": chat_id, "order_id": order_id, "direction": direction})

    def last_seen_message(self, chat_id: str) -> int:
        return int(self.scalar("SELECT last_message_id FROM chat_state WHERE chat_id=?", (chat_id,), 0))

    def set_last_seen_message(self, chat_id: str, message_id: int) -> None:
        self.exec("INSERT INTO chat_state(chat_id,last_message_id) VALUES(?,?) ON CONFLICT(chat_id) "
                  "DO UPDATE SET last_message_id=MAX(COALESCE(last_message_id,0), excluded.last_message_id)",
                  (chat_id, message_id))

    def presale_recent(self, chat_id: str, within: float) -> bool:
        ts = self.scalar("SELECT presale_at FROM chat_state WHERE chat_id=?", (chat_id,), 0) or 0
        return time.time() - float(ts) < within

    def mark_presale(self, chat_id: str) -> None:
        self.exec("INSERT INTO chat_state(chat_id,last_message_id,presale_at) VALUES(?,0,?) ON CONFLICT(chat_id) "
                  "DO UPDATE SET presale_at=excluded.presale_at", (chat_id, time.time()))

    # ------------------------------------------------------------ analytics/alerts
    def metric(self, name: str, value: float) -> None:
        self.exec("INSERT INTO metrics(ts,name,value) VALUES(?,?,?)", (time.time(), name, value))

    def add_alert(self, level: str, code: str, text: str, order_id: str | None = None) -> int:
        cur = self.exec("INSERT INTO alerts(ts,level,code,text,order_id) VALUES(?,?,?,?,?)",
                        (time.time(), level, code, text, order_id))
        self.emit({"type": "alert", "level": level, "code": code, "text": text[:300], "order_id": order_id})
        return int(cur.lastrowid)

    def audit(self, role: str, action: str, target: str, result: str) -> None:
        self.exec("INSERT INTO audit(ts,role,action,target,result) VALUES(?,?,?,?,?)",
                  (time.time(), role, action, target, result[:500]))

    def set_recommendation(self, code: str, severity: str, text: str) -> bool:
        """Возвращает True, если рекомендация новая (нужно уведомить)."""
        row = self.one("SELECT active FROM recommendations WHERE code=?", (code,))
        self.exec("INSERT INTO recommendations(code,ts,severity,text,active) VALUES(?,?,?,?,1) "
                  "ON CONFLICT(code) DO UPDATE SET ts=excluded.ts, severity=excluded.severity, "
                  "text=excluded.text, active=1", (code, time.time(), severity, text))
        return row is None or not row["active"]

    def clear_recommendation(self, code: str) -> None:
        self.exec("UPDATE recommendations SET active=0 WHERE code=?", (code,))
