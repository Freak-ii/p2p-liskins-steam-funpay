import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { summary, daily, perItem } from '../analytics.js';
import { EDITABLE_SETTINGS, getPath, saveSettings } from '../config.js';

const PUBLIC = path.join(path.dirname(fileURLToPath(import.meta.url)), 'public');
const EDITABLE_STATUSES = ['manual', 'ready', 'protected', 'failed', 'sold'];

const err = (status, body) => ({ __status: status, body });

const safeEq = (a, b) => {
  const x = Buffer.from(String(a)), y = Buffer.from(String(b));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
};

const RTBA_SETTINGS = ['strategy.rtbaEnabled', 'strategy.rtbaCooldownSec', 'strategy.rtbaMaxValuationAgeHours', 'strategy.minRoi',
  'strategy.bottomTolerancePct', 'strategy.maxFloorRisePct', 'strategy.maxSpendPerHourUsd', 'strategy.maxSpendPerDayUsd',
  'strategy.maxOpenExposureUsd', 'strategy.balanceReservePct'];

const pctl = (xs, q) => (xs.length ? xs[Math.min(xs.length - 1, Math.floor(q * xs.length))] : null);

/** Сводка RTBA для центра: состояние WebSocket, счётчики, последние решения и задержки покупок. */
export function rtbaReport(db, cfg, bot, days = 7) {
  const since = Date.now() - days * 86400_000;
  const buys = db.db.prepare(`SELECT e.ts, e.type, e.data, i.name, i.buy_price_usd price, i.expected_profit_usd profit, i.dry_run
    FROM events e JOIN items i ON i.id=e.item_id WHERE e.type IN ('buy.ok','buy.dry_run') AND i.mode='rtba' AND e.ts>=? ORDER BY e.id DESC LIMIT 500`).all(since)
    .map((r) => { let d = {}; try { d = JSON.parse(r.data || '{}'); } catch { /* старый формат */ } return { ts: r.ts, name: r.name, price: r.price, profit: r.profit, dry: !!r.dry_run, latencyMs: d.latencyMs ?? null }; });
  const lat = buys.map((b) => b.latencyMs).filter((x) => Number.isFinite(x)).sort((x, y) => x - y);
  const byMode = db.db.prepare('SELECT mode, dry_run, COUNT(*) n, COALESCE(SUM(buy_price_usd),0) spent FROM items WHERE created_at>=? GROUP BY mode, dry_run').all(since);
  const ws = bot.ws ? { connected: bot.ws.connected, ...bot.ws.stats } : null;
  return {
    enabled: !!cfg.strategy.rtbaEnabled, dryRun: !!cfg.dryRun, days, ws,
    buyer: bot.buyer?.stats || null,
    latency: { n: lat.length, p50: pctl(lat, 0.5), p90: pctl(lat, 0.9), max: lat.at(-1) ?? null },
    byMode, buys: buys.slice(0, 100),
    recent: (bot.buyer?.rtbaLog || []).slice(-150).reverse(),
    settings: Object.fromEntries(RTBA_SETTINGS.map((k) => [k, getPath(cfg, k)])),
  };
}

