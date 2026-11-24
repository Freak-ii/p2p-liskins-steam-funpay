import { RequestScheduler, RateLimitedError } from '../util/rateLimiter.js';

export class CsgoApiError extends Error {
  constructor(message, { status, code } = {}) { super(message); this.status = status; this.code = code || 'MCSGO_API'; }
}

/** Сколько минимальных единиц в одной единице валюты (документация Market.CSGO: 1 RUB = 100, 1 USD = 1000, 1 EUR = 1000). */
export const UNITS = { RUB: 100, USD: 1000, EUR: 1000 };

/**
 * Клиент API Market.CSGO v2 (https://market.csgo.com/ru/api).
 * Публичные прайсы работают без ключа. Метода вывода денег в API нет, поэтому украсть баланс через этот клиент нельзя;
 * перевод баланса (money-send) сознательно не реализован.
 * Больше 5 запросов в секунду — удаление ключа, поэтому очередь не быстрее 2 в секунду.
 */
export class CsgoMarketClient {
  constructor({ apiKey = '', baseUrl = 'https://market.csgo.com/api/v2', log, fetchImpl = globalThis.fetch, minDelayMs = 500 } = {}) {
    Object.assign(this, { apiKey, baseUrl: baseUrl.replace(/\/$/, ''), log, fetch: fetchImpl });
    this.sched = new RequestScheduler({
      name: 'mcsgo', minDelayMs, jitterMs: minDelayMs ? 100 : 0, maxPerHour: 4000, backoffMs: 60_000, maxBackoffMs: 15 * 60_000,
      onBackoff: (ms) => log?.warn(`Market.CSGO 429: пауза ${Math.round(ms / 1000)} с`),
    });
  }

  get hasKey() { return !!this.apiKey; }

  async _call(method, path, { query, body, auth = true, timeout = 30_000 } = {}) {
    if (auth && !this.apiKey) throw new CsgoApiError('нет ключа Market.CSGO', { code: 'NO_KEY' });
    const url = new URL(this.baseUrl + path);
    if (auth) url.searchParams.set('key', this.apiKey);
    for (const [k, v] of Object.entries(query || {})) {
      if (v === undefined || v === null) continue;
      if (Array.isArray(v)) v.forEach((x) => url.searchParams.append(`${k}[]`, String(x)));
      else url.searchParams.set(k, String(v));
    }
    return this.sched.schedule(async () => {
      let res;
      try {
        res = await this.fetch(url, {
          method, headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
          body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(timeout),
        });
      } catch (e) {
        throw new CsgoApiError(`Market.CSGO сеть: ${e.message}`, { code: 'NETWORK' }); // url с ключом в текст ошибки не попадает
      }
      const text = await res.text();
      if (res.status === 429) { this.sched.rateLimited(); throw new RateLimitedError('Market.CSGO 429'); }
      this.sched.ok();
      let json = null;
      try { json = text ? JSON.parse(text) : null; } catch { /* не JSON */ }
      if (!res.ok && !json) throw new CsgoApiError(`Market.CSGO ${res.status}`, { status: res.status });
      if (res.status === 401 || /bad key/i.test(json?.error || '')) throw new CsgoApiError('Market.CSGO: ключ отклонён', { status: res.status, code: 'AUTH' });
      return json;
    });
  }

  /** Лучшие предложения: Map(name -> {price, count}). */
  async prices(cur = 'USD') {
    const r = await this._call('GET', `/prices/${cur}.json`, { auth: false, timeout: 90_000 });
    return new Map((r?.items || []).map((x) => [x.market_hash_name, { price: Number(x.price), count: Number(x.volume) || 0 }]));
  }

  /** Лучшие заявки на покупку: Map(name -> {price, count}). */
  async orders(cur = 'USD') {
    const r = await this._call('GET', `/prices/orders/${cur}.json`, { auth: false, timeout: 90_000 });
    return new Map((r?.items || []).map((x) => [x.market_hash_name, { price: Number(x.price), count: Number(x.volume) || 0 }]));
  }

