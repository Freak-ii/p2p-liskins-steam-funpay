import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { openDb } from '../src/db.js';
import { Guard } from '../src/engine/guard.js';
import { LisClient } from '../src/lis/client.js';
import { Buyer } from '../src/engine/buyer.js';
import { PurchaseTracker } from '../src/engine/purchaseTracker.js';
import { TradeHandler } from '../src/engine/trades.js';
import { ConfirmationWatcher } from '../src/engine/confirmations.js';
import { Seller } from '../src/engine/seller.js';
import { BalanceWatcher } from '../src/engine/balanceWatcher.js';
import { Valuator } from '../src/engine/pricing.js';
import { summary, daily, perItem, textReport } from '../src/analytics.js';
import { rtbaReport } from '../src/web/server.js';

const DAY = 86400_000;
const baseCfg = JSON.parse(fs.readFileSync(new URL('../config.example.json', import.meta.url)));
const silent = { info() {}, warn() {}, error() {}, crit() {}, debug() {}, child() { return silent; } };

function makeEnv(over = {}) {
  const cfg = structuredClone(baseCfg);
  Object.assign(cfg, { dryRun: false, timezone: 'UTC', trade: { partner: '123', token: 'tok' } });
  cfg.strategy.rtbaCooldownSec = 0;
  cfg.sell.minListingGapSec = 0;
  cfg.risk.maxValuationStalenessHours = 24;
  Object.assign(cfg.strategy, over.strategy || {});
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lsb-'));
  const db = openDb(dir, ':memory:');
  const sent = [];
  const notify = { send: async (level, text) => { sent.push({ level, text }); return true; } };
  const guard = new Guard({ db, notify, log: silent, cfg });
  return { cfg, db, notify, sent, guard };
}

/** Фейковый LIS API поверх fetch. */
function fakeLis(state) {
  const fetchImpl = async (url, opts) => {
    const u = new URL(url);
    const ok = (data, status = 200) => ({ status, ok: status < 400, headers: new Map(), text: async () => JSON.stringify(data) });
    state.calls.push(`${opts.method} ${u.pathname}`);
    if (u.pathname.endsWith('/user/balance')) return ok({ data: { balance: state.balance } });
    if (u.pathname.endsWith('/market/search')) {
      const names = u.searchParams.getAll('names[]');
      return ok({ data: state.listings.filter((l) => !names.length || names.includes(l.name)).sort((a, b) => a.price - b.price), meta: { next_cursor: null } });
    }
    if (u.pathname.endsWith('/market/buy')) {
      const body = JSON.parse(opts.body);
      state.buyBodies.push(body);
      const skins = state.listings.filter((l) => body.ids.includes(Number(l.id)));
      const total = skins.reduce((s, l) => s + l.price, 0);
      if (total > body.max_price + 1e-9) return ok({ message: 'max price exceeded' }, 400);
      state.balance -= total;
      state.listings = state.listings.filter((l) => !body.ids.includes(Number(l.id)));
      const purchase = { purchase_id: state.nextPid++, custom_id: body.custom_id, skins: skins.map((s) => ({ id: Number(s.id), name: s.name, price: s.price, status: 'processing' })) };
      state.purchases.set(body.custom_id, purchase);
      return ok({ data: purchase });
    }
    if (u.pathname.endsWith('/market/info')) {
      const ids = u.searchParams.getAll('custom_ids[]');
      return ok({ data: ids.map((id) => state.purchases.get(id)).filter(Boolean) });
    }
    return ok({ message: 'not found' }, 404);
  };
  return new LisClient({ apiKey: 'k', baseUrl: 'https://api.test/v1', requestsPerSecond: 1000, log: silent, fetchImpl });
}

function seedValuation(db, name, netUsd, ts = Date.now()) {
  db.saveValuation({ name, ts, ok: true, netUsd, maxBuyUsd: netUsd / 1.12, dailyVolume: 300, fx: 90, medianPrice: netUsd * 90 * 1.15, trend: 0 });
}

