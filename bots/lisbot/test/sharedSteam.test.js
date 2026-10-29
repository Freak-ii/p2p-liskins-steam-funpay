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
import { SharedSteam } from '../src/engine/sharedSteam.js';

const EState = TradeOfferManager.ETradeOfferState;
const baseCfg = JSON.parse(fs.readFileSync(new URL('../config.example.json', import.meta.url)));
const silent = { info() {}, warn() {}, error() {}, crit() {}, debug() {}, child() { return silent; } };
const BUYER = '76561198000000009';

/** Реестр центра: «свой» только обмен с ровно записанными получателем и assetid (как center/steamshare.py). */
function fakeCenter(registered, { down = false } = {}) {
  const calls = [];
  const fetchImpl = async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body), headers: opts.headers });
    if (down) throw new Error('ECONNREFUSED');
    const b = JSON.parse(opts.body);
    const assets = b.give.map((g) => g.assetid).sort().join(',');
    const hit = registered.find((r) => r.partner === b.partner && r.assets.slice().sort().join(',') === assets && b.give.every((g) => g.appid === 440) && !b.receive_count && (!r.offerId || r.offerId === b.offer_id));
    return { ok: true, json: async () => (hit ? { own: true, bot: 'mannbot', ref: hit.ref } : { own: false }) };
  };
  return { calls, fetchImpl };
}

function makeEnv({ registered = [], down = false, shared = true } = {}) {
  const cfg = structuredClone(baseCfg);
  const db = openDb(fs.mkdtempSync(path.join(os.tmpdir(), 'lsb-sh-')), ':memory:');
  const notify = { send: async () => true };
  const guard = new Guard({ db, notify, log: silent, cfg });
  const center = fakeCenter(registered, { down });
  const sharedSteam = new SharedSteam({ url: 'http://127.0.0.1:8800/api/steam-share', token: 't'.repeat(48), shared, log: silent, fetchImpl: center.fetchImpl });
  const offers = new Map();
  const steam = { confs: [], responded: [] };
  const session = {
    manager: { on() {}, getOffer: (id, cb) => (offers.has(id) ? cb(null, offers.get(id)) : cb(new Error('нет обмена'))) },
    getConfirmations: async () => steam.confs,
    respondConfirmation: async (c, ok) => { steam.responded.push({ id: c.id, ok }); steam.confs = steam.confs.filter((x) => x !== c); },
  };
  const trades = new TradeHandler({ cfg, db, session, guard, notify, log: silent });
  trades.shared = sharedSteam;
  const conf = new ConfirmationWatcher({ session, db, guard, log: silent, strict: true, shared: sharedSteam });
  const offer = (id, partner, assets, { receive = 0, appid = 440 } = {}) => {
    const o = {
      id: String(id), isOurOffer: true, state: EState.Active, partner: { getSteamID64: () => partner },
      itemsToGive: assets.map((a) => ({ appid, contextid: '2', assetid: String(a) })), itemsToReceive: Array.from({ length: receive }, () => ({ appid: 730 })),
      cancel(cb) { this.state = EState.Canceled; cb(null); },
    };
    offers.set(o.id, o);
    return o;
  };
  return { guard, trades, conf, steam, center, offer, sharedSteam };
}

test('общий аккаунт: обмен MannBot из реестра центра — не взлом', async () => {
  const env = makeEnv({ registered: [{ partner: BUYER, assets: ['11', '12'], ref: 'ORD1' }] });
  const o = env.offer(5001, BUYER, ['12', '11']);
  await env.trades.onUnknownOutgoing(o, 'unknownOfferSent');
  assert.equal(env.guard.state.halted, false);
  assert.equal(o.state, EState.Active);                                  // не отменён
  const call = env.center.calls[0];
  assert.equal(call.headers['X-Bot'], 'lisbot');
  assert.deepEqual(call.body, { offer_id: '5001', partner: BUYER, give: [{ appid: 440, assetid: '12' }, { appid: 440, assetid: '11' }], receive_count: 0 });
  // его подтверждение LisBot не трогает: подтвердит MannBot
  env.steam.confs.push({ id: 'c1', key: 'k', type: 2, creator: '5001', title: 'trade' });
  await env.conf.check();
  assert.deepEqual(env.steam.responded, []);
  assert.equal(env.guard.state.halted, false);
  // тот же номер обмена, но другой состав — запомненное «своё» не помогает
  const forged = env.offer(5001, BUYER, ['12', '777']);
  await env.trades.onUnknownOutgoing(forged, 'audit');
  assert.equal(env.guard.state.haltCode, 'UNKNOWN_OUTGOING_TRADE');
});

