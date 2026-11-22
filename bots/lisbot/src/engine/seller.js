import { sellPrice, repricePlan } from '../pricing/sellStrategy.js';
import { errMsg, sleep, jitter, HOUR, DAY, inActiveHours, fmtUsd, startOfDay } from '../util/common.js';
import { esc } from '../notify/telegram.js';

const MARKET_BLOCK_RE = /not allowed|restrict|ban|cannot sell|can't sell|unable to sell|market.*unavailable|trade.*hold|steam guard/i;

/**
 * Продажа на ТП Steam:
 *  1. Синхронизация инвентаря: назначение assetid, снятие Trade Protection, контроль пропаж (откат сделки).
 *  2. Выставление лотов после окончания Trade Protection (лимиты в час/день, паузы, активные часы).
 *  3. Подтверждение ТОЛЬКО своих лотов через мобильный аутентификатор.
 *  4. Сверка лотов: продано / снято / перевыставить ниже (но не ниже «пола» себестоимости).
 */
export class Seller {
  constructor({ cfg, db, session, market, conf, valuator, fx, guard, notify, log }) {
    Object.assign(this, { cfg, db, session, market, conf, valuator, fx, guard, notify, log });
    this.lastInventorySync = 0;
    this.inventoryCache = null;
  }

  listedSince(ts) {
    return this.db.db.prepare("SELECT COUNT(*) c FROM events WHERE type='sell.listed' AND ts>=?").get(ts).c;
  }

  assignedAssets() {
    return new Set(this.db.db.prepare("SELECT asset_id FROM items WHERE asset_id IS NOT NULL AND status NOT IN ('sold','failed','reversed')").all().map((r) => r.asset_id));
  }

  // ---------- инвентарь ----------
  async syncInventory(force = false) {
    if (!force && Date.now() - this.lastInventorySync < this.cfg.steam.inventorySyncMin * 60_000) return this.inventoryCache;
    const tracked = this.db.itemsByStatus('bought', 'trade_sent', 'protected', 'ready');
    if (!tracked.length && !force) return null;
    let inv;
    try { inv = await this.session.inventory(); this.guard.recordSuccess('steam-inventory'); } catch (e) { this.guard.recordError('steam-inventory', e); return null; }
    this.lastInventorySync = Date.now();
    this.inventoryCache = inv;
    const byAsset = new Map(inv.map((i) => [String(i.assetid), i]));
    const assigned = this.assignedAssets();

    for (const it of this.db.itemsByStatus('protected', 'ready')) {
      if (!it.asset_id) continue;
      const econ = byAsset.get(it.asset_id);
      if (!econ) {
        const misses = (this.db.get(`miss.${it.id}`, 0) || 0) + 1;
        this.db.set(`miss.${it.id}`, misses);
        if (misses >= 2) {
          this.db.setItemStatus(it.id, 'reversed', {}, { assetId: it.asset_id });
          this.guard.critical('ASSET_MISSING', `Предмет «${it.name}» (asset ${it.asset_id}) пропал из инвентаря до продажи. Возможен откат сделки продавцом (Trade Protection) или компрометация аккаунта. Убыток до ${fmtUsd(it.buy_price_usd)}.`);
        }
        continue;
      }
      this.db.set(`miss.${it.id}`, 0);
      const ta = econ.cache_expiration ? Date.parse(econ.cache_expiration) : null;
      const upd = {};
      if (ta && ta !== it.tradable_after) upd.tradable_after = ta + this.cfg.steam.tradeProtectionBufferMin * 60_000;
      if (it.status === 'protected' && econ.marketable && (!ta || ta < Date.now())) { this.db.setItemStatus(it.id, 'ready', upd); continue; }
      if (Object.keys(upd).length) this.db.updateItem(it.id, upd);
    }

    // предметы без assetid (getExchangeDetails не успел, трейд принят вручную, лот снят) — сопоставляем по названию
    const needAsset = this.db.db.prepare("SELECT * FROM items WHERE dry_run=0 AND asset_id IS NULL AND status IN ('bought','trade_sent','protected','ready') ORDER BY id").all();
    for (const it of needAsset) {
      const candidate = inv.find((i) => i.market_hash_name === it.name && !assigned.has(String(i.assetid)) && (it.status === 'ready' || i.cache_expiration || !i.marketable));
      if (!candidate) continue;
      assigned.add(String(candidate.assetid));
      const fields = { asset_id: String(candidate.assetid) };
      if (['bought', 'trade_sent'].includes(it.status)) {
        const ta = candidate.cache_expiration ? Date.parse(candidate.cache_expiration) : Date.now() + this.cfg.steam.tradeProtectionDays * DAY;
        this.db.setItemStatus(it.id, 'protected', { ...fields, received_at: Date.now(), tradable_after: ta }, { via: 'inventory' });
        this.log.info(`«${it.name}» найден в инвентаре (трейд принят вне бота?) — учтён`);
      } else this.db.updateItem(it.id, fields);
    }
    return inv;
  }

