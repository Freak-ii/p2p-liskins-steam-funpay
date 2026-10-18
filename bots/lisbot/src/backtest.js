/**
 * Бэктест правил закупки на накопленной истории «пола» LIS (таблица lis_floor) той же функцией planBuys,
 * что и в живом боте. Ничего не покупает и не трогает базу: данные приходят в stdin одним JSON от центра.
 *
 * Вход: { config, valuations: {name: строка valuations}, watch: {name: строка watchlist},
 *         floors: [[name, ts, floorUsd], ...] по возрастанию ts, balance, variants: [{label, changes: {'strategy.minRoi': 0.1}}] }
 * Выход: { variants: [{ label, changes, buys, spent, expectedProfit, roi, maxExposure, items, skips, limitedBy }] }
 *
 * Упрощения (видны в панели центра): в стакане известен только самый дешёвый лот; оценка Steam — текущая
 * (на момент бэктеста), продажа через holdDaysFor() дней по этой оценке возвращает капитал.
 */
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { planBuys } from './pricing/buyStrategy.js';
import { holdDaysFor } from './pricing/valuation.js';
import { deepMerge, setPath } from './config.js';
import { startOfDay } from './util/common.js';

const HOUR = 3600_000, DAY = 24 * HOUR;

const toValuation = (r, ts) => r && ({
  name: r.name, ts, ok: !!r.ok, reason: r.reason, netUsd: r.net_usd, dailyVolume: r.daily_volume, fx: r.fx, maxBuyUsd: r.max_buy_usd,
});

export function simulate({ cfg, valuations, watch, floors, balance }) {
  const st = cfg.strategy;
  const hist = new Map();                       // name -> [{ts, floor_usd}]
  const held = [];                              // { name, price, until }
  const buys = [];
  const skips = {}, limitedBy = {};
  const lastBought = new Map();                 // name -> цена пола, по которой уже купили (тот же лот не берём дважды)
  let cash = balance, maxExposure = 0;
  for (const [name, ts, floor] of floors) {
    const w = watch[name];
    const h = hist.get(name) || [];
    hist.set(name, h);
    const baseline = h.filter((f) => f.ts >= ts - st.floorBaselineHours * HOUR);
    h.push({ ts, floor_usd: floor });
    if (!w || w.enabled === 0) continue;
    // вернувшийся после продажи капитал
    for (let i = held.length - 1; i >= 0; i--) if (held[i].until <= ts) { cash += held[i].sell; held.splice(i, 1); }
    if (lastBought.get(name) === floor) continue;
    lastBought.delete(name);
    const valuation = toValuation(valuations[name], ts);
    const sod = startOfDay(cfg.timezone, ts);
    const mine = buys.filter((b) => b.name === name);
    const exposure = held.reduce((s, x) => s + x.price, 0);
    const ctx = {
      balance: cash, exposure, exposureItem: held.filter((x) => x.name === name).reduce((s, x) => s + x.price, 0),
      spentHour: buys.filter((b) => b.ts >= ts - HOUR).reduce((s, b) => s + b.price, 0),
      spentDay: buys.filter((b) => b.ts >= sod).reduce((s, b) => s + b.price, 0),
      heldCount: held.filter((x) => x.name === name).length, boughtToday: mine.filter((b) => b.ts >= sod).length,
    };
    const plan = planBuys({ name, listings: [{ id: `${name}@${ts}`, name, price: floor, unlockAt: null }], valuation, watch: w, ctx, cfg, floorHistory: baseline, now: ts });
    if (plan.info?.budget?.limitedBy) limitedBy[plan.info.budget.limitedBy] = (limitedBy[plan.info.budget.limitedBy] || 0) + 1;
    if (!plan.buy.length) {
      const key = plan.reason.replace(/[\d.,$%]+/g, '…').slice(0, 80);
      skips[key] = (skips[key] || 0) + 1;
      continue;
    }
    for (const b of plan.buy) {
      const price = b.listing.price;
      cash -= price;
      held.push({ name, price, sell: valuation.netUsd, until: ts + holdDaysFor(cfg, 0) * DAY });
      buys.push({ name, ts, price, profit: b.expectedProfit });
    }
    lastBought.set(name, floor);
    maxExposure = Math.max(maxExposure, held.reduce((s, x) => s + x.price, 0));
  }
  const spent = buys.reduce((s, b) => s + b.price, 0);
  const profit = buys.reduce((s, b) => s + b.profit, 0);
  const items = {};
  for (const b of buys) { const it = items[b.name] ||= { name: b.name, buys: 0, spent: 0, profit: 0 }; it.buys++; it.spent += b.price; it.profit += b.profit; }
  const r2 = (x) => Math.round(x * 100) / 100;
  return {
    buys: buys.length, spent: r2(spent), expectedProfit: r2(profit), roi: spent ? r2(profit / spent * 100) : null, maxExposure: r2(maxExposure),
    items: Object.values(items).sort((a, b) => b.profit - a.profit).slice(0, 15).map((x) => ({ ...x, spent: r2(x.spent), profit: r2(x.profit) })),
    skips, limitedBy, first: floors[0]?.[1] ?? null, last: floors.at(-1)?.[1] ?? null,
  };
}

export function runBacktest(input) {
  const defaults = JSON.parse(fs.readFileSync(new URL('../config.example.json', import.meta.url), 'utf8'));
  const base = deepMerge(defaults, input.config || {});
  const variants = (input.variants?.length ? input.variants : [{ label: 'текущие правила', changes: {} }]).map((v) => {
    const cfg = structuredClone(base);
    for (const [k, val] of Object.entries(v.changes || {})) setPath(cfg, k, val);
    return { label: v.label, changes: v.changes || {}, ...simulate({ cfg, valuations: input.valuations || {}, watch: input.watch || {}, floors: input.floors || [], balance: Number(input.balance) || 0 }) };
  });
  return { variants };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === fs.realpathSync(process.argv[1])) {
  let raw = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (c) => { raw += c; });
  process.stdin.on('end', () => {
    try { process.stdout.write(JSON.stringify(runBacktest(JSON.parse(raw || '{}')))); } catch (e) { process.stdout.write(JSON.stringify({ error: e.message })); process.exitCode = 1; }
  });
}
