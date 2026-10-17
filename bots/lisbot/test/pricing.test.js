import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { buyerPaysFor, sellerReceivesFor } from '../src/steam/fees.js';
import { computeValuation } from '../src/pricing/valuation.js';
import { planBuys, budgetFor } from '../src/pricing/buyStrategy.js';
import { sellPrice, repricePlan } from '../src/pricing/sellStrategy.js';
import { parseSteamPrice, weightedQuantile } from '../src/util/common.js';
import { deepMerge, parseTradeUrl } from '../src/config.js';
import { mapLisStatus } from '../src/engine/purchaseTracker.js';
import { parseHistoryDate } from '../src/steam/market.js';

const cfg = JSON.parse(fs.readFileSync(new URL('../config.example.json', import.meta.url)));
const DAY = 86400_000;

test('комиссии Steam: прямое и обратное преобразование согласованы', () => {
  assert.equal(buyerPaysFor(100), 115);
  assert.equal(buyerPaysFor(1), 3);
  for (let p = 3; p < 50000; p += 37) {
    const r = sellerReceivesFor(p);
    assert.ok(buyerPaysFor(r) <= p, `buyer(${r}) <= ${p}`);
    assert.ok(buyerPaysFor(r + 1) > p, `buyer(${r + 1}) > ${p}`);
  }
});

test('разбор локализованных цен Steam', () => {
  assert.equal(parseSteamPrice('1 234,56 pуб.'), 1234.56);
  assert.equal(parseSteamPrice('$1,234.56'), 1234.56);
  assert.equal(parseSteamPrice('12,34€'), 12.34);
  assert.equal(parseSteamPrice('87 pуб.'), 87);
  assert.equal(parseSteamPrice('$0.03'), 0.03);
});

test('дата истории цен Steam', () => {
  assert.equal(parseHistoryDate('Oct 10 2025 01: +0'), Date.UTC(2025, 9, 10, 1));
});

test('взвешенный квантиль', () => {
  assert.equal(weightedQuantile([[10, 1], [20, 1], [30, 2]], 0.5), 20);
  assert.equal(weightedQuantile([[10, 1], [20, 1], [30, 8]], 0.3), 30);
});

test('трейд-ссылка и слияние конфигов', () => {
  assert.deepEqual(parseTradeUrl('https://steamcommunity.com/tradeoffer/new/?partner=123&token=AbC'), { partner: '123', token: 'AbC' });
  assert.equal(parseTradeUrl('bad'), null);
  assert.deepEqual(deepMerge({ a: { b: 1, c: 2 } }, { a: { c: 3 } }), { a: { b: 1, c: 3 } });
});

test('статусы LIS', () => {
  assert.equal(mapLisStatus('returned'), 'failed');
  assert.equal(mapLisStatus('trade_sent'), 'trade_sent');
  assert.equal(mapLisStatus('success'), 'delivered');
  assert.equal(mapLisStatus(null), 'pending');
});

function history({ price = 1000, days = 14, perDay = 40, drift = 0, now = Date.now() }) {
  const out = [];
  for (let d = days; d >= 0; d--) {
    for (let h = 0; h < 24; h += 3) {
      const ts = now - d * DAY - h * 3600_000;
      const p = price * (1 + drift * ((days - d) / days)) * (1 + 0.02 * Math.sin(h + d));
      out.push({ ts, price: Math.round(p * 100) / 100, volume: Math.round(perDay / 8) });
    }
  }
  return out;
}

test('оценка: ликвидный стабильный предмет проходит, макс. цена учитывает комиссии и ROI', () => {
  const v = computeValuation({ name: 'X', history: history({}), book: { lowestSell: 1010, highestBuy: 950 }, fx: 90, cfg, holdDays: 9 });
  assert.equal(v.ok, true, v.reason);
  assert.ok(v.netWallet < 1000 / 1.15 + 1, 'нетто меньше цены минус комиссии');
  const expectedMax = v.netUsd / (1 + cfg.strategy.minRoi + cfg.strategy.roiPerHoldDay * 9);
  assert.ok(Math.abs(v.maxBuyUsd - expectedMax) < 1e-9);
});

