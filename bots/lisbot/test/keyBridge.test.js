import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import TradeOfferManager from 'steam-tradeoffer-manager';
import { openDb } from '../src/db.js';
import { Guard } from '../src/engine/guard.js';
import { TradeHandler } from '../src/engine/trades.js';
import { ConfirmationWatcher } from '../src/engine/confirmations.js';
import { KeyBridge } from '../src/engine/keyBridge.js';
import { saveSettings, validateConfig } from '../src/config.js';

const EState = TradeOfferManager.ETradeOfferState;
const baseCfg = JSON.parse(fs.readFileSync(new URL('../config.example.json', import.meta.url)));
const silent = { info() {}, warn() {}, error() {}, crit() {}, debug() {}, child() { return silent; } };
const KEY = 'Mann Co. Supply Crate Key';
const MANN_URL = 'https://steamcommunity.com/tradeoffer/new/?partner=12345&token=AbCdEf';
const MANN_ID = String(76561197960265728n + 12345n);

function makeEnv({ dryRun = false, buy = false } = {}) {
  const cfg = structuredClone(baseCfg);
  Object.assign(cfg, { dryRun, timezone: 'UTC' });
  Object.assign(cfg.keyBridge, { enabled: true, tradeUrl: MANN_URL });
  Object.assign(cfg.keyBridge.buy, { enabled: buy, maxKeyPrice: 150, keysPerDay: 10, maxOrderKeys: 50, walletReserve: 100 });
  const db = openDb(fs.mkdtempSync(path.join(os.tmpdir(), 'lsb-kb-')), ':memory:');
  const sent = [];
  const notify = { send: async (level, text) => { sent.push({ level, text }); return true; } };
  const guard = new Guard({ db, notify, log: silent, cfg });

  const steam = { offers: new Map(), confs: [], responded: [], nextOffer: 900, inv: [] };
  class FakeOffer {
    constructor(partnerId) { this.partner = { getSteamID64: () => partnerId }; this.itemsToGive = []; this.itemsToReceive = []; this.state = EState.Active; }
    addMyItems(items) { this.itemsToGive.push(...items); }
    setMessage(m) { this.message = m; }
    send(cb) {
      this.id = String(steam.nextOffer++);
      this.state = EState.CreatedNeedsConfirmation;
      steam.offers.set(this.id, this);
      steam.confs.push({ id: `c${this.id}`, key: 'k', type: 2, creator: this.id, title: 'trade' });
      cb(null, 'pending');
    }
    cancel(cb) { this.state = EState.Canceled; cb(null); }
  }
  const manager = {
    createOffer: (url) => { assert.equal(url, MANN_URL); return new FakeOffer(MANN_ID); },
    getOffer: (id, cb) => cb(null, steam.offers.get(id)),
    on() {},
  };
  const session = {
    ready: true, steamId: '76561198000000001', manager, wallet: { balance: 1000 },
    inventory: async (appid, ctx, tradable) => { assert.equal(appid, 440); assert.equal(tradable, true); return steam.inv; },
    getConfirmations: async () => steam.confs,
    respondConfirmation: async (c, ok) => { steam.responded.push({ id: c.id, ok }); steam.confs = steam.confs.filter((x) => x !== c); const o = steam.offers.get(String(c.creator)); if (o && ok) o.state = EState.Active; },
  };
  const market = {
    orders: [],
    orderBook: async () => ({ highestBuy: 140, lowestSell: 160 }),
    createBuyOrder: async (o) => { market.orders.push(o); return { success: 1, buy_orderid: 'B1' }; },
    buyOrderStatus: async () => ({ success: 1, active: 1, purchased: market.purchased || 0 }),
    cancelBuyOrder: async () => ({ success: 1 }),
  };
  const conf = new ConfirmationWatcher({ session, db, guard, log: silent, strict: true });
  const bridge = new KeyBridge({ cfg, db, session, market, conf, guard, notify, log: silent });
  const trades = new TradeHandler({ cfg, db, session, guard, notify, log: silent });
  trades.bridge = bridge;
  return { cfg, db, sent, guard, steam, session, market, conf, bridge, trades, FakeOffer };
}

