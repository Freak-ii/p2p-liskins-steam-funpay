/**
 * Комиссии торговой площадки Steam (все суммы — в центах/копейках валюты кошелька).
 * Покупатель платит: receive + комиссия Steam (5%) + комиссия игры (CS2: 10%), каждая не меньше 1 цента.
 */
export function buyerPaysFor(receiveCents, fees = { steam: 0.05, game: 0.10 }) {
  const r = Math.max(1, Math.floor(receiveCents));
  return r + Math.max(1, Math.floor(r * fees.steam)) + Math.max(1, Math.floor(r * fees.game));
}

/** Максимальная сумма, которую получит продавец, если покупатель платит buyerCents. */
export function sellerReceivesFor(buyerCents, fees = { steam: 0.05, game: 0.10 }) {
  const p = Math.floor(buyerCents);
  if (p < 3) return 0;
  let r = Math.floor(p / (1 + fees.steam + fees.game));
  while (r > 1 && buyerPaysFor(r, fees) > p) r--;
  while (buyerPaysFor(r + 1, fees) <= p) r++;
  return Math.max(0, r);
}

export const toCents = (x) => Math.round(x * 100);
export const fromCents = (c) => c / 100;
