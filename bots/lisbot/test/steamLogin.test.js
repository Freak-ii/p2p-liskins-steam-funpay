import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import SteamTotp from 'steam-totp';
import { installSteamDnsFallback, fallbackResolve, isNetworkError } from '../src/steam/dns.js';
import { SteamSession } from '../src/steam/session.js';
import { isSteamSecret, validateConfig } from '../src/config.js';

const baseCfg = JSON.parse(fs.readFileSync(new URL('../config.example.json', import.meta.url)));
const silent = { info() {}, warn() {}, error() {}, crit() {}, debug() {}, child() { return silent; } };
const SECRET = Buffer.from('demo-demo-demo-demo1').toString('base64');

function memDb() {
  const kv = new Map();
  return { get: (k, d) => (kv.has(k) ? kv.get(k) : d), set: (k, v) => kv.set(k, v), kv };
}

function makeSession({ token = true } = {}) {
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'lis-steam-'));
  if (token) fs.writeFileSync(path.join(dataDir, 'steam-refresh-token.txt'), 'token');
  const cfg = { ...baseCfg, dataDir, timezone: 'UTC' };
  const db = memDb();
  const s = new SteamSession({ cfg, secrets: { steamAccountName: 'acc', steamPassword: 'pw', sharedSecret: SECRET, identitySecret: SECRET }, db, log: silent });
  return { s, db };
}

test('DNS: если системный DNS не нашёл домен Steam, адрес берётся у запасного резолвера', async (t) => {
  const asked = [];
  const fb = installSteamDnsFallback({ resolve: async (h) => { asked.push(h); return ['127.0.0.1']; } });
  t.after(() => fb.uninstall());
  const srv = http.createServer((_q, r) => r.end('ok')).listen(0, '127.0.0.1');
  t.after(() => srv.close());
  await new Promise((r) => srv.once('listening', r));
  const host = 'no-such-host-for-lisbot-test.steampowered.com';
  const body = await new Promise((resolve, reject) => {
    http.get({ host, port: srv.address().port, path: '/' }, (res) => { let b = ''; res.on('data', (c) => { b += c; }); res.on('end', () => resolve(b)); }).on('error', reject);
  });
  assert.equal(body, 'ok');
  assert.deepEqual(asked, [host]);
  // чужие домены запасной резолвер не трогает
  await new Promise((r) => http.get({ host: 'no-such-host-for-lisbot-test.example', port: 1 }).on('error', () => r()));
  assert.deepEqual(asked, [host]);
});

test('DNS: запасные резолверы опрашиваются по очереди до первого ответа', async () => {
  const calls = [];
  const ips = await fallbackResolve('api.steampowered.com', [
    async () => { calls.push(1); throw new Error('udp 53 закрыт'); },
    async () => { calls.push(2); return ['23.1.2.3']; },
    async () => { calls.push(3); return ['9.9.9.9']; },
  ]);
  assert.deepEqual(ips, ['23.1.2.3']);
  assert.deepEqual(calls, [1, 2]);
});

test('ошибка сети отличается от отказа Steam', () => {
  assert.ok(isNetworkError(Object.assign(new Error('getaddrinfo ENOENT api.steampowered.com'), { code: 'ENOENT' })));
  assert.ok(isNetworkError(new Error('getaddrinfo ENOTFOUND api.steampowered.com')));
  assert.ok(!isNetworkError(Object.assign(new Error('InvalidPassword'), { eresult: 5 })));
});

test('вход: ошибка DNS не останавливает бота и не тратит суточный лимит логинов', async () => {
  const { s, db } = makeSession();
  const crit = [];
  s.on('critical', (c) => crit.push(c));
  s.user.logOn = () => setImmediate(() => s.user.emit('error', Object.assign(new Error('getaddrinfo ENOENT api.steampowered.com'), { code: 'ENOENT' })));
  for (let i = 0; i < baseCfg.steam.maxLoginsPerDay + 2; i++) {
    await assert.rejects(s.login(), (e) => e.network === true && /Нет связи со Steam/.test(e.message));
  }
  assert.ok(crit.every((c) => c.code === 'STEAM_NETWORK' && !c.fatal));
  assert.deepEqual([...db.kv.values()], [0]);
  assert.equal(s.listenerCount('critical'), 1);
  assert.equal(s.listenerCount('ready'), 0);
  s.stop();
});

test('вход: неверный пароль по-прежнему считается попыткой и останавливает', async () => {
  const { s, db } = makeSession();
  s.on('critical', () => {});
  s.user.logOn = () => setImmediate(() => s.user.emit('error', Object.assign(new Error('InvalidPassword'), { eresult: 5 })));
  await assert.rejects(s.login(), (e) => !e.network && /InvalidPassword/.test(e.message));
  assert.deepEqual([...db.kv.values()], [1]);
  s.stop();
});

test('вход по паролю: код SDA считается с поправкой на часы Steam', async () => {
  const { s } = makeSession({ token: false });
  s.syncTime = async () => { s._timeOffset = 120; };
  let opts;
  s.user.logOn = (o) => { opts = o; setImmediate(() => s.emit('ready')); };
  await s.login();
  assert.equal(opts.accountName, 'acc');
  assert.equal(opts.twoFactorCode, SteamTotp.generateAuthCode(SECRET, 120));
  s.stop();
});

test('shared_secret проверяется по формату maFile', () => {
  assert.ok(isSteamSecret(SECRET));
  assert.ok(!isSteamSecret('12345'));
  assert.ok(!isSteamSecret('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123'));
  const cfg = { ...baseCfg, secrets: { lisApiKey: 'k', steamAccountName: 'a', steamPassword: 'p', sharedSecret: 'не тот ключ', identitySecret: SECRET, tradeUrl: 'x' }, trade: { partner: '1', token: 't' } };
  assert.ok(validateConfig(cfg).some((e) => e.startsWith('STEAM_SHARED_SECRET не похож')));
});
