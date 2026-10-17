import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

/** Простой парсер .env (без зависимостей). Не перезаписывает уже заданные переменные окружения. */
export function loadEnvFile(file = path.join(ROOT, '.env')) {
  if (!fs.existsSync(file)) return;
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    let val = line.slice(eq + 1).trim();
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) val = val.slice(1, -1);
    if (process.env[key] === undefined) process.env[key] = val;
  }
}

function isObj(v) { return v && typeof v === 'object' && !Array.isArray(v); }

export function deepMerge(base, over) {
  if (!isObj(base) || !isObj(over)) return over === undefined ? base : over;
  const out = { ...base };
  for (const [k, v] of Object.entries(over)) out[k] = isObj(v) && isObj(base[k]) ? deepMerge(base[k], v) : v;
  return out;
}

/** Разбирает трейд-ссылку Steam на partner/token. */
export function parseTradeUrl(url) {
  if (!url) return null;
  try {
    const u = new URL(url);
    const partner = u.searchParams.get('partner');
    const token = u.searchParams.get('token');
    if (!partner || !token) return null;
    return { partner, token };
  } catch { return null; }
}

export function loadConfig({ configPath, env, dataDir } = {}) {
  if (!env) { loadEnvFile(); env = process.env; }
  const defaults = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.example.json'), 'utf8'));
  const file = configPath || env.BOT_CONFIG || path.join(ROOT, 'config.json');
  const user = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {};
  const cfg = deepMerge(defaults, user);
  cfg.root = ROOT;
  cfg.dataDir = dataDir ? path.resolve(dataDir) : path.resolve(ROOT, cfg.dataDir || './data');
  cfg.configFile = file;

  cfg.secrets = {
    lisApiKey: env.LIS_API_KEY || '',
    steamAccountName: env.STEAM_ACCOUNT_NAME || '',
    steamPassword: env.STEAM_PASSWORD || '',
    sharedSecret: env.STEAM_SHARED_SECRET || '',
    identitySecret: env.STEAM_IDENTITY_SECRET || '',
    tradeUrl: env.STEAM_TRADE_URL || '',
    telegramToken: env.TELEGRAM_BOT_TOKEN || '',
    telegramChatId: env.TELEGRAM_CHAT_ID || '',
    dashboardToken: env.DASHBOARD_TOKEN || '',
  };
  cfg.trade = parseTradeUrl(cfg.secrets.tradeUrl);
  return cfg;
}

/** Возвращает список проблем конфигурации. Пустой массив — всё ок. */
export function validateConfig(cfg) {
  const errs = [];
  const s = cfg.secrets;
  if (!s.lisApiKey) errs.push('LIS_API_KEY не задан');
  if (!s.steamAccountName || !s.steamPassword) errs.push('STEAM_ACCOUNT_NAME / STEAM_PASSWORD не заданы');
  if (!s.sharedSecret) errs.push('STEAM_SHARED_SECRET не задан (нужен для входа без ручного кода)');
  if (!s.identitySecret) errs.push('STEAM_IDENTITY_SECRET не задан (нужен для подтверждения лотов на ТП)');
  if (!cfg.trade) errs.push('STEAM_TRADE_URL не задан или некорректен');
  if (cfg.telegram.enabled && (!s.telegramToken || !s.telegramChatId)) errs.push('Telegram включён, но TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы');
  if (cfg.dashboard.enabled && (!s.dashboardToken || s.dashboardToken.startsWith('change-me') || s.dashboardToken.length < 16)) errs.push('DASHBOARD_TOKEN должен быть длинной случайной строкой (16+ символов)');
  const st = cfg.strategy;
  if (st.minRoi < 0.02) errs.push('strategy.minRoi слишком мал (< 2%) — комиссии и колебания съедят прибыль');
  if (st.valuationQuantile <= 0 || st.valuationQuantile >= 1) errs.push('strategy.valuationQuantile должен быть в (0;1)');
  if (st.balanceReservePct < 0 || st.balanceReservePct >= 1) errs.push('strategy.balanceReservePct должен быть в [0;1)');
  if (cfg.lis.allowLockedDays < 0 || cfg.lis.allowLockedDays > 8) errs.push('lis.allowLockedDays должен быть 0..8');
  if (cfg.watchlist.mode === 'list' && !(cfg.watchlist.items || []).length) errs.push('watchlist.items пуст');
  return errs;
}