  // ---------- сверка лотов ----------
  async syncListings() {
    const open = this.db.itemsByStatus('listing', 'listed');
    if (!open.length) return;
    let my;
    try { my = await this.market.myListings(); this.guard.recordSuccess('steam-listings'); } catch (e) { this.guard.recordError('steam-listings', e); return; }
    const active = new Map(my.active.map((l) => [l.listingId, l]));
    const toConfirm = new Map(my.toConfirm.map((l) => [l.listingId, l]));
    const byAsset = new Map([...my.active, ...my.toConfirm].map((l) => [l.assetId, l]));
    let sold = null;

    for (const it of open) {
      const l = (it.listing_id && (active.get(it.listing_id) || toConfirm.get(it.listing_id))) || byAsset.get(it.asset_id);
      if (l) {
        if (!it.listing_id) this.db.updateItem(it.id, { listing_id: l.listingId });
        if (toConfirm.has(l.listingId)) { this.conf.expectListing(l.listingId, it.id); await this.conf.confirmListing(l.listingId).catch(() => false); }
        else if (it.status === 'listing') this.db.setItemStatus(it.id, 'listed', { listed_at: it.listed_at || Date.now() });
        continue;
      }
      if (it.status === 'listing' && Date.now() - it.updated_at < 10 * 60_000) continue;
      sold ??= await this.market.soldListings(200).catch(() => new Map());
      const s = it.listing_id && sold.get(it.listing_id);
      if (s) { this.markSold(it, s.receiveCents || it.list_receive_cents, s.ts, false); continue; }
      if (it.status === 'listed') {
        // лота нет ни среди активных, ни в истории продаж: либо снят (предмет вернулся), либо продажа старше истории
        const inv = await this.syncInventory(true);
        if (!inv) continue; // инвентарь недоступен — решим позже, не гадаем
        const assigned = this.assignedAssets();
        const back = inv.find((i) => String(i.assetid) === it.asset_id) || inv.find((i) => i.market_hash_name === it.name && i.marketable && !assigned.has(String(i.assetid)));
        if (back) { // лот снят вручную/Steam — вернём в продажу
          this.db.setItemStatus(it.id, 'ready', { listing_id: null, asset_id: String(back.assetid) }, { reason: 'listing_gone' });
          continue;
        }
        this.markSold(it, it.list_receive_cents, Date.now(), true);
      } else {
        this.db.setItemStatus(it.id, 'ready', { listing_id: null }, { reason: 'listing_not_created' });
      }
    }
  }

  markSold(it, receiveCents, ts, assumed) {
    const soldUsd = receiveCents / 100 / this.fx.rate;
    const profit = soldUsd - it.buy_price_usd;
    this.db.setItemStatus(it.id, 'sold', { sold_at: ts, sold_receive_cents: receiveCents, sold_usd: soldUsd, profit_usd: profit }, { assumed, receiveCents });
    const cur = this.cfg.steam.walletCurrencyCode;
    this.notify.send('money', `Продано: <b>${esc(it.name)}</b>\nПолучено: ${(receiveCents / 100).toFixed(2)} ${cur} (${fmtUsd(soldUsd)})\nКуплено за ${fmtUsd(it.buy_price_usd)} → прибыль <b>${fmtUsd(profit)}</b> (${((profit / it.buy_price_usd) * 100).toFixed(1)}%)`);
    this.checkDailyLoss();
  }

  checkDailyLoss() {
    const pnl = this.db.realizedPnlSince(startOfDay(this.cfg.timezone));
    if (pnl < -this.cfg.risk.maxDailyLossUsd) this.guard.critical('DAILY_LOSS', `Реализованный убыток за день ${fmtUsd(pnl)} превысил лимит ${fmtUsd(this.cfg.risk.maxDailyLossUsd)}. Стратегия работает в минус — проверьте настройки.`);
  }

  // ---------- выставление ----------
  async listReady() {
    if (!this.guard.canSell || !this.cfg.sell.enabled || !inActiveHours(this.cfg)) return;
    const s = this.cfg.sell;
    const ready = this.db.itemsByStatus('ready').filter((i) => i.asset_id);
    for (const it of ready) {
      if (!this.guard.canSell) return;
      if (this.listedSince(Date.now() - HOUR) >= s.maxListingsPerHour || this.listedSince(Date.now() - DAY) >= s.maxListingsPerDay) {
        this.log.info('Лимит выставлений достигнут — продолжу позже');
        return;
      }
      await this.listOne(it);
      await sleep(jitter(s.minListingGapSec * 1000, s.minListingGapSec * 1000));
    }
  }

