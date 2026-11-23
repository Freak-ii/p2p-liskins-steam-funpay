import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { loadConfig, validateConfig } from './config.js';
import { openDb } from './db.js';
import { createLogger, sleep, jitter, errMsg, HOUR, DAY, hourIn, dayKey, fmtUsd } from './util/common.js';
import { Telegram, esc } from './notify/telegram.js';
import { Guard } from './engine/guard.js';
import { LisClient } from './lis/client.js';
import { LisWs } from './lis/ws.js';
import { SteamSession } from './steam/session.js';
import { SteamMarket } from './steam/market.js';
import { FxService, Valuator } from './engine/pricing.js';
import { Buyer } from './engine/buyer.js';
import { PurchaseTracker } from './engine/purchaseTracker.js';
import { TradeHandler } from './engine/trades.js';
import { ConfirmationWatcher } from './engine/confirmations.js';
import { Seller } from './engine/seller.js';
import { BalanceWatcher } from './engine/balanceWatcher.js';
import { KeyBridge } from './engine/keyBridge.js';
import { SharedSteam } from './engine/sharedSteam.js';
import { CsgoMarketClient } from './arb/csgoClient.js';
import { ArbScanner } from './arb/scanner.js';
import { ArbSeller } from './arb/seller.js';
import { summary, textReport, perItem } from './analytics.js';
import { startDashboard } from './web/server.js';
import { startDemo } from './emu/demo.js';

export class Bot {
  /** hooks: { onRestartRequest(), appMode } — для десктоп-приложения; emu — эмулятор LIS/Steam (демо, src/emu) */
  constructor(cfg, hooks = {}) {
    this.cfg = cfg;
    this.hooks = hooks;
    this.log = createLogger(cfg.dataDir);
    this.db = openDb(cfg.dataDir);
    this.notify = new Telegram({ token: cfg.secrets.telegramToken, chatId: cfg.secrets.telegramChatId, enabled: cfg.telegram.enabled, dedupeMinutes: cfg.telegram.dedupeMinutes, log: this.log.child('tg') });
    this.guard = new Guard({ db: this.db, notify: this.notify, log: this.log.child('guard'), cfg });
    this.lis = new LisClient({ apiKey: cfg.secrets.lisApiKey, baseUrl: cfg.lis.baseUrl, requestsPerSecond: cfg.lis.requestsPerSecond, log: this.log.child('lis') });
    this.balanceRef = { value: this.db.get('balance.last')?.balance ?? null };
    this.loops = new Map();
    this.stopped = false;
    this.emu = hooks.emu || null;
  }

  seedWatchlist() {
    const exclude = (this.cfg.watchlist.excludePatterns || []).map((p) => p.toLowerCase());
    const existing = new Set(this.db.watchlist(false).map((w) => w.name));
    for (const it of this.cfg.watchlist.items || []) {
      if (existing.has(it.name)) continue;
      if (exclude.some((p) => it.name.toLowerCase().includes(p))) { this.log.warn(`«${it.name}» исключён excludePatterns`); continue; }
      this.db.upsertWatch({ name: it.name, source: 'config', max_held: it.maxHeld ?? null, max_per_day: it.maxPerDay ?? null, min_roi: it.minRoi ?? null, max_price_usd: it.maxPriceUsd ?? null });
    }
  }

  async discoverWatchlist() {
    const a = this.cfg.watchlist.auto;
    const exclude = (this.cfg.watchlist.excludePatterns || []).map((p) => p.toLowerCase());
    const counts = new Map();
    let cursor = null;
    for (let p = 0; p < a.pages; p++) {
      const r = await this.lis.search({ game: this.cfg.lis.game, priceFrom: a.minPriceUsd, priceTo: a.maxPriceUsd, onlyUnlocked: 1, sortBy: 'lowest_price', cursor });
      for (const l of r.items) counts.set(l.name, (counts.get(l.name) || 0) + 1);
      if (!r.nextCursor) break;
      cursor = r.nextCursor;
    }
    const top = [...counts].filter(([n, c]) => c >= a.minListingsOnLis && !exclude.some((p) => n.toLowerCase().includes(p))).sort((x, y) => y[1] - x[1]).slice(0, a.maxItems);
    const keep = new Set(top.map(([n]) => n));
    for (const w of this.db.watchlist(false)) if (w.source === 'auto' && !keep.has(w.name)) this.db.upsertWatch({ ...w, enabled: 0 });
    for (const [name] of top) if (!this.db.watchlist(false).some((w) => w.name === name && w.source !== 'auto')) this.db.upsertWatch({ name, source: 'auto' });
    this.log.info(`Автовотчлист: ${top.length} предметов`);
  }