/** Настройки, которые можно менять из панели (путь -> тип/границы). */
export const EDITABLE_SETTINGS = {
  dryRun: { type: 'bool', label: 'Режим симуляции (DRY-RUN)' },
  'steam.walletCurrency': { type: 'int', min: 1, max: 50, label: 'Валюта кошелька Steam (1=USD, 5=RUB, 3=EUR, 37=KZT)' },
  'steam.walletCurrencyCode': { type: 'str', label: 'Код валюты кошелька' },
  'steam.activeHours.enabled': { type: 'bool', label: 'Выставлять лоты только в активные часы' },
  'steam.activeHours.from': { type: 'int', min: 0, max: 23, label: 'Активные часы: с' },
  'steam.activeHours.to': { type: 'int', min: 0, max: 23, label: 'Активные часы: до' },
  'strategy.minRoi': { type: 'num', min: 0.02, max: 1, pct: true, label: 'Минимальный ROI' },
  'strategy.roiPerHoldDay': { type: 'num', min: 0, max: 0.05, pct: true, label: 'Надбавка ROI за день удержания' },
  'strategy.valuationQuantile': { type: 'num', min: 0.05, max: 0.9, label: 'Квантиль оценки (меньше = осторожнее)' },
  'strategy.minDailyVolume': { type: 'num', min: 1, max: 100000, label: 'Мин. продаж в день в Steam' },
  'strategy.bottomTolerancePct': { type: 'num', min: 0, max: 0.2, pct: true, label: 'Коридор от минимума LIS' },
  'strategy.maxFloorRisePct': { type: 'num', min: 0, max: 0.5, pct: true, label: 'Стоп при росте минимума к медиане 24ч' },
  'strategy.maxBookSharePerScan': { type: 'num', min: 0.05, max: 1, pct: true, label: 'Доля дешёвых лотов за проход' },
  'strategy.maxPerItemPerDay': { type: 'int', min: 0, max: 1000, label: 'Макс. штук предмета в день' },
  'strategy.maxHeldPerItem': { type: 'int', min: 0, max: 1000, label: 'Макс. штук предмета на руках' },
  'strategy.maxShareOfDailyVolume': { type: 'num', min: 0.001, max: 1, pct: true, label: 'Макс. доля от дневного объёма Steam' },
  'strategy.maxItemCapitalShare': { type: 'num', min: 0.01, max: 1, pct: true, label: 'Макс. доля капитала в одном предмете' },
  'strategy.maxItemPriceUsd': { type: 'num', min: 0.1, max: 100000, label: 'Макс. цена предмета, $' },
  'strategy.minItemPriceUsd': { type: 'num', min: 0, max: 10000, label: 'Мин. цена предмета, $' },
  'strategy.balanceReservePct': { type: 'num', min: 0, max: 0.95, pct: true, label: 'Резерв баланса' },
  'strategy.maxSpendPerHourUsd': { type: 'num', min: 0, max: 1e6, label: 'Лимит трат в час, $' },
  'strategy.maxSpendPerDayUsd': { type: 'num', min: 0, max: 1e7, label: 'Лимит трат в день, $' },
  'strategy.maxOpenExposureUsd': { type: 'num', min: 0, max: 1e8, label: 'Макс. сумма в товаре, $' },
  'strategy.scanIntervalSec': { type: 'int', min: 30, max: 3600, label: 'Интервал скана LIS, с' },
  'strategy.rtbaEnabled': { type: 'bool', label: 'RTBA (мгновенная покупка по WebSocket)' },
  'lis.allowLockedDays': { type: 'int', min: 0, max: 8, label: 'Покупать с блокировкой LIS до, дней' },
  'sell.enabled': { type: 'bool', label: 'Продажа на ТП Steam' },
  'sell.minMarginOverCost': { type: 'num', min: 0, max: 1, pct: true, label: 'Мин. маржа над себестоимостью' },
  'sell.maxListingsPerHour': { type: 'int', min: 1, max: 100, label: 'Макс. выставлений в час' },
  'sell.maxListingsPerDay': { type: 'int', min: 1, max: 1000, label: 'Макс. выставлений в день' },
  'sell.repriceAfterHours': { type: 'num', min: 1, max: 720, label: 'Перевыставлять через, ч' },
  'risk.maxDailyLossUsd': { type: 'num', min: 0, max: 1e6, label: 'Стоп при дневном убытке, $' },
  'risk.lowBalanceUsd': { type: 'num', min: 0, max: 1e6, label: 'Уведомлять о низком балансе, $' },
  'risk.unknownSpendIsCritical': { type: 'bool', label: 'Неизвестная трата баланса = аварийная остановка' },
  'risk.strictConfirmations': { type: 'bool', label: 'Отменять чужие мобильные подтверждения' },
  'watchlist.mode': { type: 'enum', values: ['list', 'auto'], label: 'Вотчлист: список / авто' },
  'watchlist.auto.minPriceUsd': { type: 'num', min: 0, max: 1e5, label: 'Автовотчлист: цена от, $' },
  'watchlist.auto.maxPriceUsd': { type: 'num', min: 0, max: 1e5, label: 'Автовотчлист: цена до, $' },
  'watchlist.auto.maxItems': { type: 'int', min: 1, max: 1000, label: 'Автовотчлист: макс. предметов' },
  timezone: { type: 'str', label: 'Часовой пояс (IANA)' },
};

export const getPath = (obj, p) => p.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
export function setPath(obj, p, v) {
  const ks = p.split('.');
  let o = obj;
  for (const k of ks.slice(0, -1)) o = o[k] ??= {};
  o[ks.at(-1)] = v;
}

/** Проверяет и сохраняет изменения в файл пользовательского конфига. Возвращает список ошибок. */
export function saveSettings(cfg, changes) {
  const errs = [];
  const clean = {};
  for (const [k, raw] of Object.entries(changes || {})) {
    const def = EDITABLE_SETTINGS[k];
    if (!def) { errs.push(`${k}: нельзя менять`); continue; }
    let v = raw;
    if (def.type === 'bool') v = raw === true || raw === 'true' || raw === 1;
    else if (def.type === 'int' || def.type === 'num') {
      v = Number(raw);
      if (!Number.isFinite(v) || (def.type === 'int' && !Number.isInteger(v))) { errs.push(`${def.label}: не число`); continue; }
      if (v < def.min || v > def.max) { errs.push(`${def.label}: допустимо ${def.min}…${def.max}`); continue; }
    } else if (def.type === 'enum') { if (!def.values.includes(v)) { errs.push(`${def.label}: недопустимое значение`); continue; } }
    else if (typeof v !== 'string' || !v.trim()) { errs.push(`${def.label}: пусто`); continue; }
    if (k === 'timezone') { try { new Intl.DateTimeFormat('en', { timeZone: v }); } catch { errs.push('Неизвестный часовой пояс'); continue; } }
    clean[k] = v;
  }
  if (errs.length) return errs;
  const file = cfg.configFile;
  const user = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {};
  for (const [k, v] of Object.entries(clean)) { setPath(user, k, v); setPath(cfg, k, v); }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(user, null, 2));
  return [];
}
