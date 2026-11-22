import { EventEmitter } from 'node:events';
import { buyerPaysFor, sellerReceivesFor } from '../steam/fees.js';

/**
 * Эмуляция рынка для демо и интеграционных тестов: LIS-SKINS (лоты, баланс, покупки, отправка
 * трейдов) и Steam (инвентарь с Trade Protection, ТП, мобильные подтверждения, входящие офферы).
 * Деньги и предметы ненастоящие; боту отдаются те же форматы ответов, что у настоящих API,
 * поэтому закупка, приём трейдов и продажа работают своим обычным кодом.
 */

// предмет: [цена закупки на LIS, $; чистая выручка в Steam после комиссий, $] — как в демо центра (center/demo.py)
export const CATALOG = {
  'AK-47 | Redline (Field-Tested)': [11.2, 12.9],
  'AWP | Asiimov (Field-Tested)': [71.5, 80.3],
  'M4A1-S | Printstream (Minimal Wear)': [148.0, 163.5],
  'Glock-18 | Fade (Factory New)': [1215.0, 1302.0],
  'Desert Eagle | Blaze (Factory New)': [402.0, 431.0],
  'USP-S | Kill Confirmed (Field-Tested)': [39.6, 44.1],
  'Revolution Case': [0.7, 0.81],
};

export const LIS_BOT_STEAMID = '76561199000000777';
export const OUR_STEAMID = '76561199000000001';
const HOUR = 3600_000;
const DAY = 24 * HOUR;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Детерминированный генератор (mulberry32): одинаковые прогоны тестов. */
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const round2 = (x) => Math.round(x * 100) / 100;

export class DemoWorld extends EventEmitter {
  /**
   * opts: fx — курс USD→валюта кошелька; balance — баланс LIS, $; tradeDelayMs — через сколько LIS
   * отправляет трейд после покупки; protectionMs — Trade Protection в Steam; saleMs — [от, до] время
   * продажи лота на ТП; listingEveryMs — как часто на LIS появляется новый лот (0 — не появляются);
   * requireConfirmation — лоты ТП ждут мобильного подтверждения; catalog, seed.
   */
  constructor(opts = {}) {
    super();
    this.opts = {
      fx: 92.4, balance: 1840.25, tradeDelayMs: 20_000, protectionMs: 7 * DAY, saleMs: [60_000, 240_000],
      listingEveryMs: 0, requireConfirmation: true, catalog: CATALOG, seed: 42, now: () => Date.now(), ...opts,
    };
    this.rand = rng(this.opts.seed);
    this.now = this.opts.now;
    this.fx = this.opts.fx;
    this.balance = this.opts.balance;
    this.seq = 1000;
    this.listings = new Map();        // LIS: id -> { id, name, price, created }
    this.purchases = new Map();       // LIS: custom_id -> { purchase_id, custom_id, skins, created, offerId }
    this.inventory = new Map();       // Steam: assetid -> { assetid, name, protectedUntil }
    this.market = new Map();          // Steam ТП: listingid -> { listingid, assetid, name, receiveCents, feeCents, created, confirmed, sellAt }
    this.sold = [];                   // Steam: { listingid, receiveCents, ts }
    this.offers = new Map();          // Steam: входящие офферы
    this.confirmations = new Map();   // Steam: id -> { id, key, type, creator, title }
    this.timers = new Set();
    for (const name of Object.keys(this.opts.catalog)) for (let i = 0; i < 6; i++) this.addListing(name, { silent: true });
  }

  nextId() { return ++this.seq; }

  // ------------------------------------------------------------------ цены
  /** Цена, которую платит покупатель в Steam (в копейках/центах валюты кошелька). */
  steamBuyerCents(name, currencyFx = this.fx) {
    const net = this.opts.catalog[name]?.[1];
    if (net == null) return null;
    return buyerPaysFor(Math.round(net * currencyFx * 100));
  }