test('оценка: неликвид, падающий тренд и волатильность отсекаются', () => {
  assert.equal(computeValuation({ name: 'X', history: history({ perDay: 4 }), book: null, fx: 90, cfg, holdDays: 9 }).ok, false);
  assert.match(computeValuation({ name: 'X', history: history({ drift: -0.3 }), book: null, fx: 90, cfg, holdDays: 9 }).reason, /тренд/);
  const wild = history({}).map((h, i) => ({ ...h, price: h.price * (Math.floor(i / 8) % 2 ? 1.6 : 0.6) }));
  assert.match(computeValuation({ name: 'X', history: wild, book: null, fx: 90, cfg, holdDays: 9 }).reason, /волатильность/);
});

const val = (over = {}) => ({ ok: true, ts: Date.now(), netUsd: 10, dailyVolume: 200, fx: 90, ...over });
const ctx = (over = {}) => ({ balance: 1000, exposure: 0, exposureItem: 0, spentHour: 0, spentDay: 0, heldCount: 0, boughtToday: 0, ...over });
const L = (id, price, extra = {}) => ({ id: String(id), name: 'X', price, unlockAt: null, ...extra });

test('закупка: берём только низ рынка, не весь стакан', () => {
  const listings = [L(1, 8.0), L(2, 8.05), L(3, 8.1), L(4, 8.12), L(5, 8.6), L(6, 9.0)];
  const plan = planBuys({ name: 'X', listings, valuation: val(), watch: null, ctx: ctx(), cfg });
  assert.equal(plan.reason, 'ok');
  // в коридоре 2% от 8.00 — 4 лота, берём не больше половины (maxBookSharePerScan=0.5) и не больше дневного лимита 3
  assert.equal(plan.buy.length, 2);
  assert.deepEqual(plan.buy.map((b) => b.listing.id), ['1', '2']);
  assert.ok(plan.buy.every((b) => b.listing.price <= 8.0 * 1.02));
});

test('закупка: пол вырос относительно медианы за 24ч — не догоняем цену', () => {
  const floorHistory = [8, 8.02, 7.98, 8.01].map((f) => ({ floor_usd: f }));
  const plan = planBuys({ name: 'X', listings: [L(1, 8.6), L(2, 8.7)], valuation: val(), watch: null, ctx: ctx(), cfg, floorHistory });
  assert.equal(plan.buy.length, 0);
  assert.match(plan.reason, /пол LIS вырос/);
});

test('закупка: после скупки дешёвого остаток выше коридора не покупается', () => {
  const floorHistory = [8, 8, 8, 8].map((f) => ({ floor_usd: f }));
  // дешёвые 8.0 скуплены, остались 8.3+ (рост 3.75% < 5% порога, но выше коридора 2% от базовой линии)
  const plan = planBuys({ name: 'X', listings: [L(1, 8.3), L(2, 8.35)], valuation: val(), watch: null, ctx: ctx(), cfg, floorHistory });
  assert.equal(plan.buy.length, 0);
});

test('закупка: ROI не проходит — нет покупки', () => {
  const plan = planBuys({ name: 'X', listings: [L(1, 9.8)], valuation: val(), watch: null, ctx: ctx(), cfg });
  assert.equal(plan.buy.length, 0);
  assert.match(plan.reason, /ROI/);
});

