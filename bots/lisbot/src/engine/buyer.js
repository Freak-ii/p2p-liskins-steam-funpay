import { planBuys } from '../pricing/buyStrategy.js';
import { uid, round2, errMsg, HOUR, startOfDay, fmtUsd } from '../util/common.js';
import { esc } from '../notify/telegram.js';

/** Простой мьютекс: закупки и сверка баланса не должны пересекаться. */
export class Mutex {
  constructor() { this.p = Promise.resolve(); }
  run(fn) { const r = this.p.then(fn, fn); this.p = r.catch(() => {}); return r; }
}

/**
 * Закупщик: два пути покупки
 *  - scan: периодический обход вотчлиста через /market/search (сортировка по цене);
 *  - RTBA (Real-Time Buy Automation): событие о новом лоте из WebSocket LIS ->
 *    мгновенная проверка по кэшированной оценке и стакану -> покупка за доли секунды.
 * Оба пути используют одну и ту же стратегию planBuys и общий мьютекс бюджета.
 */
export class Buyer {
  constructor({ cfg, db, lis, valuator, guard, notify, log, balanceRef }) {
    Object.assign(this, { cfg, db, lis, valuator, guard, notify, log, balanceRef });
    this.lock = new Mutex();
    this.books = new Map(); // name -> Map(id -> listing)
    this.lastRtba = new Map();
    this.failedInRow = 0;
    this.stats = { scans: 0, rtbaSeen: 0, rtbaBuys: 0, scanBuys: 0, lastScanAt: 0, lastScanMs: 0, skips: {} };
    this.lastDecision = new Map();
  }

  watchMap() { return new Map(this.db.watchlist(true).map((w) => [w.name, w])); }

  ctxFor(name) {
    const sod = startOfDay(this.cfg.timezone);
    const dry = this.cfg.dryRun;
    return {
      balance: this.balanceRef.value ?? 0,
      exposure: this.db.exposureUsd() + (dry ? this._dryExposure() : 0),
      exposureItem: this.db.exposureUsd(name),
      spentHour: this.db.spentSince(Date.now() - HOUR, dry),
      spentDay: this.db.spentSince(sod, dry),
      heldCount: this.db.heldCount(name),
      boughtToday: this.db.boughtSince(name, sod, dry),
    };
  }

  _dryExposure() { return this.db.db.prepare("SELECT COALESCE(SUM(buy_price_usd),0) s FROM items WHERE dry_run=1 AND created_at>=?").get(Date.now() - 24 * HOUR).s; }

  floorHistory(name) {
    return this.db.floors(name, Date.now() - this.cfg.strategy.floorBaselineHours * HOUR);
  }

  setBook(name, listings) { this.books.set(name, new Map(listings.map((l) => [l.id, l]))); }

  /** Периодический проход по вотчлисту. */
  async scan() {
    if (!this.guard.canBuy) return;
    const t0 = Date.now();
    const watch = this.watchMap();
    const names = [...watch.keys()];
    const { lis: lc } = this.cfg;
    for (let i = 0; i < names.length && this.guard.canBuy; i += lc.namesPerSearchRequest) {
      const chunk = names.slice(i, i + lc.namesPerSearchRequest);
      const found = new Map(chunk.map((n) => [n, []]));
      try {
        let cursor = null;
        for (let page = 0; page < lc.searchPagesPerScan; page++) {
          const r = await this.lis.search({ game: lc.game, names: chunk, onlyUnlocked: lc.allowLockedDays > 0 ? 0 : 1, sortBy: 'lowest_price', cursor });
          for (const l of r.items) found.get(l.name)?.push(l);
          if (!r.nextCursor) break;
          cursor = r.nextCursor;
        }
        this.guard.recordSuccess('lis-search');
      } catch (e) {
        if (this.guard.recordError('lis-search', e)) return;
        continue;
      }
      for (const [name, listings] of found) {
        if (listings.length) {
          this.db.saveFloor(name, Math.min(...listings.map((l) => l.price)), listings.length);
          this.setBook(name, listings);
        }
        await this.consider(name, listings, watch.get(name), 'scan');
      }
    }
    this.stats.scans++;
    this.stats.lastScanAt = Date.now();
    this.stats.lastScanMs = Date.now() - t0;
  }

