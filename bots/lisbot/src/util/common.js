import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));
export const randInt = (min, max) => Math.floor(min + Math.random() * (max - min + 1));
export const jitter = (base, spread) => base + randInt(0, Math.max(0, spread));
export const now = () => Date.now();
export const HOUR = 3600_000;
export const DAY = 24 * HOUR;
export const round2 = (x) => Math.round((x + Number.EPSILON) * 100) / 100;
export const uid = (prefix = '') => prefix + crypto.randomBytes(8).toString('hex');

export function quantile(sorted, q) {
  if (!sorted.length) return NaN;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos), hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

/** Квантиль с весами (например, цена продажи, взвешенная по объёму). */
export function weightedQuantile(pairs, q) {
  const arr = pairs.filter(([v, w]) => Number.isFinite(v) && w > 0).sort((a, b) => a[0] - b[0]);
  const total = arr.reduce((s, [, w]) => s + w, 0);
  if (!total) return NaN;
  let acc = 0;
  for (const [v, w] of arr) { acc += w; if (acc >= total * q) return v; }
  return arr[arr.length - 1][0];
}

export function median(values) {
  const s = values.filter(Number.isFinite).sort((a, b) => a - b);
  return s.length ? quantile(s, 0.5) : NaN;
}

/** Разбор локализованной цены Steam: "1 234,56 pуб.", "$1,234.56", "12,34€" */
export function parseSteamPrice(str) {
  if (str == null) return NaN;
  if (typeof str === 'number') return str;
  let s = String(str).replace(/[^\d.,]/g, '').replace(/^[.,]+|[.,]+$/g, '');
  if (!s) return NaN;
  const lastDot = s.lastIndexOf('.'), lastComma = s.lastIndexOf(',');
  const decPos = Math.max(lastDot, lastComma);
  if (decPos >= 0 && s.length - decPos - 1 <= 2 && s.length - decPos - 1 > 0) {
    const intPart = s.slice(0, decPos).replace(/[.,]/g, '');
    return parseFloat(`${intPart}.${s.slice(decPos + 1)}`);
  }
  return parseFloat(s.replace(/[.,]/g, ''));
}

export function ensureDir(dir) { fs.mkdirSync(dir, { recursive: true }); return dir; }

export function createLogger(dataDir) {
  ensureDir(path.join(dataDir, 'logs'));
  const fileFor = () => path.join(dataDir, 'logs', `bot-${new Date().toISOString().slice(0, 10)}.log`);
  const write = (level, scope, msg, extra) => {
    const line = `${new Date().toISOString()} ${level.padEnd(5)} [${scope}] ${msg}${extra !== undefined ? ' ' + safeJson(extra) : ''}`;
    (level === 'ERROR' || level === 'CRIT' ? console.error : console.log)(line);
    try { fs.appendFileSync(fileFor(), line + '\n'); } catch { /* диск недоступен — не роняем бота */ }
  };
  const make = (scope) => ({
    debug: (m, e) => process.env.DEBUG && write('DEBUG', scope, m, e),
    info: (m, e) => write('INFO', scope, m, e),
    warn: (m, e) => write('WARN', scope, m, e),
    error: (m, e) => write('ERROR', scope, m, e),
    crit: (m, e) => write('CRIT', scope, m, e),
    child: (s) => make(`${scope}:${s}`),
  });
  return make('bot');
}

export function safeJson(v) {
  try {
    return JSON.stringify(v, (k, val) => (val instanceof Error ? { message: val.message, code: val.code } : val));
  } catch { return String(v); }
}

export function errMsg(e) { return e?.message || String(e); }

/** Отложенный промис для callback-API. */
export function promisify(fn, ctx) {
  return (...args) => new Promise((resolve, reject) => fn.call(ctx, ...args, (err, ...res) => (err ? reject(err) : resolve(res.length > 1 ? res : res[0]))));
}

export function dayKey(ts = Date.now(), tz = 'UTC') {
  return new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(ts));
}

export function hourIn(tz = 'UTC', ts = Date.now()) {
  return Number(new Intl.DateTimeFormat('en-GB', { timeZone: tz, hour: '2-digit', hourCycle: 'h23' }).format(new Date(ts)));
}

export function inActiveHours(cfg, ts = Date.now()) {
  const ah = cfg.steam.activeHours;
  if (!ah?.enabled) return true;
  const h = hourIn(cfg.timezone, ts);
  return ah.from <= ah.to ? h >= ah.from && h < ah.to : h >= ah.from || h < ah.to;
}

export function fmtUsd(x) { return Number.isFinite(x) ? `$${x.toFixed(2)}` : '—'; }

/** Начало текущих суток в часовом поясе tz (точность — час, для лимитов этого достаточно). */
export function startOfDay(tz = 'UTC', ts = Date.now()) {
  const k = dayKey(ts, tz);
  let t = ts - (ts % HOUR);
  while (dayKey(t - HOUR, tz) === k) t -= HOUR;
  return t;
}