const key = (id, tradable = true) => ({ assetid: String(id), market_hash_name: KEY, tradable });

test('мост ключей: отправляет передаваемые ключи на MannBot и подтверждает только свой обмен', async () => {
  const env = makeEnv();
  env.steam.inv = [key(1), key(2), { assetid: '3', market_hash_name: 'Strange Scattergun', tradable: true }];
  await env.bridge.transfer(env.bridge.partner);
  const [offer] = env.steam.offers.values();
  assert.deepEqual(offer.itemsToGive.map((i) => i.assetid), ['1', '2']);
  assert.equal(env.steam.responded.length, 1);
  assert.equal(env.steam.responded[0].id, `c${offer.id}`);
  assert.equal(env.guard.state.halted, false);
  // события опроса по своему офферу — не тревога
  await env.trades.onUnknownOutgoing(offer, 'sentOfferChanged');
  assert.equal(env.guard.state.halted, false);
  offer.state = EState.Accepted;
  await env.trades.onUnknownOutgoing(offer, 'sentOfferChanged');
  assert.equal(env.bridge.stats.delivered, 2);
  assert.ok(env.sent.some((m) => /MannBot принял 2/.test(m.text)));
  // ключи, уже ушедшие в открытый обмен, повторно не отправляются
  const env2 = makeEnv();
  env2.steam.inv = [key(5)];
  await env2.bridge.transfer(env2.bridge.partner);
  await env2.bridge.transfer(env2.bridge.partner);
  assert.equal(env2.steam.offers.size, 1);
});

test('мост ключей: чужой исходящий обмен по-прежнему = взлом, даже на тот же аккаунт MannBot', async () => {
  const env = makeEnv();
  env.steam.inv = [key(1)];
  await env.bridge.transfer(env.bridge.partner);
  const [own] = env.steam.offers.values();
  // тот же id, но отдаём другой предмет
  const forged = new env.FakeOffer(MANN_ID);
  forged.id = own.id; forged.itemsToGive = [{ assetid: '777' }];
  await env.trades.onUnknownOutgoing(forged, 'sentOfferChanged');
  assert.equal(env.guard.state.haltCode, 'UNKNOWN_OUTGOING_TRADE');

  const env2 = makeEnv();
  const foreign = new env2.FakeOffer(MANN_ID);
  foreign.id = '555'; foreign.itemsToGive = [{ assetid: '1' }];
  await env2.trades.onUnknownOutgoing(foreign, 'unknownOfferSent');
  assert.equal(env2.guard.state.haltCode, 'UNKNOWN_OUTGOING_TRADE');

  // и чужое подтверждение обмена отменяется
  const env3 = makeEnv();
  env3.steam.confs.push({ id: 'cx', key: 'k', type: 2, creator: '999', title: 'trade' });
  await env3.conf.check();
  assert.equal(env3.guard.state.haltCode, 'UNKNOWN_CONFIRMATION');
  assert.deepEqual(env3.steam.responded, [{ id: 'cx', ok: false }]);
});

test('мост ключей: DRY-RUN и выключенный мост ничего не отправляют', async () => {
  const env = makeEnv({ dryRun: true });
  env.steam.inv = [key(1)];
  await env.bridge.cycle();
  assert.equal(env.steam.offers.size, 0);
  const env2 = makeEnv();
  env2.cfg.keyBridge.enabled = false;
  env2.steam.inv = [key(1)];
  await env2.bridge.cycle();
  assert.equal(env2.steam.offers.size, 0);
  // ссылка на сам аккаунт LisBot
  const env3 = makeEnv();
  env3.session.steamId = MANN_ID;
  env3.steam.inv = [key(1)];
  await env3.bridge.cycle();
  assert.equal(env3.steam.offers.size, 0);
});