test('общий аккаунт: подтверждение раньше события обмена тоже узнаётся по составу из Steam', async () => {
  const env = makeEnv({ registered: [{ partner: BUYER, assets: ['21'], ref: 'ORD2' }] });
  env.offer(6001, BUYER, ['21']);
  env.steam.confs.push({ id: 'c2', key: 'k', type: 2, creator: '6001', title: 'trade' });
  await env.conf.check();
  assert.deepEqual(env.steam.responded, []);
  assert.equal(env.guard.state.halted, false);
});

test('общий аккаунт: всё, что не совпало с записью, по-прежнему взлом', async () => {
  const cases = [
    (e) => e.offer(7001, '76561198000000002', ['11', '12']),             // другой получатель
    (e) => e.offer(7002, BUYER, ['11', '12', '13']),                     // лишний предмет
    (e) => e.offer(7003, BUYER, ['11']),                                 // не все предметы
    (e) => e.offer(7004, BUYER, ['11', '12'], { receive: 1 }),           // от нас что-то просят
    (e) => e.offer(7005, BUYER, ['11', '12'], { appid: 730 }),           // скины CS2 вместо ключей
  ];
  for (const make of cases) {
    const env = makeEnv({ registered: [{ partner: BUYER, assets: ['11', '12'], ref: 'ORD1' }] });
    const o = make(env);
    await env.trades.onUnknownOutgoing(o, 'unknownOfferSent');
    assert.equal(env.guard.state.haltCode, 'UNKNOWN_OUTGOING_TRADE', o.id);
    assert.equal(o.state, EState.Canceled);
  }
  // центр недоступен — чужой
  const env = makeEnv({ registered: [{ partner: BUYER, assets: ['11'], ref: 'X' }], down: true });
  await env.trades.onUnknownOutgoing(env.offer(7100, BUYER, ['11']), 'audit');
  assert.equal(env.guard.state.haltCode, 'UNKNOWN_OUTGOING_TRADE');
  // режим двух аккаунтов: реестр не спрашиваем вовсе
  const env2 = makeEnv({ registered: [{ partner: BUYER, assets: ['11'], ref: 'X' }], shared: false });
  await env2.trades.onUnknownOutgoing(env2.offer(7200, BUYER, ['11']), 'audit');
  assert.equal(env2.guard.state.haltCode, 'UNKNOWN_OUTGOING_TRADE');
  assert.equal(env2.center.calls.length, 0);
  // подтверждение незаписанного обмена и обмена, которого Steam не знает, отменяется
  const env3 = makeEnv({ registered: [{ partner: BUYER, assets: ['11'], ref: 'X' }] });
  env3.offer(7300, BUYER, ['99']);
  env3.steam.confs.push({ id: 'c3', key: 'k', type: 2, creator: '7300', title: 'trade' });
  await env3.conf.check();
  assert.equal(env3.guard.state.haltCode, 'UNKNOWN_CONFIRMATION');
  assert.deepEqual(env3.steam.responded, [{ id: 'c3', ok: false }]);
  const env4 = makeEnv({ registered: [{ partner: BUYER, assets: ['11'], ref: 'X' }] });
  env4.steam.confs.push({ id: 'c4', key: 'k', type: 2, creator: '424242', title: 'trade' });
  await env4.conf.check();
  assert.equal(env4.guard.state.haltCode, 'UNKNOWN_CONFIRMATION');
  // чужое подтверждение лота на ТП реестр не спасает
  const env5 = makeEnv({ registered: [{ partner: BUYER, assets: ['11'], ref: 'X' }] });
  env5.offer(7500, BUYER, ['11']);
  env5.steam.confs.push({ id: 'c5', key: 'k', type: 3, creator: '7500', title: 'listing' });
  await env5.conf.check();
  assert.equal(env5.guard.state.haltCode, 'UNKNOWN_CONFIRMATION');
});

test('SharedSteam из переменных центра', () => {
  assert.equal(SharedSteam.fromEnv({}).enabled, false);
  assert.equal(SharedSteam.fromEnv({ CENTER_STEAM_URL: 'http://x', CENTER_STEAM_TOKEN: 't', CENTER_STEAM_SHARED: '0' }).enabled, false);
  assert.equal(SharedSteam.fromEnv({ CENTER_STEAM_URL: 'http://x', CENTER_STEAM_TOKEN: 't', CENTER_STEAM_SHARED: '1' }).enabled, true);
});
