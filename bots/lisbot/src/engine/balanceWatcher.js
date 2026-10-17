import { fmtUsd, round2 } from '../util/common.js';

/**
 * Сверка баланса LIS-SKINS.
 * Изменение баланса раскладывается на: наши покупки (известны из БД) + возвраты по
 * несостоявшимся покупкам (refundCredit) + «необъяснённое».
 *   необъяснённое > 0  -> пополнение баланса (уведомление 💰);
 *   необъяснённое < 0  -> кто-то тратит баланс помимо бота (ручная покупка или утечка API-ключа) -> тревога.
 * Опрос выполняется под тем же мьютексом, что и покупки, — гонок с покупкой нет.
 */
export class BalanceWatcher {
  constructor({ cfg, db, lis, guard, notify, log, lock, balanceRef }) {
    Object.assign(this, { cfg, db, lis, guard, notify, log, lock, balanceRef });
    this.last = db.get('balance.last'); // { balance, ts }
    this.carry = db.get('balance.carry', { amount: 0, since: null });
  }

  poll() { return this.lock.run(() => this._poll()); }

  async _poll() {
    let bal;
    try { bal = await this.lis.balance(); this.guard.recordSuccess('lis-balance'); } catch (e) { this.guard.recordError('lis-balance', e); return; }
    const t = Date.now();
    this.balanceRef.value = bal;
    const r = this.cfg.risk;

    if (!this.last) {
      this.last = { balance: bal, ts: t };
      this.db.set('balance.last', this.last);
      this.db.st('INSERT INTO balance_snapshots(ts,balance_usd,delta,unexplained,kind) VALUES(?,?,?,?,?)').run(t, bal, 0, 0, 'init');
      this.notify.send('info', `Баланс LIS-SKINS: ${fmtUsd(bal)}`);
      return bal;
    }

    const delta = round2(bal - this.last.balance);
    const spent = this.db.db.prepare("SELECT COALESCE(SUM(total_usd),0) s FROM purchases WHERE dry_run=0 AND total_usd IS NOT NULL AND ts>? AND ts<=?").get(this.last.ts, t).s;
    // покупки, которые «нашлись» позже (сетевая ошибка при /buy), трекер учитывает в balance.lateSpend
    const late = this.db.get('balance.lateSpend', 0) || 0;
    if (late) this.db.set('balance.lateSpend', 0);
    let unexplained = round2(delta + spent + late + this.carry.amount);
    const unresolved = this.db.db.prepare("SELECT COUNT(*) c FROM purchases WHERE dry_run=0 AND status='pending' AND total_usd IS NULL").get().c;
    let kind = 'ok';

    if (unexplained >= r.topUpThresholdUsd) {
      // часть может быть возвратом за несостоявшиеся покупки
      const refundCredit = this.db.get('refundCredit', 0) || 0;
      const pendingCount = this.db.pendingPurchases().length;
      const refund = Math.min(refundCredit, unexplained);
      this.db.set('refundCredit', round2(refundCredit - refund));
      unexplained = round2(unexplained - refund);
      if (refund > 0) this.log.info(`Возврат за несостоявшиеся покупки: ${fmtUsd(refund)}`);
      if (unexplained >= r.topUpThresholdUsd) {
        // если есть незавершённые покупки — даём трекеру 10 минут распознать возможный возврат
        if (pendingCount && (!this.carry.since || t - this.carry.since < 10 * 60_000)) {
          this.carry = { amount: unexplained, since: this.carry.since || t };
          kind = 'deferred';
        } else {
          kind = 'topup';
          this.carry = { amount: 0, since: null };
          this.db.event('balance.topup', { amount: unexplained, balance: bal });
          this.notify.send('money', `Пополнение баланса LIS-SKINS: <b>+${fmtUsd(unexplained)}</b>\nТекущий баланс: ${fmtUsd(bal)}`);
        }
      } else this.carry = { amount: 0, since: null };
    } else if (unexplained <= -r.unknownSpendThresholdUsd && unresolved && (!this.carry.since || t - this.carry.since < 15 * 60_000)) {
      // есть покупка с неизвестным итогом (сетевая ошибка) — ждём, пока трекер её распознает
      this.carry = { amount: unexplained, since: this.carry.since || t };
      kind = 'deferred';
    } else if (unexplained <= -r.unknownSpendThresholdUsd) {
      kind = 'unknown_spend';
      this.carry = { amount: 0, since: null };
      this.db.event('balance.unknown_spend', { amount: unexplained, balance: bal });
      const msg = `Баланс LIS уменьшился на ${fmtUsd(-unexplained)} без покупок бота. Это ручная покупка — или утечка API-ключа LIS!`;
      r.unknownSpendIsCritical ? this.guard.critical('UNKNOWN_SPEND', `${msg} Если это не вы — перевыпустите ключ.`) : this.guard.warn('UNKNOWN_SPEND', msg);
    } else {
      this.carry = { amount: 0, since: null };
    }

    this.db.st('INSERT INTO balance_snapshots(ts,balance_usd,delta,unexplained,kind) VALUES(?,?,?,?,?)').run(t, bal, delta, unexplained, kind);
    this.last = { balance: bal, ts: t };
    this.db.set('balance.last', this.last);
    this.db.set('balance.carry', this.carry);

    if (bal < r.lowBalanceUsd) this.notify.send('warn', `Низкий баланс LIS-SKINS: ${fmtUsd(bal)} (порог ${fmtUsd(r.lowBalanceUsd)}). Закупки скоро остановятся.`, { dedupeKey: 'lowbal' });
    return bal;
  }
}