  async listOne(it) {
    let book;
    try { book = await this.market.orderBook(it.name); } catch (e) { this.guard.recordError('steam-price', e); return; }
    const valuation = this.valuator.cached(it.name);
    const price = sellPrice({ costUsd: it.buy_price_usd, fx: this.fx.rate, book, valuation, cfg: this.cfg });
    if (!price) { this.log.warn(`«${it.name}»: нет данных для цены`); return; }
    this.conf.beginListing();
    const res = await this.market.sellItem(it.asset_id, price.receiveCents);
    if (!res?.success) {
      const msg = res?.message || 'неизвестная ошибка';
      if (/no longer in your inventory|not allowed to be traded|is not marketable|trade protected/i.test(msg)) {
        this.db.setItemStatus(it.id, 'protected', { tradable_after: Date.now() + HOUR }, { sellError: msg });
        return;
      }
      if (MARKET_BLOCK_RE.test(msg)) { this.guard.critical('MARKET_BLOCKED', `Steam не даёт выставлять предметы: «${msg}». Проверьте ограничения торговой площадки аккаунта.`); return; }
      if (/pending confirmation/i.test(msg)) { await this.conf.check(); return; }
      this.guard.recordError('steam-sell', new Error(msg));
      return;
    }
    this.guard.recordSuccess('steam-sell');
    this.db.setItemStatus(it.id, 'listing', { list_price_cents: price.buyerCents, list_receive_cents: price.receiveCents, listed_at: Date.now() }, { mode: price.mode, buyerCents: price.buyerCents });
    this.db.event('sell.listed', { buyerCents: price.buyerCents, receiveCents: price.receiveCents, mode: price.mode }, it.id);
    this.log.info(`Выставлен «${it.name}» за ${(price.buyerCents / 100).toFixed(2)} ${this.cfg.steam.walletCurrencyCode} (${price.mode})`);

    if (res.requires_confirmation || res.needs_mobile_confirmation) {
      await sleep(jitter(4000, 3000));
      try {
        const my = await this.market.myListings();
        const l = [...my.toConfirm, ...my.active].find((x) => x.assetId === it.asset_id);
        if (!l) return; // найдём на следующей сверке
        this.db.updateItem(it.id, { listing_id: l.listingId });
        this.conf.expectListing(l.listingId, it.id);
        const ok = await this.conf.confirmListing(l.listingId);
        if (ok) this.db.setItemStatus(it.id, 'listed', {}, { confirmed: true });
      } catch (e) { this.log.warn(`Подтверждение лота «${it.name}»: ${errMsg(e)} — повторю при сверке`); }
    } else {
      this.db.setItemStatus(it.id, 'listed');
    }
  }

  // ---------- перевыставление ----------
  async reprice() {
    if (!this.guard.canSell || !inActiveHours(this.cfg)) return;
    for (const it of this.db.itemsByStatus('listed')) {
      if (!it.listing_id || !it.listed_at || Date.now() - it.listed_at < this.cfg.sell.repriceAfterHours * HOUR) continue;
      let book;
      try { book = await this.market.orderBook(it.name); } catch { continue; }
      const plan = repricePlan({ item: it, book, fx: this.fx.rate, cfg: this.cfg });
      if (!plan || plan.action !== 'reprice') continue;
      try {
        await this.market.removeListing(it.listing_id);
        this.db.setItemStatus(it.id, 'ready', { listing_id: null, asset_id: null, relists: it.relists + 1 }, { reprice: plan });
        this.log.info(`«${it.name}»: снят для перевыставления → ${(plan.buyerCents / 100).toFixed(2)}`);
        await sleep(jitter(8000, 6000));
      } catch (e) { this.guard.recordError('steam-reprice', e); }
    }
    if (this.db.itemsByStatus('ready').some((i) => !i.asset_id)) await this.syncInventory(true);
  }

  agedAlerts() {
    const lim = this.cfg.sell.maxHoldDaysAlert * DAY;
    for (const it of this.db.itemsByStatus('ready', 'listing', 'listed')) {
      if (it.received_at && Date.now() - it.received_at > lim) {
        this.guard.warn(`AGED_${it.id}`, `«${it.name}» не продаётся уже ${Math.round((Date.now() - it.received_at) / DAY)} дн. (цена выше рынка из-за пола себестоимости). Решите вручную: держать или снизить minMarginOverCost.`);
      }
    }
  }

  async cycle() {
    await this.syncInventory();
    await this.syncListings();
    await this.listReady();
    await this.reprice();
    this.agedAlerts();
  }
}
