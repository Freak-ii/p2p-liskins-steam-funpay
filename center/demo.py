"""Демо-данные LisBot для `python -m center --demo`.

В демо MannBot разыгрывает заказы на эмуляции FunPay, а LisBot (`--demo`) торгует на эмуляции LIS-SKINS и
Steam (bots/lisbot/src/emu). Чтобы аналитика, CRM, RTBA и координатор сразу показывали месяц истории, центр
кладёт в демо-папку LisBot базу с прошлыми сделками; LisBot продолжает её живыми сделками. Схема берётся из самого LisBot (bots/lisbot/src/db.js),
поэтому демо-база всегда совпадает с настоящей. Вне демо-режима этот модуль не используется.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
from pathlib import Path

from center.config import BOTS_DIR

log = logging.getLogger("center.demo")

DAY = 86400
# предмет: (цена закупки $, ожидаемая чистая выручка $)
ITEMS = {
    "AK-47 | Redline (Field-Tested)": (11.2, 12.9),
    "AWP | Asiimov (Field-Tested)": (71.5, 80.3),
    "M4A1-S | Printstream (Minimal Wear)": (148.0, 163.5),
    "Glock-18 | Fade (Factory New)": (1215.0, 1302.0),
    "Desert Eagle | Blaze (Factory New)": (402.0, 431.0),
    "USP-S | Kill Confirmed (Field-Tested)": (39.6, 44.1),
}
PARTNERS = ["76561198000000101", "76561198000000202", "76561198000000303"]


def lisbot_schema() -> str:
    """SQL схемы из исходников LisBot: константа SCHEMA в src/db.js."""
    src = (BOTS_DIR / "lisbot" / "src" / "db.js").read_text(encoding="utf-8")
    m = re.search(r"const SCHEMA = `(.*?)`;", src, re.S)
    if not m:
        raise RuntimeError("не найдена схема LisBot в src/db.js")
    return m.group(1)


def seed_lisbot(data_dir: Path, now: float | None = None) -> bool:
    """Создать демо-базу LisBot. Прежнюю демо-базу пересоздаёт, чтобы даты сделок были свежими;
    базу, которую вёл сам LisBot (без отметки демо), не трогает. Вернуть True, если база создана."""
    db_path = Path(data_dir) / "bot.sqlite"
    if db_path.exists():
        if not _is_demo_db(db_path):
            return False
        for suffix in ("", "-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    try:
        schema = lisbot_schema()
    except (OSError, RuntimeError) as e:
        log.warning("демо-данные LisBot не созданы: %s", e)
        return False
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now = now or time.time()
    ms = lambda t: int(t * 1000)  # noqa: E731
    rnd = random.Random(42)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(schema)
        names = list(ITEMS)
        for name, (buy, net) in ITEMS.items():
            con.execute("INSERT INTO watchlist(name, enabled, source, max_held, max_per_day, min_roi, max_price_usd, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)", (name, 1, "manual", 3, 2, 0.05, buy * 1.1, ms(now - 40 * DAY)))
            con.execute("INSERT INTO valuations(name, ts, ok, q_price, median_price, recent_median, lowest_sell, highest_buy, "
                        "daily_volume, trend, volatility, expected_sell, net_wallet, net_usd, fx, max_buy_usd) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (name, ms(now - 3600), 1, net * 1.15, net * 1.15, net * 1.14, net * 1.16, net * 1.1, 40.0, 0.01,
                         0.04, net * 1.15, net, net, 92.4, round(net / 1.06, 2)))
            for h in range(7 * 24):                       # пол LIS за неделю: по нему считает бэктест RTBA
                t = now - (7 * 24 - h) * 3600
                con.execute("INSERT INTO lis_floor(name, ts, floor_usd, listings) VALUES(?,?,?,?)",
                            (name, ms(t), round(buy * (1 + rnd.uniform(-0.05, 0.06)), 2), rnd.randint(3, 40)))
        # сделки за 30 дней: большая часть в плюс, несколько в минус; последние продажи — в плюс
        n = 0
        for day in range(30, 0, -1):
            for _ in range(rnd.choice((0, 1, 1, 2))):
                name = rnd.choice(names)
                buy, net = ITEMS[name]
                bought = now - day * DAY + rnd.uniform(0, DAY / 2)
                held = rnd.uniform(7.2, 9.5) * DAY             # Trade Protection 7 дней + продажа на ТП
                sold_at = bought + held
                if sold_at > now - 3600:
                    continue
                sold = round(net * (1 + rnd.uniform(-0.06, 0.04)), 2) if day > 3 else round(net * 1.01, 2)
                n += 1
                _item(con, ms, name, "rtba" if n % 3 else "scan", "sold", buy, net, bought, sold_at, sold,
                      PARTNERS[n % len(PARTNERS)])
        for name, status, ago in (("AK-47 | Redline (Field-Tested)", "listed", 8.5 * DAY),
                                  ("USP-S | Kill Confirmed (Field-Tested)", "protected", 3 * DAY),
                                  ("AWP | Asiimov (Field-Tested)", "trade_sent", 2 * 3600)):
            buy, net = ITEMS[name]
            _item(con, ms, name, "rtba", status, buy, net, now - ago, None, None, PARTNERS[0])
        _item(con, ms, "M4A1-S | Printstream (Minimal Wear)", "rtba", "sold", 148.0, 163.5, now - 9 * DAY,
              now - DAY, 165.0, PARTNERS[1], dry=True)       # тестовая (dry-run) — CRM прячет её по умолчанию
        for key, value in (("center.demo", 1), ("fx", {"rate": 92.4, "ts": ms(now - 1800)}),
                           ("balance.last", {"balance": 1840.25, "ts": ms(now - 600)})):
            con.execute("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?)", (key, json.dumps(value), ms(now)))
        for ago, level, code, msg in ((5 * 3600, "warn", "LISTING_SLOW", "Лот AK-47 | Redline висит на ТП дольше 36 ч"),
                                      (26 * 3600, "info", "BALANCE_TOPUP", "Баланс LIS пополнен на 500 $")):
            con.execute("INSERT INTO alerts(ts, level, code, message) VALUES(?,?,?,?)", (ms(now - ago), level, code, msg))
        con.commit()
    finally:
        con.close()
    log.info("демо: создана база LisBot с историей сделок (%s)", db_path)
    return True


def _is_demo_db(path: Path) -> bool:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return con.execute("SELECT 1 FROM kv WHERE key='center.demo'").fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _item(con, ms, name, mode, status, buy, net, bought, sold_at, sold, partner, *, dry=False) -> None:
    received = bought + 1800
    listed = received + 7 * DAY if status in ("listed", "sold") else None
    profit = round(sold - buy, 2) if sold is not None else None
    cur = con.execute(
        "INSERT INTO items(name, mode, status, dry_run, buy_price_usd, fx_at_buy, expected_net_usd, expected_profit_usd, "
        "partner_steamid, received_at, listed_at, sold_at, sold_usd, profit_usd, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, mode, status, int(dry), buy, 92.4, net, round(net - buy, 2), partner,
         ms(received) if status != "trade_sent" else None, ms(listed) if listed else None,
         ms(sold_at) if sold_at else None, sold, profit, ms(bought), ms(sold_at or bought)))
    iid = cur.lastrowid
    con.execute("INSERT INTO events(ts, type, item_id, data) VALUES(?,?,?,?)",
                (ms(bought), "buy.dry_run" if dry else "buy.ok", iid, json.dumps({"latencyMs": 180 + iid * 7 % 260})))
    if listed:
        con.execute("INSERT INTO events(ts, type, item_id, data) VALUES(?,?,?,?)", (ms(listed), "sell.listed", iid, "{}"))
    if sold_at:
        con.execute("INSERT INTO events(ts, type, item_id, data) VALUES(?,?,?,?)",
                    (ms(sold_at), "item.sold", iid, json.dumps({"sold_usd": sold})))
