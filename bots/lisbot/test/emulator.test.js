import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { openDb } from '../src/db.js';
import { Guard } from '../src/engine/guard.js';
import { LisClient } from '../src/lis/client.js';
import { LisWs } from '../src/lis/ws.js';
import { SteamMarket } from '../src/steam/market.js';
import { FxService, Valuator } from '../src/engine/pricing.js';
import { Buyer } from '../src/engine/buyer.js';
import { PurchaseTracker } from '../src/engine/purchaseTracker.js';
import { TradeHandler } from '../src/engine/trades.js';
import { ConfirmationWatcher } from '../src/engine/confirmations.js';
import { Seller } from '../src/engine/seller.js';
import { DemoWorld } from '../src/emu/world.js';
import { DemoSteamSession } from '../src/emu/steam.js';
import { startLisEmulator } from '../src/emu/lisServer.js';
import { demoConfig } from '../src/emu/demo.js';
import { validateConfig } from '../src/config.js';

// Интеграционные тесты LisBot на эмуляторе LIS-SKINS и Steam (src/emu): настоящий HTTP и WebSocket,
// настоящие модули закупки, приёма трейдов и продажи — без фейков внутри бота.

const baseCfg = JSON.parse(fs.readFileSync(new URL('../config.example.json', import.meta.url)));
const silent = { info() {}, warn() {}, error() {}, crit() {}, debug() {}, child() { return silent; } };
const AK = 'AK-47 | Redline (Field-Tested)';

function makeEnv() {
  const cfg = structuredClone(baseCfg);
  Object.assign(cfg, { dryRun: false, timezone: 'UTC', trade: { partner: '1', token: 'demo' } });
  cfg.sell.minListingGapSec = 0;
  cfg.steam.activeHours.enabled = false;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lsb-emu-'));
  const db = openDb(dir, ':memory:');
  const sent = [];
  const notify = { send: async (level, text) => { sent.push({ level, text }); return true; } };
  const guard = new Guard({ db, notify, log: silent, cfg });
  return { cfg, db, notify, sent, guard };
}

async function withFastTimers(fn) {
  const orig = globalThis.setTimeout;
  globalThis.setTimeout = (cb, _ms, ...a) => orig(cb, 0, ...a);
  try { return await fn(); } finally { globalThis.setTimeout = orig; }
}

async function until(pred, ms = 5000) {
  for (let t = 0; t < ms; t += 20) {
    if (await pred()) return true;
    await new Promise((r) => setTimeout(r, 20));
  }
  return false;
}

test('эмулятор LIS-SKINS: баланс, поиск, покупка с max_price и custom_id, статусы покупки', async (t) => {
  const world = new DemoWorld({ tradeDelayMs: 30, balance: 20 });
  const srv = await startLisEmulator(world);
  t.after(() => { world.stop(); return srv.close(); });
  const lis = new LisClient({ apiKey: 'k', baseUrl: srv.baseUrl, requestsPerSecond: 1000, log: silent });
  t.after(() => lis.sched.stop());

  assert.equal(await lis.balance(), 20);
  const { items } = await lis.search({ game: 'csgo', names: [AK], sortBy: 'lowest_price' });
  assert.equal(items.length, 6);
  assert.ok(items.every((l, i) => l.name === AK && (i === 0 || l.price >= items[i - 1].price)));

  await assert.rejects(lis.buy({ ids: [items[0].id], partner: '1', token: 't', maxPrice: items[0].price - 0.01, customId: 'c0' }), /max_price/);
  const r = await lis.buy({ ids: [items[0].id], partner: '1', token: 't', maxPrice: items[0].price, customId: 'c1' });
  assert.equal(r.skins.length, 1);
  assert.equal(await lis.balance(), Math.round((20 - items[0].price) * 100) / 100);
  await assert.rejects(lis.buy({ ids: [items[1].id, items[2].id], partner: '1', token: 't', customId: 'c2' }), /Insufficient balance/);
  assert.equal((await lis.buy({ ids: [items[0].id], partner: '1', token: 't', customId: 'c3' })).skins.length, 0, 'проданный лот пропускается');

  assert.ok(await until(async () => (await lis.purchaseInfo({ customIds: ['c1'] }))[0]?.skins[0].status === 'trade_sent'));
  const offer = [...world.offers.values()][0];
  assert.deepEqual(offer.items.map((i) => i.market_hash_name), [AK]);
  await assert.rejects(new LisClient({ apiKey: '', baseUrl: srv.baseUrl, log: silent }).balance(), /авторизация/);
});