test('полный цикл: скан → покупка → трейд → Trade Protection → выставление → подтверждение → продажа', async () => {
  const { cfg, db, notify, sent, guard } = makeEnv();
  const NAME = 'AK-47 | Redline (Field-Tested)';
  db.upsertWatch({ name: NAME, source: 'config' });
  seedValuation(db, NAME, 10);
  const lisState = {
    balance: 500, calls: [], buyBodies: [], purchases: new Map(), nextPid: 1000,
    listings: [
      { id: 1, name: NAME, price: 8.0 }, { id: 2, name: NAME, price: 8.05 }, { id: 3, name: NAME, price: 8.1 },
      { id: 4, name: NAME, price: 8.12 }, { id: 5, name: NAME, price: 9.5 }, { id: 6, name: 'Other', price: 1 },
    ],
  };
  const lis = fakeLis(lisState);
  const balanceRef = { value: 500 };
  const valuator = new Valuator({ cfg, market: null, db, fx: { rate: 90 }, log: silent });
  const buyer = new Buyer({ cfg, db, lis, valuator, guard, notify, log: silent, balanceRef });

  await buyer.scan();
  assert.equal(lisState.buyBodies.length, 1, 'одна покупка');
  const body = lisState.buyBodies[0];
  assert.deepEqual(body.ids, [1, 2], 'берём только 2 самых дешёвых из коридора (половина низа)');
  assert.equal(body.partner, '123');
  assert.ok(body.custom_id && body.max_price === 16.05);
  let items = db.itemsByStatus('bought');
  assert.equal(items.length, 2);

  // повторный скан: дневной лимит 3 → докупит максимум 1, и только в коридоре от базовой линии
  await buyer.scan();
  const second = lisState.buyBodies[1];
  assert.ok(!second || second.ids.every((id) => [3, 4].includes(id)));
  assert.ok(db.itemsByStatus('bought').length <= 3);

  // трекер: LIS отправил трейд
  for (const p of lisState.purchases.values()) p.skins.forEach((s) => (s.status = 'trade_sent'));
  const tracker = new PurchaseTracker({ cfg, db, lis, guard, log: silent, notify });
  await tracker.poll();
  items = db.itemsByStatus('trade_sent');
  assert.equal(items.length, db.itemsByStatus('bought', 'trade_sent').length);

  // входящий оффер от LIS: только получаем, предметы совпадают
  const received = items.map((it, i) => ({ market_hash_name: it.name, assetid: String(5000 + i) }));
  let accepted = false;
  const offer = {
    id: '777', partner: { getSteamID64: () => '7656119800000001' }, itemsToGive: [], itemsToReceive: received,
    isGlitched: () => false, accept: (_a, cb) => { accepted = true; cb(null, 'accepted'); },
    getExchangeDetails: (_f, cb) => cb(null, 3, new Date(), received.map((r) => ({ ...r, new_assetid: r.assetid }))),
  };
  const fakeManager = { on() {} };
  const trades = new TradeHandler({ cfg, db, session: { manager: fakeManager }, guard, notify, log: silent });
  trades.assignAssetIds = async (o, matched) => matched.forEach((m, i) => db.updateItem(m.crm.id, { asset_id: String(5000 + i) }));
  const realSleep = globalThis.setTimeout;
  await withFastTimers(() => trades.onNewOffer(offer));
  globalThis.setTimeout = realSleep;
  assert.ok(accepted, 'оффер принят');
  const prot = db.itemsByStatus('protected');
  assert.equal(prot.length, items.length);
  assert.ok(prot.every((p) => p.tradable_after > Date.now() + 6.9 * DAY), 'Trade Protection ~7 дней');

  // инвентарь: защита снята — предмет marketable
  const inventory = prot.map((p) => ({ assetid: p.asset_id, market_hash_name: p.name, marketable: true, cache_expiration: new Date(Date.now() - 1000).toISOString() }));
  const listings = { active: [], toConfirm: [] };
  const confirmations = [];
  const session = {
    inventory: async () => inventory,
    getConfirmations: async () => confirmations,
    respondConfirmation: async (c, accept) => { c.done = accept; if (accept) { const l = listings.toConfirm.splice(listings.toConfirm.findIndex((x) => x.listingId === String(c.creator)), 1)[0]; listings.active.push(l); } },
  };
  let listingSeq = 9000;
  const market = {
    orderBook: async () => ({ lowestSell: 1000, highestBuy: 900 }),
    sellItem: async (assetId, receiveCents) => {
      const l = { listingId: String(listingSeq++), assetId: String(assetId), receiveCents };
      listings.toConfirm.push(l);
      confirmations.push({ id: l.listingId + 'c', key: 'k', type: 3, creator: l.listingId, title: 'Sell' });
      return { success: true, requires_confirmation: 1, needs_mobile_confirmation: true };
    },
    myListings: async () => ({ active: [...listings.active], toConfirm: [...listings.toConfirm], onHold: [] }),
    soldListings: async () => new Map(listings.sold || []),
  };
  const conf = new ConfirmationWatcher({ session, db, guard, log: silent });
  const seller = new Seller({ cfg, db, session, market, conf, valuator, fx: { rate: 90 }, guard, notify, log: silent });
  cfg.steam.activeHours.enabled = false;
  await withFastTimers(() => seller.cycle());
  const listed = db.itemsByStatus('listed');
  assert.equal(listed.length, prot.length, 'все выставлены и подтверждены');
  assert.ok(listed.every((l) => l.list_price_cents === 99999), 'на 1 копейку ниже минимального лота');
  assert.ok(confirmations.every((c) => c.done === true), 'подтверждены только свои лоты');
  assert.equal(guard.state.halted, false);

  // продажа: лот исчез из активных и есть в истории
  listings.sold = listings.active.map((l) => [l.listingId, { receiveCents: l.receiveCents, ts: Date.now() }]);
  listings.active = [];
  await withFastTimers(() => seller.syncListings());
  const sold = db.itemsByStatus('sold');
  assert.equal(sold.length, listed.length);
  assert.ok(sold.every((s) => s.profit_usd > 0), 'прибыль положительная');
  assert.ok(sent.some((m) => m.level === 'money' && /Продано/.test(m.text)));

  const s = summary(db, { tz: 'UTC', fx: 90 });
  assert.equal(s.sold.n, sold.length);
  assert.ok(s.sold.profit > 0);
  assert.ok(daily(db, { tz: 'UTC', days: 3 }).some((d) => d.sold > 0));
  assert.equal(perItem(db)[0].name, NAME);
  assert.match(textReport(s), /Всего продано/);
});

