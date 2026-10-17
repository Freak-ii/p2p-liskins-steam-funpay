import path from 'node:path';
import Database from 'better-sqlite3';
import { ensureDir } from './util/common.js';

const SCHEMA = `
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER);

CREATE TABLE IF NOT EXISTS watchlist (
  name TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL DEFAULT 'manual',
  max_held INTEGER,
  max_per_day INTEGER,
  min_roi REAL,
  max_price_usd REAL,
  notes TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS valuations (
  name TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  reason TEXT,
  q_price REAL, median_price REAL, recent_median REAL,
  lowest_sell REAL, highest_buy REAL,
  daily_volume REAL, trend REAL, volatility REAL,
  expected_sell REAL, net_wallet REAL, net_usd REAL, fx REAL,
  max_buy_usd REAL
);

CREATE TABLE IF NOT EXISTS item_nameids (name TEXT PRIMARY KEY, nameid TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS lis_floor (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, ts INTEGER NOT NULL, floor_usd REAL NOT NULL, listings INTEGER
);
CREATE INDEX IF NOT EXISTS lis_floor_name_ts ON lis_floor(name, ts);

CREATE TABLE IF NOT EXISTS purchases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  custom_id TEXT UNIQUE NOT NULL,
  purchase_id TEXT,
  ts INTEGER NOT NULL,
  mode TEXT NOT NULL,
  requested_usd REAL NOT NULL,
  total_usd REAL,
  status TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  raw TEXT
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lis_item_id TEXT,
  custom_id TEXT,
  purchase_id TEXT,
  name TEXT NOT NULL,
  mode TEXT,
  status TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 0,
  buy_price_usd REAL NOT NULL,
  fx_at_buy REAL,
  expected_net_usd REAL,
  expected_profit_usd REAL,
  lis_unlock_at INTEGER,
  lis_status TEXT,
  trade_offer_id TEXT,
  partner_steamid TEXT,
  received_at INTEGER,
  asset_id TEXT,
  tradable_after INTEGER,
  listing_id TEXT,
  list_price_cents INTEGER,
  list_receive_cents INTEGER,
  listed_at INTEGER,
  relists INTEGER NOT NULL DEFAULT 0,
  sold_at INTEGER,
  sold_receive_cents INTEGER,
  sold_usd REAL,
  profit_usd REAL,
  notes TEXT,
  tags TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE INDEX IF NOT EXISTS items_name ON items(name);
CREATE INDEX IF NOT EXISTS items_asset ON items(asset_id);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, type TEXT NOT NULL, item_id INTEGER, data TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_item ON events(item_id);
CREATE INDEX IF NOT EXISTS events_type ON events(type, ts);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, level TEXT NOT NULL, code TEXT, message TEXT NOT NULL, data TEXT, acked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, balance_usd REAL NOT NULL, delta REAL, unexplained REAL, kind TEXT
);

CREATE TABLE IF NOT EXISTS counterparties (
  steamid TEXT PRIMARY KEY,
  label TEXT,
  trusted INTEGER NOT NULL DEFAULT 0,
  first_seen INTEGER, last_seen INTEGER,
  offers_accepted INTEGER NOT NULL DEFAULT 0,
  offers_declined INTEGER NOT NULL DEFAULT 0,
  items_received INTEGER NOT NULL DEFAULT 0,
  notes TEXT
);
`;

export const ACTIVE_STATUSES = ['bought', 'locked_on_lis', 'trade_sent', 'protected', 'ready', 'listing', 'listed'];
export const HELD_STATUSES = ACTIVE_STATUSES;

export function openDb(dataDir, file = 'bot.sqlite') {
  ensureDir(dataDir);
  const db = new Database(file === ':memory:' ? ':memory:' : path.join(dataDir, file));
  db.pragma('journal_mode = WAL');
  db.pragma('busy_timeout = 5000');
  db.exec(SCHEMA);
  return new Repo(db);
}

const j = (v) => (v === undefined ? null : JSON.stringify(v));

export class Repo {
  constructor(db) { this.db = db; this._stmts = new Map(); }

  st(sql) {
    let s = this._stmts.get(sql);
    if (!s) { s = this.db.prepare(sql); this._stmts.set(sql, s); }
    return s;
  }

  tx(fn) { return this.db.transaction(fn)(); }

  // --- kv ---
  get(key, def = null) {
    const r = this.st('SELECT value FROM kv WHERE key=?').get(key);
    return r ? JSON.parse(r.value) : def;
  }
  set(key, value) {
    this.st('INSERT INTO kv(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at').run(key, JSON.stringify(value), Date.now());
  }

