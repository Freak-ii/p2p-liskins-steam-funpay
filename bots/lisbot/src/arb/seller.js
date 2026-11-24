import TradeOfferManager from 'steam-tradeoffer-manager';
import { Seller } from '../engine/seller.js';
import { errMsg, sleep, HOUR, DAY, fmtUsd } from '../util/common.js';
import { esc } from '../notify/telegram.js';
import { listPrice, netFactor } from './math.js';

const EState = TradeOfferManager.ETradeOfferState;
const STEAMID64_BASE = 76561197960265728n;
const OPEN = [EState.Active, EState.CreatedNeedsConfirmation];
export const ARB_PREFIX = 'mcsgo:';
export const isArbListing = (it) => String(it.listing_id || '').startsWith(ARB_PREFIX);
const marketId = (it) => String(it.listing_id).slice(ARB_PREFIX.length);

/**
 * Продажа на Market.CSGO вместо ТП Steam:
 *   ready → listed → delivering (покупатель оплатил, обмен отправлен) → settling (обмен принят, деньги в холде) → sold.
 * Исходящий обмен создаётся только под запись Market.CSGO о продаже нашего предмета: предмет есть в CRM в статусе listed,
 * отдаём ровно его, взамен ничего не берём. Оффер запоминается (id, получатель, assetid) до подтверждения, поэтому защита
 * TradeHandler/ConfirmationWatcher по-прежнему считает любой другой исходящий обмен взломом.
 * Если продажа на площадке сорвалась, наш оффер сразу отменяется: иначе покупатель заберёт предмет бесплатно.
 */
export class ArbSeller extends Seller {
  constructor(deps) {
    super(deps);
    this.client = deps.client;
    this.state = { offers: {}, ...this.db.get('arb.offers', {}) };
    for (const [id, o] of Object.entries(this.state.offers)) if (o.open) this.conf?.expectTrade(id);
    this.online = null;
    this.money = null;
  }

  get a() { return this.cfg.arbitrage; }

  _save() { this.db.set('arb.offers', this.state); }

  get stats() {
    return { online: this.online, money: this.money, openOffers: Object.entries(this.state.offers).filter(([, o]) => o.open).map(([id, o]) => ({ id, assetId: o.assetId, ts: o.ts })) };
  }

  // ---------- свои офферы ----------
  isOwnOffer(offer) {
    const own = this.state.offers[String(offer.id)];
    if (!own || offer.partner?.getSteamID64?.() !== own.partner || (offer.itemsToReceive || []).length) return false;
    return offer.itemsToGive?.length === 1 && String(offer.itemsToGive[0].assetid || offer.itemsToGive[0].id) === own.assetId;
  }

  onOwnOfferChanged(offer) {
    const id = String(offer.id);
    const own = this.state.offers[id];
    if (!own?.open || OPEN.includes(offer.state)) return;
    own.open = false;
    own.finalState = EState[offer.state];
    this.conf?.expectedTrades.delete(id);
    this._save();
    const it = this.db.item(own.crmId);
    if (!it || it.status !== 'delivering') return;
    if (offer.state === EState.Accepted) {
      this.db.setItemStatus(it.id, 'settling', {}, { offerId: id });
      this.notify.send('info', `📤 Покупатель Market.CSGO принял обмен: <b>${esc(it.name)}</b>. Деньги в холде площадки до ~8 дней.`);
    } else {
      this.db.setItemStatus(it.id, 'listed', {}, { offerId: id, state: own.finalState });
      this.log.warn(`Market.CSGO: обмен ${id} не состоялся (${own.finalState})`);
    }
  }

  async cancelOffer(id, why) {
    const own = this.state.offers[id];
    const offer = await new Promise((res, rej) => this.session.manager.getOffer(id, (err, o) => (err ? rej(err) : res(o))));
    if (OPEN.includes(offer.state)) {
      await new Promise((res, rej) => offer.cancel((err) => (err ? rej(err) : res())));
      own.open = false; own.finalState = 'Canceled';
      this.conf?.expectedTrades.delete(id);
      this._save();
      const it = this.db.item(own.crmId);
      if (it?.status === 'delivering') this.db.setItemStatus(it.id, 'listed', {}, { offerId: id, canceled: why });
      this.log.warn(`Market.CSGO: обмен ${id} отменён (${why})`);
    } else this.onOwnOfferChanged(offer);
  }

  // ---------- онлайн ----------
  async steamToken() {
    const r = await this.session.request({ url: 'https://steamcommunity.com/pointssummary/ajaxgetasyncconfig', priority: 4 });
    const t = r?.data?.webapi_token;
    if (!t) throw new Error('Steam не выдал web-токен');
    return t;
  }