  steamNetCents(name) { return sellerReceivesFor(this.steamBuyerCents(name)); }

  // ------------------------------------------------------------------ LIS-SKINS
  addListing(name = null, { silent = false } = {}) {
    const names = Object.keys(this.opts.catalog);
    name ??= names[Math.floor(this.rand() * names.length)];
    const buy = this.opts.catalog[name][0];
    // разброс вокруг обычной цены закупки: часть лотов дешевле порога бота, часть дороже
    const price = round2(buy * (0.96 + this.rand() * 0.16));
    const l = { id: String(this.nextId()), name, price, created: this.now() };
    this.listings.set(l.id, l);
    const book = [...this.listings.values()].filter((x) => x.name === name);
    if (book.length > 15) this.removeListing(book.sort((a, b) => b.price - a.price)[0].id);
    if (!silent) this.emit('lis.listing', l);
    return l;
  }

  removeListing(id) {
    if (this.listings.delete(String(id))) this.emit('lis.removed', { id: String(id) });
  }

  search({ names = [], priceFrom, priceTo, sortBy = 'lowest_price' } = {}) {
    let out = [...this.listings.values()].filter((l) => (!names.length || names.includes(l.name))
      && (priceFrom == null || l.price >= priceFrom) && (priceTo == null || l.price <= priceTo));
    out.sort((a, b) => (sortBy === 'highest_price' ? b.price - a.price : sortBy === 'newest' ? b.created - a.created
      : sortBy === 'oldest' ? a.created - b.created : a.price - b.price));
    return out;
  }

  /** Покупка: как POST /v1/market/buy. Ошибка — { status, message }. */
  buy({ ids, maxPrice, customId, skipUnavailable }) {
    if (customId && this.purchases.has(customId)) return this.purchases.get(customId);
    const want = ids.map(String);
    const found = want.map((id) => this.listings.get(id)).filter(Boolean);
    if (found.length < want.length && !skipUnavailable) throw Object.assign(new Error('Some skins are unavailable'), { status: 400 });
    const total = round2(found.reduce((s, l) => s + l.price, 0));
    if (maxPrice != null && total > maxPrice + 1e-9) throw Object.assign(new Error('Total price is greater than max_price'), { status: 400 });
    if (total > this.balance + 1e-9) throw Object.assign(new Error('Insufficient balance'), { status: 400 });
    this.balance = round2(this.balance - total);
    for (const l of found) this.removeListing(l.id);
    const p = {
      purchase_id: this.nextId(), custom_id: customId || `emu_${this.seq}`, created: this.now(), offerId: null,
      skins: found.map((l) => ({ id: Number(l.id), name: l.name, price: l.price, status: 'processing', trade_offer_id: null })),
    };
    this.purchases.set(p.custom_id, p);
    if (found.length) this.later(this.opts.tradeDelayMs, () => this.sendTrade(p));
    return p;
  }

  purchaseView(p) {
    return { purchase_id: p.purchase_id, custom_id: p.custom_id, skins: p.skins.map((s) => ({ ...s })) };
  }

  info({ customIds = [], purchaseIds = [] }) {
    return [...this.purchases.values()].filter((p) => customIds.includes(p.custom_id) || purchaseIds.map(String).includes(String(p.purchase_id)))
      .map((p) => this.purchaseView(p));
  }

  /** LIS отправляет обмен с купленными предметами на наш аккаунт Steam. */
  sendTrade(p) {
    if (p.offerId) return;
    const offer = this.createOffer(p.skins.map((s) => s.name), { purchase: p });
    p.offerId = offer.id;
    for (const s of p.skins) Object.assign(s, { status: 'trade_sent', trade_offer_id: offer.id });
  }

