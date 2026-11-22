import TradeOfferManager from 'steam-tradeoffer-manager';
import { parseTradeUrl } from '../config.js';
import { errMsg, dayKey, sleep, HOUR } from '../util/common.js';
import { esc } from '../notify/telegram.js';

const EState = TradeOfferManager.ETradeOfferState;
const STEAMID64_BASE = 76561197960265728n;
const OPEN = [EState.Active, EState.CreatedNeedsConfirmation];

/**
 * Мост ключей: деньги кошелька Steam аккаунта LisBot -> ключи Mann Co. -> склад MannBot.
 *  1. закуп: один ордер на покупку ключей на ТП Steam (цена не выше keyBridge.buy.maxKeyPrice,
 *     не больше keysPerDay в день, на кошельке остаётся walletReserve);
 *  2. ключи с ТП Steam разрешает передавать через 7 дней — тогда бот САМ создаёт исходящий оффер
 *     на трейд-ссылку MannBot (keyBridge.tradeUrl) и подтверждает только его.
 * Один аккаунт Steam с MannBot (keyBridge.tradeUrl пуст или ведёт на этот же аккаунт): шаг 2 не нужен,
 * купленные ключи остаются в общем инвентаре, и MannBot продаёт их, когда Steam разрешит обмен.
 * Защита не ослабляется: свой оффер = id записан ботом при отправке + получатель из настроек +
 * отдаём только записанные assetid ключей + ничего не получаем. Всё остальное по-прежнему
 * UNKNOWN_OUTGOING_TRADE / UNKNOWN_CONFIRMATION. В DRY-RUN только пишет в журнал, что сделал бы.
 */
export class KeyBridge {
  constructor({ cfg, db, session, market, conf, guard, notify, log }) {
    Object.assign(this, { cfg, db, session, market, conf, guard, notify, log });
    this.state = { buyOrder: null, ownOffers: {}, bought: {}, totals: { bought: 0, spentCents: 0, sent: 0, delivered: 0 }, ...db.get('keybridge', {}) };
    for (const id of Object.keys(this.state.ownOffers)) if (this.state.ownOffers[id].open) conf?.expectTrade(id);
  }

  get bc() { return this.cfg.keyBridge || {}; }

  get enabled() { return !!this.bc.enabled; }

  /** Ключи остаются на этом аккаунте: MannBot торгует с него же (трейд-ссылки нет или она на себя). */
  get sameAccount() {
    if (!String(this.bc.tradeUrl || '').trim()) return true;
    const p = this.partner;
    return !!p && !!this.session.steamId && p.steamId === this.session.steamId;
  }

  get partner() {
    const t = parseTradeUrl(this.bc.tradeUrl);
    return t ? { ...t, steamId: String(STEAMID64_BASE + BigInt(t.partner)) } : null;
  }

  _save() { this.db.set('keybridge', this.state); }

  get stats() {
    const t = this.state.totals;
    return {
      enabled: this.enabled, sameAccount: this.sameAccount, partnerSteamId: this.sameAccount ? null : this.partner?.steamId ?? null, buyOrder: this.state.buyOrder,
      ...t, avgKeyPrice: t.bought ? t.spentCents / t.bought / 100 : null,
      openOffers: Object.entries(this.state.ownOffers).filter(([, o]) => o.open).map(([id, o]) => ({ id, keys: o.assetIds.length, ts: o.ts })),
    };
  }

  /** Это исходящий оффер, который создал сам мост? Проверяем id, получателя и состав. */
  isOwnOffer(offer) {
    const own = this.state.ownOffers[String(offer.id)];
    if (!own) return false;
    if (offer.partner?.getSteamID64?.() !== own.partner) return false;
    if ((offer.itemsToReceive || []).length) return false;
    const allowed = new Set(own.assetIds);
    return (offer.itemsToGive || []).length > 0 && offer.itemsToGive.every((i) => allowed.has(String(i.assetid || i.id)));
  }

  /** Событие по своему офферу (вызывает TradeHandler вместо тревоги). */
  onOwnOfferChanged(offer) {
    const own = this.state.ownOffers[String(offer.id)];
    if (!own || !own.open) return;
    if (OPEN.includes(offer.state)) return;
    own.open = false;
    own.finalState = EState[offer.state];
    this.conf?.expectedTrades.delete(String(offer.id));
    if (offer.state === EState.Accepted) {
      this.state.totals.delivered += own.assetIds.length;
      this.db.event('keybridge.delivered', { offerId: String(offer.id), keys: own.assetIds.length });
      this.notify.send('ok', `🔑 Мост ключей: MannBot принял ${own.assetIds.length} ключ(ей) (обмен ${offer.id})`);
    } else {
      this.db.event('keybridge.failed', { offerId: String(offer.id), state: own.finalState });
      this.guard.warn('KEY_BRIDGE_OFFER', `Мост ключей: обмен ${offer.id} на MannBot не прошёл (${own.finalState}). Ключи остались у LisBot, бот попробует снова.`);
    }
    this._save();
  }