  /** Периодическая задача с защитой от падений и разбросом интервала. */
  loop(name, intervalMs, fn, { initialDelayMs = 0, spread = 0.15 } = {}) {
    const state = { name, lastRun: 0, lastOk: 0, lastError: null, runs: 0 };
    this.loops.set(name, state);
    (async () => {
      await sleep(initialDelayMs);
      while (!this.stopped) {
        state.lastRun = Date.now();
        try { await fn(); state.lastOk = Date.now(); state.lastError = null; } catch (e) { state.lastError = errMsg(e); this.log.error(`[${name}] ${errMsg(e)}`); }
        state.runs++;
        await sleep(jitter(intervalMs, intervalMs * spread));
      }
    })();
  }

  runtimeStats() {
    return {
      steam: this.session?.stats, lis: this.lis.sched.stats, ws: this.ws ? { connected: this.ws.connected, ...this.ws.stats } : null,
      buyer: this.buyer?.stats, keyBridge: this.bridge?.stats, arbitrage: this.arb ? { ...this.arb.stats, seller: this.seller?.stats } : null, fx: this.fx ? { rate: this.fx.rate, updatedAt: this.fx.updatedAt } : null,
      loops: Object.fromEntries([...this.loops].map(([k, v]) => [k, v])),
    };
  }