/** Веб-панель: дашборд, мини-CRM по предметам, вотчлист, тревоги, управление. */
export function startDashboard({ cfg, db, guard, bot, log }) {
  const token = cfg.secrets.dashboardToken;

  const json = (res, code, data) => { res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(data)); };
  const body = (req) => new Promise((resolve, reject) => {
    let s = '';
    req.on('data', (c) => { s += c; if (s.length > 1e6) req.destroy(); });
    req.on('end', () => { try { resolve(s ? JSON.parse(s) : {}); } catch (e) { reject(e); } });
  });
  const authed = (req) => {
    const h = req.headers.authorization?.replace(/^Bearer /, '');
    const c = /(?:^|;\s*)lsb_token=([^;]+)/.exec(req.headers.cookie || '')?.[1];
    return (h && safeEq(h, token)) || (c && safeEq(decodeURIComponent(c), token));
  };

  const routes = {
    'GET /api/summary': () => ({ ...summary(db, { tz: cfg.timezone, fx: bot.fx?.rate }), guard: guard.state, dryRun: cfg.dryRun, runtime: bot.runtimeStats(), currency: cfg.steam.walletCurrencyCode }),
    'GET /api/daily': (u) => daily(db, { tz: cfg.timezone, days: Number(u.searchParams.get('days')) || 30 }),
    'GET /api/per-item': () => perItem(db),
    'GET /api/items': (u) => {
      const st = u.searchParams.get('status');
      const qtext = u.searchParams.get('q');
      const dry = u.searchParams.get('dry') === '1' ? 1 : 0;
      let sql = 'SELECT * FROM items WHERE dry_run=?';
      const args = [dry];
      if (st) { sql += ` AND status IN (${st.split(',').map(() => '?').join(',')})`; args.push(...st.split(',')); }
      if (qtext) { sql += ' AND (name LIKE ? OR notes LIKE ? OR tags LIKE ?)'; args.push(`%${qtext}%`, `%${qtext}%`, `%${qtext}%`); }
      return db.db.prepare(sql + ' ORDER BY id DESC LIMIT 500').all(...args);
    },
    'GET /api/item': (u) => {
      const id = Number(u.searchParams.get('id'));
      return { item: db.item(id), events: db.db.prepare('SELECT * FROM events WHERE item_id=? ORDER BY id').all(id) };
    },
    'POST /api/item': async (u, req) => {
      const b = await body(req);
      const it = db.item(Number(b.id));
      if (!it) return err(404, { error: 'нет такого предмета' });
      const fields = {};
      if (typeof b.notes === 'string') fields.notes = b.notes.slice(0, 2000);
      if (typeof b.tags === 'string') fields.tags = b.tags.slice(0, 200);
      if (b.status && EDITABLE_STATUSES.includes(b.status)) { db.setItemStatus(it.id, b.status, fields, { by: 'dashboard' }); return { ok: true }; }
      db.updateItem(it.id, fields);
      db.event('crm.edit', fields, it.id);
      return { ok: true };
    },
    'GET /api/watchlist': () => db.watchlist(false).map((w) => ({ ...w, valuation: db.valuation(w.name) || null, decision: bot.buyer?.lastDecision.get(w.name) || null, held: db.heldCount(w.name) })),
    'POST /api/watchlist': async (u, req) => {
      const b = await body(req);
      if (!b.name || typeof b.name !== 'string') return err(400, { error: 'name обязателен' });
      const num = (v) => (v === '' || v == null ? null : Number(v));
      db.upsertWatch({ name: b.name.trim(), enabled: b.enabled !== false, source: 'manual', max_held: num(b.max_held), max_per_day: num(b.max_per_day), min_roi: num(b.min_roi), max_price_usd: num(b.max_price_usd), notes: b.notes ?? null });
      bot.valuator?.refresh(b.name.trim()).catch(() => {});
      return { ok: true };
    },
    'POST /api/watchlist/delete': async (u, req) => { const b = await body(req); db.removeWatch(b.name); return { ok: true }; },
    'GET /api/alerts': () => db.db.prepare('SELECT * FROM alerts ORDER BY id DESC LIMIT 200').all(),
    'POST /api/alerts/ack': async (u, req) => { const b = await body(req); b.all ? db.db.prepare('UPDATE alerts SET acked=1').run() : db.db.prepare('UPDATE alerts SET acked=1 WHERE id=?').run(Number(b.id)); return { ok: true }; },
    'GET /api/counterparties': () => db.db.prepare('SELECT * FROM counterparties ORDER BY last_seen DESC').all(),
    'GET /api/balance': () => db.db.prepare('SELECT * FROM balance_snapshots ORDER BY id DESC LIMIT 500').all(),
    'GET /api/events': () => db.db.prepare('SELECT * FROM events ORDER BY id DESC LIMIT 300').all(),
    'POST /api/control': async (u, req) => {
      const b = await body(req);
      switch (b.action) {
        case 'pause': guard.pause('панель'); break;
        case 'resume': guard.resume('панель'); break;
        case 'buyPause': guard.setFlag('buyPaused', true); break;
        case 'buyResume': guard.setFlag('buyPaused', false); break;
        case 'sellPause': guard.setFlag('sellPaused', true); break;
        case 'sellResume': guard.setFlag('sellPaused', false); break;
        case 'stop': guard.critical('MANUAL_STOP', 'Аварийная остановка из панели'); break;
        default: return err(400, { error: 'неизвестное действие' });
      }
      return { ok: true, guard: guard.state };
    },
    'GET /api/settings': () => ({
      fields: Object.entries(EDITABLE_SETTINGS).map(([key, def]) => ({ key, ...def, value: getPath(cfg, key) })),
      canRestart: !!bot.hooks?.onRestartRequest,
    }),
    'POST /api/settings': async (u, req) => {
      const b = await body(req);
      const errs = saveSettings(cfg, b.changes);
      if (errs.length) return err(400, { error: errs.join('; ') });
      db.event('settings.changed', b.changes);
      log.info(`Настройки изменены: ${Object.keys(b.changes || {}).join(', ')}`);
      if (b.restart && bot.hooks?.onRestartRequest) setTimeout(() => bot.hooks.onRestartRequest('настройки изменены'), 300);
      return { ok: true, restarting: !!(b.restart && bot.hooks?.onRestartRequest) };
    },
    // Мягкая остановка по команде центра: на Windows сигналов нет, а TerminateProcess не даёт дописать базу
    'POST /api/shutdown': () => {
      if (!bot.hooks?.onShutdownRequest) return err(409, { error: 'остановка через API недоступна в этом режиме' });
      setTimeout(() => bot.hooks.onShutdownRequest('команда центра'), 200);
      return { ok: true };
    },
    'GET /api/rtba': (u) => rtbaReport(db, cfg, bot, Number(u.searchParams.get('days')) || 7),
    'GET /api/meta': () => ({ appMode: !!bot.hooks?.appMode, version: bot.hooks?.version || null, dataDir: cfg.dataDir, pid: process.pid }),
    'GET /api/export/items.csv': () => {
      const rows = db.db.prepare('SELECT * FROM items WHERE dry_run=0 ORDER BY id').all();
      const cols = rows[0] ? Object.keys(rows[0]) : ['id'];
      const esc = (v) => (v == null ? '' : /[",\n;]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
      return { __raw: [cols.join(';'), ...rows.map((r) => cols.map((c) => esc(r[c])).join(';'))].join('\n'), __type: 'text/csv; charset=utf-8' };
    },
  };

  const server = http.createServer(async (req, res) => {
    const u = new URL(req.url, 'http://localhost');
    try {
      if (u.pathname === '/login' && req.method === 'POST') {
        const b = await body(req);
        if (!safeEq(b.token || '', token)) return json(res, 401, { error: 'неверный токен' });
        res.writeHead(200, { 'Set-Cookie': `lsb_token=${encodeURIComponent(token)}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000`, 'Content-Type': 'application/json' });
        return res.end('{"ok":true}');
      }
      if (u.pathname === '/' || u.pathname === '/index.html') {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'X-Frame-Options': 'DENY' });
        return res.end(fs.readFileSync(path.join(PUBLIC, 'index.html')));
      }
      if (!u.pathname.startsWith('/api/')) { res.writeHead(404); return res.end(); }
      if (!authed(req)) return json(res, 401, { error: 'нужна авторизация' });
      if (req.method === 'POST' && req.headers['content-type']?.split(';')[0] !== 'application/json') return json(res, 415, { error: 'нужен application/json' });
      const handler = routes[`${req.method} ${u.pathname}`];
      if (!handler) return json(res, 404, { error: 'нет такого метода' });
      let out = await handler(u, req);
      let code = 200;
      if (out?.__status) { code = out.__status; out = out.body; }
      if (out?.__raw !== undefined) { res.writeHead(200, { 'Content-Type': out.__type }); return res.end(out.__raw); }
      json(res, code, out);
    } catch (e) {
      log.error(`dashboard ${req.method} ${u.pathname}: ${e.message}`);
      json(res, 500, { error: e.message });
    }
  });
  server.listen(cfg.dashboard.port, cfg.dashboard.host, () => log.info(`Панель: http://${cfg.dashboard.host}:${cfg.dashboard.port}`));
  return server;
}
