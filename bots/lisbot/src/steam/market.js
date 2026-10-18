import { parseSteamPrice } from '../util/common.js';

const BASE = 'https://steamcommunity.com/market';
const enc = (s) => encodeURIComponent(s);

/** Разбор даты из pricehistory: "Oct 10 2025 01: +0" */
export function parseHistoryDate(s) {
  const m = /^(\w{3}) (\d{1,2}) (\d{4}) (\d{1,2}): \+0$/.exec(s);
  if (!m) return Date.parse(s) || NaN;
  return Date.parse(`${m[1]} ${m[2]} ${m[3]} ${m[4]}:00:00 UTC`);
}

/** Методы торговой площадки Steam поверх SteamSession (все запросы — через очереди с паузами). */
export class SteamMarket {
  constructor({ session, db, cfg, log }) {
    Object.assign(this, { session, db, cfg, log });
  }

  /** История продаж (нужна авторизация). Возвращает [{ ts, price, volume }] в валюте кошелька. */
  async priceHistory(name) {
    const body = await this.session.request({ url: `${BASE}/pricehistory/?appid=730&market_hash_name=${enc(name)}`, kind: 'price' });
    if (!body?.success || !Array.isArray(body.prices)) throw new Error(`pricehistory: нет данных для "${name}"`);
    return body.prices.map(([d, p, v]) => ({ ts: parseHistoryDate(d), price: Number(p), volume: parseInt(String(v).replace(/\D/g, ''), 10) || 0 }))
      .filter((x) => Number.isFinite(x.ts) && Number.isFinite(x.price));
  }

  async itemNameId(name, appid = 730) {
    const cached = this.db.nameid(name);
    if (cached) return cached;
    const html = await this.session.request({ url: `${BASE}/listings/${appid}/${enc(name)}`, json: false, kind: 'price' });
    const m = /Market_LoadOrderSpread\(\s*(\d+)\s*\)/.exec(String(html));
    if (!m) throw new Error(`item_nameid не найден для "${name}"`);
    this.db.saveNameid(name, m[1]);
    return m[1];
  }

  /** Стакан: лучший ордер на покупку и минимальный лот (в валюте кошелька, не в центах). */
  async orderBook(name, appid = 730) {
    const nameid = await this.itemNameId(name, appid);
    const c = this.cfg.steam.walletCurrency;
    const body = await this.session.request({ url: `${BASE}/itemordershistogram?country=RU&language=english&currency=${c}&item_nameid=${nameid}&two_factor=0`, kind: 'price' });
    if (!body || body.success !== 1) throw new Error(`itemordershistogram: ошибка для "${name}"`);
    const toMoney = (v) => (v == null || v === '' ? null : Number(v) / 100);
    return {
      highestBuy: toMoney(body.highest_buy_order),
      lowestSell: toMoney(body.lowest_sell_order),
      sellCount: Number(String(body.sell_order_count ?? '').replace(/\D/g, '')) || null,
    };
  }

  async priceOverview(name, currency) {
    const body = await this.session.request({ url: `${BASE}/priceoverview/?appid=730&currency=${currency}&market_hash_name=${enc(name)}`, kind: 'price' });
    if (!body?.success) throw new Error(`priceoverview: ошибка для "${name}"`);
    return { lowest: parseSteamPrice(body.lowest_price), median: parseSteamPrice(body.median_price), volume: parseInt(String(body.volume || '0').replace(/\D/g, ''), 10) || 0 };
  }

  /** Выставить предмет. receiveCents — сколько получит продавец (без комиссий). */
  async sellItem(assetId, receiveCents) {
    const body = await this.session.request({
      method: 'POST', url: `${BASE}/sellitem/`, priority: 5,
      headers: { Referer: `https://steamcommunity.com/profiles/${this.session.steamId}/inventory/` },
      form: { sessionid: this.session.sessionId(), appid: 730, contextid: 2, assetid: String(assetId), amount: 1, price: receiveCents },
    }).catch((e) => ({ success: false, message: e.body?.message || e.message }));
    return body || { success: false, message: 'пустой ответ' };
  }

  /** Все активные лоты + ожидающие подтверждения. */
  async myListings() {
    const out = { active: [], toConfirm: [], onHold: [] };
    for (let start = 0; start < 2000; start += 100) {
      const body = await this.session.request({ url: `${BASE}/mylistings/?start=${start}&count=100&norender=1`, priority: 3 });
      if (!body?.success) throw new Error('mylistings: ошибка');
      const map = (l) => ({
        listingId: String(l.listingid), assetId: String(l.asset?.id ?? ''), name: l.asset?.market_hash_name,
        receiveCents: Number(l.price), feeCents: Number(l.fee), createdAt: Number(l.time_created) * 1000 || null,
      });
      out.active.push(...(body.listings || []).map(map));
      out.toConfirm.push(...(body.listings_to_confirm || []).map(map));
      out.onHold.push(...(body.listings_on_hold || []).map(map));
      if (start + 100 >= Number(body.total_count || 0)) break;
    }
    return out;
  }

  /** Ордер на покупку (мост ключей). priceCents — цена за штуку в центах валюты кошелька. */
  async createBuyOrder({ appid, name, priceCents, quantity, confirmation = '0' }) {
    const body = await this.session.request({
      method: 'POST', url: `${BASE}/createbuyorder/`, priority: 4,
      headers: { Referer: `${BASE}/listings/${appid}/${enc(name)}` },
      form: { sessionid: this.session.sessionId(), currency: this.cfg.steam.walletCurrency, appid, market_hash_name: name, price_total: priceCents * quantity, quantity, billing_state: '', save_my_address: 0, confirmation },
    }).catch((e) => ({ success: 0, message: e.body?.message || e.message }));
    return body || { success: 0, message: 'пустой ответ' };
  }

  async buyOrderStatus(buyOrderId) {
    return this.session.request({ url: `${BASE}/getbuyorderstatus/?sessionid=${enc(this.session.sessionId())}&buy_orderid=${enc(buyOrderId)}`, priority: 3 });
  }

  async cancelBuyOrder(buyOrderId) {
    return this.session.request({ method: 'POST', url: `${BASE}/cancelbuyorder/`, form: { sessionid: this.session.sessionId(), buy_orderid: String(buyOrderId) }, priority: 4 });
  }

  async removeListing(listingId) {
    // Steam отвечает пустым телом при успехе — отключаем проверку JSON
    return this.session.request({ method: 'POST', url: `${BASE}/removelisting/${listingId}`, form: { sessionid: this.session.sessionId() }, priority: 4, json: true, extra: { checkJsonError: false } });
  }

  /** История операций на ТП: проданные лоты. Возвращает Map(listingId -> { receiveCents, ts }). */
  async soldListings(count = 100) {
    const body = await this.session.request({ url: `${BASE}/myhistory/?start=0&count=${count}&norender=1`, priority: 2 });
    const res = new Map();
    if (!body?.success) return res;
    const listings = body.listings || {};
    for (const ev of body.events || []) {
      if (Number(ev.event_type) !== 3) continue; // 3 = лот продан
      const l = listings[ev.listingid] || {};
      res.set(String(ev.listingid), { receiveCents: Number(l.price) || null, ts: Number(ev.time_event) * 1000 || Date.now() });
    }
    return res;
  }
}
