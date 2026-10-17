import { EventEmitter } from 'node:events';
import { esc } from '../notify/telegram.js';
import { errMsg } from '../util/common.js';

/**
 * Предохранитель (circuit breaker).
 *  - critical(): немедленная остановка закупок и продаж + критическое уведомление.
 *    Снимается только вручную (/resume в Telegram или кнопка в панели).
 *  - счётчики подряд идущих ошибок по подсистемам: N ошибок подряд => critical.
 *  - пауза (ручная) отличается от остановки (аварийной).
 */
export class Guard extends EventEmitter {
  constructor({ db, notify, log, cfg }) {
    super();
    Object.assign(this, { db, notify, log, cfg });
    this.state = { halted: false, haltReason: null, haltCode: null, haltAt: null, paused: false, buyPaused: false, sellPaused: false, ...db.get('guard', {}) };
    this.errors = new Map();
  }

  _save() { this.db.set('guard', this.state); this.emit('change', this.state); }

  get canBuy() { return !this.state.halted && !this.state.paused && !this.state.buyPaused; }
  get canSell() { return !this.state.halted && !this.state.paused && !this.state.sellPaused; }
  /** Приём входящих трейдов от LIS продолжаем даже на паузе — иначе купленное потеряется. Но не при аварии. */
  get canAcceptTrades() { return !this.state.halted; }

  critical(code, message, data) {
    this.log.crit(`${code}: ${message}`, data);
    this.db.alert('critical', code, message, data);
    this.db.event('guard.critical', { code, message });
    const first = !this.state.halted;
    this.state = { ...this.state, halted: true, haltReason: message, haltCode: code, haltAt: Date.now() };
    this._save();
    this.notify.send('critical', `${esc(message)}\n\nКод: <code>${esc(code)}</code>\n${first ? 'Бот <b>остановлен</b>: закупки и продажи прекращены.' : 'Бот уже остановлен.'}\nПосле проверки: /resume`, { dedupeKey: `crit:${code}` });
    this.emit('halt', { code, message });
  }

  warn(code, message, data, { dedupe = true } = {}) {
    this.log.warn(`${code}: ${message}`, data);
    this.db.alert('warn', code, message, data);
    this.notify.send('warn', esc(message), dedupe ? { dedupeKey: `warn:${code}:${message.slice(0, 60)}` } : {});
  }

  info(code, message, level = 'info') {
    this.db.alert('info', code, message);
    this.notify.send(level, esc(message));
  }

  /** Учёт ошибок подсистемы. Возвращает true, если превышен порог и бот остановлен. */
  recordError(scope, err) {
    const n = (this.errors.get(scope) || 0) + 1;
    this.errors.set(scope, n);
    this.log.error(`[${scope}] ошибка #${n}: ${errMsg(err)}`);
    this.db.event('error', { scope, message: errMsg(err), n });
    if (err?.code === 'AUTH') { this.critical('LIS_AUTH', 'LIS-SKINS отклонил API-ключ (401/403). Ключ отозван или скомпрометирован?'); return true; }
    if (n >= this.cfg.risk.maxConsecutiveErrors) { this.critical(`ERRORS_${scope.toUpperCase()}`, `${n} ошибок подряд в модуле «${scope}». Последняя: ${errMsg(err)}`); return true; }
    if (n === Math.ceil(this.cfg.risk.maxConsecutiveErrors / 2)) this.warn(`ERR_${scope}`, `Повторяющиеся ошибки в «${scope}» (${n}): ${errMsg(err)}`);
    return false;
  }

  recordSuccess(scope) { this.errors.set(scope, 0); }

  resume(by = 'manual') {
    const was = this.state.haltReason;
    this.state = { ...this.state, halted: false, haltReason: null, haltCode: null, haltAt: null, paused: false };
    this.errors.clear();
    this._save();
    this.db.event('guard.resume', { by, was });
    this.notify.send('ok', `Бот возобновлён (${by})${was ? `. Причина остановки была: ${esc(was)}` : ''}`);
  }

  pause(by = 'manual') { this.state.paused = true; this._save(); this.db.event('guard.pause', { by }); this.notify.send('info', `Бот на паузе (${by}). Входящие трейды от LIS продолжают приниматься.`); }

  setFlag(flag, value) { if (['buyPaused', 'sellPaused'].includes(flag)) { this.state[flag] = !!value; this._save(); } }
}
