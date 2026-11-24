import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import TradeOfferManager from 'steam-tradeoffer-manager';
import { openDb } from '../src/db.js';
import { Guard } from '../src/engine/guard.js';
import { Buyer } from '../src/engine/buyer.js';
import { TradeHandler } from '../src/engine/trades.js';
import { ConfirmationWatcher } from '../src/engine/confirmations.js';
import { validateConfig, saveSettings, loadConfig } from '../src/config.js';
import { CsgoMarketClient } from '../src/arb/csgoClient.js';
import { ArbScanner } from '../src/arb/scanner.js';
import { ArbSeller } from '../src/arb/seller.js';
import { netFactor, valueItem, listPrice } from '../src/arb/math.js';

const EState = TradeOfferManager.ETradeOfferState;
const DAY = 86400_000;
const baseCfg = JSON.parse(fs.readFileSync(new URL('../config.example.json', import.meta.url)));
const silent = { info() {}, warn() {}, error() {}, crit() {}, debug() {}, child() { return silent; } };
const NAME = 'StatTrak™ M4A4 | Tooth Fairy (Battle-Scarred)';
const near = (x, y, eps = 1e-6) => assert.ok(Math.abs(x - y) < eps, `${x} ≈ ${y}`);

function env({ dryRun = false } = {}) {
  const cfg = structuredClone(baseCfg);
  Object.assign(cfg, { dryRun, timezone: 'UTC', trade: { partner: '1', token: 't' } });
  cfg.arbitrage.enabled = true;
  cfg.arbitrage.inventoryWaitMs = 0;
  const db = openDb(fs.mkdtempSync(path.join(os.tmpdir(), 'lsb-arb-')), ':memory:');
  const sent = [];
  const notify = { send: async (level, text) => { sent.push({ level, text }); return true; } };
  return { cfg, db, sent, notify, guard: new Guard({ db, notify, log: silent, cfg }) };
}

const week = (price, n) => Array.from({ length: n }, (_, i) => ({ ts: Date.now() - (i % 7) * DAY - 1000, price }));

test('математика: комиссии перемножаются, заявка не ниже аска не считается, худший случай ограничивает цену', () => {
  const a = baseCfg.arbitrage;
  near(netFactor({ saleFeePct: 0.05, withdrawFeePct: 0.05 }), 0.9025);
  near(netFactor({ saleFeePct: 0.05, withdrawFeePct: 0.05, payoutFxLossPct: 0.01, lisDepositFeePct: 0.1 }), 0.9025 * 0.99 / 1.1);
  const base = { name: NAME, ask: 10, bid: 8, bidCount: 20, a, minRoi: 0.05 };
  assert.equal(valueItem(base).mode, 'bid');
  near(valueItem(base).netUsd, 8 * netFactor(a));
  assert.equal(valueItem({ ...base, bid: 10 }).ok, false);
  assert.equal(valueItem({ ...base, bidCount: 1 }).ok, false);
  assert.equal(valueItem({ ...base, history: week(9.5, 1) }).ok, false); // почти не торгуется
  const v = valueItem({ ...base, history: week(9.5, 21) });
  assert.equal(v.mode, 'list');
  near(v.expectedSell, 9.5);
  // купив по netUsd/(1+minRoi), даже продажа в заявку теряет не больше maxDownsidePct
  const maxBuy = v.netUsd / 1.05;
  assert.ok(v.bidNetUsd / maxBuy >= 1 - a.maxDownsidePct - 1e-9);
});

test('цена выставления: подрезаем аск, не ниже себестоимости, близкая заявка — продаём сразу', () => {
  const a = baseCfg.arbitrage;
  near(listPrice({ costUsd: 5, ask: 8, bid: 7, a }).price, 8 * (1 - a.undercutPct));
  assert.equal(listPrice({ costUsd: 5, ask: 8, bid: 7.95, a }).mode, 'bid');
  const f = listPrice({ costUsd: 8, ask: 8, bid: 7, a });
  assert.equal(f.mode, 'floor');
  near(f.price * netFactor(a), 8);
  assert.equal(listPrice({ costUsd: 5, ask: 8, bid: 9, a }).mode, 'ask'); // неисполнимая заявка
});

test('клиент: единицы валют, ключ не попадает в ошибки, без ключа приватные методы закрыты', async () => {
  const urls = [];
  const c = new CsgoMarketClient({ apiKey: 'SECRET', minDelayMs: 0, log: silent, fetchImpl: async (u) => { urls.push(String(u)); return { status: 200, ok: true, text: async () => JSON.stringify({ success: true, item_id: 77 }) }; } });
  assert.equal(await c.addToSale('1', 6.757, 'USD'), '77');
  assert.match(urls[0], /price=6757&cur=USD/);
  const bad = new CsgoMarketClient({ apiKey: 'SECRET', minDelayMs: 0, log: silent, fetchImpl: async () => { throw new Error('fetch failed'); } });
  await assert.rejects(bad.money(), (e) => e.code === 'NETWORK' && !e.message.includes('SECRET'));
  await assert.rejects(new CsgoMarketClient({ minDelayMs: 0, log: silent }).money(), (e) => e.code === 'NO_KEY');
  for (const x of [c, bad]) x.sched.stop();
});

