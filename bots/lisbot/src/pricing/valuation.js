import { weightedQuantile, median, DAY } from '../util/common.js';
import { sellerReceivesFor, toCents } from '../steam/fees.js';

/**
 * Оценка «сколько реально получим на ТП Steam через ~7+ дней».
 * Берём не текущую цену, а консервативный квантиль реальных продаж за N дней,
 * ограниченный сверху текущим минимальным лотом, со скидкой на волатильность.
 * Отсекаем неликвид, падающий тренд и слишком волатильные предметы.
 *
 * history: [{ts, price, volume}] (валюта кошелька), book: {lowestSell, highestBuy}
 */
export function computeValuation({ name, history, book, fx, cfg, holdDays, now = Date.now() }) {
  const st = cfg.strategy;
  const v = { name, ts: now, ok: false, fx };
  const windowStart = now - st.historyDays * DAY;
  const recent = history.filter((h) => h.ts >= windowStart);
  const totalVol = recent.reduce((s, h) => s + h.volume, 0);
  v.dailyVolume = totalVol / st.historyDays;
  v.lowestSell = book?.lowestSell ?? null;
  v.highestBuy = book?.highestBuy ?? null;

  if (recent.length < 5 || totalVol < 10) return { ...v, reason: 'мало продаж за период' };

  const pairs = recent.map((h) => [h.price, h.volume]);
  v.qPrice = weightedQuantile(pairs, st.valuationQuantile);
  v.medianPrice = weightedQuantile(pairs, 0.5);
  const last3 = recent.filter((h) => h.ts >= now - 3 * DAY);
  v.recentMedian = last3.length ? weightedQuantile(last3.map((h) => [h.price, h.volume]), 0.5) : v.medianPrice;
  v.trend = v.medianPrice ? v.recentMedian / v.medianPrice - 1 : 0;

  // волатильность: разброс дневных медиан относительно общей медианы
  const byDay = new Map();
  for (const h of recent) {
    const d = Math.floor(h.ts / DAY);
    if (!byDay.has(d)) byDay.set(d, []);
    byDay.get(d).push(h.price);
  }
  const dayMedians = [...byDay.values()].map(median);
  const m = median(dayMedians);
  const sd = Math.sqrt(dayMedians.reduce((s, x) => s + (x - m) ** 2, 0) / Math.max(1, dayMedians.length));
  v.volatility = m ? sd / m : 1;

  if (v.dailyVolume < st.minDailyVolume) return { ...v, reason: `ликвидность ${v.dailyVolume.toFixed(1)}/день < ${st.minDailyVolume}` };
  if (v.trend < -st.maxTrendDropPct) return { ...v, reason: `падающий тренд ${(v.trend * 100).toFixed(1)}%` };
  if (v.volatility > st.maxVolatility) return { ...v, reason: `волатильность ${(v.volatility * 100).toFixed(0)}%` };

  // ожидаемая цена продажи: квантиль продаж, но не выше текущего минимального лота и свежей медианы
  let expected = Math.min(v.qPrice, v.recentMedian);
  if (v.lowestSell) expected = Math.min(expected, v.lowestSell);
  expected *= 1 - v.volatility * st.volatilityHaircut;
  // если тренд падающий (в допустимых пределах) — экстраполируем падение на срок удержания
  if (v.trend < 0) expected *= 1 + v.trend * Math.min(1, holdDays / 7);
  v.expectedSell = expected;

  v.netWallet = sellerReceivesFor(toCents(expected), cfg.steam.fees) / 100;
  if (!fx || !Number.isFinite(fx) || fx <= 0) return { ...v, reason: 'нет курса валют' };
  v.netUsd = v.netWallet / fx;
  const requiredRoi = st.minRoi + st.roiPerHoldDay * holdDays;
  v.maxBuyUsd = v.netUsd / (1 + requiredRoi);
  v.requiredRoi = requiredRoi;
  v.ok = v.maxBuyUsd > 0;
  if (!v.ok) v.reason = 'нулевая оценка';
  return v;
}

/** Срок удержания капитала: блокировка на LIS + Trade Protection + ожидаемое время продажи. */
export function holdDaysFor(cfg, lisLockDays = 0) {
  return lisLockDays + cfg.steam.tradeProtectionDays + cfg.strategy.expectedSellDays;
}