  // --- events / alerts ---
  event(type, data = {}, itemId = null) {
    this.st('INSERT INTO events(ts,type,item_id,data) VALUES(?,?,?,?)').run(Date.now(), type, itemId, j(data));
  }
  alert(level, code, message, data) {
    return this.st('INSERT INTO alerts(ts,level,code,message,data) VALUES(?,?,?,?,?)').run(Date.now(), level, code, message, j(data)).lastInsertRowid;
  }

  // --- watchlist ---
  upsertWatch({ name, enabled = 1, source = 'manual', max_held = null, max_per_day = null, min_roi = null, max_price_usd = null, notes = null }) {
    this.st(`INSERT INTO watchlist(name,enabled,source,max_held,max_per_day,min_roi,max_price_usd,notes,created_at)
      VALUES(@name,@enabled,@source,@max_held,@max_per_day,@min_roi,@max_price_usd,@notes,@ts)
      ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled, max_held=excluded.max_held, max_per_day=excluded.max_per_day,
        min_roi=excluded.min_roi, max_price_usd=excluded.max_price_usd, notes=COALESCE(excluded.notes, watchlist.notes)`)
      .run({ name, enabled: enabled ? 1 : 0, source, max_held, max_per_day, min_roi, max_price_usd, notes, ts: Date.now() });
  }
  watchlist(onlyEnabled = true) {
    return this.st(`SELECT * FROM watchlist ${onlyEnabled ? 'WHERE enabled=1' : ''} ORDER BY name`).all();
  }
  removeWatch(name) { this.st('DELETE FROM watchlist WHERE name=?').run(name); }

  // --- valuations ---
  saveValuation(v) {
    this.st(`INSERT INTO valuations(name,ts,ok,reason,q_price,median_price,recent_median,lowest_sell,highest_buy,daily_volume,trend,volatility,expected_sell,net_wallet,net_usd,fx,max_buy_usd)
      VALUES(@name,@ts,@ok,@reason,@q_price,@median_price,@recent_median,@lowest_sell,@highest_buy,@daily_volume,@trend,@volatility,@expected_sell,@net_wallet,@net_usd,@fx,@max_buy_usd)
      ON CONFLICT(name) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, reason=excluded.reason, q_price=excluded.q_price, median_price=excluded.median_price,
        recent_median=excluded.recent_median, lowest_sell=excluded.lowest_sell, highest_buy=excluded.highest_buy, daily_volume=excluded.daily_volume,
        trend=excluded.trend, volatility=excluded.volatility, expected_sell=excluded.expected_sell, net_wallet=excluded.net_wallet,
        net_usd=excluded.net_usd, fx=excluded.fx, max_buy_usd=excluded.max_buy_usd`)
      .run({
        name: v.name, ts: v.ts ?? Date.now(), ok: v.ok ? 1 : 0, reason: v.reason ?? null,
        q_price: v.qPrice ?? null, median_price: v.medianPrice ?? null, recent_median: v.recentMedian ?? null,
        lowest_sell: v.lowestSell ?? null, highest_buy: v.highestBuy ?? null, daily_volume: v.dailyVolume ?? null,
        trend: v.trend ?? null, volatility: v.volatility ?? null, expected_sell: v.expectedSell ?? null,
        net_wallet: v.netWallet ?? null, net_usd: v.netUsd ?? null, fx: v.fx ?? null, max_buy_usd: v.maxBuyUsd ?? null,
      });
  }
  valuation(name) { return this.st('SELECT * FROM valuations WHERE name=?').get(name); }
  valuations() { return this.st('SELECT * FROM valuations ORDER BY name').all(); }

  nameid(name) { return this.st('SELECT nameid FROM item_nameids WHERE name=?').get(name)?.nameid ?? null; }
  saveNameid(name, nameid) { this.st('INSERT OR REPLACE INTO item_nameids(name,nameid) VALUES(?,?)').run(name, String(nameid)); }

  // --- LIS floor history ---
  saveFloor(name, floorUsd, listings) {
    this.st('INSERT INTO lis_floor(name,ts,floor_usd,listings) VALUES(?,?,?,?)').run(name, Date.now(), floorUsd, listings);
  }
  floors(name, sinceTs) { return this.st('SELECT ts, floor_usd FROM lis_floor WHERE name=? AND ts>=? ORDER BY ts').all(name, sinceTs); }
  pruneFloors(beforeTs) { this.st('DELETE FROM lis_floor WHERE ts<?').run(beforeTs); }

  // --- purchases ---
  insertPurchase(p) {
    return this.st(`INSERT INTO purchases(custom_id,purchase_id,ts,mode,requested_usd,total_usd,status,dry_run,error,raw)
      VALUES(@custom_id,@purchase_id,@ts,@mode,@requested_usd,@total_usd,@status,@dry_run,@error,@raw)`)
      .run({ purchase_id: null, total_usd: null, error: null, raw: null, dry_run: 0, ts: Date.now(), ...p }).lastInsertRowid;
  }
  updatePurchase(customId, fields) {
    const keys = Object.keys(fields);
    if (!keys.length) return;
    this.db.prepare(`UPDATE purchases SET ${keys.map((k) => `${k}=@${k}`).join(',')} WHERE custom_id=@__id`).run({ ...fields, __id: customId });
  }
  purchase(customId) { return this.st('SELECT * FROM purchases WHERE custom_id=?').get(customId); }
  pendingPurchases() { return this.st("SELECT * FROM purchases WHERE status IN ('pending','partial') AND dry_run=0").all(); }

