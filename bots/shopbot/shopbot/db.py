"""SQLite ShopBot: клиенты (мини-CRM), заказы покупки и выкупа, платежи, прозрачный журнал денег, RTBA."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, lang TEXT DEFAULT 'ru',
    created_at REAL, last_seen REAL, ref_code TEXT UNIQUE, referred_by INTEGER,
    blocked INTEGER DEFAULT 0, note TEXT DEFAULT '', tags TEXT DEFAULT '',
    balance_rub REAL DEFAULT 0, bonus_rub REAL DEFAULT 0, stars_credit INTEGER DEFAULT 0, usdt REAL DEFAULT 0,
    orders_count INTEGER DEFAULT 0, spent_rub REAL DEFAULT 0, sold_usdt REAL DEFAULT 0, first_order_at REAL,
    trade_url TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, user_id INTEGER, kind TEXT, product TEXT, item TEXT, amount INTEGER DEFAULT 0,
    recipient TEXT DEFAULT '', status TEXT, price_rub REAL DEFAULT 0, cost_rub REAL DEFAULT 0,
    discount_rub REAL DEFAULT 0, fee_rub REAL DEFAULT 0, total_rub REAL DEFAULT 0, margin_rub REAL DEFAULT 0,
    pay_method TEXT DEFAULT '', pay_currency TEXT DEFAULT '', pay_amount REAL DEFAULT 0, rate REAL DEFAULT 0,
    breakdown TEXT DEFAULT '[]', payment_id TEXT DEFAULT '', supplier TEXT DEFAULT '', supplier_ref TEXT DEFAULT '',
    supplier_status TEXT DEFAULT '', payout_kind TEXT DEFAULT '', payout_amount REAL DEFAULT 0,
    payout_asset TEXT DEFAULT '', payout_id TEXT DEFAULT '', fail_reason TEXT DEFAULT '', referrer_id INTEGER,
    ref_reward_rub REAL DEFAULT 0, created_at REAL, paid_at REAL, delivered_at REAL, updated_at REAL,
    expires_at REAL, note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY, order_id TEXT, provider TEXT, status TEXT, amount REAL, currency TEXT, url TEXT,
    created_at REAL, updated_at REAL, raw TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user_id INTEGER, account TEXT, delta REAL, kind TEXT,
    ref TEXT DEFAULT '', note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ledger_user ON ledger(user_id, ts);
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, ts REAL, kind TEXT, data TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_order ON order_events(order_id);
CREATE TABLE IF NOT EXISTS sessions (chat_id INTEGER PRIMARY KEY, state TEXT, data TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS rtba_points (
    key TEXT PRIMARY KEY, mode TEXT DEFAULT 'auto', state TEXT DEFAULT 'open', reason TEXT DEFAULT '',
    value TEXT DEFAULT '', checked_at REAL, changed_at REAL
);
CREATE TABLE IF NOT EXISTS rtba_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, key TEXT, decision TEXT, reason TEXT, data TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS supplier_fails (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, supplier TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, code TEXT, text TEXT, acknowledged INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channel_posts (
    id TEXT PRIMARY KEY, title TEXT, text TEXT, buttons TEXT DEFAULT '[]', message_id INTEGER DEFAULT 0,
    status TEXT DEFAULT 'draft', updated_at REAL, published_at REAL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS reviews (
    order_id TEXT PRIMARY KEY, user_id INTEGER, rating INTEGER, text TEXT DEFAULT '', product TEXT DEFAULT '',
    status TEXT DEFAULT 'new', bonus_rub REAL DEFAULT 0, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, role TEXT, action TEXT, target TEXT, result TEXT
);
"""

# новые колонки старых таблиц (база на ПК уже создана — CREATE TABLE IF NOT EXISTS их не добавит)
MIGRATIONS = [("users", "source", "TEXT DEFAULT ''"), ("users", "source_at", "REAL"),
              ("orders", "source", "TEXT DEFAULT ''"), ("orders", "loyalty_rub", "REAL DEFAULT 0"),
              ("orders", "bonus_rub", "REAL DEFAULT 0"), ("orders", "tier", "TEXT DEFAULT ''")]