  async start() {
    const { cfg, log, db, guard } = this;
    this.seedWatchlist();
    if (cfg.dashboard.enabled) this.dashboard = startDashboard({ cfg, db, guard, bot: this, log: log.child('web') });
    this.registerCommands();
    this.notify.startPolling();
    if (this.emu) log.warn('ДЕМО: LIS-SKINS и Steam эмулируются (src/emu), деньги и предметы ненастоящие');
    await this.notify.send('ok', `Бот запущен${this.emu ? ' в <b>демо-режиме</b> (эмуляция LIS-SKINS и Steam)' : ''}${cfg.dryRun ? ' в режиме <b>DRY-RUN</b> (покупки и продажи не выполняются)' : ''}. Вотчлист: ${db.watchlist().length} предм.`);
    if (guard.state.halted) await this.notify.send('warn', `Бот был остановлен ранее: ${esc(guard.state.haltReason)}. Закупки/продажи не начнутся до /resume.`);

    // --- LIS ---
    this.balanceWatcher = new BalanceWatcher({ cfg, db, lis: this.lis, guard, notify: this.notify, log: log.child('balance'), lock: null, balanceRef: this.balanceRef });

    // --- Steam ---
    this.session = this.emu ? this.emu.session({ log: log.child('steam') }) : new SteamSession({ cfg, secrets: cfg.secrets, db, log: log.child('steam') });
    this.emu?.world.adoptDb(db);
    this.session.on('critical', (c) => (c.fatal || c.code === 'STEAM_RATE_LIMIT' ? guard.critical(c.code, c.message) : guard.warn(c.code, c.message)));
    this.market = new SteamMarket({ session: this.session, db, cfg, log: log.child('market') });
    this.fx = new FxService({ cfg, market: this.market, db, guard, log: log.child('fx') });
    this.valuator = new Valuator({ cfg, market: this.market, db, fx: this.fx, log: log.child('val') });
    this.buyer = new Buyer({ cfg, db, lis: this.lis, valuator: this.valuator, guard, notify: this.notify, log: log.child('buy'), balanceRef: this.balanceRef });
    if (cfg.arbitrage?.enabled && !this.emu) {
      // арбитраж Lis → Market.CSGO: закупщик тот же, меняются источник оценки, список предметов и лимиты
      this.mcsgo = new CsgoMarketClient({ apiKey: cfg.secrets.csgoApiKey, log: log.child('mcsgo') });
      this.arb = new ArbScanner({ cfg, db, client: this.mcsgo, guard, log: log.child('arb') });
      this.buyer.cfg = Object.create(cfg, { strategy: { get: () => ({ ...cfg.strategy, ...cfg.arbitrage.strategy }) } });
      this.buyer.valuator = this.arb;
      this.buyer.watchMap = () => this.arb.watchMap();
      this.buyer.rank = (name) => this.arb.roi(name);
    }
    this.balanceWatcher.lock = this.buyer.lock;
    this.tracker = new PurchaseTracker({ cfg, db, lis: this.lis, guard, log: log.child('track'), notify: this.notify });
    this.trades = new TradeHandler({ cfg, db, session: this.session, guard, notify: this.notify, log: log.child('trade') });
    // один аккаунт Steam с MannBot: его обмены, записанные в центре, не считаются взломом
    this.shared = SharedSteam.fromEnv(process.env, log.child('shared'));
    if (this.shared.enabled) log.info('Steam: общий аккаунт с MannBot, его обмены сверяются с реестром центра');
    this.trades.shared = this.shared;
    this.conf = new ConfirmationWatcher({ session: this.session, db, guard, log: log.child('conf'), strict: cfg.risk.strictConfirmations, shared: this.shared });
    const sellerDeps = { cfg, db, session: this.session, market: this.market, conf: this.conf, valuator: this.valuator, fx: this.fx, guard, notify: this.notify, log: log.child('sell') };
    this.seller = this.arb ? new ArbSeller({ ...sellerDeps, client: this.mcsgo }) : new Seller(sellerDeps);
    if (this.arb) this.trades.arb = this.seller;
    this.bridge = new KeyBridge({ cfg, db, session: this.session, market: this.market, conf: this.conf, guard, notify: this.notify, log: log.child('keys') });
    this.trades.bridge = this.bridge;

    await this.balanceWatcher.poll();

    await this.ensureSteam(true);
    // после /resume — сразу пробуем восстановить сессию Steam
    guard.on('change', (s) => { if (!s.halted && !this.session.ready) this.ensureSteam(); });
    this.trades.start();
    if (this.session.ready) await this.fx.refresh();

    const st = cfg.strategy;
    const steamReady = () => this.session.ready;
    this.loop('balance', cfg.lis.balancePollSec * 1000, () => this.balanceWatcher.poll(), { initialDelayMs: 30_000 });
    this.loop('purchases', cfg.lis.purchasePollSec * 1000, () => this.tracker.poll(), { initialDelayMs: 5_000 });
    if (this.arb) {
      this.loop('arb-scan', cfg.arbitrage.scanIntervalMin * 60_000, () => this.arb.scan(), { initialDelayMs: 5_000 });
      this.loop('arb-ping', 170_000, () => (steamReady() ? this.seller.ping() : null), { initialDelayMs: 100_000, spread: 0 });
      this.loop('arb-deliver', 45_000, () => (steamReady() ? this.seller.deliver() : null), { initialDelayMs: 110_000 });
    }
    this.loop('valuations', 60_000, async () => {
      if (!steamReady() || this.arb) return;
      const stale = db.watchlist().map((w) => ({ w, v: db.valuation(w.name) })).filter(({ v }) => !v || Date.now() - v.ts > st.valuationTtlHours * HOUR).sort((a, b) => (a.v?.ts || 0) - (b.v?.ts || 0)).slice(0, 8);
      for (const { w } of stale) {
        try { await this.valuator.refresh(w.name); this.guard.recordSuccess('valuation'); } catch (e) { if (this.guard.recordError('valuation', e)) return; }
      }
    }, { initialDelayMs: 10_000 });
    this.loop('scan', st.scanIntervalSec * 1000, () => this.buyer.scan(), { initialDelayMs: 60_000 });
    this.loop('seller', 180_000, () => (steamReady() ? this.seller.cycle() : null), { initialDelayMs: 90_000, spread: 0.4 });
    this.loop('confirmations', cfg.steam.confirmationsCheckSec * 1000, () => (steamReady() ? this.conf.check() : null), { initialDelayMs: 45_000, spread: 0.3 });
    this.loop('audit-outgoing', 10 * 60_000, () => (steamReady() ? this.trades.auditOutgoing() : null), { initialDelayMs: 120_000 });
    this.loop('fx', cfg.fx.refreshHours * HOUR, () => (steamReady() ? this.fx.refresh() : null), { initialDelayMs: cfg.fx.refreshHours * HOUR });
    if (cfg.watchlist.mode === 'auto') this.loop('discover', cfg.watchlist.auto.refreshHours * HOUR, () => this.discoverWatchlist(), { initialDelayMs: 2_000 });
    this.loop('key-bridge', (cfg.keyBridge?.checkIntervalMin || 30) * 60_000, () => (steamReady() && !this.emu ? this.bridge.cycle() : null), { initialDelayMs: 150_000 });
    this.loop('housekeeping', 15 * 60_000, () => this.housekeeping(), { initialDelayMs: 60_000 });

    if (st.rtbaEnabled) {
      this.ws = new LisWs({ url: cfg.lis.wsUrl, channels: cfg.lis.wsChannels, getToken: () => this.lis.wsToken(), log: log.child('ws') });
      this.ws.on('listing', (l, meta) => this.buyer.onRealtimeListing(l, meta).catch((e) => log.error(`RTBA: ${errMsg(e)}`)));
      this.ws.on('removed', (x) => this.buyer.onRealtimeRemoved(x));
      this.ws.on('status', (s) => db.event(`ws.${s}`));
      this.ws.start();
    }
    log.info('Все модули запущены');
  }