  // ------------------------------------------------------------------ Steam: обмены
  createOffer(names, { purchase = null } = {}) {
    const offer = {
      id: String(this.nextId()), state: 2, purchase, created: this.now(),
      items: names.map((name) => ({ market_hash_name: name, assetid: String(this.nextId()) })),
    };
    this.offers.set(offer.id, offer);
    this.emit('steam.offer', offer);
    return offer;
  }

  acceptOffer(offerId) {
    const o = this.offers.get(String(offerId));
    if (!o || o.state !== 2) throw new Error('Offer is no longer active');
    o.state = 3;
    o.received = o.items.map((it) => {
      const assetid = String(this.nextId());
      this.inventory.set(assetid, { assetid, name: it.market_hash_name, protectedUntil: this.now() + this.opts.protectionMs });
      return { ...it, new_assetid: assetid };
    });
    if (o.purchase) for (const s of o.purchase.skins) s.status = 'accepted';
    return o;
  }

  declineOffer(offerId) {
    const o = this.offers.get(String(offerId));
    if (!o || o.state !== 2) return;
    o.state = 7;
    if (o.purchase) {                                   // LIS возвращает деньги за непринятый обмен
      for (const s of o.purchase.skins) s.status = 'returned';
      this.balance = round2(this.balance + o.purchase.skins.reduce((a, s) => a + s.price, 0));
    }
  }

  // ------------------------------------------------------------------ Steam: инвентарь и ТП
  inventoryItems() {
    const t = this.now();
    return [...this.inventory.values()].map((a) => ({
      assetid: a.assetid, market_hash_name: a.name, marketable: a.protectedUntil <= t,
      ...(a.protectedUntil > t ? { cache_expiration: new Date(a.protectedUntil).toISOString() } : {}),
    }));
  }

  sellItem(assetid, receiveCents) {
    const a = this.inventory.get(String(assetid));
    if (!a) return { success: false, message: 'The item specified is no longer in your inventory or is not allowed to be traded on the Community Market.' };
    if (a.protectedUntil > this.now()) return { success: false, message: 'This item is trade protected and cannot be sold yet.' };
    this.inventory.delete(a.assetid);
    const receive = Math.floor(Number(receiveCents));
    const l = { listingid: String(this.nextId()), assetid: a.assetid, name: a.name, receiveCents: receive,
      feeCents: buyerPaysFor(receive) - receive, created: this.now(), confirmed: !this.opts.requireConfirmation, sellAt: null };
    this.market.set(l.listingid, l);
    if (this.opts.requireConfirmation) {
      const c = { id: String(this.nextId()), key: String(this.nextId()), type: 3, creator: l.listingid, title: `Sell - ${a.name}` };
      this.confirmations.set(c.id, c);
      return { success: true, requires_confirmation: 1, needs_mobile_confirmation: true };
    }
    this.scheduleSale(l);
    return { success: true, requires_confirmation: 0 };
  }

  respondConfirmation(id, accept) {
    const c = this.confirmations.get(String(id));
    if (!c) throw new Error('Подтверждение не найдено');
    this.confirmations.delete(c.id);
    const l = this.market.get(c.creator);
    if (!l) return;
    if (accept) { l.confirmed = true; this.scheduleSale(l); } else this.removeMarketListing(l.listingid);
  }

  /** Лот покупают, если цена не выше рынка более чем на 3%; дороже — висит, пока бот не снизит. */
  scheduleSale(l) {
    const buyerCents = buyerPaysFor(l.receiveCents);
    if (buyerCents > this.steamBuyerCents(l.name) * 1.03) return;
    const [a, b] = this.opts.saleMs;
    l.sellAt = this.now() + a + this.rand() * (b - a);
  }

  removeMarketListing(listingid) {
    const l = this.market.get(String(listingid));
    if (!l) return;
    this.market.delete(l.listingid);
    this.inventory.set(l.assetid, { assetid: l.assetid, name: l.name, protectedUntil: 0 });
  }