test('безопасность трейдов: оффер с запросом наших предметов отклоняется, неизвестный — игнорируется', async () => {
  const { cfg, db, notify, guard, sent } = makeEnv();
  cfg.risk.acceptFreeOffers = false;
  const trades = new TradeHandler({ cfg, db, session: { manager: { on() {} } }, guard, notify, log: silent });
  let declined = false, accepted = false;
  const base = { partner: { getSteamID64: () => '1' }, isGlitched: () => false, accept: (_a, cb) => { accepted = true; cb(null, 'accepted'); }, decline: (cb) => { declined = true; cb(null); } };
  await trades.onNewOffer({ ...base, id: '1', itemsToGive: [{ market_hash_name: 'Knife' }], itemsToReceive: [] });
  assert.ok(declined && !accepted);
  await trades.onNewOffer({ ...base, id: '2', itemsToGive: [], itemsToReceive: [{ market_hash_name: 'Random gift' }] });
  assert.ok(!accepted, 'не совпадает с покупками — не принимаем');
  assert.ok(sent.filter((m) => m.level === 'warn').length >= 2);
  assert.equal(guard.state.halted, false);
});

test('правило «обмены без отдачи»: принимаем от любого, если от нас ничего не требуется', async () => {
  const { cfg, db, notify, guard, sent } = makeEnv();
  assert.equal(cfg.risk.acceptFreeOffers, true, 'по умолчанию включено');
  const trades = new TradeHandler({ cfg, db, session: { manager: { on() {} } }, guard, notify, log: silent });
  trades.assignAssetIds = async () => {};
  const id = db.insertItem({ name: 'AK-47 | Redline (Field-Tested)', status: 'trade_sent', buy_price_usd: 10 });
  const res = {};
  const offer = (oid, give, receive) => ({
    id: oid, partner: { getSteamID64: () => `7656${oid}` }, isGlitched: () => false, itemsToGive: give, itemsToReceive: receive,
    accept: (_a, cb) => { res[oid] = 'accepted'; cb(null, 'accepted'); }, decline: (cb) => { res[oid] = 'declined'; cb(null); },
  });
  // незнакомый отправитель, неизвестный предмет, от нас ничего — принимаем
  await withFastTimers(() => trades.onNewOffer(offer('1', [], [{ market_hash_name: 'Random gift' }])));
  assert.equal(res['1'], 'accepted');
  assert.ok(sent.some((m) => /🎁/.test(m.text) && /Random gift/.test(m.text)));
  // смешанный оффер: совпавший с покупкой предмет учитывается в CRM
  await withFastTimers(() => trades.onNewOffer(offer('2', [], [{ market_hash_name: 'AK-47 | Redline (Field-Tested)' }, { market_hash_name: 'Sticker' }])));
  assert.equal(res['2'], 'accepted');
  const it = db.itemsByStatus('protected').find((x) => x.id === Number(id));
  assert.ok(it && it.trade_offer_id === '2');
  // если от нас что-то требуется — по-прежнему отклоняем
  await withFastTimers(() => trades.onNewOffer(offer('3', [{ market_hash_name: 'Knife' }], [{ market_hash_name: 'Gift' }])));
  assert.equal(res['3'], 'declined');
  // правило выключено — неизвестный оффер остаётся без ответа
  cfg.risk.acceptFreeOffers = false;
  await withFastTimers(() => trades.onNewOffer(offer('4', [], [{ market_hash_name: 'Random gift' }])));
  assert.equal(res['4'], undefined);
  assert.equal(guard.state.halted, false);
});