  /** История продаж: Map(name -> [{ts, price}]), цены в валюте аккаунта. */
  async history(names) {
    const out = new Map();
    for (let i = 0; i < names.length; i += 20) {
      const r = await this._call('GET', '/get-list-items-info', { query: { list_hash_name: names.slice(i, i + 20) } });
      if (!r?.success) continue;
      for (const [n, d] of Object.entries(r.data || {})) {
        out.set(n, (d?.history || []).map(([ts, p]) => ({ ts: Number(ts) * 1000, price: Number(p) })).filter((h) => h.ts && h.price > 0));
      }
    }
    return out;
  }

  /** Стакан без альфаскинов (у них другая цена: они без трейдбана). */
  async bidAsk(name) {
    const r = await this._call('GET', '/bid-ask', { query: { hash_name: name, with_alfaskins: 0 } });
    const norm = (a) => (a || []).map((x) => ({ price: Number(x.price), total: Number(x.total) || 0 })).filter((x) => x.price > 0);
    return { bid: norm(r?.bid).sort((a, b) => b.price - a.price), ask: norm(r?.ask).sort((a, b) => a.price - b.price), currency: r?.currency || null };
  }

  async money() {
    const r = await this._call('GET', '/get-money');
    if (!r?.success) throw new CsgoApiError(r?.error || 'get-money: неожиданный ответ');
    return { money: Number(r.money), settlement: Number(r.money_settlement) || 0, currency: r.currency };
  }

  async test() { return (await this._call('GET', '/test'))?.status || {}; }

  /** Наши предметы на площадке (статусы: 1 на продаже, 2 продан и ждёт передачи; settlement > 0 — передан, деньги в холде). */
  async items() {
    const r = await this._call('GET', '/items');
    if (!r?.success) throw new CsgoApiError(r?.error || 'items: неожиданный ответ');
    return (r.items || []).map((x) => ({
      itemId: String(x.item_id), assetId: String(x.assetid), name: x.market_hash_name, price: Number(x.price), currency: x.currency,
      status: Number(x.status), settlement: Number(x.settlement) || 0, left: x.left, source: x.source || 'STEAM',
    }));
  }

  async updateInventory() { return this._call('GET', '/update-inventory'); }

  async addToSale(assetId, price, cur) {
    const r = await this._call('GET', '/add-to-sale', { query: { id: assetId, price: Math.round(price * UNITS[cur]), cur } });
    if (!r?.success) throw new CsgoApiError(r?.error || 'не удалось выставить', { code: r?.error });
    return String(r.item_id);
  }

  /** price = 0 снимает с продажи. */
  async setPrice(itemId, price, cur) {
    const r = await this._call('GET', '/set-price', { query: { item_id: itemId, price: Math.round(price * UNITS[cur]), cur } });
    if (!r?.success) throw new CsgoApiError(r?.error || 'не удалось изменить цену', { code: r?.error });
    return true;
  }

  /** Включить продажи (раз в 3 минуты). Требует web-токен Steam, срок жизни 24 ч. */
  async ping(accessToken) { return this._call('POST', '/ping-new', { body: { access_token: accessToken } }); }

  /** Данные для обменов с покупателями. */
  async giveRequests() {
    const r = await this._call('GET', '/trade-request-give-p2p-all');
    return r?.success ? r.offers || [] : [];
  }

  async tradeReady(offerId) { return this._call('GET', '/trade-ready', { query: { tradeoffer: offerId } }); }

  async saleHistory(fromTs, toTs = Date.now()) {
    const r = await this._call('GET', '/history', { query: { date: Math.floor(fromTs / 1000), date_end: Math.floor(toTs / 1000) } });
    return (r?.data || []).filter((x) => x.event === 'sell').map((x) => ({
      itemId: String(x.item_id), name: x.market_hash_name, stage: Number(x.stage), received: Number(x.received) / (UNITS[x.currency] || 100), currency: x.currency,
    }));
  }
}