test('закупка: лимиты удержания, дня и бюджета', () => {
  const listings = [L(1, 8), L(2, 8), L(3, 8), L(4, 8), L(5, 8), L(6, 8)];
  assert.equal(planBuys({ name: 'X', listings, valuation: val(), watch: null, ctx: ctx({ heldCount: 5 }), cfg }).buy.length, 0);
  assert.equal(planBuys({ name: 'X', listings, valuation: val(), watch: null, ctx: ctx({ boughtToday: 3 }), cfg }).buy.length, 0);
  assert.equal(planBuys({ name: 'X', listings, valuation: val(), watch: { max_per_day: 10, max_held: 10 }, ctx: ctx(), cfg }).buy.length, 3);
  const poor = planBuys({ name: 'X', listings, valuation: val(), watch: null, ctx: ctx({ balance: 80 }), cfg });
  assert.equal(poor.buy.length, 1, 'доля капитала на предмет (12% от $80) пускает только 1 лот');
  assert.equal(planBuys({ name: 'X', listings, valuation: val(), watch: null, ctx: ctx({ balance: 12 }), cfg }).buy.length, 0, 'малый капитал — без покупок');
  assert.equal(planBuys({ name: 'X', listings, valuation: val(), watch: null, ctx: ctx({ spentDay: 299 }), cfg }).buy.length, 0);
});

test('закупка: подозрительно дешёвый лот и устаревшая оценка пропускаются', () => {
  assert.ok(planBuys({ name: 'X', listings: [L(1, 2)], valuation: val(), watch: null, ctx: ctx(), cfg }).info.suspicious);
  assert.match(planBuys({ name: 'X', listings: [L(1, 8)], valuation: val({ ts: Date.now() - 48 * 3600_000 }), watch: null, ctx: ctx(), cfg }).reason, /устарела/);
});

test('закупка: заблокированные на LIS лоты не берутся при allowLockedDays=0', () => {
  const plan = planBuys({ name: 'X', listings: [L(1, 7.9, { unlockAt: Date.now() + 3 * DAY })], valuation: val(), watch: null, ctx: ctx(), cfg });
  assert.equal(plan.buy.length, 0);
});

test('бюджет: ограничение по самому жёсткому лимиту', () => {
  const b = budgetFor(ctx({ balance: 300, exposure: 700, exposureItem: 100 }), cfg);
  assert.equal(b.limitedBy, 'item');
  assert.equal(b.amount, Math.max(0, 1000 * cfg.strategy.maxItemCapitalShare - 100));
});

test('продажа: undercut, ордер на покупку и пол себестоимости', () => {
  const p1 = sellPrice({ costUsd: 8, fx: 90, book: { lowestSell: 1000, highestBuy: 900 }, valuation: null, cfg });
  assert.equal(p1.mode, 'undercut');
  assert.equal(p1.buyerCents, 99999);
  const p2 = sellPrice({ costUsd: 8, fx: 90, book: { lowestSell: 1000, highestBuy: 985 }, valuation: null, cfg });
  assert.equal(p2.mode, 'buy_order');
  const p3 = sellPrice({ costUsd: 9.5, fx: 90, book: { lowestSell: 900, highestBuy: 800 }, valuation: null, cfg });
  assert.equal(p3.mode, 'floor');
  assert.ok(p3.receiveCents >= Math.ceil(9.5 * 90 * 1.02 * 100));
});

test('перевыставление: не ниже пола и только если перебили', () => {
  const item = { listed_at: Date.now() - 30 * 3600_000, relists: 0, list_price_cents: 100000, buy_price_usd: 8 };
  assert.equal(repricePlan({ item, book: { lowestSell: 1100 }, fx: 90, cfg }), null);
  const r = repricePlan({ item, book: { lowestSell: 950 }, fx: 90, cfg });
  assert.equal(r.action, 'reprice');
  assert.ok(r.buyerCents < 100000);
  const floor = repricePlan({ item: { ...item, buy_price_usd: 10 }, book: { lowestSell: 500 }, fx: 90, cfg });
  assert.ok(floor.action === 'hold' || floor.buyerCents >= buyerPaysFor(Math.ceil(10 * 90 * 1.02 * 100)));
});