  // --- items (CRM) ---
  insertItem(it) {
    const t = Date.now();
    const row = {
      lis_item_id: null, custom_id: null, purchase_id: null, mode: null, dry_run: 0, fx_at_buy: null, expected_net_usd: null,
      expected_profit_usd: null, lis_unlock_at: null, lis_status: null, notes: null, tags: null, ...it, created_at: t, updated_at: t,
    };
    return this.st(`INSERT INTO items(lis_item_id,custom_id,purchase_id,name,mode,status,dry_run,buy_price_usd,fx_at_buy,expected_net_usd,expected_profit_usd,lis_unlock_at,lis_status,notes,tags,created_at,updated_at)
      VALUES(@lis_item_id,@custom_id,@purchase_id,@name,@mode,@status,@dry_run,@buy_price_usd,@fx_at_buy,@expected_net_usd,@expected_profit_usd,@lis_unlock_at,@lis_status,@notes,@tags,@created_at,@updated_at)`).run(row).lastInsertRowid;
  }
  updateItem(id, fields) {
    const keys = Object.keys(fields);
    if (!keys.length) return;
    this.db.prepare(`UPDATE items SET ${keys.map((k) => `${k}=@${k}`).join(',')}, updated_at=@__ts WHERE id=@__id`).run({ ...fields, __id: id, __ts: Date.now() });
  }
  setItemStatus(id, status, extra = {}, eventData = {}) {
    this.updateItem(id, { status, ...extra });
    this.event(`item.${status}`, eventData, id);
  }
  item(id) { return this.st('SELECT * FROM items WHERE id=?').get(id); }
  itemsByStatus(...statuses) {
    return this.db.prepare(`SELECT * FROM items WHERE dry_run=0 AND status IN (${statuses.map(() => '?').join(',')}) ORDER BY id`).all(...statuses);
  }
  itemsByPurchase(customId) { return this.st('SELECT * FROM items WHERE custom_id=?').all(customId); }
  itemByAsset(assetId) { return this.st('SELECT * FROM items WHERE asset_id=? AND dry_run=0').get(String(assetId)); }

  heldCount(name) {
    return this.db.prepare(`SELECT COUNT(*) c FROM items WHERE name=? AND dry_run=0 AND status IN (${HELD_STATUSES.map(() => '?').join(',')})`).get(name, ...HELD_STATUSES).c;
  }
  boughtSince(name, ts, includeDry = false) {
    return this.st(`SELECT COUNT(*) c FROM items WHERE name=? AND created_at>=? AND status NOT IN ('failed') AND (dry_run=0 OR ?)`).get(name, ts, includeDry ? 1 : 0).c;
  }
  exposureUsd(name = null) {
    const ph = HELD_STATUSES.map(() => '?').join(',');
    if (name) return this.db.prepare(`SELECT COALESCE(SUM(buy_price_usd),0) s FROM items WHERE name=? AND dry_run=0 AND status IN (${ph})`).get(name, ...HELD_STATUSES).s;
    return this.db.prepare(`SELECT COALESCE(SUM(buy_price_usd),0) s FROM items WHERE dry_run=0 AND status IN (${ph})`).get(...HELD_STATUSES).s;
  }
  spentSince(ts, includeDry = false) {
    return this.st(`SELECT COALESCE(SUM(COALESCE(total_usd, requested_usd)),0) s FROM purchases WHERE ts>=? AND status NOT IN ('failed','error') AND (dry_run=0 OR ?)`).get(ts, includeDry ? 1 : 0).s;
  }
  realizedPnlSince(ts) {
    return this.st('SELECT COALESCE(SUM(profit_usd),0) s FROM items WHERE dry_run=0 AND sold_at>=?').get(ts).s;
  }

  // --- counterparties ---
  touchCounterparty(steamid, field, n = 1) {
    const t = Date.now();
    this.st('INSERT INTO counterparties(steamid,first_seen,last_seen) VALUES(?,?,?) ON CONFLICT(steamid) DO UPDATE SET last_seen=excluded.last_seen').run(String(steamid), t, t);
    if (field) this.db.prepare(`UPDATE counterparties SET ${field}=${field}+? WHERE steamid=?`).run(n, String(steamid));
  }
  counterparty(steamid) { return this.st('SELECT * FROM counterparties WHERE steamid=?').get(String(steamid)); }
}