  /** RTBA: новый лот из WebSocket. */
  async onRealtimeListing(listing, meta) {
    const st = this.cfg.strategy;
    const watch = this.watchMap().get(listing.name);
    const book = this.books.get(listing.name) || new Map();
    book.set(listing.id, listing);
    this.books.set(listing.name, book);
    if (!watch || !st.rtbaEnabled || !this.guard.canBuy) return;
    this.stats.rtbaSeen++;
    const last = this.lastRtba.get(listing.name) || 0;
    if (Date.now() - last < st.rtbaCooldownSec * 1000) return;
    if (!this.valuator.isFresh(listing.name, st.rtbaMaxValuationAgeHours)) return;
    this.lastRtba.set(listing.name, Date.now());
    await this.consider(listing.name, [...book.values()], watch, 'rtba', { onlyId: listing.id, receivedAt: meta?.receivedAt });
  }

  onRealtimeRemoved({ id }) { for (const b of this.books.values()) b.delete(id); }

  consider(name, listings, watch, mode, { onlyId, receivedAt } = {}) {
    return this.lock.run(async () => {
      if (!this.guard.canBuy) return;
      const valuation = this.valuator.cached(name);
      const plan = planBuys({ name, listings, valuation, watch, ctx: this.ctxFor(name), cfg: this.cfg, floorHistory: this.floorHistory(name) });
      this.lastDecision.set(name, { ts: Date.now(), mode, reason: plan.reason, info: plan.info });
      if (plan.info?.suspicious) this.guard.warn('SUSPICIOUS_PRICE', `«${name}»: лот за ${fmtUsd(plan.info.floor)} при оценке ${fmtUsd(plan.info.netUsd)} — подозрительно, пропущено`);
      if (!plan.buy.length) { this.stats.skips[plan.reason.split(' ')[0]] = (this.stats.skips[plan.reason.split(' ')[0]] || 0) + 1; return; }
      let buys = plan.buy;
      if (onlyId) { buys = buys.filter((b) => b.listing.id === onlyId); if (!buys.length) return; }
      await this.execute(name, buys, valuation, mode, receivedAt);
    });
  }

