import { quantile, median, DAY } from '../util/common.js';

/** Доля цены продажи, которая вернётся в $ Lis-Skins: комиссия продажи, вывод, потеря на курсе, комиссия пополнения Lis. */
export function netFactor(a) {
  return (1 - a.saleFeePct) * (1 - a.withdrawFeePct) * (1 - (a.payoutFxLossPct || 0)) / (1 + (a.lisDepositFeePct || 0));
}

/**
 * Оценка продажи на Market.CSGO. Цены в USD.
 *  - bid: мгновенная продажа в заявку (нижняя граница). Заявка не ниже аска неисполнима — не учитываем;
 *  - history (если есть ключ): осторожный квантиль реальных продаж, не выше свежей медианы и текущего аска;
 *  - без истории считаем только мгновенную продажу в заявку;
 *  - предохранитель: даже продав в заявку, теряем не больше maxDownsidePct.
 * netUsd — то, с чем стратегия сравнивает цену Lis (уже после всех комиссий).
 */
export function valueItem({ name, ask, bid, bidCount = 0, history = null, a, minRoi, now = Date.now() }) {
  const v = { name, ts: now, ok: false, ask: ask || null, bid: bid || null, fx: 1 };
  if (!(ask > 0)) return { ...v, reason: 'нет предложений на Market.CSGO' };
  if (!(bid > 0) || bidCount < a.minBidCount || bid >= ask) return { ...v, reason: 'нет исполнимых заявок на покупку' };
  const k = netFactor(a);
  v.bidNetUsd = bid * k;
  let expected = bid;
  v.mode = 'bid';
  v.weeklySales = 0;
  if (history) {
    const recent = history.filter((h) => h.ts >= now - a.historyDays * DAY);
    const week = history.filter((h) => h.ts >= now - 7 * DAY);
    v.weeklySales = week.length;
    // предмет, которым почти не торгуют: заявка может не дожить до конца холда
    if (week.length < Math.ceil(a.minWeeklySales / 3)) return { ...v, reason: `продаж за неделю ${week.length}` };
    if (week.length >= a.minWeeklySales && recent.length) {
      const q = quantile(recent.map((h) => h.price).sort((x, y) => x - y), a.saleQuantile);
      const fresh = history.filter((h) => h.ts >= now - 3 * DAY);
      const list = Math.min(q, median((fresh.length >= 2 ? fresh : week).map((h) => h.price)), ask * (1 - a.undercutPct));
      if (list > expected) { expected = list; v.mode = 'list'; }
    }
  }
  v.expectedSell = expected;
  v.expectedNetUsd = expected * k;
  const downsideCap = (v.bidNetUsd / (1 - a.maxDownsidePct)) * (1 + minRoi);
  v.netUsd = Math.min(v.expectedNetUsd, downsideCap);
  // стратегия берёт floor(dailyVolume × maxShareOfDailyVolume): у Market.CSGO сделки редкие, считаем окно в неделю
  v.dailyVolume = Math.max(v.weeklySales, v.mode === 'bid' ? bidCount : 0);
  v.ok = v.netUsd > 0;
  return v;
}

/** Цена выставления: чуть ниже аска, но не ниже пола себестоимости; если заявка уже даёт цель — сразу в заявку. */
export function listPrice({ costUsd, ask, bid, a, relists = 0 }) {
  const k = netFactor(a);
  const floor = (costUsd * (1 + a.minMarginOverCost)) / k;
  const target = (ask > 0 ? ask : floor * 1.1) * (1 - a.undercutPct) * (1 - a.repriceStepPct * relists);
  const executableBid = bid > 0 && (!(ask > 0) || bid < ask) ? bid : 0;
  if (executableBid >= target * (1 - a.sellIntoBidWithinPct) && executableBid >= floor) return { price: executableBid, floor, mode: 'bid' };
  return target >= floor ? { price: target, floor, mode: 'ask' } : { price: floor, floor, mode: 'floor' };
}