const feeds = (over = {}) => ({
  hasKey: false,
  prices: async () => new Map([[NAME, { price: 8.2, count: 27 }], ['Bad', { price: 2, count: 5 }], ['Fake bid', { price: 5, count: 9 }], ['X | Doppler', { price: 9, count: 9 }]]),
  orders: async () => new Map([[NAME, { price: 6.757, count: 29 }], ['Bad', { price: 1.2, count: 50 }], ['Fake bid', { price: 6, count: 90 }], ['X | Doppler', { price: 8, count: 90 }]]),
  ...over,
});
const lisFeed = async () => new Map([[NAME, { price: 5.57, count: 221 }], ['Bad', { price: 2, count: 100 }], ['Fake bid', { price: 3, count: 100 }], ['X | Doppler', { price: 3, count: 90 }]]);

test('сканер: берёт только исполнимые связки и кормит закупщика', async () => {
  const { cfg, db, guard, notify } = env();
  const arb = new ArbScanner({ cfg, db, client: feeds(), log: silent, lisFeed, guard });
  const picks = await arb.scan();
  assert.deepEqual(picks.map((p) => p.name), [NAME]);
  assert.ok(picks[0].roi > 0.05);
  assert.deepEqual([...arb.watchMap().keys()], [NAME]);
  assert.equal(arb.cached('Bad'), null);

  const bodies = [];
  const lis = {
    search: async () => ({ items: [{ id: '1', name: NAME, price: 5.57, unlockAt: null }, { id: '2', name: NAME, price: 5.58, unlockAt: null }, { id: '3', name: NAME, price: 7, unlockAt: null }], nextCursor: null }),
    buy: async (b) => { bodies.push(b); return { purchase_id: 9, skins: [{ id: 1, name: NAME, price: 5.57, status: 'processing' }] }; },
  };
  const buyer = new Buyer({ cfg: Object.create(cfg, { strategy: { get: () => ({ ...cfg.strategy, ...cfg.arbitrage.strategy }) } }), db, lis, valuator: arb, guard, notify, log: silent, balanceRef: { value: 114 } });
  buyer.watchMap = () => arb.watchMap();
  buyer.rank = (n) => arb.roi(n);
  await buyer.scan();
  assert.equal(bodies.length, 1);
  assert.deepEqual(bodies[0].ids, ['1']);
  await buyer.scan(); // лимит 1 шт. в день
  assert.equal(bodies.length, 1);
  assert.equal(db.itemsByStatus('bought').length, 1);
});

test('сканер с ключом: история и стакан; аккаунт не в USD — только мгновенная продажа и предупреждение', async () => {
  const { cfg, db, guard, sent } = env();
  const keyed = (currency) => feeds({
    hasKey: true, money: async () => ({ money: 0, currency }),
    history: async () => new Map([[NAME, week(7.6, 30)]]),
    bidAsk: async () => ({ bid: [{ price: 6.9, total: 1 }, { price: 6.7, total: 12 }], ask: [{ price: 8.1, total: 1 }] }),
  });
  const usd = new ArbScanner({ cfg, db, client: keyed('USD'), log: silent, lisFeed, guard });
  const p = (await usd.scan())[0];
  assert.equal(p.mode, 'list');
  assert.equal(p.weeklySales, 30);
  near(p.expectedSell, 7.6);
  assert.equal(p.bid, 6.7); // одиночная верхняя заявка не считается
  const rub = new ArbScanner({ cfg, db, client: keyed('RUB'), log: silent, lisFeed, guard });
  assert.equal((await rub.scan())[0].mode, 'bid');
  assert.ok(sent.some((s) => /RUB/.test(s.text)));
});

