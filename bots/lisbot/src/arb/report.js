/** Отчёт по связкам Lis → Market.CSGO на живых ценах, без покупок и без входа в Steam: npm run arb */
import { loadConfig } from '../config.js';
import { CsgoMarketClient } from './csgoClient.js';
import { ArbScanner } from './scanner.js';
import { netFactor } from './math.js';

const cfg = loadConfig();
const log = { info() {}, warn: (m) => console.warn(m), error: (m) => console.error(m), debug() {} };
const client = new CsgoMarketClient({ apiKey: cfg.secrets.csgoApiKey, log });
const arb = new ArbScanner({ cfg, db: null, client, log });
const picks = await arb.scan();
const pct = (x) => `${(x * 100).toFixed(1)}%`;
console.log(`После всех комиссий возвращается ${pct(netFactor(cfg.arbitrage))} цены продажи. Кандидатов: ${arb.scanned}, проходят цель ${pct(arb.minRoi)}: ${picks.length}`);
console.log(client.hasKey ? 'Оценка: история продаж и стакан по ключу' : 'Ключа нет: считаю продажу в верхнюю заявку. Её глубина неизвестна (число заявок в прайсе — по всем ценам), для покупок нужен ключ');
for (const p of picks) console.log(`${pct(p.roi).padStart(6)}  ${p.name}  Lis $${p.lis} ×${p.count}  продажа $${p.expectedSell.toFixed(2)} (${p.mode})  заявка $${p.bid} ×${p.bidCount} (${pct(p.bidRoi)})  аск $${p.ask}  продаж/нед ${p.weeklySales}`);
client.sched.stop();