  async cycle() {
    if (!this.enabled || !this.session.ready || this.guard.state.halted) return;
    const p = this.partner;
    if (!this.sameAccount && !p) { this.guard.warn('KEY_BRIDGE_CONFIG', 'Мост ключей: keyBridge.tradeUrl (трейд-ссылка MannBot) неверна. Если MannBot на этом же аккаунте Steam, оставьте её пустой.'); return; }
    await this.expireOffers();
    if (!this.sameAccount) await this.transfer(p);
    if (this.bc.buy?.enabled) await this.buy();
  }

  /** Отправить на MannBot все ключи, которые Steam уже разрешает передавать. */
  async transfer(p) {
    const name = this.bc.itemName;
    const busy = new Set(Object.values(this.state.ownOffers).filter((o) => o.open).flatMap((o) => o.assetIds));
    let inv;
    try { inv = await this.session.inventory(this.bc.appid, this.bc.contextid, true); } catch (e) { this.log.warn(`Мост ключей: инвентарь TF2: ${errMsg(e)}`); return; }
    const keys = inv.filter((i) => i.market_hash_name === name && i.tradable !== false && !busy.has(String(i.assetid || i.id)));
    if (keys.length < Math.max(1, this.bc.minKeysToSend || 1)) return;
    const batch = keys.slice(0, this.bc.maxKeysPerOffer || 100);
    const assetIds = batch.map((i) => String(i.assetid || i.id));
    if (this.cfg.dryRun) {
      this.db.event('keybridge.dry_run_send', { keys: assetIds.length, to: p.steamId });
      this.log.info(`[DRY-RUN] Мост ключей: отправил бы ${assetIds.length} ключ(ей) на MannBot`);
      return;
    }
    const offer = this.session.manager.createOffer(this.bc.tradeUrl);
    offer.addMyItems(assetIds.map((assetid) => ({ appid: this.bc.appid, contextid: String(this.bc.contextid), assetid })));
    offer.setMessage('LisBot → MannBot: ключи для склада');
    let status;
    try {
      status = await new Promise((res, rej) => offer.send((err, st) => {
        if (err) return rej(err);
        // записываем оффер как свой синхронно, до любых событий опроса и проверки подтверждений
        this.state.ownOffers[String(offer.id)] = { partner: p.steamId, assetIds, ts: Date.now(), open: true };
        this.state.totals.sent += assetIds.length;
        this._save();
        this.conf.expectTrade(offer.id);
        res(st);
      }));
    } catch (e) {
      this.guard.warn('KEY_BRIDGE_SEND', `Мост ключей: не удалось отправить обмен на MannBot: ${errMsg(e)}`);
      return;
    }
    this.db.event('keybridge.sent', { offerId: String(offer.id), keys: assetIds.length, to: p.steamId });
    if (status === 'pending') {
      let ok = false;
      for (let i = 0; i < 4 && !ok; i++) {
        if (i) await sleep(5_000 * i);
        try { ok = await this.conf.confirmTrade(offer.id); } catch (e) { this.log.warn(`Мост ключей: подтверждение ${offer.id}: ${errMsg(e)}`); }
      }
      if (!ok) this.log.warn(`Мост ключей: подтверждение обмена ${offer.id} пока не найдено, его подтвердит общая проверка`);
    }
    this.notify.send('info', `🔑 Мост ключей: отправлено ${assetIds.length} ключ(ей) на MannBot (обмен ${offer.id})`);
  }

  /** Снять свои офферы, которые MannBot не принял за offerTtlHours. */
  async expireOffers() {
    const ttl = (this.bc.offerTtlHours || 24) * HOUR;
    for (const [id, own] of Object.entries(this.state.ownOffers)) {
      if (!own.open || Date.now() - own.ts < ttl) continue;
      try {
        const offer = await new Promise((res, rej) => this.session.manager.getOffer(id, (err, o) => (err ? rej(err) : res(o))));
        if (OPEN.includes(offer.state)) await new Promise((res, rej) => offer.cancel((err) => (err ? rej(err) : res())));
        else { this.onOwnOfferChanged(offer); continue; }
      } catch (e) { this.log.warn(`Мост ключей: отмена ${id}: ${errMsg(e)}`); continue; }
      own.open = false; own.finalState = 'Expired';
      this.conf.expectedTrades.delete(id);
      this.guard.warn('KEY_BRIDGE_OFFER', `Мост ключей: MannBot не принял обмен ${id} за ${this.bc.offerTtlHours || 24} ч, обмен отменён. Проверьте, что в MannBot включён приём ключей от SteamID ${this.session.steamId}.`);
      this._save();
    }
    // старые закрытые офферы не храним вечно
    for (const [id, own] of Object.entries(this.state.ownOffers)) if (!own.open && Date.now() - own.ts > 30 * 24 * HOUR) delete this.state.ownOffers[id];
  }

