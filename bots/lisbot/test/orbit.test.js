import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { OrbitGate } from '../src/util/orbit.js';

test('Орбита: закупка закрывается и открывается файлом центра, подтверждение пишется', () => {
  const dir = mkdtempSync(join(tmpdir(), 'orbit-'));
  const path = join(dir, 'orbit.json');
  const g = new OrbitGate({ bot: 'lisbot', path, ttlMs: 0 });
  assert.equal(g.blocked('buy.steam'), false);                 // файла нет — всё открыто
  writeFileSync(path, JSON.stringify({ version: 3, closed: { lisbot: ['buy.steam'] } }));
  assert.equal(g.blocked('buy.steam'), true);
  assert.equal(g.blocked('buy.arb'), false);
  const ack = JSON.parse(readFileSync(join(dir, 'orbit-ack', 'lisbot.json'), 'utf8'));
  assert.equal(ack.version, 3);
  assert.equal(ack.applied['buy.steam'], 'ok');
  assert.ok(existsSync(join(dir, 'orbit-ack')));
  assert.equal(new OrbitGate({ path: '' }).blocked('buy.steam'), false);
});
