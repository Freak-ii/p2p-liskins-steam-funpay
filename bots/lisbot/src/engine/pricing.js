import { computeValuation, holdDaysFor } from '../pricing/valuation.js';
import { errMsg, HOUR } from '../util/common.js';

/**
 * Курс USD -> валюта кошелька Steam. В режиме auto берётся из самого Steam
 * (одна и та же вещь в двух валютах) — именно по этому курсу Steam пересчитывает цены.
 */
export class FxService {
  constructor({ cfg, market, db, guard, log }) {
    Object.assign(this, { cfg, market, db, guard, log });
    const saved = db.get('fx');
    this.rate = saved?.rate || (cfg.fx.usdToWallet === 'auto' ? cfg.fx.fallbackRate : Number(cfg.fx.usdToWallet));
    this.updatedAt = saved?.ts || 0;
    if (cfg.steam.walletCurrency === 1) this.rate = 1;
  }

  get stale() { return Date.now() - this.updatedAt > this.cfg.fx.refreshHours * HOUR; }

  async refresh() {
    const { cfg } = this;
    if (cfg.steam.walletCurrency === 1) { this.rate = 1; this.updatedAt = Date.now(); return 1; }
    if (cfg.fx.usdToWallet !== 'auto') { this.rate = Number(cfg.fx.usdToWallet); this.updatedAt = Date.now(); return this.rate; }
    try {
      const usd = await this.market.priceOverview(cfg.fx.referenceItem, 1);
      const wal = await this.market.priceOverview(cfg.fx.referenceItem, cfg.steam.walletCurrency);
      const rate = (wal.lowest || wal.median) / (usd.lowest || usd.median);
      const { min, max } = cfg.risk.fxSanity;
      if (!Number.isFinite(rate) || rate < min || rate > max) {
        this.guard.warn('FX_SANITY', `Курс ${rate?.toFixed?.(2)} вне допустимых границ [${min}; ${max}] — оставляю ${this.rate}`);
        return this.rate;
      }
      if (this.updatedAt && Math.abs(rate / this.rate - 1) > cfg.fx.maxChangePct) {
        this.guard.warn('FX_JUMP', `Курс изменился резко: ${this.rate.toFixed(2)} → ${rate.toFixed(2)}. Проверьте; закупки используют новый курс.`);
      }
      this.rate = rate;
      this.updatedAt = Date.now();
      this.db.set('fx', { rate, ts: this.updatedAt });
      this.log.info(`Курс USD→${cfg.steam.walletCurrencyCode}: ${rate.toFixed(3)}`);
    } catch (e) {
      this.log.warn(`FX: не удалось обновить (${errMsg(e)}), использую ${this.rate}`);
    }
    return this.rate;
  }
}

/** Оценки предметов по данным Steam с кэшем в БД. */
export class Valuator {
  constructor({ cfg, market, db, fx, log }) {
    Object.assign(this, { cfg, market, db, fx, log });
    this.inflight = new Map();
  }

  /** Возвращает сохранённую оценку в виде объекта для стратегии (или null). */
  cached(name) {
    const r = this.db.valuation(name);
    if (!r) return null;
    return {
      name, ts: r.ts, ok: !!r.ok, reason: r.reason, qPrice: r.q_price, medianPrice: r.median_price, recentMedian: r.recent_median,
      lowestSell: r.lowest_sell, highestBuy: r.highest_buy, dailyVolume: r.daily_volume, trend: r.trend, volatility: r.volatility,
      expectedSell: r.expected_sell, netWallet: r.net_wallet, netUsd: r.net_usd, fx: r.fx, maxBuyUsd: r.max_buy_usd,
    };
  }

  isFresh(name, maxAgeH = this.cfg.strategy.valuationTtlHours) {
    const v = this.db.valuation(name);
    return !!v && Date.now() - v.ts < maxAgeH * HOUR;
  }

  refresh(name) {
    if (this.inflight.has(name)) return this.inflight.get(name);
    const p = (async () => {
      const [history, book] = [await this.market.priceHistory(name), await this.market.orderBook(name).catch(() => null)];
      const v = computeValuation({ name, history, book, fx: this.fx.rate, cfg: this.cfg, holdDays: holdDaysFor(this.cfg, 0) });
      this.db.saveValuation(v);
      return v;
    })().finally(() => this.inflight.delete(name));
    this.inflight.set(name, p);
    return p;
  }
}
