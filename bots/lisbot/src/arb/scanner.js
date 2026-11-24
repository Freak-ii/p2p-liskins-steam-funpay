import { errMsg, HOUR } from '../util/common.js';
import { valueItem, netFactor } from './math.js';

const LIS_FEED = 'https://lis-skins.com/market_export_json/csgo.json';

/** Публичный прайс Lis-Skins: Map(name -> {price, count}); цена — самый дешёвый лот без блокировки. */
export async function lisPrices(fetchImpl = globalThis.fetch, url = LIS_FEED) {
  const r = await fetchImpl(url, { headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(120_000) });
  if (!r.ok) throw new Error(`прайс Lis-Skins: HTTP ${r.status}`);
  const arr = await r.json();
  return new Map(arr.filter((x) => x.unlocked_price > 0).map((x) => [x.name, { price: Number(x.unlocked_price), count: Number(x.count) || 0 }]));
}

/**
 * Поиск связок Lis-Skins → Market.CSGO по публичным прайсам; при наличии ключа кандидаты уточняются
 * историей продаж и стаканом без альфаскинов. Одновременно источник оценок для закупщика
 * (тот же интерфейс, что у Valuator: cached / isFresh / refresh).
 */
export class ArbScanner {
  constructor({ cfg, db, client, log, lisFeed = lisPrices, guard = null }) {
    Object.assign(this, { cfg, db, client, log, lisFeed, guard });
    const saved = db?.get('arb.scan');
    this.values = new Map(saved?.values || []);
    this.picks = saved?.picks || [];
    this.lastScanAt = saved?.ts || 0;
    this.scanned = saved?.scanned || 0;
  }

  get a() { return this.cfg.arbitrage; }

  get minRoi() { return this.a.strategy?.minRoi ?? this.cfg.strategy.minRoi; }

  excluded(name) {
    const n = name.toLowerCase();
    return (this.cfg.watchlist.excludePatterns || []).some((p) => n.includes(p.toLowerCase()));
  }

  async scan() {
    const { a, minRoi } = this;
    const st = { ...this.cfg.strategy, ...a.strategy };
    const [lis, asks, bids] = [await this.lisFeed(), await this.client.prices('USD'), await this.client.orders('USD')];
    const k = netFactor(a);
    const pre = [];
    for (const [name, l] of lis) {
      if (l.price < st.minItemPriceUsd || l.price > st.maxItemPriceUsd || l.count < a.minLisCount || this.excluded(name)) continue;
      const ask = asks.get(name), bid = bids.get(name);
      if (!ask || !bid || bid.price >= ask.price || bid.count < a.minBidCount) continue;
      if ((bid.price * k) / l.price < 1 - a.maxDownsidePct) continue;                    // плохая нижняя граница
      if ((ask.price * k) / l.price < 1 + minRoi) continue;                              // даже по аску цели нет
      pre.push({ name, lis: l.price, count: l.count, ask: ask.price, bid: bid.price, bidCount: bid.count });
    }
    pre.sort((x, y) => y.bid / y.lis - x.bid / x.lis);
    const short = pre.slice(0, a.maxCandidates);
    const detail = await this._details(short);

    const values = new Map(), picks = [];
    for (const c of short) {
      const d = detail?.get(c.name);
      const v = valueItem({ name: c.name, ask: d?.ask ?? c.ask, bid: d?.bid ?? c.bid, bidCount: d?.bidCount ?? c.bidCount, history: d?.history ?? null, a, minRoi });
      values.set(c.name, v);
      const roi = v.ok ? v.netUsd / c.lis - 1 : -1;
      if (v.ok && roi >= minRoi) picks.push({ ...c, ask: v.ask, bid: v.bid, mode: v.mode, weeklySales: v.weeklySales, expectedSell: v.expectedSell, roi, bidRoi: v.bidNetUsd / c.lis - 1 });
    }
    picks.sort((x, y) => y.roi - x.roi);
    this.values = values;
    this.picks = picks.slice(0, a.maxPicks);
    this.lastScanAt = Date.now();
    this.scanned = pre.length;
    this.db?.set('arb.scan', { ts: this.lastScanAt, values: [...values].filter(([n]) => this.picks.some((p) => p.name === n)), picks: this.picks, scanned: pre.length });
    this.log.info(`Арбитраж: кандидатов ${pre.length}, проходят цель ${(minRoi * 100).toFixed(1)}% — ${picks.length}${picks[0] ? `, лучший ${picks[0].name} ${(picks[0].roi * 100).toFixed(1)}%` : ''}`);
    return this.picks;
  }

  /** Уточнение по ключу: история и стакан. Валюта аккаунта должна быть USD, иначе цены не сравнить без курса. */
  async _details(list) {
    if (!this.client.hasKey || !list.length) return null;
    try {
      const m = await this.client.money();
      this.currency = m.currency;
      if (m.currency !== 'USD') {
        this.guard?.warn('ARB_CURRENCY', `Аккаунт Market.CSGO в ${m.currency}. Для арбитража нужна валюта USD: история и стакан приходят в валюте аккаунта. Пока считаю только мгновенную продажу в заявку.`);
        return null;
      }
      const out = new Map(list.map((c) => [c.name, {}]));
      for (const [n, h] of await this.client.history(list.map((c) => c.name))) if (out.has(n)) out.get(n).history = h;
      for (const c of list) {
        const b = await this.client.bidAsk(c.name).catch(() => null);
        if (!b?.bid.length || !b.ask.length) continue;
        // верх стакана часто держат 1–2 бота по одной заявке: худший случай считаем по цене, где накопилось bidDepth штук
        let depth = 0, at = null;
        for (const x of b.bid) { depth += x.total; if (depth >= this.a.bidDepth) { at = x; break; } }
        Object.assign(out.get(c.name), { bid: at ? at.price : 0, bidCount: at ? Math.max(depth, this.a.minBidCount) : 0, ask: b.ask[0].price });
      }
      return out;
    } catch (e) {
      this.log.warn(`Арбитраж: история Market.CSGO недоступна (${errMsg(e)}), считаю по заявкам`);
      return null;
    }
  }

  // --- интерфейс Valuator для закупщика ---
  cached(name) { const v = this.values.get(name); return v && Date.now() - v.ts < this.a.maxValuationAgeHours * HOUR ? v : null; }

  isFresh(name) { return !!this.cached(name); }

  async refresh(name) { return this.cached(name); }

  roi(name) { return this.picks.find((p) => p.name === name)?.roi ?? -1; }

  watchMap() { return new Map(this.picks.filter((p) => this.isFresh(p.name)).map((p) => [p.name, { name: p.name, enabled: 1, source: 'arb' }])); }

  get stats() { return { lastScanAt: this.lastScanAt, candidates: this.scanned, currency: this.currency || null, picks: this.picks.slice(0, 50) }; }
}