test('исходящий оффер, созданный не ботом → критическая остановка и отмена', async () => {
  const { cfg, db, notify, guard, sent } = makeEnv();
  const trades = new TradeHandler({ cfg, db, session: { manager: { on() {} } }, guard, notify, log: silent });
  let canceled = false;
  await trades.onUnknownOutgoing({ id: '9', state: 2, itemsToGive: [{}], partner: { getSteamID64: () => '666' }, cancel: (cb) => { canceled = true; cb(null); } }, 'test');
  assert.ok(guard.state.halted);
  assert.equal(guard.canBuy, false);
  assert.equal(guard.canSell, false);
  assert.ok(canceled);
  assert.ok(sent.some((m) => m.level === 'critical'));
});

test('неизвестное мобильное подтверждение → отмена и остановка', async () => {
  const { db, guard } = makeEnv();
  const conf = { id: '1', key: 'k', type: 2, creator: '555', title: 'Trade - scam' };
  let rejected = false;
  const session = { getConfirmations: async () => [conf], respondConfirmation: async (_c, accept) => { rejected = !accept; } };
  const w = new ConfirmationWatcher({ session, db, guard, log: silent });
  await w.check();
  assert.ok(rejected);
  assert.ok(guard.state.halted);
  guard.resume('test');
  assert.ok(!guard.state.halted && guard.canBuy);
});

test('баланс: пополнение уведомляет, неизвестная трата останавливает', async () => {
  const { cfg, db, notify, guard, sent } = makeEnv();
  const state = { balance: 100, calls: [], buyBodies: [], purchases: new Map(), nextPid: 1, listings: [] };
  const lis = fakeLis(state);
  const lock = { run: (fn) => fn() };
  const bw = new BalanceWatcher({ cfg, db, lis, guard, notify, log: silent, lock, balanceRef: { value: null } });
  await bw.poll();
  state.balance = 150;
  await bw.poll();
  assert.ok(sent.some((m) => m.level === 'money' && /\+\$50\.00/.test(m.text)), 'уведомление о пополнении');
  // покупка бота на $20 — не тревога
  db.insertPurchase({ custom_id: 'x1', mode: 'scan', requested_usd: 20, total_usd: 20, status: 'pending', ts: Date.now() + 1 });
  state.balance = 130;
  await new Promise((r) => setTimeout(r, 5));
  await bw.poll();
  assert.equal(guard.state.halted, false);
  // кто-то потратил $30 помимо бота
  db.updatePurchase('x1', { status: 'done' });
  state.balance = 100;
  await bw.poll();
  assert.ok(guard.state.halted, 'неизвестная трата → остановка');
  assert.equal(guard.state.haltCode, 'UNKNOWN_SPEND');
});