test('мост ключей: ордер на ключи в пределах цены, лимита и резерва кошелька', async () => {
  const env = makeEnv({ buy: true });
  await env.bridge.buy();
  assert.equal(env.market.orders.length, 1);
  const o = env.market.orders[0];
  assert.equal(o.priceCents, 14001);              // на копейку выше лучшего ордера, но ≤ 150
  assert.equal(o.quantity, 6);                    // (1000 − 100 резерв) / 140.01 = 6
  env.market.purchased = 4;
  await env.bridge.buy();
  assert.equal(env.bridge.stats.bought, 4);
  assert.ok(Math.abs(env.bridge.stats.avgKeyPrice - 140.01) < 1e-9);
  // ордер ещё активен — второй не ставим
  await env.bridge.buy();
  assert.equal(env.market.orders.length, 1);

  // Steam просит подтвердить ордер: подтверждаем именно его и повторяем запрос
  const env2 = makeEnv({ buy: true });
  let calls = 0;
  env2.market.createBuyOrder = async (q) => { env2.market.orders.push(q); calls++; if (calls === 1) { env2.steam.confs.push({ id: 'CONF7', key: 'k', type: 12, creator: 'X', title: 'buy' }); return { success: 22, need_confirmation: true, confirmation: { confirmation_id: 'CONF7' } }; } return { success: 1, buy_orderid: 'B2' }; };
  await env2.bridge.buy();
  assert.deepEqual(env2.steam.responded, [{ id: 'CONF7', ok: true }]);
  assert.equal(env2.market.orders[1].confirmation, 'CONF7');
  assert.equal(env2.bridge.state.buyOrder.id, 'B2');
});

test('мост ключей: настройки проверяют трейд-ссылку', () => {
  const cfg = structuredClone(baseCfg);
  cfg.configFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'lsb-kbc-')), 'config.json');
  assert.ok(saveSettings(cfg, { 'keyBridge.tradeUrl': 'не ссылка' }).length);
  assert.deepEqual(saveSettings(cfg, { 'keyBridge.tradeUrl': MANN_URL, 'keyBridge.enabled': true }), []);
  cfg.secrets = {}; cfg.keyBridge.tradeUrl = 'не ссылка';
  assert.ok(validateConfig(cfg).some((e) => e.startsWith('keyBridge.tradeUrl')));
  // пустая ссылка — MannBot на этом же аккаунте, это не ошибка
  cfg.keyBridge.tradeUrl = '';
  assert.ok(!validateConfig(cfg).some((e) => e.startsWith('keyBridge.tradeUrl')));
  assert.deepEqual(saveSettings(cfg, { 'keyBridge.tradeUrl': '' }), []);
});

test('мост ключей на одном аккаунте с MannBot: только покупка, ключи остаются в инвентаре', async () => {
  for (const setup of [(e) => { e.cfg.keyBridge.tradeUrl = ''; }, (e) => { e.session.steamId = MANN_ID; }]) {
    const env = makeEnv({ buy: true });
    setup(env);
    env.steam.inv = [key(1), key(2)];
    assert.equal(env.bridge.sameAccount, true);
    await env.bridge.cycle();
    assert.equal(env.steam.offers.size, 0);                 // никаких исходящих обменов
    assert.equal(env.market.orders.length, 1);              // а ордер на ключи стоит
    assert.equal(env.bridge.stats.partnerSteamId, null);
    assert.ok(!env.guard.state.halted);
    env.market.purchased = 2;
    await env.bridge.cycle();
    assert.ok(env.sent.some((m) => /MannBot начнёт продавать их через 7 дней/.test(m.text)));
  }
  const env = makeEnv();
  env.cfg.keyBridge.tradeUrl = 'https://example.com/x';
  assert.equal(env.bridge.sameAccount, false);
  await env.bridge.cycle();
  assert.equal(env.steam.offers.size, 0);
});
