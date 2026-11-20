import { EventEmitter } from 'node:events';
import { OUR_STEAMID, LIS_BOT_STEAMID } from './world.js';

/** Входящий оффер в формате steam-tradeoffer-manager (то, чем пользуется TradeHandler). */
class DemoOffer {
  constructor(world, o) {
    this.world = world;
    this.id = o.id;
    this.partner = { getSteamID64: () => LIS_BOT_STEAMID };
    this.itemsToGive = [];
    this.itemsToReceive = o.items.map((i) => ({ ...i }));
    this.raw = o;
  }

  get state() { return this.raw.state; }

  isGlitched() { return false; }

  accept(_autoConfirm, cb) {
    if (typeof _autoConfirm === 'function') cb = _autoConfirm;
    setImmediate(() => { try { this.world.acceptOffer(this.id); cb(null, 'accepted'); } catch (e) { cb(e); } });
  }

  decline(cb) { setImmediate(() => { this.world.declineOffer(this.id); cb?.(null); }); }

  cancel(cb) { this.decline(cb); }

  getExchangeDetails(_getDetails, cb) {
    setImmediate(() => cb(null, this.raw.received ? 3 : 0, new Date(this.raw.created), this.raw.received || [], []));
  }
}

/** TradeOfferManager: события о новых офферах и аудит активных. */
class DemoTradeManager extends EventEmitter {
  constructor(world) {
    super();
    this.world = world;
    this.onOffer = (o) => { if (!this.stopped) setImmediate(() => this.emit('newOffer', new DemoOffer(world, o))); };
    world.on('steam.offer', this.onOffer);
  }

  getOffers(_filter, cb) {
    const received = [...this.world.offers.values()].filter((o) => o.state === 2).map((o) => new DemoOffer(this.world, o));
    setImmediate(() => cb(null, [], received));
  }

  shutdown() { this.stopped = true; this.world.off('steam.offer', this.onOffer); }
}

/**
 * Замена SteamSession для демо: тот же интерфейс (вход, request к steamcommunity, инвентарь,
 * мобильные подтверждения, manager), но ответы берутся из эмулятора рынка, а не из Steam.
 */
export class DemoSteamSession extends EventEmitter {
  constructor({ world, cfg, log }) {
    super();
    Object.assign(this, { world, cfg, log });
    this.manager = new DemoTradeManager(world);
    this.steamId = null;
    this.ready = false;
    this.counters = { requests: 0 };
  }

  async login() {
    this.steamId = OUR_STEAMID;
    this.ready = true;
    this.log?.info?.('Steam (эмуляция): вход выполнен');
    this.emit('ready');
  }

  async syncTime() {}

  sessionId() { return 'demo-session'; }

  async request({ method = 'GET', url, form }) {
    this.counters.requests++;
    this.world.settle();
    const u = new URL(url);
    const p = u.pathname.replace(/^\/market/, '');
    const q = u.searchParams;
    const w = this.world;
    const name = q.get('market_hash_name');
    if (p.startsWith('/pricehistory')) return { success: true, prices: w.priceHistory(name) };
    if (p.startsWith('/listings/730/')) {
      const n = decodeURIComponent(p.slice('/listings/730/'.length));
      const id = Object.keys(w.opts.catalog).indexOf(n);
      if (id < 0) throw Object.assign(new Error('404'), { status: 404 });
      return `<script>Market_LoadOrderSpread( ${100000 + id} );</script>`;
    }
    if (p.startsWith('/itemordershistogram')) {
      const n = Object.keys(w.opts.catalog)[Number(q.get('item_nameid')) - 100000];
      const c = w.steamBuyerCents(n);
      return { success: 1, highest_buy_order: String(Math.floor(c * 0.95)), lowest_sell_order: String(c), sell_order_count: '137' };
    }
    if (p.startsWith('/priceoverview')) {
      const usd = Number(q.get('currency')) === 1;
      const c = w.steamBuyerCents(name, usd ? 1 : w.fx);
      const fmt = (x) => (usd ? `$${(x / 100).toFixed(2)}` : `${(x / 100).toFixed(2).replace('.', ',')} pуб.`);
      return { success: true, lowest_price: fmt(c), median_price: fmt(Math.round(c * 1.01)), volume: '1,240' };
    }
    if (p.startsWith('/sellitem') && method === 'POST') return w.sellItem(form.assetid, form.price);
    if (p.startsWith('/mylistings')) {
      const map = (l) => ({ listingid: l.listingid, asset: { id: l.assetid, market_hash_name: l.name }, price: l.receiveCents, fee: l.feeCents, time_created: Math.floor(l.created / 1000) });
      const all = [...w.market.values()];
      return { success: true, total_count: all.length, listings: all.filter((l) => l.confirmed).map(map), listings_to_confirm: all.filter((l) => !l.confirmed).map(map), listings_on_hold: [] };
    }
    if (p.startsWith('/removelisting/') && method === 'POST') { w.removeMarketListing(p.split('/').pop()); return null; }
    if (p.startsWith('/myhistory')) {
      const listings = {};
      const events = w.sold.slice(0, Number(q.get('count')) || 100).map((s) => {
        listings[s.listingid] = { price: s.receiveCents };
        return { listingid: s.listingid, event_type: 3, time_event: Math.floor(s.ts / 1000) };
      });
      return { success: true, events, listings };
    }
    throw Object.assign(new Error(`эмулятор Steam: неизвестный запрос ${method} ${u.pathname}`), { status: 404 });
  }

  async inventory() { this.world.settle(); return this.world.inventoryItems(); }

  async getConfirmations() { return [...this.world.confirmations.values()].map((c) => ({ ...c })); }

  async respondConfirmation(conf, accept) { this.world.respondConfirmation(conf.id, accept); }

  stop() { this.manager.shutdown(); this.ready = false; }

  get stats() { return { ready: this.ready, steamId: this.steamId, emulated: true, requests: this.counters.requests }; }
}