  /** Вход в Steam (с учётом суточного лимита логинов внутри SteamSession). */
  async ensureSteam(initial = false) {
    if (this.session.ready || this.loggingIn) return;
    if (!initial && this.guard.state.halted) return;
    this.loggingIn = true;
    try {
      await this.session.login();
      await this.session.syncTime();
      if (!initial) this.notify.send('ok', 'Сессия Steam восстановлена');
    } catch (e) {
      if (e.network) {
        // сеть/DNS: Steam попытку не видел, останавливать бота незачем — повторим вход через пару минут
        this.guard.warn('STEAM_NETWORK', `Нет связи со Steam, повтор входа через 2 мин: ${errMsg(e)}`);
        clearTimeout(this.steamRetry);
        this.steamRetry = setTimeout(() => this.ensureSteam(), 2 * 60_000);
        this.steamRetry.unref?.();
      } else this.guard.critical('STEAM_START', `Не удалось войти в Steam: ${errMsg(e)}`);
    } finally { this.loggingIn = false; }
  }

  housekeeping() {
    const { db, cfg } = this;
    db.pruneFloors(Date.now() - 7 * DAY);
    db.db.prepare('DELETE FROM events WHERE ts<? AND type IN (\'trade.incoming\',\'ws.connected\',\'ws.disconnected\')').run(Date.now() - 60 * DAY);
    // здоровье: WS молчит слишком долго
    if (this.ws && this.ws.stats.lastMessageAt && Date.now() - this.ws.stats.lastMessageAt > 15 * 60_000) this.guard.warn('WS_SILENT', 'WebSocket LIS молчит более 15 минут — RTBA не работает, закупка идёт только сканом');
    if (this.session && !this.session.ready) {
      this.guard.warn('STEAM_NOT_READY', 'Сессия Steam не активна — продажи и приём трейдов приостановлены');
      this.ensureSteam();
    }
    // ежедневный отчёт
    const key = `report.${dayKey(Date.now(), cfg.timezone)}`;
    if (hourIn(cfg.timezone) === cfg.telegram.dailyReportHour && !db.get(key)) {
      db.set(key, true);
      this.notify.send('report', textReport(summary(db, { tz: cfg.timezone, fx: this.fx?.rate }), cfg.steam.walletCurrencyCode));
    }
  }

  registerCommands() {
    const t = this.notify, g = this.guard, db = this.db, cfg = this.cfg;
    t.on('status', () => {
      const s = g.state;
      const r = this.runtimeStats();
      return [
        s.halted ? `🛑 ОСТАНОВЛЕН: ${esc(s.haltReason)}` : s.paused ? '⏸ На паузе' : '▶️ Работает',
        cfg.dryRun ? 'Режим: DRY-RUN' : 'Режим: боевой',
        `Steam: ${r.steam?.ready ? 'онлайн' : 'нет сессии'} · WS LIS: ${r.ws?.connected ? 'онлайн' : 'нет'}`,
        `Баланс: ${fmtUsd(this.balanceRef.value)} · Курс: ${this.fx?.rate?.toFixed(2) ?? '—'}`,
        `Покупки: скан ${r.buyer?.scanBuys ?? 0}, RTBA ${r.buyer?.rtbaBuys ?? 0} (с запуска)`,
      ].join('\n');
    }, 'состояние');
    t.on('stats', () => textReport(summary(db, { tz: cfg.timezone, fx: this.fx?.rate }), cfg.steam.walletCurrencyCode), 'отчёт и аналитика');
    t.on('balance', async () => `Баланс LIS: ${fmtUsd(await this.balanceWatcher.poll())}`, 'баланс LIS-SKINS');
    t.on('pause', () => { g.pause('telegram'); return null; }, 'пауза (трейды от LIS принимаются)');
    t.on('resume', () => { g.resume('telegram'); return null; }, 'снять паузу/аварийную остановку');
    t.on('stop', () => { g.critical('MANUAL_STOP', 'Аварийная остановка из Telegram'); return null; }, 'аварийная остановка');
    t.on('top', () => perItem(db).slice(0, 10).map((r) => `${esc(r.name)}: ${r.sold}/${r.bought} продано, прибыль ${fmtUsd(r.profit)}`).join('\n') || 'Пока нет данных', 'топ предметов по прибыли');
    t.on('help', () => t.helpText(), 'список команд');
  }

