import { DAY, startOfDay, dayKey } from './util/common.js';

const STATUS_RU = {
  bought: 'Куплен (ждём трейд)', locked_on_lis: 'Заблокирован на LIS', trade_sent: 'Трейд отправлен', protected: 'Trade Protection',
  ready: 'Готов к продаже', listing: 'Выставляется', listed: 'На ТП', sold: 'Продан', failed: 'Не состоялся', reversed: 'Откат сделки',
  dry_run: 'Симуляция', manual: 'Вручную',
};
export { STATUS_RU };

/** Вся аналитика работы бота одной функцией (для панели, отчётов и /stats). */
export function summary(db, { tz = 'UTC', fx } = {}) {
  const q = (sql, ...a) => db.db.prepare(sql).get(...a);
  const all = (sql, ...a) => db.db.prepare(sql).all(...a);
  const sod = startOfDay(tz);

  const funnel = Object.fromEntries(all('SELECT status, COUNT(*) c, COALESCE(SUM(buy_price_usd),0) s FROM items WHERE dry_run=0 GROUP BY status').map((r) => [r.status, { count: r.c, usd: r.s }]));
  const sold = q('SELECT COUNT(*) n, COALESCE(SUM(buy_price_usd),0) cost, COALESCE(SUM(sold_usd),0) rev, COALESCE(SUM(profit_usd),0) profit, AVG((sold_at - received_at) / 86400000.0) hold, AVG((sold_at - listed_at) / 3600000.0) sellHours FROM items WHERE dry_run=0 AND status=\'sold\'');
  const today = q('SELECT COALESCE(SUM(profit_usd),0) profit, COUNT(*) n FROM items WHERE dry_run=0 AND sold_at>=?', sod);
  const spentToday = db.spentSince(sod);
  const boughtToday = q("SELECT COUNT(*) n FROM items WHERE dry_run=0 AND created_at>=? AND status<>'failed'", sod).n;
  const exposure = db.exposureUsd();
  const expectedOpen = q(`SELECT COALESCE(SUM(expected_profit_usd),0) s FROM items WHERE dry_run=0 AND status IN ('bought','locked_on_lis','trade_sent','protected','ready','listing','listed')`).s;
  const bal = db.get('balance.last');

  // прогноз vs факт: насколько оценка совпала с реальной продажей
  const accuracy = q('SELECT AVG(sold_usd / expected_net_usd) r, COUNT(*) n FROM items WHERE dry_run=0 AND status=\'sold\' AND expected_net_usd>0');

  const byMode = all("SELECT mode, COUNT(*) n, COALESCE(SUM(buy_price_usd),0) spent, COALESCE(SUM(CASE WHEN status='sold' THEN profit_usd END),0) profit, SUM(status='sold') sold FROM items WHERE dry_run=0 AND status<>'failed' GROUP BY mode");
  const latency = q("SELECT AVG(json_extract(data,'$.latencyMs')) avg, MIN(json_extract(data,'$.latencyMs')) min, COUNT(*) n FROM events WHERE type IN ('buy.ok','buy.dry_run') AND json_extract(data,'$.latencyMs') IS NOT NULL AND ts>=?", Date.now() - 7 * DAY);
  const purchases = q("SELECT COUNT(*) total, SUM(status='failed') failed, SUM(status IN ('done','pending')) ok FROM purchases WHERE dry_run=0 AND ts>=?", Date.now() - 7 * DAY);
  const errors24 = all("SELECT json_extract(data,'$.scope') scope, COUNT(*) n FROM events WHERE type='error' AND ts>=? GROUP BY scope ORDER BY n DESC", Date.now() - DAY);
  const alerts = all('SELECT * FROM alerts WHERE acked=0 ORDER BY id DESC LIMIT 50');
  const topups = all("SELECT ts, json_extract(data,'$.amount') amount FROM events WHERE type='balance.topup' ORDER BY id DESC LIMIT 10");
  const dry = q("SELECT COUNT(*) n, COALESCE(SUM(buy_price_usd),0) spent, COALESCE(SUM(expected_profit_usd),0) exp FROM items WHERE dry_run=1");

  return {
    generatedAt: Date.now(), fx,
    balance: bal?.balance ?? null, exposure, capital: (bal?.balance || 0) + exposure, expectedOpenProfit: expectedOpen,
    funnel, statusNames: STATUS_RU,
    sold: { ...sold, roi: sold.cost ? sold.profit / sold.cost : null },
    today: { profit: today.profit, soldCount: today.n, spent: spentToday, bought: boughtToday },
    forecastAccuracy: accuracy, byMode, rtbaLatency: latency, purchases7d: purchases, errors24, alerts, topups, dryRunStats: dry,
  };
}