  /** Продажи лотов, у которых подошло время. Вызывается таймером и перед ответами Steam. */
  settle() {
    const t = this.now();
    for (const l of [...this.market.values()]) {
      if (!l.confirmed || l.sellAt == null || l.sellAt > t) continue;
      this.market.delete(l.listingid);
      this.sold.unshift({ listingid: l.listingid, receiveCents: l.receiveCents, name: l.name, ts: l.sellAt });
    }
  }

  // ------------------------------------------------------------------ состояние бота (демо-база центра)
  /**
   * Подхватить незавершённые сделки из базы бота, чтобы эмуляция им не противоречила: купленные и
   * ещё не полученные предметы придут обменом, полученные лежат в инвентаре, выставленные — на ТП.
   */
  adoptDb(db) {
    const items = db.itemsByStatus('bought', 'trade_sent', 'protected', 'ready', 'listing', 'listed').filter((i) => !i.dry_run);
    const awaiting = items.filter((i) => ['bought', 'trade_sent'].includes(i.status) && !i.received_at);
    if (awaiting.length) this.later(this.opts.tradeDelayMs, () => this.createOffer(awaiting.map((i) => i.name)));
    for (const it of items) {
      if (['bought', 'trade_sent'].includes(it.status) && !it.received_at) continue;
      if (it.listing_id && ['listing', 'listed'].includes(it.status)) {
        const l = { listingid: it.listing_id, assetid: it.asset_id || String(this.nextId()), name: it.name,
          receiveCents: it.list_receive_cents || this.steamNetCents(it.name), feeCents: 0, created: it.listed_at || this.now(), confirmed: true, sellAt: null };
        this.market.set(l.listingid, l);
        this.scheduleSale(l);
        continue;
      }
      // без номера лота (или ещё на Trade Protection) — предмет в инвентаре
      const assetid = it.asset_id || String(this.nextId());
      const until = it.status === 'protected' ? Math.min(it.tradable_after || 0, this.now() + this.opts.protectionMs) : 0;
      this.inventory.set(assetid, { assetid, name: it.name, protectedUntil: until });
    }
    const last = db.get('balance.last');
    if (last?.balance != null) this.balance = last.balance;
  }

  // ------------------------------------------------------------------ время
  later(ms, fn) {
    const t = setTimeout(() => { this.timers.delete(t); fn(); }, ms);
    t.unref?.();
    this.timers.add(t);
  }

  /** Живой рынок для демо: новые лоты на LIS и продажи на ТП. */
  start() {
    if (this.opts.listingEveryMs > 0) {
      const tick = () => { this.addListing(); if (this.rand() < 0.3) this.removeListing(this.search()[Math.floor(this.rand() * this.listings.size)]?.id); };
      this.ticker = setInterval(tick, this.opts.listingEveryMs);
      this.ticker.unref?.();
    }
    this.settler = setInterval(() => this.settle(), 5000);
    this.settler.unref?.();
    return this;
  }

  stop() {
    clearInterval(this.ticker);
    clearInterval(this.settler);
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
  }

  /** История цен Steam за historyDays: почасовые продажи вокруг рыночной цены (формат pricehistory). */
  priceHistory(name, days = 30) {
    const base = this.steamBuyerCents(name) / 100;
    const r = rng([...name].reduce((s, ch) => s * 31 + ch.charCodeAt(0), 7));
    const out = [];
    const end = Math.floor(this.now() / HOUR) * HOUR;
    for (let t = end - days * DAY; t <= end; t += HOUR) {
      const d = new Date(t);
      const label = `${MONTHS[d.getUTCMonth()]} ${String(d.getUTCDate()).padStart(2, '0')} ${d.getUTCFullYear()} ${String(d.getUTCHours()).padStart(2, '0')}: +0`;
      out.push([label, round2(base * (0.995 + r() * 0.03)), String(2 + Math.floor(r() * 5))]);
    }
    return out;
  }
}

export { HOUR, DAY };