  async stop(reason = 'завершение процесса') {
    if (this.stopped) return;
    this.stopped = true;
    clearTimeout(this.steamRetry);
    this.ws?.stop();
    this.notify.stop();
    this.session?.stop();
    this.lis.sched.stop();
    this.mcsgo?.sched.stop();
    await new Promise((r) => (this.dashboard ? this.dashboard.close(() => r()) : r()));
    this.dashboard?.closeAllConnections?.();
    await Promise.race([this.notify.send('info', `Бот остановлен (${reason})`), sleep(5000)]);
    await sleep(300);
    await this.emu?.close().catch(() => {});
    try { this.db.db.close(); } catch { /* уже закрыта */ }
  }
}

/** Проверка конфигурации и доступа без покупок: npm run check */
async function check(cfg) {
  const log = createLogger(cfg.dataDir);
  const errs = validateConfig(cfg);
  errs.forEach((e) => log.error(`config: ${e}`));
  const lis = new LisClient({ apiKey: cfg.secrets.lisApiKey, baseUrl: cfg.lis.baseUrl, log });
  try { log.info(`LIS баланс: ${fmtUsd(await lis.balance())}`); } catch (e) { log.error(`LIS: ${errMsg(e)}`); errs.push('LIS недоступен'); }
  const name = cfg.watchlist.items?.[0]?.name;
  if (name) {
    try { const r = await lis.search({ game: cfg.lis.game, names: [name], sortBy: 'lowest_price' }); log.info(`LIS поиск «${name}»: ${r.items.length} лотов, мин. ${fmtUsd(r.items[0]?.price)}`); } catch (e) { log.error(`LIS поиск: ${errMsg(e)}`); }
  }
  try { log.info(`LIS ws-токен: ${(await lis.wsToken()) ? 'получен' : 'нет'}`); } catch (e) { log.warn(`LIS ws-токен: ${errMsg(e)}`); }
  const tg = new Telegram({ token: cfg.secrets.telegramToken, chatId: cfg.secrets.telegramChatId, enabled: cfg.telegram.enabled, log });
  await tg.send('ok', 'Проверка связи: уведомления работают');
  lis.sched.stop();
  log.info(errs.length ? `Проблем: ${errs.length}` : 'Конфигурация в порядке');
  return errs.length ? 1 : 0;
}

// pathToFileURL: корректно и для путей с пробелами/кириллицей, и для Windows (C:\...)
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const cfg = loadConfig();
  // демо: LIS-SKINS и Steam эмулируются, ключи не нужны (так LisBot запускает центр в демо-режиме)
  const emu = process.argv.includes('--demo') || process.env.LISBOT_DEMO === '1' ? await startDemo(cfg) : null;
  if (process.argv.includes('--check')) {
    process.exit(await check(cfg));
  }
  const errs = validateConfig(cfg);
  if (errs.length) {
    console.error('Ошибки конфигурации:\n - ' + errs.join('\n - ') + '\n\nЗапустите `npm run check` после исправления.');
    process.exit(1);
  }
  const bot = new Bot(cfg, { emu });
  const shutdown = async (sig) => { bot.log.info(`Получен ${sig}, завершаю…`); await bot.stop().catch(() => {}); process.exit(0); };
  bot.hooks.onShutdownRequest = async (why) => { bot.log.info(`Остановка: ${why}`); await bot.stop(why).catch(() => {}); process.exit(0); };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
  process.on('unhandledRejection', (e) => bot.log.error(`unhandledRejection: ${errMsg(e)}`));
  process.on('uncaughtException', (e) => {
    bot.log.crit(`uncaughtException: ${errMsg(e)}`);
    bot.guard.critical('CRASH', `Необработанная ошибка: ${errMsg(e)}`);
  });
  bot.start().catch((e) => { bot.guard.critical('START', `Сбой запуска: ${errMsg(e)}`); });
}