/** Дневные ряды за N дней: траты, выручка, прибыль, покупки, продажи. */
export function daily(db, { tz = 'UTC', days = 30 } = {}) {
  const from = Date.now() - days * DAY;
  const rows = new Map();
  const row = (k) => { if (!rows.has(k)) rows.set(k, { day: k, spent: 0, revenue: 0, profit: 0, bought: 0, sold: 0 }); return rows.get(k); };
  for (let t = from; t <= Date.now(); t += DAY) row(dayKey(t, tz));
  for (const it of db.db.prepare("SELECT created_at, buy_price_usd, status FROM items WHERE dry_run=0 AND created_at>=? AND status<>'failed'").all(from)) {
    const r = row(dayKey(it.created_at, tz)); r.spent += it.buy_price_usd; r.bought++;
  }
  for (const it of db.db.prepare('SELECT sold_at, sold_usd, profit_usd FROM items WHERE dry_run=0 AND sold_at>=?').all(from)) {
    const r = row(dayKey(it.sold_at, tz)); r.revenue += it.sold_usd; r.profit += it.profit_usd; r.sold++;
  }
  return [...rows.values()].sort((a, b) => a.day.localeCompare(b.day));
}

/** Статистика по предметам: что приносит деньги, а что нет. */
export function perItem(db) {
  return db.db.prepare(`SELECT name, COUNT(*) bought, SUM(status='sold') sold,
      COALESCE(SUM(buy_price_usd),0) spent, COALESCE(SUM(CASE WHEN status='sold' THEN profit_usd END),0) profit,
      AVG(CASE WHEN status='sold' THEN profit_usd / buy_price_usd END) roi,
      AVG(CASE WHEN status='sold' THEN (sold_at - received_at) / 86400000.0 END) holdDays,
      SUM(status IN ('bought','locked_on_lis','trade_sent','protected','ready','listing','listed')) open
    FROM items WHERE dry_run=0 AND status<>'failed' GROUP BY name ORDER BY profit DESC`).all();
}

export function textReport(s, cur = 'USD') {
  const f = (x) => (Number.isFinite(x) ? `$${x.toFixed(2)}` : '—');
  const fn = s.funnel;
  const c = (k) => fn[k]?.count || 0;
  return [
    `<b>Отчёт</b>`,
    `Баланс LIS: ${f(s.balance)} · В товаре: ${f(s.exposure)} · Капитал: ${f(s.capital)}`,
    `Сегодня: куплено ${s.today.bought} на ${f(s.today.spent)}, продано ${s.today.soldCount}, прибыль ${f(s.today.profit)}`,
    `Всего продано: ${s.sold.n} · прибыль ${f(s.sold.profit)} · ROI ${s.sold.roi != null ? (s.sold.roi * 100).toFixed(1) + '%' : '—'} · ср. удержание ${s.sold.hold ? s.sold.hold.toFixed(1) + ' дн' : '—'}`,
    `Воронка: ждём трейд ${c('bought') + c('trade_sent') + c('locked_on_lis')} · Trade Protection ${c('protected')} · готово ${c('ready')} · на ТП ${c('listing') + c('listed')}`,
    `Ожидаемая прибыль по открытым: ${f(s.expectedOpenProfit)}`,
    s.forecastAccuracy?.n ? `Точность оценки: факт/прогноз = ${(s.forecastAccuracy.r * 100).toFixed(1)}% (n=${s.forecastAccuracy.n})` : null,
    s.fx ? `Курс USD→${cur}: ${s.fx.toFixed(2)}` : null,
  ].filter(Boolean).join('\n');
}
