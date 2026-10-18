import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { runBacktest } from '../src/backtest.js';

const H = 3600_000;
const T0 = Date.UTC(2026, 9, 1);
const NAME = 'AK-47 | Redline (Field-Tested)';

function input(variants) {
  // пол держится около 10$, на 5-м часу падает до 8$, потом выскакивает до 14$ (рост — не догоняем)
  const prices = [10, 10.1, 9.9, 10, 8, 8, 10, 14, 10];
  return {
    config: { timezone: 'UTC' },
    valuations: { [NAME]: { name: NAME, ok: 1, net_usd: 12, daily_volume: 500, fx: 90 } },
    watch: { [NAME]: { name: NAME, enabled: 1 } },
    floors: prices.map((p, i) => [NAME, T0 + i * H, p]),
    balance: 1000,
    variants,
  };
}

test('бэктест: правила planBuys на истории пола, строгий ROI покупает меньше', () => {
  const { variants: [soft, strict] } = runBacktest(input([
    { label: 'ROI 10%', changes: { 'strategy.minRoi': 0.1 } },
    { label: 'ROI 60%', changes: { 'strategy.minRoi': 0.6 } },
  ]));
  assert.ok(soft.buys >= 1, 'при ROI 10% пол около 10$ проходит');
  assert.ok(soft.items.some((i) => i.name === NAME));
  assert.ok(soft.expectedProfit > 0);
  assert.equal(strict.buys, 0, 'при ROI 60% максимум 12/1.63 < 8$');
  assert.ok(Object.keys(strict.skips).length > 0);
});

test('бэктест: запуск как программы через stdin/stdout (так его вызывает центр)', () => {
  const script = fileURLToPath(new URL('../src/backtest.js', import.meta.url));
  const out = JSON.parse(execFileSync(process.execPath, [script], { input: JSON.stringify(input([])) }).toString());
  assert.equal(out.variants.length, 1);
  assert.equal(out.variants[0].label, 'текущие правила');
});