  /** Market.CSGO продаёт только при «онлайне» раз в 3 минуты; токен Steam уходит площадке, поэтому по умолчанию выключено. */
  async ping() {
    if (this.cfg.dryRun || !this.a.sendSteamToken || !this.guard.canSell || !this.db.itemsByStatus('listed', 'delivering').some(isArbListing)) return;
    if (!this._token || Date.now() - this._tokenAt > 6 * HOUR) { this._token = await this.steamToken(); this._tokenAt = Date.now(); }
    const r = await this.client.ping(this._token);
    if (r?.success === false && /token/i.test(r.message || '')) this._token = null;
    const on = !!r?.online;
    if (!on && !/too early/i.test(r?.message || '')) this.guard.warn('ARB_OFFLINE', `Market.CSGO не включил продажи: ${r?.message || JSON.stringify(r).slice(0, 200)}`);
    this.online = on || /too early/i.test(r?.message || '');
  }

  // ---------- передача проданного ----------
  async deliver() {
    if (this.cfg.dryRun || !this.guard.canSell || !this.client.hasKey) return;
    const tracked = this.db.itemsByStatus('listed', 'delivering').filter(isArbListing);
    if (!tracked.length && !Object.values(this.state.offers).some((o) => o.open)) return;
    const items = await this.client.items();
    const byAsset = new Map(items.map((i) => [i.assetId, i]));

    // продажа сорвалась или оффер висит слишком долго — отменяем, пока покупатель не забрал предмет даром
    for (const [id, o] of Object.entries(this.state.offers)) {
      if (!o.open) { if (Date.now() - o.ts > 30 * DAY) delete this.state.offers[id]; continue; }
      const m = byAsset.get(o.assetId);
      const alive = m && (m.status === 2 || m.settlement > 0);
      if (!alive && Date.now() - o.ts > 60_000) await this.cancelOffer(id, 'продажа на площадке больше не активна').catch((e) => this.log.error(`отмена ${id}: ${errMsg(e)}`));
      else if (Date.now() - o.ts > this.a.offerTtlMin * 60_000) await this.cancelOffer(id, `покупатель не принял за ${this.a.offerTtlMin} мин`).catch((e) => this.log.error(`отмена ${id}: ${errMsg(e)}`));
    }
    if (!items.some((i) => i.status === 2)) return;

    for (const req of await this.client.giveRequests()) {
      const list = req.items || [];
      const assetId = String(list[0]?.assetid || '');
      const it = list.length === 1 && tracked.find((x) => x.asset_id === assetId);
      if (!it || Number(list[0].appid) !== 730 || byAsset.get(assetId)?.status !== 2 || marketId(it) !== byAsset.get(assetId).itemId) {
        this.guard.warn('ARB_UNKNOWN_SALE', `Market.CSGO просит передать предмет, которого нет среди выставленных ботом (asset ${assetId || '?'}). Обмен не отправлен.`);
        continue;
      }
      if (Object.values(this.state.offers).some((o) => o.hash === req.hash || (o.open && o.assetId === assetId))) continue;
      await this.sendOffer(it, req);
    }
  }

  async sendOffer(it, req) {
    const partner = String(STEAMID64_BASE + BigInt(req.partner));
    const offer = this.session.manager.createOffer(`https://steamcommunity.com/tradeoffer/new/?partner=${req.partner}&token=${req.token}`);
    offer.addMyItems([{ appid: 730, contextid: '2', assetid: it.asset_id }]);
    offer.setMessage(String(req.tradeoffermessage || ''));
    let status;
    try {
      status = await new Promise((res, rej) => offer.send((err, st) => {
        if (err) return rej(err);
        this.state.offers[String(offer.id)] = { partner, assetId: it.asset_id, crmId: it.id, hash: req.hash, ts: Date.now(), open: true };
        this._save();
        this.conf.expectTrade(offer.id);
        this.db.setItemStatus(it.id, 'delivering', {}, { offerId: String(offer.id), partner });
        res(st);
      }));
    } catch (e) {
      this.guard.recordError('arb-send', e);
      return;
    }
    if (status === 'pending') {
      let ok = false;
      for (let i = 0; i < 4 && !ok; i++) {
        if (i) await sleep(5_000 * i);
        try { ok = await this.conf.confirmTrade(offer.id); } catch (e) { this.log.warn(`Market.CSGO: подтверждение ${offer.id}: ${errMsg(e)}`); }
      }
      if (!ok) this.log.warn(`Market.CSGO: подтверждение обмена ${offer.id} пока не найдено, его подтвердит общая проверка`);
    }
    await this.client.tradeReady(offer.id).catch((e) => this.log.warn(`trade-ready: ${errMsg(e)}`));
    this.guard.recordSuccess('arb-send');
    this.notify.send('info', `📤 Продано на Market.CSGO, обмен ${offer.id} отправлен: <b>${esc(it.name)}</b>`);
  }