function sellEnv(opts) {
  const e = env(opts);
  const steam = { offers: new Map(), confs: [], responded: [], next: 500 };
  class Offer {
    constructor(url) { const p = new URL(url).searchParams.get('partner'); const id = String(76561197960265728n + BigInt(p)); this.partner = { getSteamID64: () => id }; this.itemsToGive = []; this.itemsToReceive = []; }
    addMyItems(x) { this.itemsToGive.push(...x); }
    setMessage(m) { this.message = m; }
    send(cb) { this.id = String(steam.next++); this.state = EState.CreatedNeedsConfirmation; steam.offers.set(this.id, this); steam.confs.push({ id: `c${this.id}`, type: 2, creator: this.id, title: 't' }); cb(null, 'pending'); }
    cancel(cb) { this.state = EState.Canceled; cb(null); }
  }
  const session = {
    ready: true, steamId: '76561198000000001',
    manager: { createOffer: (u) => new Offer(u), getOffer: (id, cb) => cb(null, steam.offers.get(id)), on() {} },
    request: async () => ({ data: { webapi_token: 'TOK' } }),
    getConfirmations: async () => steam.confs,
    respondConfirmation: async (c, ok) => { steam.responded.push({ id: c.id, ok }); steam.confs = steam.confs.filter((x) => x !== c); const o = steam.offers.get(String(c.creator)); if (o && ok) o.state = EState.Active; },
  };
  const mk = { hasKey: true, sale: [], gives: [], ready: [], hist: [], pings: [] };
  Object.assign(mk, {
    money: async () => ({ money: 1, currency: 'USD' }), updateInventory: async () => ({}),
    bidAsk: async () => ({ bid: [{ price: 6.7, total: 12 }], ask: [{ price: 8.1, total: 1 }] }),
    addToSale: async (assetId, price) => { mk.sale.push({ itemId: '900', assetId, name: NAME, price, status: 1, settlement: 0 }); return '900'; },
    items: async () => mk.sale, giveRequests: async () => mk.gives, tradeReady: async (id) => mk.ready.push(id), saleHistory: async () => mk.hist, ping: async (t) => { mk.pings.push(t); return { success: true, online: true }; },
  });
  const conf = new ConfirmationWatcher({ session, db: e.db, guard: e.guard, log: silent });
  const seller = new ArbSeller({ ...e, session, market: null, conf, valuator: null, fx: { rate: 1 }, log: silent, client: mk });
  const trades = new TradeHandler({ ...e, session, log: silent });
  trades.arb = seller;
  const id = e.db.insertItem({ name: NAME, status: 'ready', buy_price_usd: 5.57 });
  e.db.updateItem(id, { asset_id: '4242' });
  return { ...e, steam, mk, conf, seller, trades, id, session };
}
const give = (over = {}) => ({ partner: 777, token: 'tk', hash: 'h1', tradeoffermessage: 'm', items: [{ appid: 730, contextid: 2, assetid: 4242, amount: 1 }], ...over });

test('продажа: выставление → обмен под продажу → подтверждение только своего → холд → оплата', async () => {
  const x = sellEnv();
  await x.seller.listReady();
  assert.equal(x.db.item(x.id).status, 'listed');
  assert.equal(x.db.item(x.id).listing_id, 'mcsgo:900');
  await x.seller.deliver();
  assert.equal(x.steam.offers.size, 0); // никто не купил — ничего не отдаём

  x.mk.sale[0].status = 2;
  x.mk.gives = [give()];
  await x.seller.deliver();
  assert.equal(x.steam.offers.size, 1);
  const o = [...x.steam.offers.values()][0];
  assert.deepEqual(x.steam.responded, [{ id: `c${o.id}`, ok: true }]);
  assert.deepEqual(x.mk.ready, [o.id]);
  assert.equal(x.db.item(x.id).status, 'delivering');
  await x.seller.deliver(); // повторно не отправляем
  assert.equal(x.steam.offers.size, 1);

  await x.trades.onUnknownOutgoing(o, 'audit');
  assert.equal(x.guard.state.halted, false); // свой обмен — не тревога
  o.state = EState.Accepted;
  await x.trades.onUnknownOutgoing(o, 'sentOfferChanged');
  assert.equal(x.db.item(x.id).status, 'settling');
  assert.ok(x.db.exposureUsd() > 5); // деньги ещё не пришли — считаются в обороте

  x.mk.sale = [];
  x.mk.hist = [{ itemId: '900', stage: 2, received: 7.6, currency: 'USD' }];
  await x.seller.syncMarket();
  const it = x.db.item(x.id);
  assert.equal(it.status, 'sold');
  near(it.sold_usd, 7.6 * 0.95 * 0.99);
});

test('чужая продажа, чужой предмет, другой appid — обмен не отправляется', async () => {
  const x = sellEnv();
  await x.seller.listReady();
  x.mk.sale[0].status = 2;
  x.mk.gives = [give({ items: [{ appid: 730, assetid: 9999 }] }), give({ hash: 'h2', items: [{ appid: 440, assetid: 4242 }] }), give({ hash: 'h3', items: [{ appid: 730, assetid: 4242 }, { appid: 730, assetid: 1 }] })];
  await x.seller.deliver();
  assert.equal(x.steam.offers.size, 0);
  assert.ok(x.sent.some((s) => /Обмен не отправлен/.test(s.text)));
});