test('серия ошибок → остановка; RTBA покупает мгновенно новый дешёвый лот', async () => {
  const { cfg, db, notify, guard } = makeEnv();
  for (let i = 0; i < cfg.risk.maxConsecutiveErrors; i++) guard.recordError('lis-search', new Error('boom'));
  assert.ok(guard.state.halted);
  guard.resume('test');

  const NAME = 'AWP | Asiimov (Field-Tested)';
  db.upsertWatch({ name: NAME, source: 'config' });
  seedValuation(db, NAME, 50);
  const state = { balance: 1000, calls: [], buyBodies: [], purchases: new Map(), nextPid: 1, listings: [{ id: 50, name: NAME, price: 42 }] };
  const lis = fakeLis(state);
  const buyer = new Buyer({ cfg, db, lis, valuator: new Valuator({ cfg, market: null, db, fx: { rate: 90 }, log: silent }), guard, notify, log: silent, balanceRef: { value: 1000 } });
  buyer.setBook(NAME, [{ id: '49', name: NAME, price: 42.5, unlockAt: null }]);
  await buyer.onRealtimeListing({ id: '50', name: NAME, price: 42, unlockAt: null }, { receivedAt: Date.now() });
  assert.equal(state.buyBodies.length, 1);
  assert.deepEqual(state.buyBodies[0].ids, [50]);
  assert.equal(buyer.stats.rtbaBuys, 1);
  // дорогой лот вне коридора — не покупаем
  await buyer.onRealtimeListing({ id: '51', name: NAME, price: 47, unlockAt: null }, { receivedAt: Date.now() });
  assert.equal(state.buyBodies.length, 1);
  // журнал решений RTBA для центра: покупка и отказ с причиной
  assert.deepEqual(buyer.rtbaLog.map((r) => r.outcome), ['buy', 'skip']);
  assert.ok(buyer.rtbaLog[0].latencyMs >= 0 && /куплено/.test(buyer.rtbaLog[0].reason));
  const rep = rtbaReport(db, cfg, { buyer, ws: null }, 7);
  assert.equal(rep.recent[0].id, '51');
  assert.equal(rep.buys.length, 1);
  assert.equal(rep.latency.n, 1);
  assert.equal(rep.settings['strategy.minRoi'], cfg.strategy.minRoi);
});

test('dry-run: ничего не покупает по-настоящему', async () => {
  const { cfg, db, notify, guard } = makeEnv();
  cfg.dryRun = true;
  const NAME = 'X';
  db.upsertWatch({ name: NAME, source: 'config' });
  seedValuation(db, NAME, 10);
  const state = { balance: 100, calls: [], buyBodies: [], purchases: new Map(), nextPid: 1, listings: [{ id: 1, name: NAME, price: 8 }] };
  const buyer = new Buyer({ cfg, db, lis: fakeLis(state), valuator: new Valuator({ cfg, market: null, db, fx: { rate: 90 }, log: silent }), guard, notify, log: silent, balanceRef: { value: 100 } });
  await buyer.scan();
  assert.equal(state.buyBodies.length, 0);
  assert.equal(db.db.prepare('SELECT COUNT(*) c FROM items WHERE dry_run=1').get().c, 1);
  assert.equal(summary(db).dryRunStats.n, 1);
});

/** Ускоряем setTimeout (паузы «как человек» в тестах не нужны). */
async function withFastTimers(fn) {
  const orig = globalThis.setTimeout;
  globalThis.setTimeout = (cb, _ms, ...a) => orig(cb, 0, ...a);
  try { return await fn(); } finally { globalThis.setTimeout = orig; }
}

test('LIS WebSocket (Centrifugo): connect, subscribe, ping, публикация нового лота', async () => {
  const { WebSocketServer } = await import('ws');
  const { LisWs } = await import('../src/lis/ws.js');
  const wss = new WebSocketServer({ port: 0 });
  const port = wss.address().port;
  const got = [];
  wss.on('connection', (sock) => {
    sock.on('message', (raw) => {
      const m = JSON.parse(raw.toString());
      if (m.connect) {
        assert.equal(m.connect.token, 'T');
        sock.send(JSON.stringify({ id: m.id, connect: { client: 'c1' } }));
      } else if (m.subscribe) {
        got.push(m.subscribe.channel);
        sock.send(JSON.stringify({ id: m.id, subscribe: {} }) + '\n{}');
        sock.send(JSON.stringify({ push: { channel: m.subscribe.channel, pub: { data: { event: 'obtained_skin_added', payload: { id: 77, name: 'AK-47 | Redline (Field-Tested)', price: 8.5, unlock_at: null } } } } }));
        sock.send(JSON.stringify({ push: { channel: m.subscribe.channel, pub: { data: { event: 'obtained_skin_deleted', payload: { id: 76 } } } } }));
      }
    });
  });
  const ws = new LisWs({ url: `ws://127.0.0.1:${port}`, channels: ['public:obtained-skins'], getToken: async () => 'T', log: silent });
  const listing = new Promise((res) => ws.on('listing', (l, meta) => res({ l, meta })));
  const removed = new Promise((res) => ws.on('removed', res));
  ws.start();
  const { l, meta } = await listing;
  assert.equal(l.id, '77');
  assert.equal(l.price, 8.5);
  assert.ok(meta.receivedAt > 0);
  assert.equal((await removed).id, '76');
  assert.deepEqual(got, ['public:obtained-skins']);
  ws.stop();
  wss.close();
});
