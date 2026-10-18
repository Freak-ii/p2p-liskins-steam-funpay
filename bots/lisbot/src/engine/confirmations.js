import { errMsg } from '../util/common.js';

const TYPE_TRADE = 2;
const TYPE_MARKET = 3;

/**
 * Мобильные подтверждения. В отличие от «принять всё», подтверждаем ТОЛЬКО лоты ТП,
 * которые бот выставил сам (по listingId). Любое неизвестное подтверждение
 * (трейд, чужой лот, новый тип) — критическая тревога и отмена.
 */
export class ConfirmationWatcher {
  constructor({ session, db, guard, log, strict = true, shared = null }) {
    Object.assign(this, { session, db, guard, log, strict, shared });
    this.expected = new Map(); // listingId -> { itemId, ts }
    this.graceUntil = 0; // пока бот сам выставляет лот, его подтверждение ещё «без имени»
    this.expectedTrades = new Set(); // id исходящих офферов моста ключей (создал сам бот)
    this.expectedIds = new Set(); // id подтверждений ордеров моста ключей (Steam вернул их сам)
  }

  expectTrade(offerId) { this.expectedTrades.add(String(offerId)); }

  expectConfirmation(confId) { this.expectedIds.add(String(confId)); }

  /** Подтвердить свой исходящий оффер (только по его id). */
  async confirmTrade(offerId) {
    const confs = await this.session.getConfirmations();
    const conf = confs.find((c) => c.type === TYPE_TRADE && String(c.creator) === String(offerId));
    if (!conf) return false;
    await this.session.respondConfirmation(conf, true);
    this.expectedTrades.delete(String(offerId));
    this.db.event('confirm.trade', { offerId: String(offerId) });
    return true;
  }

  /** Подтвердить подтверждение с известным id (ордер на покупку моста ключей). */
  async confirmById(confId) {
    const confs = await this.session.getConfirmations();
    const conf = confs.find((c) => String(c.id) === String(confId) || String(c.creator) === String(confId));
    if (!conf) return false;
    await this.session.respondConfirmation(conf, true);
    this.expectedIds.delete(String(confId));
    this.db.event('confirm.buy_order', { confId: String(confId) });
    return true;
  }

  beginListing() { this.graceUntil = Date.now() + 5 * 60_000; }

  expectListing(listingId, itemId) { this.expected.set(String(listingId), { itemId, ts: Date.now() }); }

  /** Подтвердить конкретный лот. Возвращает true, если подтверждение найдено и принято. */
  async confirmListing(listingId) {
    const confs = await this.session.getConfirmations();
    const conf = confs.find((c) => c.type === TYPE_MARKET && String(c.creator) === String(listingId));
    if (!conf) return false;
    await this.session.respondConfirmation(conf, true);
    this.expected.delete(String(listingId));
    this.db.event('confirm.listing', { listingId });
    return true;
  }

  /** Обмен с этим id — обмен MannBot, записанный в центре? Состав берём из самого Steam, не из подтверждения. */
  async _mannbotTrade(offerId) {
    if (!this.shared?.enabled || !this.session.manager) return false;
    if (this.shared.own.has(offerId)) return true;
    try {
      const offer = await new Promise((res, rej) => this.session.manager.getOffer(offerId, (err, o) => (err ? rej(err) : res(o))));
      return offer?.isOurOffer === true && String(offer?.id) === offerId && await this.shared.isOwn(offer);
    } catch (e) {
      this.log.warn(`Подтверждение обмена ${offerId}: не удалось получить обмен: ${errMsg(e)}`);
      return false;
    }
  }

  async check() {
    let confs;
    try { confs = await this.session.getConfirmations(); this.guard.recordSuccess('steam-conf'); } catch (e) { this.guard.recordError('steam-conf', e); return; }
    for (const c of confs) {
      const id = String(c.creator);
      if (c.type === TYPE_MARKET && this.expected.has(id)) {
        try { await this.session.respondConfirmation(c, true); this.expected.delete(id); this.db.event('confirm.listing', { listingId: id, late: true }); } catch (e) { this.log.warn(`Подтверждение лота ${id}: ${errMsg(e)}`); }
        continue;
      }
      const ownTrade = c.type === TYPE_TRADE && this.expectedTrades.has(id);
      const ownConf = this.expectedIds.has(String(c.id)) || this.expectedIds.has(id);
      if (ownTrade || ownConf) {
        try {
          await this.session.respondConfirmation(c, true);
          if (ownTrade) this.expectedTrades.delete(id);
          this.expectedIds.delete(String(c.id)); this.expectedIds.delete(id);
          this.db.event(ownTrade ? 'confirm.trade' : 'confirm.buy_order', { creator: id, late: true });
        } catch (e) { this.log.warn(`Подтверждение моста ключей ${id}: ${errMsg(e)}`); }
        continue;
      }
      if (c.type === TYPE_MARKET && Date.now() < this.graceUntil) continue;
      if (c.type === TYPE_MARKET && this.db.db.prepare("SELECT 1 FROM items WHERE listing_id=? AND status IN ('listing','listed')").get(id)) continue;
      // один аккаунт с MannBot: подтверждение его обмена, записанного в центре, подтверждает сам MannBot
      if (c.type === TYPE_TRADE && await this._mannbotTrade(id)) continue;
      const kind = c.type === TYPE_TRADE ? 'обмен' : c.type === TYPE_MARKET ? 'лот на ТП' : `тип ${c.type}`;
      if (!this.strict) { this.guard.warn('FOREIGN_CONFIRMATION', `Ожидает подтверждения ${kind} «${c.title}», созданный не ботом — не трогаю (risk.strictConfirmations=false)`); continue; }
      this.guard.critical('UNKNOWN_CONFIRMATION', `Неизвестное мобильное подтверждение (${kind}): «${c.title}» ${c.sending || ''}. Бот его не создавал — отменяю. Если это не вы — СРОЧНО смените пароль и отзовите Web API ключ.`, { id: c.id, creator: id, type: c.type });
      try { await this.session.respondConfirmation(c, false); } catch (e) { this.log.error(`Не удалось отменить подтверждение ${c.id}: ${errMsg(e)}`); }
    }
  }
}