test('продажа сорвалась — висящий обмен отменяется, предмет возвращается в продажу', async () => {
  const x = sellEnv();
  await x.seller.listReady();
  x.mk.sale[0].status = 2;
  x.mk.gives = [give()];
  await x.seller.deliver();
  const [oid, o] = [...x.steam.offers.entries()][0];
  x.mk.sale = [];
  x.mk.gives = [];
  x.seller.state.offers[oid].ts -= 120_000;
  await x.seller.deliver();
  assert.equal(o.state, EState.Canceled);
  assert.equal(x.db.item(x.id).status, 'listed');
  x.db.db.prepare('UPDATE items SET updated_at=updated_at-3600000').run();
  await x.seller.syncMarket();
  assert.equal(x.db.item(x.id).status, 'ready');
});

test('посторонний исходящий обмен по-прежнему тревога, подменённый состав — тоже', async () => {
  const x = sellEnv();
  await x.seller.listReady();
  x.mk.sale[0].status = 2;
  x.mk.gives = [give()];
  await x.seller.deliver();
  const o = [...x.steam.offers.values()][0];
  o.itemsToGive.push({ assetid: '1' });
  await x.trades.onUnknownOutgoing(o, 'audit');
  assert.equal(x.guard.state.halted, true);
});

test('свой обмен узнаётся после перезапуска', async () => {
  const x = sellEnv();
  await x.seller.listReady();
  x.mk.sale[0].status = 2;
  x.mk.gives = [give()];
  await x.seller.deliver();
  const o = [...x.steam.offers.values()][0];
  const again = new ArbSeller({ cfg: x.cfg, db: x.db, session: x.session, conf: x.conf, guard: x.guard, notify: x.notify, log: silent, client: x.mk });
  assert.equal(again.isOwnOffer(o), true);
});

test('dry-run и выключенный токен: ничего не выставляем, не отдаём и не пингуем', async () => {
  const x = sellEnv({ dryRun: true });
  await x.seller.listReady();
  assert.equal(x.db.item(x.id).status, 'ready');
  const y = sellEnv();
  await y.seller.listReady();
  await y.seller.ping();
  assert.equal(y.mk.pings.length, 0);
  y.cfg.arbitrage.sendSteamToken = true;
  await y.seller.ping();
  assert.deepEqual(y.mk.pings, ['TOK']);
});

test('настройки: ключ нужен в боевом режиме, по умолчанию режим выключен', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lsb-arbcfg-'));
  const cfg = loadConfig({ configPath: path.join(dir, 'c.json'), env: {}, dataDir: dir });
  assert.equal(cfg.arbitrage.enabled, false);
  assert.equal(cfg.arbitrage.sendSteamToken, false);
  cfg.arbitrage.enabled = true;
  assert.ok(!validateConfig(cfg).some((e) => /MARKET_CSGO/.test(e))); // dry-run без ключа
  cfg.dryRun = false;
  assert.ok(validateConfig(cfg).some((e) => /MARKET_CSGO/.test(e)));
  assert.deepEqual(saveSettings(cfg, { 'arbitrage.strategy.minRoi': 0.08 }), []);
  assert.equal(cfg.arbitrage.strategy.minRoi, 0.08);
});

test('бот целиком в dry-run: ищет связки без Steam и ключа, записывает симуляцию, ничего не покупает', async () => {
  const { Bot } = await import('../src/index.js');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lsb-arbbot-'));
  const cfg = loadConfig({ configPath: path.join(dir, 'c.json'), env: {}, dataDir: dir });
  cfg.arbitrage.enabled = true;
  cfg.telegram.enabled = false;
  cfg.dashboard.enabled = false;
  cfg.strategy.rtbaEnabled = false;
  cfg.trade = { partner: '1', token: 't' };
  Object.assign(cfg.secrets, { lisApiKey: 'k', sharedSecret: 'ZGVtby1kZW1vLWRlbW8tZGVtbzE=', identitySecret: 'ZGVtby1kZW1vLWRlbW8tZGVtbzE=' });
  const bot = new Bot(cfg, { mcsgo: feeds(), lisFeed, arbDelayMs: 0 });
  bot.ensureSteam = async () => {};
  let bought = 0;
  bot.lis = { balance: async () => 114, search: async () => ({ items: [{ id: '1', name: NAME, price: 5.57, unlockAt: null }], nextCursor: null }), buy: async () => { bought++; }, sched: { stats: {}, stop() {} } };
  await bot.start();
  for (let i = 0; i < 50 && !bot.arb.lastScanAt; i++) await new Promise((r) => setTimeout(r, 20));
  assert.equal(bot.arb.picks.length, 1);
  bot.lis.balance && (bot.balanceRef.value = 114);
  await bot.buyer.scan();
  assert.equal(bought, 0);
  assert.equal(bot.db.db.prepare('SELECT COUNT(*) c FROM items WHERE dry_run=1').get().c, 1);
  assert.equal(bot.runtimeStats().arbitrage.picks.length, 1);
  await bot.stop('тест');
});