  // ---------- сверка и цены ----------
  async syncMarket() {
    const open = this.db.itemsByStatus('listed', 'delivering', 'settling').filter(isArbListing);
    if (!open.length) return;
    const byId = new Map((await this.client.items()).map((i) => [i.itemId, i]));
    let hist = null;
    for (const it of open) {
      const m = byId.get(marketId(it));
      if (m) {
        if (it.status === 'delivering' && m.settlement > 0) this.db.setItemStatus(it.id, 'settling', {}, { via: 'items' });
        continue;
      }
      hist ??= new Map((await this.client.saleHistory(Date.now() - 30 * DAY)).map((h) => [h.itemId, h]));
      const h = hist.get(marketId(it));
      if (h?.stage === 2 && it.status !== 'listed') { this.markArbSold(it, h); continue; }
      if (h?.stage === 5 && it.status === 'settling') {
        this.db.setItemStatus(it.id, 'manual', {}, { reversed: true });
        this.guard.warn(`ARB_REVERSED_${it.id}`, `Сделка по «${it.name}» на Market.CSGO отменена после передачи. Проверьте инвентарь и историю площадки.`);
        continue;
      }
      if (it.status === 'listed' && Date.now() - it.updated_at > 10 * 60_000) this.db.setItemStatus(it.id, 'ready', { listing_id: null }, { reason: 'not_on_market' });
    }
  }

  markArbSold(it, h) {
    // «received» — уже после комиссии продажи; вывод, курс и пополнение Lis ещё впереди
    const soldUsd = (h.received * netFactor(this.a)) / (1 - this.a.saleFeePct);
    const profit = soldUsd - it.buy_price_usd;
    this.db.setItemStatus(it.id, 'sold', { sold_at: Date.now(), sold_receive_cents: Math.round(h.received * 100), sold_usd: soldUsd, profit_usd: profit }, { venue: 'market.csgo', received: h.received, currency: h.currency });
    this.notify.send('money', `Market.CSGO: <b>${esc(it.name)}</b> оплачен. После вывода ${fmtUsd(soldUsd)}, куплен за ${fmtUsd(it.buy_price_usd)} → <b>${fmtUsd(profit)}</b> (${((profit / it.buy_price_usd) * 100).toFixed(1)}%)`);
    this.checkDailyLoss();
  }

  async listReady() {
    if (!this.guard.canSell || !this.cfg.sell.enabled || !this.client.hasKey) return;
    const ready = this.db.itemsByStatus('ready').filter((i) => i.asset_id);
    if (!ready.length) return;
    const m = this.money = await this.client.money();
    if (m.currency !== 'USD') { this.guard.warn('ARB_CURRENCY', `Аккаунт Market.CSGO в ${m.currency}, бот выставляет только в USD. Смените валюту в профиле площадки.`); return; }
    if (this.cfg.dryRun) { this.log.info(`[DRY-RUN] Market.CSGO: выставил бы ${ready.length} предм.`); return; }
    await this.client.updateInventory().catch(() => null);
    await sleep(this.a.inventoryWaitMs ?? 15_000);
    for (const it of ready) {
      if (!this.guard.canSell) return;
      try {
        const b = await this.client.bidAsk(it.name);
        const p = listPrice({ costUsd: it.buy_price_usd, ask: b.ask[0]?.price, bid: b.bid[0]?.price, a: this.a, relists: it.relists });
        const id = await this.client.addToSale(it.asset_id, p.price, 'USD');
        this.db.setItemStatus(it.id, 'listed', { listing_id: ARB_PREFIX + id, listed_at: Date.now(), list_price_cents: Math.round(p.price * 100) }, { venue: 'market.csgo', ...p });
        this.db.event('sell.listed', { venue: 'market.csgo', price: p.price, mode: p.mode }, it.id);
        this.log.info(`Market.CSGO: выставлен «${it.name}» за ${fmtUsd(p.price)} (${p.mode})`);
      } catch (e) {
        if (['item_not_in_inventory', 'inventory_not_loaded', 'item_not_recieved', 'no_description_found'].includes(e.code)) { this.log.info(`Market.CSGO ещё не видит «${it.name}» (${e.code})`); continue; }
        this.guard.recordError('arb-list', e);
      }
    }
  }

  async reprice() {
    if (this.cfg.dryRun || !this.guard.canSell || !this.client.hasKey) return;
    for (const it of this.db.itemsByStatus('listed').filter(isArbListing)) {
      if (Date.now() - (it.listed_at || 0) < this.a.repriceAfterHours * HOUR) continue;
      try {
        const b = await this.client.bidAsk(it.name);
        const own = it.list_price_cents / 100;
        const ask = b.ask.find((x) => Math.abs(x.price - own) > 0.005 || x.total > 1)?.price; // не подрезаем сами себя
        const p = listPrice({ costUsd: it.buy_price_usd, ask, bid: b.bid[0]?.price, a: this.a, relists: it.relists + 1 });
        if (Math.abs(p.price / own - 1) >= 0.003) await this.client.setPrice(marketId(it), p.price, 'USD');
        this.db.updateItem(it.id, { listed_at: Date.now(), list_price_cents: Math.round(p.price * 100), relists: Math.min(it.relists + 1, this.a.maxRelists) });
      } catch (e) { this.log.warn(`Market.CSGO: цена «${it.name}»: ${errMsg(e)}`); }
    }
  }

  async cycle() {
    await this.syncInventory();
    await this.syncListings();            // старые лоты Steam досматриваем как раньше
    if (this.client.hasKey) await this.syncMarket();
    await this.listReady();
    await this.reprice();
    this.agedAlerts();
  }
}
