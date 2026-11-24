import { median, HOUR } from '../util/common.js';

/**
 * Способы вывода с Market.CSGO (старый сайт market-old.csgo.com, скрины пользователя 2026-11-24, сумма 10 000 ₽):
 *   USDT TRC20/ERC20 — 5% (мин. 4545 ₽), ETH 5%, TRX 5%, BTC — 1%. Вывод на Steam +2% — не круг (деньги застрянут в Steam).
 * payoutFxLossPct — что теряем после вывода: курс площадки к бирже, комиссия сети, спот BTC→USDT и перевод на Lis-Skins.
 * BTC: 1% площадки + ≈0.1% спот Bybit + сеть/спред/курс площадки — держим 1.5% запаса, пока нет живых замеров.
 */
export const WITHDRAW_METHODS = {
  btc: { withdrawFeePct: 0.01, payoutFxLossPct: 0.015, label: 'BTC (1%) → биржа → USDT' },
  usdt: { withdrawFeePct: 0.05, payoutFxLossPct: 0.005, label: 'USDT TRC20 (5%)' },
  trx: { withdrawFeePct: 0.05, payoutFxLossPct: 0.01, label: 'TRX (5%) → биржа' },
  eth: { withdrawFeePct: 0.05, payoutFxLossPct: 0.01, label: 'ETH (5%) → биржа' },
};

/** Подставляет комиссии выбранного способа вывода (custom — оставить заданные вручную). */
export function applyWithdrawMethod(a) {
  const m = WITHDRAW_METHODS[String(a?.withdrawMethod || 'custom').toLowerCase()];
  if (m) { a.withdrawFeePct = m.withdrawFeePct; a.payoutFxLossPct = m.payoutFxLossPct; }
  return a;
}

/**
 * Курс валюты аккаунта Market.CSGO к доллару (сколько ₽ в 1 $). Рублёвый аккаунт нельзя перевести в USD,
 * поэтому история, стакан и цены выставления пересчитываются через этот курс.
 * Порядок: arbitrage.rubPerUsd > 0 → медиана отношения публичных прайсов RUB/USD самой площадки (кэш 6 ч) → запасной курс.
 */
export class AccountFx {
  constructor({ a, client, db = null, log = null }) {
    Object.assign(this, { a, client, db, log });
    this.cache = db?.get('arb.fx') || null;
  }

  async rate(currency) {
    const cur = String(currency || 'USD').toUpperCase();
    if (cur === 'USD') return 1;
    if (cur !== 'RUB') throw new Error(`валюта аккаунта ${cur} не поддерживается (нужен USD или RUB)`);
    if (Number(this.a.rubPerUsd) > 0) return Number(this.a.rubPerUsd);
    if (this.cache && Date.now() - this.cache.ts < 6 * HOUR) return this.cache.rate;
    try {
      const [rub, usd] = [await this.client.prices('RUB'), await this.client.prices('USD')];
      const r = derive(rub, usd);
      if (r) {
        this.cache = { rate: r, ts: Date.now() };
        this.db?.set('arb.fx', this.cache);
        this.log?.info(`Market.CSGO: курс аккаунта ${r.toFixed(2)} ₽ за $ (по прайсам площадки)`);
        return r;
      }
    } catch (e) { this.log?.warn(`Market.CSGO: курс RUB/USD не получен (${e.message || e}), беру запасной`); }
    return this.cache?.rate || Number(this.a.rubPerUsdFallback) || 86;
  }
}

/** Медиана отношения цен одного предмета в RUB и USD (только ликвидные позиции, разумный коридор). */
export function derive(rub, usd, minCount = 5) {
  const ratios = [];
  for (const [name, u] of usd) {
    const r = rub.get(name);
    if (!r || !(u.price >= 0.5) || !(r.price > 0) || (u.count || 0) < minCount) continue;
    const k = r.price / u.price;
    if (k > 30 && k < 300) ratios.push(k);
  }
  if (ratios.length < 20) return null;
  return median(ratios.sort((x, y) => x - y));
}
