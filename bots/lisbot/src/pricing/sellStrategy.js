import { buyerPaysFor, sellerReceivesFor, toCents } from '../steam/fees.js';

/**
 * Цена выставления на ТП Steam.
 *  - по умолчанию: на 1 цент ниже минимального лота;
 *  - если спред до лучшего ордера на покупку ≤ useBuyOrderIfWithinPct — продаём сразу в ордер;
 *  - никогда не ниже «пола»: себестоимость × (1 + minMarginOverCost) в валюте кошелька.
 * Возвращает { buyerCents, receiveCents, floorReceiveCents, mode }.
 */
export function sellPrice({ costUsd, fx, book, valuation, cfg }) {
  const s = cfg.sell;
  const fees = cfg.steam.fees;
  const floorReceiveCents = Math.ceil(costUsd * fx * (1 + s.minMarginOverCost) * 100);
  const floorBuyerCents = buyerPaysFor(floorReceiveCents, fees);

  let buyerCents, mode;
  const lowest = book?.lowestSell ? toCents(book.lowestSell) : null;
  const highestBuy = book?.highestBuy ? toCents(book.highestBuy) : null;
  if (lowest && highestBuy && (lowest - highestBuy) / lowest <= s.useBuyOrderIfWithinPct) {
    buyerCents = highestBuy; mode = 'buy_order';
  } else if (lowest) {
    buyerCents = lowest - s.undercutCents; mode = 'undercut';
  } else if (valuation?.medianPrice) {
    buyerCents = toCents(valuation.medianPrice * 1.05); mode = 'no_listings';
  } else {
    return null;
  }
  if (buyerCents < floorBuyerCents) { buyerCents = floorBuyerCents; mode = 'floor'; }
  return { buyerCents, receiveCents: sellerReceivesFor(buyerCents, fees), floorReceiveCents, mode };
}

/** Нужно ли переставить лот (нас перебили, лот висит дольше repriceAfterHours). */
export function repricePlan({ item, book, fx, cfg, now = Date.now() }) {
  const s = cfg.sell;
  if (!item.listed_at || now - item.listed_at < s.repriceAfterHours * 3600_000) return null;
  if (item.relists >= s.maxRelists) return { action: 'hold', reason: 'достигнут лимит перевыставлений' };
  const lowest = book?.lowestSell ? toCents(book.lowestSell) : null;
  if (!lowest || lowest >= item.list_price_cents) return null; // мы и так самые дешёвые
  const fees = cfg.steam.fees;
  const floorBuyer = buyerPaysFor(Math.ceil(item.buy_price_usd * fx * (1 + s.minMarginOverCost) * 100), fees);
  const stepped = Math.floor(item.list_price_cents * (1 - s.repriceStepPct));
  const target = Math.max(floorBuyer, Math.max(lowest - s.undercutCents, stepped));
  if (target >= item.list_price_cents) return { action: 'hold', reason: 'ниже пола не опускаемся' };
  return { action: 'reprice', buyerCents: target, receiveCents: sellerReceivesFor(target, fees) };
}
