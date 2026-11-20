import { DemoWorld, DAY } from './world.js';
import { startLisEmulator } from './lisServer.js';
import { DemoSteamSession } from './steam.js';

/**
 * Демо-режим LisBot (`node src/index.js --demo`): бот работает своим обычным кодом, но LIS-SKINS
 * и Steam заменены эмулятором (src/emu). Ключи не нужны, деньги и предметы ненастоящие.
 * Сроки сжаты, чтобы весь путь «покупка → обмен → Trade Protection → продажа» шёл минуты, а не дни.
 */
export const DEMO_OVERRIDES = {
  dryRun: false,
  lis: { purchasePollSec: 10, balancePollSec: 30 },
  steam: { tradeProtectionDays: 3 / (24 * 60), tradeProtectionBufferMin: 0, inventorySyncMin: 1, confirmationsCheckSec: 30, maxLoginsPerDay: 1000 },
  strategy: { scanIntervalSec: 30, rtbaCooldownSec: 3 },
  sell: { minListingGapSec: 5 },
  telegram: { enabled: false },              // демо-сделки не должны уходить в настоящий Telegram
};

function merge(dst, src) {
  for (const [k, v] of Object.entries(src)) {
    if (v && typeof v === 'object' && !Array.isArray(v)) merge(dst[k] ??= {}, v);
    else dst[k] = v;
  }
  return dst;
}

/** Применить демо-настройки и ненастоящие ключи (токен панели остаётся настоящим — по нему входит центр). */
export function demoConfig(cfg) {
  merge(cfg, structuredClone(DEMO_OVERRIDES));
  Object.assign(cfg.secrets, {
    lisApiKey: 'demo', steamAccountName: 'demo', steamPassword: 'demo', sharedSecret: 'ZGVtby1kZW1vLWRlbW8tZGVtbzE=', identitySecret: 'ZGVtby1kZW1vLWRlbW8tZGVtbzE=',
    tradeUrl: 'https://steamcommunity.com/tradeoffer/new/?partner=1&token=demo',
  });
  cfg.trade = { partner: '1', token: 'demo' };
  cfg.demo = true;
  return cfg;
}

/** Запустить эмулятор и направить в него бота. Вернуть { world, session(), close() }. */
export async function startDemo(cfg, worldOpts = {}) {
  demoConfig(cfg);
  const world = new DemoWorld({
    tradeDelayMs: 20_000, protectionMs: cfg.steam.tradeProtectionDays * DAY, saleMs: [60_000, 240_000], listingEveryMs: 8_000, ...worldOpts,
  }).start();
  const lis = await startLisEmulator(world);
  cfg.lis.baseUrl = lis.baseUrl;
  cfg.lis.wsUrl = lis.wsUrl;
  return {
    world,
    session: ({ log }) => new DemoSteamSession({ world, cfg, log }),
    close: async () => { world.stop(); await lis.close(); },
  };
}
