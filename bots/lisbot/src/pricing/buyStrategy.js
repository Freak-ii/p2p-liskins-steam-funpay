import { median, DAY } from '../util/common.js';
import { holdDaysFor } from './valuation.js';

const lockDays = (l, now) => (l.unlockAt && l.unlockAt > now ? Math.ceil((l.unlockAt - now) / DAY) : 0);

/** Максимальная цена покупки (USD) для лота с учётом блокировки на LIS и индивидуального ROI. */
export function maxBuyFor(valuation, cfg, watch, days) {
  const st = cfg.strategy;
  const roi = (watch?.min_roi ?? st.minRoi) + st.roiPerHoldDay * holdDaysFor(cfg, days) + cfg.lis.lockedExtraRoiPerDay * days;
  return valuation.netUsd / (1 + roi);
}

/**
 * Свободный бюджет с учётом всех ограничений.
 * ctx: { balance, exposure, exposureItem, spentHour, spentDay }
 */
export function budgetFor(ctx, cfg, watch) {
  const st = cfg.strategy;
  const capital = ctx.balance + ctx.exposure;
  const reserve = capital * st.balanceReservePct;
  const limits = {
    balance: ctx.balance - reserve,
    item: capital * st.maxItemCapitalShare - ctx.exposureItem,
    hour: st.maxSpendPerHourUsd - ctx.spentHour,
    day: st.maxSpendPerDayUsd - ctx.spentDay,
    exposure: st.maxOpenExposureUsd - ctx.exposure,
  };
  let min = Infinity, by = null;
  for (const [k, v] of Object.entries(limits)) if (v < min) { min = v; by = k; }
  return { amount: Math.max(0, min), limitedBy: by, limits };
}

/**
 * Решение о закупке одного предмета («по низу рынка», без погони за ценой).
 *
 * Главные принципы:
 *  1. Цена покупки ограничена И оценкой выручки в Steam (ROI), И «полом» LIS: берём только лоты
 *     в пределах bottomTolerancePct от минимальной цены.
 *  2. «Пол» сравнивается с медианой пола за последние floorBaselineHours. Если пол поднялся
 *     (дешёвое уже скупили — мы или конкуренты), НЕ докупаем по завышенной цене — ждём.
 *  3. За один проход берём не больше maxBookSharePerScan дешёвых лотов, чтобы не выкупать
 *     весь низ и не двигать цену против себя.
 *  4. Количество ограничено дневным лимитом, лимитом удержания и долей от дневного объёма
 *     продаж в Steam (чтобы потом продать, не обрушив цену).
 *  5. Бюджет: резерв баланса, доля капитала на предмет, лимиты трат в час/день, общая экспозиция.
 *
 * Возвращает { buy: [{listing, maxPrice, expectedProfit}], reason, info }.
 */
export function planBuys({ name, listings, valuation, watch, ctx, cfg, floorHistory = [], now = Date.now() }) {
  const st = cfg.strategy;
  const info = {};
  const skip = (reason, extra = {}) => ({ buy: [], reason, info: { ...info, ...extra } });

  if (!valuation?.ok) return skip(`оценка недоступна: ${valuation?.reason || 'нет данных'}`);
  const age = now - valuation.ts;
  if (age > cfg.risk.maxValuationStalenessHours * 3600_000) return skip('оценка устарела');

  const usable = listings
    .filter((l) => l.name === name && lockDays(l, now) <= cfg.lis.allowLockedDays)
    .sort((a, b) => a.price - b.price);
  if (!usable.length) return skip('нет подходящих лотов');

  const floor = usable[0].price;
  info.floor = floor;
  info.netUsd = valuation.netUsd;

  if (floor < valuation.netUsd * st.suspiciousCheapPct) {
    return skip('подозрительно дешёвый лот (возможна ошибка сопоставления/фазы) — пропуск', { suspicious: true });
  }

  // базовая линия пола LIS
  let reference = floor;
  if (floorHistory.length >= st.minFloorSamples) {
    const baseline = median(floorHistory.map((f) => f.floor_usd ?? f));
    info.baseline = baseline;
    if (floor > baseline * (1 + st.maxFloorRisePct)) {
      return skip(`пол LIS вырос на ${((floor / baseline - 1) * 100).toFixed(1)}% к медиане ${st.floorBaselineHours}ч — не догоняем цену`, { floorRisen: true });
    }
    reference = Math.min(baseline, floor);
  }
  const bandCap = reference * (1 + st.bottomTolerancePct);
  const hardCap = Math.min(watch?.max_price_usd ?? Infinity, st.maxItemPriceUsd);
  info.bandCap = bandCap;

  const inBand = usable.filter((l) => l.price <= bandCap);
  const candidates = inBand.filter((l) => {
    const cap = Math.min(maxBuyFor(valuation, cfg, watch, lockDays(l, now)), hardCap);
    return l.price <= cap && l.price >= st.minItemPriceUsd;
  });
  info.maxBuyUsd = Math.min(maxBuyFor(valuation, cfg, watch, 0), hardCap);
  if (!candidates.length) {
    return skip(floor > info.maxBuyUsd ? `пол ${floor.toFixed(2)} > макс. цены ${info.maxBuyUsd.toFixed(2)} (ROI не проходит)` : 'нет лотов в ценовом коридоре');
  }

  // количество
  const heldLimit = (watch?.max_held ?? st.maxHeldPerItem) - ctx.heldCount;
  const dayLimit = (watch?.max_per_day ?? st.maxPerItemPerDay) - ctx.boughtToday;
  const volumeLimit = Math.floor(valuation.dailyVolume * st.maxShareOfDailyVolume) - ctx.heldCount;
  const bookLimit = Math.max(1, Math.floor(inBand.length * st.maxBookSharePerScan));
  const qty = Math.min(heldLimit, dayLimit, volumeLimit, bookLimit);
  Object.assign(info, { heldLimit, dayLimit, volumeLimit, bookLimit });
  if (qty <= 0) {
    const which = qty === heldLimit ? 'лимит удержания' : qty === dayLimit ? 'дневной лимит' : 'лимит по объёму продаж Steam';
    return skip(`достигнут ${which}`);
  }

  // бюджет
  const budget = budgetFor(ctx, cfg, watch);
  info.budget = budget;
  const buy = [];
  let sum = 0;
  for (const l of candidates) {
    if (buy.length >= qty) break;
    if (sum + l.price > budget.amount) break;
    sum += l.price;
    buy.push({ listing: l, maxPrice: l.price, expectedProfit: valuation.netUsd - l.price, lockDays: lockDays(l, now) });
  }
  if (!buy.length) return skip(`нет бюджета (ограничение: ${budget.limitedBy}, доступно $${budget.amount.toFixed(2)})`);
  return { buy, reason: 'ok', info: { ...info, total: sum } };
}