  async execute(name, buys, valuation, mode, receivedAt) {
    const customId = uid(`${mode}_`);
    const requested = round2(buys.reduce((s, b) => s + b.listing.price, 0));
    const dry = this.cfg.dryRun;
    this.db.insertPurchase({ custom_id: customId, mode, requested_usd: requested, status: dry ? 'dry_run' : 'pending', dry_run: dry ? 1 : 0 });

    if (dry) {
      for (const b of buys) {
        const id = this.db.insertItem({ lis_item_id: b.listing.id, custom_id: customId, name, mode, status: 'dry_run', dry_run: 1, buy_price_usd: b.listing.price, fx_at_buy: valuation.fx, expected_net_usd: valuation.netUsd, expected_profit_usd: b.expectedProfit });
        this.db.event('buy.dry_run', { price: b.listing.price, expectedProfit: b.expectedProfit, latencyMs: receivedAt ? Date.now() - receivedAt : null }, id);
      }
      this.log.info(`[DRY-RUN] ${mode}: купил бы «${name}» ×${buys.length} за ${fmtUsd(requested)} (ожид. прибыль ${fmtUsd(buys.reduce((s, b) => s + b.expectedProfit, 0))})`);
      return;
    }

    const { partner, token } = this.cfg.trade;
    let res;
    try {
      res = await this.lis.buy({ ids: buys.map((b) => b.listing.id), partner, token, maxPrice: requested, customId, skipUnavailable: true });
    } catch (e) {
      this.db.updatePurchase(customId, { status: e.code === 'NETWORK' ? 'pending' : 'failed', error: errMsg(e).slice(0, 500) });
      this.failedInRow++;
      if (/balance|недостаточно|insufficient|funds/i.test(errMsg(e))) {
        this.guard.warn('LOW_BALANCE_BUY', `Не хватает баланса LIS для покупки «${name}» (${fmtUsd(requested)}). Пополните баланс.`);
      } else if (e.code === 'NETWORK') {
        // запрос мог дойти — статус выясним по custom_id в трекере покупок
        this.guard.warn('BUY_UNKNOWN', `Сетевая ошибка при покупке «${name}» — проверю статус по custom_id ${customId}`);
      } else {
        this.guard.recordError('lis-buy', e);
      }
      for (const b of buys) this.onRealtimeRemoved({ id: b.listing.id });
      if (this.failedInRow >= this.cfg.risk.maxFailedPurchasesInRow) this.guard.critical('BUY_FAILURES', `${this.failedInRow} неудачных покупок подряд. Последняя ошибка: ${errMsg(e)}`);
      return;
    }

    const skins = Array.isArray(res?.skins) ? res.skins : [];
    const purchaseId = res?.purchase_id != null ? String(res.purchase_id) : null;
    const got = [];
    for (const s of skins) {
      const b = buys.find((x) => String(x.listing.id) === String(s.id)) || buys.find((x) => x.listing.name === s.name);
      const price = Number(s.price ?? b?.listing.price);
      if (b && price > b.maxPrice + 0.0001) {
        this.guard.critical('OVERPAY', `LIS списал ${fmtUsd(price)} за «${name}» при лимите ${fmtUsd(b.maxPrice)}`, { customId, s });
      }
      const unlockAt = s.unlock_at ? Date.parse(s.unlock_at) || null : b?.listing.unlockAt || null;
      const itemId = this.db.insertItem({
        lis_item_id: String(s.id), custom_id: customId, purchase_id: purchaseId, name: s.name || name, mode,
        status: unlockAt && unlockAt > Date.now() ? 'locked_on_lis' : 'bought', buy_price_usd: price,
        fx_at_buy: valuation.fx, expected_net_usd: valuation.netUsd, expected_profit_usd: valuation.netUsd - price,
        lis_unlock_at: unlockAt, lis_status: s.status || null,
      });
      this.db.event('buy.ok', { price, mode, latencyMs: receivedAt ? Date.now() - receivedAt : null, purchaseId }, itemId);
      got.push(price);
    }
    const total = round2(got.reduce((a, b) => a + b, 0));
    this.db.updatePurchase(customId, { purchase_id: purchaseId, total_usd: total, status: got.length ? 'pending' : 'failed', raw: JSON.stringify(res).slice(0, 4000), error: got.length ? null : 'лоты недоступны' });
    for (const b of buys) this.onRealtimeRemoved({ id: b.listing.id });
    if (!got.length) { this.log.info(`«${name}»: лоты уже проданы (${mode})`); return; }
    this.failedInRow = 0;
    this.guard.recordSuccess('lis-buy');
    if (this.balanceRef.value != null) this.balanceRef.value -= total;
    mode === 'rtba' ? this.stats.rtbaBuys++ : this.stats.scanBuys++;
    const exp = got.reduce((s, p) => s + (valuation.netUsd - p), 0);
    this.log.info(`Куплено (${mode}): «${name}» ×${got.length} за ${fmtUsd(total)}, ожидаемая прибыль ${fmtUsd(exp)}`);
    this.notify.send('info', `🛒 ${mode === 'rtba' ? 'RTBA' : 'Скан'}: <b>${esc(name)}</b> ×${got.length} за ${fmtUsd(total)}\nОжидаемая чистая прибыль: ${fmtUsd(exp)}${receivedAt ? `\nЗадержка: ${Date.now() - receivedAt} мс` : ''}`);
  }
}