ACCOUNTS = {"rub": "balance_rub", "bonus": "bonus_rub", "stars": "stars_credit", "usdt": "usdt"}
ORDER_COLS = {
    "user_id", "kind", "product", "item", "amount", "recipient", "status", "price_rub", "cost_rub", "discount_rub",
    "fee_rub", "total_rub", "margin_rub", "pay_method", "pay_currency", "pay_amount", "rate", "breakdown",
    "payment_id", "supplier", "supplier_ref", "supplier_status", "payout_kind", "payout_amount", "payout_asset",
    "payout_id", "fail_reason", "referrer_id", "ref_reward_rub", "created_at", "paid_at", "delivered_at",
    "expires_at", "note", "source", "loyalty_rub", "bonus_rub", "tier",
}


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if self.path != ":memory:":
                self.con.execute("PRAGMA journal_mode=WAL")
            self.con.executescript(SCHEMA)
            for table, col, decl in MIGRATIONS:
                have = {r[1] for r in self.con.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    self.con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self.con.commit()
        self.listeners: list = []

    # ------------------------------------------------------------------ базовое
    def exec(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.con.execute(sql, tuple(args))
            self.con.commit()
            return cur

    def q(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.con.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.con.execute(sql, tuple(args)).fetchone()

    def scalar(self, sql: str, args: Iterable = (), default: Any = None) -> Any:
        r = self.one(sql, args)
        return default if r is None or r[0] is None else r[0]

    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return default if r is None else json.loads(r["value"])

    def kv_set(self, key: str, value: Any) -> None:
        self.exec("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value, ensure_ascii=False)))

    def emit(self, ev: dict) -> None:
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001
                pass

    def audit(self, role: str, action: str, target: str = "", result: str = "") -> None:
        self.exec("INSERT INTO audit(ts,role,action,target,result) VALUES(?,?,?,?,?)",
                  (time.time(), role, action, target, str(result)[:300]))

    def alert(self, level: str, code: str, text: str) -> int:
        cur = self.exec("INSERT INTO alerts(ts,level,code,text) VALUES(?,?,?,?)", (time.time(), level, code, text))
        self.emit({"type": "alert", "level": level, "code": code})
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ клиенты
    def user(self, uid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE id=?", (uid,))

    def user_by_ref(self, code: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE ref_code=?", (code,))

    def touch_user(self, uid: int, username: str = "", first_name: str = "", lang: str = "ru",
                   referred_by: int | None = None) -> tuple[sqlite3.Row, bool]:
        now = time.time()
        u = self.user(uid)
        if u is None:
            code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
            ref = referred_by if referred_by and referred_by != uid and self.user(referred_by) else None
            self.exec("INSERT INTO users(id,username,first_name,lang,created_at,last_seen,ref_code,referred_by) "
                      "VALUES(?,?,?,?,?,?,?,?)", (uid, username, first_name, lang or "ru", now, now, code, ref))
            return self.user(uid), True
        self.exec("UPDATE users SET username=COALESCE(NULLIF(?,''),username), first_name=COALESCE(NULLIF(?,''),"
                  "first_name), last_seen=? WHERE id=?", (username, first_name, now, uid))
        return self.user(uid), False

    def set_source(self, uid: int, source: str, window_days: float = 30) -> bool:
        """Метка трафика: первая побеждает; новая метка — только если старой больше window_days дней."""
        u = self.user(uid)
        if u is None or not source:
            return False
        if u["source"] and time.time() - float(u["source_at"] or 0) < window_days * 86400:
            return False
        self.exec("UPDATE users SET source=?, source_at=? WHERE id=?", (source, time.time(), uid))
        return True

    def credit(self, uid: int, account: str, delta: float, kind: str, ref: str = "", note: str = "") -> None:
        """Любое движение баланса клиента — только через журнал (клиент видит его в «Балансе»)."""
        col = ACCOUNTS[account]
        with self.lock:
            cur = self.con.execute(f"SELECT {col} FROM users WHERE id=?", (uid,)).fetchone()
            if cur is None:
                raise ValueError(f"нет клиента {uid}")
            if float(cur[0] or 0) + delta < -1e-9:
                raise ValueError("недостаточно средств на балансе")
            self.con.execute(f"UPDATE users SET {col}={col}+? WHERE id=?", (delta, uid))
            self.con.execute("INSERT INTO ledger(ts,user_id,account,delta,kind,ref,note) VALUES(?,?,?,?,?,?,?)",
                             (time.time(), uid, account, delta, kind, ref, note))
            self.con.commit()

    # ------------------------------------------------------------------ заказы
    def new_order_id(self, kind: str) -> str:
        prefix = "B" if kind == "buy" else "S"
        while True:
            oid = f"{prefix}{time.strftime('%y%m%d')}{secrets.token_hex(3).upper()}"
            if self.order(oid) is None:
                return oid

    def create_order(self, oid: str, **fields: Any) -> sqlite3.Row:
        now = time.time()
        fields.setdefault("created_at", now)
        if isinstance(fields.get("breakdown"), (list, dict)):
            fields["breakdown"] = json.dumps(fields["breakdown"], ensure_ascii=False)
        bad = set(fields) - ORDER_COLS
        if bad:
            raise ValueError(f"unknown order columns: {bad}")
        cols = ["id", "updated_at", *fields]
        self.exec(f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                  [oid, now, *fields.values()])
        self.event(oid, "created", status=fields.get("status"))
        return self.order(oid)

    def order(self, oid: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM orders WHERE id=?", (oid,))

    def update_order(self, oid: str, event: str | None = None, **fields: Any) -> sqlite3.Row:
        if isinstance(fields.get("breakdown"), (list, dict)):
            fields["breakdown"] = json.dumps(fields["breakdown"], ensure_ascii=False)
        bad = set(fields) - ORDER_COLS
        if bad:
            raise ValueError(f"unknown order columns: {bad}")
        if fields:
            fields["updated_at"] = time.time()
            self.exec(f"UPDATE orders SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), oid])
        if event or "status" in fields:
            self.event(oid, event or str(fields["status"]), **{k: v for k, v in fields.items()
                                                                if k in ("status", "fail_reason", "supplier_status")})
        self.emit({"type": "order", "order_id": oid, "status": fields.get("status")})
        return self.order(oid)

    def event(self, oid: str, kind: str, **data: Any) -> None:
        self.exec("INSERT INTO order_events(order_id,ts,kind,data) VALUES(?,?,?,?)",
                  (oid, time.time(), kind, json.dumps(data, ensure_ascii=False, default=str)))

    def events(self, oid: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM order_events WHERE order_id=? ORDER BY id", (oid,))

    def user_spent_since(self, uid: int, since: float) -> float:
        return float(self.scalar("SELECT SUM(total_rub) FROM orders WHERE user_id=? AND kind='buy' AND "
                                 "created_at>=? AND status NOT IN ('expired','cancelled','refunded')",
                                 (uid, since), 0) or 0)

    # ------------------------------------------------------------------ сессии диалога
    def session(self, chat_id: int) -> tuple[str, dict]:
        r = self.one("SELECT state, data FROM sessions WHERE chat_id=?", (chat_id,))
        return (r["state"], json.loads(r["data"] or "{}")) if r else ("", {})

    def set_session(self, chat_id: int, state: str, data: dict | None = None) -> None:
        self.exec("INSERT INTO sessions(chat_id,state,data,updated_at) VALUES(?,?,?,?) ON CONFLICT(chat_id) DO "
                  "UPDATE SET state=excluded.state, data=excluded.data, updated_at=excluded.updated_at",
                  (chat_id, state, json.dumps(data or {}, ensure_ascii=False), time.time()))