  /** Держим один ордер на ключи на ТП Steam, в пределах цены, дневного лимита и резерва кошелька. */
  async buy() {
    const b = this.bc.buy;
    const name = this.bc.itemName;
    const day = dayKey(Date.now(), this.cfg.timezone);
    const order = this.state.buyOrder;
    if (order) {
      const st = await this.market.buyOrderStatus(order.id).catch((e) => { this.log.warn(`Мост ключей: статус ордера: ${errMsg(e)}`); return null; });
      if (!st || st.success !== 1) return;
      const purchased = Number(st.purchased) || 0;
      const fresh = purchased - (order.purchased || 0);
      if (fresh > 0) {
        order.purchased = purchased;
        this.state.bought[day] = (this.state.bought[day] || 0) + fresh;
        this.state.totals.bought += fresh;
        this.state.totals.spentCents += fresh * order.priceCents;
        this.db.event('keybridge.bought', { keys: fresh, priceCents: order.priceCents });
        this.notify.send('info', `🔑 Мост ключей: куплено ${fresh} ключ(ей) по ${(order.priceCents / 100).toFixed(2)} ${esc(this.cfg.steam.walletCurrencyCode)}. ${this.sameAccount ? 'MannBot начнёт продавать их через 7 дней, когда Steam разрешит обмен.' : 'Передать на MannBot можно через 7 дней.'}`);
      }
      const active = Number(st.active) === 1;
      if (active && Date.now() - order.createdAt > (b.repriceHours || 24) * HOUR) {
        await this.market.cancelBuyOrder(order.id).catch((e) => this.log.warn(`Мост ключей: отмена ордера: ${errMsg(e)}`));
        this.state.buyOrder = null;
      } else if (!active) this.state.buyOrder = null;
      this._save();
      return;
    }
    if (this.state.buyBackoffUntil && Date.now() < this.state.buyBackoffUntil) return;
    const left = Math.max(0, (b.keysPerDay || 0) - (this.state.bought[day] || 0));
    const wallet = this.session.wallet?.balance;
    if (!Number.isFinite(wallet)) { this.log.warn('Мост ключей: баланс кошелька Steam неизвестен, закуп пропущен'); return; }
    const maxCents = Math.round((b.maxKeyPrice || 0) * 100);
    if (!left || maxCents <= 0) return;
    let priceCents = maxCents;
    try {
      const book = await this.market.orderBook(name, this.bc.appid);
      if (Number.isFinite(book.lowestSell) && Math.round(book.lowestSell * 100) <= maxCents) priceCents = Math.round(book.lowestSell * 100);
      else if (Number.isFinite(book.highestBuy)) priceCents = Math.min(maxCents, Math.round(book.highestBuy * 100) + 1);
    } catch (e) { this.log.warn(`Мост ключей: стакан ключа: ${errMsg(e)}`); return; }
    const budgetCents = Math.floor((wallet - (b.walletReserve || 0)) * 100);
    const quantity = Math.min(left, b.maxOrderKeys || left, Math.floor(budgetCents / priceCents));
    if (quantity < 1) return;
    if (this.cfg.dryRun) {
      this.db.event('keybridge.dry_run_buy', { quantity, priceCents });
      this.log.info(`[DRY-RUN] Мост ключей: поставил бы ордер на ${quantity} ключ(ей) по ${(priceCents / 100).toFixed(2)}`);
      return;
    }
    let r = await this.market.createBuyOrder({ appid: this.bc.appid, name, priceCents, quantity });
    const confId = r?.confirmation?.confirmation_id;
    if (r?.success !== 1 && r?.need_confirmation && confId) {
      this.conf.expectConfirmation(confId);
      let ok = false;
      for (let i = 0; i < 4 && !ok; i++) {
        if (i) await sleep(3_000 * i);
        try { ok = await this.conf.confirmById(confId); } catch (e) { this.log.warn(`Мост ключей: подтверждение ордера: ${errMsg(e)}`); }
      }
      r = ok ? await this.market.createBuyOrder({ appid: this.bc.appid, name, priceCents, quantity, confirmation: String(confId) }) : r;
    }
    if (r?.success === 1 && r.buy_orderid) {
      this.state.buyOrder = { id: String(r.buy_orderid), priceCents, quantity, purchased: 0, createdAt: Date.now() };
      this.db.event('keybridge.order', { id: String(r.buy_orderid), quantity, priceCents });
    } else {
      this.state.buyBackoffUntil = Date.now() + 6 * HOUR;
      this.guard.warn('KEY_BRIDGE_BUY', `Мост ключей: Steam не принял ордер на ключи (${r?.message || `код ${r?.success}`}). Повтор через 6 ч.`);
    }
    this._save();
  }
}