test('эмулятор LIS-SKINS: WebSocket Centrifugo присылает новые и снятые лоты', async (t) => {
  const world = new DemoWorld();
  const srv = await startLisEmulator(world);
  const lis = new LisClient({ apiKey: 'k', baseUrl: srv.baseUrl, requestsPerSecond: 1000, log: silent });
  const ws = new LisWs({ url: srv.wsUrl, channels: ['public:obtained-skins'], getToken: () => lis.wsToken(), log: silent });
  t.after(() => { ws.stop(); lis.sched.stop(); world.stop(); return srv.close(); });
  const got = [];
  const removed = [];
  ws.on('listing', (l) => got.push(l));
  ws.on('removed', (x) => removed.push(x.id));
  ws.start();
  assert.ok(await until(() => ws.connected));
  await new Promise((r) => setTimeout(r, 50));              // подписка на канал
  const l = world.addListing(AK);
  assert.ok(await until(() => got.some((x) => x.id === l.id)));
  assert.equal(got.find((x) => x.id === l.id).price, l.price);
  world.removeListing(l.id);
  assert.ok(await until(() => removed.includes(l.id)));
});

test('полный цикл на эмуляторе: скан → покупка → трейд LIS → Trade Protection → ТП → подтверждение → продажа', async (t) => {
  const { cfg, db, notify, sent, guard } = makeEnv();
  const world = new DemoWorld({ tradeDelayMs: 0, protectionMs: 1500, saleMs: [800, 800] });
  const srv = await startLisEmulator(world);
  const lis = new LisClient({ apiKey: 'k', baseUrl: srv.baseUrl, requestsPerSecond: 1000, log: silent });
  const session = new DemoSteamSession({ world, cfg, log: silent });
  t.after(() => { session.stop(); lis.sched.stop(); world.stop(); return srv.close(); });
  await session.login();

  db.upsertWatch({ name: AK, source: 'config' });
  const market = new SteamMarket({ session, db, cfg, log: silent });
  const fx = new FxService({ cfg, market, db, guard, log: silent });
  assert.ok(Math.abs(await fx.refresh() - world.fx) < 1, 'курс из Steam ≈ курсу эмулятора');
  const valuator = new Valuator({ cfg, market, db, fx, log: silent });
  const v = await valuator.refresh(AK);
  assert.ok(v.ok, v.reason);
  assert.ok(Math.abs(v.netUsd - 12.9) < 0.6, `оценка ${v.netUsd}`);

  const balanceRef = { value: await lis.balance() };
  const buyer = new Buyer({ cfg, db, lis, valuator, guard, notify, log: silent, balanceRef });
  const tracker = new PurchaseTracker({ cfg, db, lis, guard, log: silent, notify });
  const trades = new TradeHandler({ cfg, db, session, guard, notify, log: silent });
  const offers = [];
  session.manager.on('newOffer', (o) => offers.push(o));       // обработчик вызываем сами, с ускоренными таймерами

  await buyer.scan();
  const bought = db.itemsByStatus('bought');
  assert.ok(bought.length >= 1, 'куплены лоты ниже порога');
  assert.ok(bought.every((i) => i.buy_price_usd <= v.maxBuyUsd + 1e-9));
  assert.ok(balanceRef.value < 1840.25);

  const purchases = db.pendingPurchases().length;                // по обмену на каждую покупку
  assert.ok(await until(() => offers.length >= purchases));
  await tracker.poll();
  assert.equal(db.itemsByStatus('trade_sent').length, bought.length, 'LIS отправил трейд');

  // приём обмена: обработчик ждёт «как человек» — ускоряем таймеры
  await withFastTimers(() => Promise.all(offers.map((o) => trades.onNewOffer(o))));
  const prot = db.itemsByStatus('protected');
  assert.equal(prot.length, bought.length);
  assert.ok(prot.every((p) => p.asset_id), 'assetid получены из обмена');
  await tracker.poll();
  assert.equal(db.pendingPurchases().length, 0, 'покупка на LIS завершена');

  const conf = new ConfirmationWatcher({ session, db, guard, log: silent });
  const seller = new Seller({ cfg, db, session, market, conf, valuator, fx, guard, notify, log: silent });
  await seller.syncInventory(true);
  assert.equal(db.itemsByStatus('ready').length, 0, 'пока идёт Trade Protection, не продаём');
  await new Promise((r) => setTimeout(r, 1600));
  await seller.syncInventory(true);
  assert.equal(db.itemsByStatus('ready').length, bought.length);

  await withFastTimers(() => seller.listReady());
  assert.equal(db.itemsByStatus('listed').length, bought.length, 'выставлены и подтверждены');
  assert.equal(world.confirmations.size, 0);
  await new Promise((r) => setTimeout(r, 900));
  await seller.syncListings();
  const sold = db.itemsByStatus('sold');
  assert.equal(sold.length, bought.length);
  assert.ok(sold.every((s) => s.profit_usd > 0), 'продано в плюс');
  assert.ok(sent.some((m) => m.level === 'money' && /Продано/.test(m.text)));
  assert.equal(guard.state.halted, false, guard.state.haltReason || '');
});

