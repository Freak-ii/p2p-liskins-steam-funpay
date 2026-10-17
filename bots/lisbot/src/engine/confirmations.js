import { errMsg } from '../util/common.js';

const TYPE_TRADE = 2;
const TYPE_MARKET = 3;

/**
 * Мобильные подтверждения. В отличие от «принять всё», подтверждаем ТОЛЬКО лоты ТП,
 * которые бот выставил сам (по listingId). Любое неизвестное подтверждение
 * (трейд, чужой лот, новый тип) — критическая тревога и отмена.
 */
export class ConfirmationWatcher {
  constructor({ session, db, guard, log, strict = true }) {
    Object.assign(this, { session, db, guard, log, strict });
    this.expected = new Map(); // listingId -> { itemId, ts }
    this.graceUntil = 0; // пока бот сам выставляет лот, его подтверждение ещё «без имени»
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

  async check() {
    let confs;
    try { confs = await this.session.getConfirmations(); this.guard.recordSuccess('steam-conf'); } catch (e) { this.guard.recordError('steam-conf', e); return; }
    for (const c of confs) {
      const id = String(c.creator);
      if (c.type === TYPE_MARKET && this.expected.has(id)) {
        try { await this.session.respondConfirmation(c, true); this.expected.delete(id); this.db.event('confirm.listing', { listingId: id, late: true }); } catch (e) { this.log.warn(`Подтверждение лота ${id}: ${errMsg(e)}`); }
        continue;
      }
      if (c.type === TYPE_MARKET && Date.now() < this.graceUntil) continue;
      if (c.type === TYPE_MARKET && this.db.db.prepare("SELECT 1 FROM items WHERE listing_id=? AND status IN ('listing','listed')").get(id)) continue;
      const kind = c.type === TYPE_TRADE ? 'обмен' : c.type === TYPE_MARKET ? 'лот на ТП' : `тип ${c.type}`;
      if (!this.strict) { this.guard.warn('FOREIGN_CONFIRMATION', `Ожидает подтверждения ${kind} «${c.title}», созданный не ботом — не трогаю (risk.strictConfirmations=false)`); continue; }
      this.guard.critical('UNKNOWN_CONFIRMATION', `Неизвестное мобильное подтверждение (${kind}): «${c.title}» ${c.sending || ''}. Бот его не создавал — отменяю. Если это не вы — СРОЧНО смените пароль и отзовите Web API ключ.`, { id: c.id, creator: id, type: c.type });
      try { await this.session.respondConfirmation(c, false); } catch (e) { this.log.error(`Не удалось отменить подтверждение ${c.id}: ${errMsg(e)}`); }
    }
  }
}
