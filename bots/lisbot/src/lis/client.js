import { RequestScheduler, RateLimitedError } from '../util/rateLimiter.js';
import { sleep } from '../util/common.js';

export class LisApiError extends Error {
  constructor(message, { status, body, code } = {}) { super(message); this.status = status; this.body = body; this.code = code || 'LIS_API'; }
}

/** Приводит запись лота из API (поля могут немного отличаться между версиями) к единому виду. */
export function normalizeListing(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const id = raw.id ?? raw.item_id ?? raw.skin_id;
  const name = raw.name ?? raw.market_hash_name ?? raw.skin?.name ?? raw.full_name;
  const price = Number(raw.price ?? raw.final_withdrawal_price ?? raw.final_price);
  if (id == null || !name || !Number.isFinite(price)) return null;
  let unlockAt = raw.unlock_at ?? raw.unlocked_at ?? null;
  if (unlockAt && typeof unlockAt === 'string') unlockAt = Date.parse(unlockAt) || null;
  else if (typeof unlockAt === 'number' && unlockAt < 1e12) unlockAt *= 1000;
  return {
    id: String(id), name: String(name), price,
    unlockAt: unlockAt && unlockAt > Date.now() ? unlockAt : null,
    float: raw.item_float != null ? Number(raw.item_float) : null,
    createdAt: raw.created_at ? Date.parse(raw.created_at) || null : null,
  };
}

/** Клиент официального API LIS-SKINS (https://api.lis-skins.com/v1, Bearer-ключ). */
export class LisClient {
  constructor({ apiKey, baseUrl, requestsPerSecond = 2, log, fetchImpl = globalThis.fetch }) {
    this.apiKey = apiKey;
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.log = log;
    this.fetch = fetchImpl;
    this.sched = new RequestScheduler({
      name: 'lis', minDelayMs: Math.ceil(1000 / requestsPerSecond), jitterMs: 150, maxPerHour: requestsPerSecond * 3000,
      backoffMs: 30_000, maxBackoffMs: 15 * 60_000,
      onBackoff: (ms) => log?.warn(`LIS 429: пауза ${Math.round(ms / 1000)}с`),
    });
  }

  async request(method, path, { query, body, priority = 0, retries = 2 } = {}) {
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of Object.entries(query || {})) {
      if (v === undefined || v === null) continue;
      if (Array.isArray(v)) v.forEach((x) => url.searchParams.append(`${k}[]`, String(x)));
      else url.searchParams.set(k, String(v));
    }
    let attempt = 0;
    for (;;) {
      try {
        return await this.sched.schedule(() => this._do(method, url, body), { priority, label: path });
      } catch (e) {
        const retriable = e.code === 'RATE_LIMITED' || e.code === 'NETWORK' || (e.status >= 500 && e.status < 600);
        if (!retriable || attempt >= retries || method === 'POST') throw e; // POST /buy никогда не повторяем вслепую
        attempt++;
        await sleep(1500 * attempt);
      }
    }
  }

  async _do(method, url, body) {
    let res;
    try {
      res = await this.fetch(url, {
        method,
        headers: { Accept: 'application/json', Authorization: `Bearer ${this.apiKey}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(20_000),
      });
    } catch (e) {
      throw new LisApiError(`LIS сеть: ${e.message}`, { code: 'NETWORK' });
    }
    const text = await res.text();
    let json = null;
    try { json = text ? JSON.parse(text) : null; } catch { /* не JSON */ }
    if (res.status === 429) {
      const ra = Number(res.headers.get('retry-after')) * 1000 || 0;
      this.sched.rateLimited(ra);
      throw new RateLimitedError('LIS rate limit', ra);
    }
    this.sched.ok();
    if (res.status === 401 || res.status === 403) throw new LisApiError(`LIS авторизация отклонена (${res.status})`, { status: res.status, body: json ?? text, code: 'AUTH' });
    if (!res.ok) {
      const msg = json?.message || json?.error || (json?.errors && JSON.stringify(json.errors)) || text.slice(0, 300);
      throw new LisApiError(`LIS ${res.status}: ${msg}`, { status: res.status, body: json ?? text });
    }
    return json;
  }

  async balance() {
    const r = await this.request('GET', '/user/balance', { priority: 5 });
    const b = Number(r?.data?.balance ?? r?.balance);
    if (!Number.isFinite(b)) throw new LisApiError('LIS: неожиданный ответ баланса', { body: r });
    return b;
  }

  /**
   * Поиск лотов. sort_by: oldest | newest | lowest_price | highest_price.
   * Возвращает { items, nextCursor }.
   */
  async search({ game, names, priceFrom, priceTo, onlyUnlocked = 1, unlockDays, sortBy = 'lowest_price', cursor, floatFrom, floatTo }) {
    const r = await this.request('GET', '/market/search', {
      query: {
        game, names, price_from: priceFrom, price_to: priceTo, only_unlocked: onlyUnlocked, unlock_days: unlockDays,
        sort_by: sortBy, cursor, float_from: floatFrom, float_to: floatTo,
      },
    });
    return { items: (r?.data || []).map(normalizeListing).filter(Boolean), nextCursor: r?.meta?.next_cursor || null };
  }

  /** Покупка. max_price — страховка от изменения цены между поиском и покупкой. custom_id — идемпотентность. */
  async buy({ ids, partner, token, maxPrice, customId, skipUnavailable = true }) {
    const body = { ids: ids.map(Number), partner: String(partner), token, custom_id: customId };
    if (maxPrice != null) body.max_price = maxPrice;
    if (skipUnavailable) body.skip_unavailable = true;
    const r = await this.request('POST', '/market/buy', { body, priority: 10, retries: 0 });
    return r?.data ?? r;
  }

  async purchaseInfo({ purchaseIds, customIds }) {
    const r = await this.request('GET', '/market/info', { query: { purchase_ids: purchaseIds, custom_ids: customIds }, priority: 3 });
    return r?.data ?? [];
  }

  async checkAvailability(ids) {
    const r = await this.request('GET', '/market/check-availability', { query: { ids }, priority: 8 });
    return r?.data ?? [];
  }

  async withdraw({ purchaseId, customId, partner, token }) {
    const body = { purchase_id: purchaseId, custom_id: customId, partner, token };
    Object.keys(body).forEach((k) => body[k] == null && delete body[k]);
    const r = await this.request('POST', '/market/withdraw', { body, priority: 4, retries: 0 });
    return r?.data ?? r;
  }

  async history({ page, startUnix, endUnix } = {}) {
    return this.request('GET', '/market/history', { query: { page, start_unix_time: startUnix, end_unix_time: endUnix } });
  }

  async wsToken() {
    const r = await this.request('GET', '/user/get-ws-token', { priority: 5 });
    return r?.data?.token ?? r?.token;
  }
}