test('эмулятор Steam: чужое мобильное подтверждение отменяется, своё — принимается', async (t) => {
  const { cfg, db, guard } = makeEnv();
  const world = new DemoWorld({ protectionMs: 0 });
  const session = new DemoSteamSession({ world, cfg, log: silent });
  t.after(() => { session.stop(); world.stop(); });
  world.inventory.set('a1', { assetid: 'a1', name: AK, protectedUntil: 0 });
  world.inventory.set('a2', { assetid: 'a2', name: AK, protectedUntil: 0 });
  assert.equal(world.sellItem('a1', 1000).requires_confirmation, 1);
  assert.equal(world.sellItem('a2', 1000).requires_confirmation, 1);
  const [mine, foreign] = [...world.market.values()];
  const conf = new ConfirmationWatcher({ session, db, guard, log: silent });
  conf.expectListing(mine.listingid, 1);
  await conf.check();
  assert.equal(world.market.get(mine.listingid).confirmed, true);
  assert.equal(world.market.has(foreign.listingid), false, 'чужой лот отменён, предмет вернулся');
  assert.ok(world.inventory.has('a2'));
  assert.equal(guard.state.halted, true);
});

test('демо-режим: ключи не нужны, конфиг проходит проверку, Telegram выключен', () => {
  const cfg = structuredClone(baseCfg);
  cfg.secrets = { dashboardToken: '0123456789abcdef0123' };
  cfg.dashboard.enabled = true;
  cfg.telegram.enabled = true;
  demoConfig(cfg);
  assert.deepEqual(validateConfig(cfg), []);
  assert.equal(cfg.dryRun, false);
  assert.equal(cfg.telegram.enabled, false);
  assert.equal(cfg.secrets.dashboardToken, '0123456789abcdef0123');
});

test('демо поверх базы центра: незавершённые сделки не противоречат эмуляции', () => {
  const { db } = makeEnv();
  const now = Date.now();
  const add = (status, extra = {}) => db.insertItem({ lis_item_id: null, custom_id: null, name: AK, mode: 'rtba', status, buy_price_usd: 11, ...extra });
  add('trade_sent');
  const p = add('protected', { received_at: now - 1000 });
  db.updateItem(p, { tradable_after: now + 10 * 86400_000 });
  add('listed', { received_at: now - 1000 });
  const world = new DemoWorld({ tradeDelayMs: 0, protectionMs: 60_000 });
  world.adoptDb(db);
  const inv = world.inventoryItems();
  assert.equal(inv.length, 2, 'полученные предметы — в инвентаре');
  assert.equal(inv.filter((i) => !i.marketable).length, 1, 'защищённый ещё не продаётся (защита сжата до демо-срока)');
  world.stop();
});
