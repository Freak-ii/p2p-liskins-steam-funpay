import dns from 'node:dns';
import https from 'node:https';

/**
 * Запасное разрешение имён для Steam.
 * Бывает, что системный DNS Windows не отдаёт адрес api.steampowered.com
 * («getaddrinfo ENOENT/ENOTFOUND»), хотя браузер открывает Steam: он ходит через свой защищённый DNS.
 * Тогда спрашиваем публичные DNS-серверы, а если и они недоступны — DNS-over-HTTPS по IP-адресу.
 * Подменяется только dns.lookup и только для доменов Steam, всё остальное идёт как раньше.
 */
export const STEAM_DOMAINS = /(^|\.)(steampowered\.com|steamcommunity\.com|steamstatic\.com|steamserver\.net|steamcontent\.com)$/i;
export const DNS_ERRORS = new Set(['ENOENT', 'ENOTFOUND', 'EAI_AGAIN', 'EAI_FAIL', 'ESERVFAIL', 'ETIMEOUT', 'ECONNREFUSED', 'ENODATA']);
const PUBLIC_DNS = ['1.1.1.1', '8.8.8.8', '77.88.8.8'];
const DOH = ['https://1.1.1.1/dns-query?type=A&name=', 'https://8.8.8.8/resolve?type=A&name='];
const TTL_MS = 10 * 60_000;

/** Ошибка сети/DNS (бот не дошёл до Steam), а не отказ Steam во входе. */
export function isNetworkError(err) {
  if (!err) return false;
  if (DNS_ERRORS.has(err.code) || ['ECONNRESET', 'ETIMEDOUT', 'EHOSTUNREACH', 'ENETUNREACH', 'ECONNABORTED'].includes(err.code)) return true;
  return /getaddrinfo|ENOTFOUND|ENOENT|EAI_AGAIN|ECONNREFUSED|ETIMEDOUT|ECONNRESET|socket hang up|network/i.test(err.message || '');
}

function viaServers(host) {
  const r = new dns.promises.Resolver({ timeout: 3000, tries: 1 });
  r.setServers(PUBLIC_DNS);
  return r.resolve4(host);
}

function viaDoh(host, base) {
  return new Promise((resolve, reject) => {
    const req = https.get(base + encodeURIComponent(host), { headers: { accept: 'application/dns-json' }, timeout: 5000 }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { body += c; });
      res.on('end', () => {
        try {
          const ips = (JSON.parse(body).Answer || []).filter((a) => a.type === 1).map((a) => a.data);
          ips.length ? resolve(ips) : reject(new Error(`DoH: нет адреса для ${host}`));
        } catch (e) { reject(e); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('DoH: таймаут')));
    req.on('error', reject);
  });
}

/** Адреса хоста через запасные резолверы (по очереди). resolvers можно подменить в тестах. */
export async function fallbackResolve(host, resolvers = [viaServers, ...DOH.map((b) => (h) => viaDoh(h, b))]) {
  let last;
  for (const r of resolvers) {
    try { const ips = await r(host); if (ips?.length) return ips; } catch (e) { last = e; }
  }
  throw last || new Error(`не удалось найти адрес ${host}`);
}

let installed = null;

/**
 * Обернуть dns.lookup: сначала системный DNS, при ошибке для домена Steam — запасные резолверы.
 * opts.resolve — функция (host) => Promise<string[]> (для тестов), opts.onFallback(host, ips, err) — для лога.
 */
export function installSteamDnsFallback({ resolve = (h) => fallbackResolve(h), onFallback = () => {} } = {}) {
  if (installed) return installed;
  const original = dns.lookup;
  const cache = new Map();
  function lookup(hostname, options, callback) {
    if (typeof options === 'function') { callback = options; options = {}; }
    if (typeof options === 'number') options = { family: options };
    options = options || {};
    if (typeof hostname !== 'string' || !STEAM_DOMAINS.test(hostname) || options.family === 6) return original.call(dns, hostname, options, callback);
    const answer = (ips) => {
      if (options.all) return callback(null, ips.map((address) => ({ address, family: 4 })));
      return callback(null, ips[0], 4);
    };
    const hit = cache.get(hostname);
    original.call(dns, hostname, options, (err, address, family) => {
      if (!err) return callback(null, address, family);
      if (!DNS_ERRORS.has(err.code)) return callback(err);
      if (hit && hit.until > Date.now()) return answer(hit.ips);
      resolve(hostname).then((ips) => {
        cache.set(hostname, { ips, until: Date.now() + TTL_MS });
        onFallback(hostname, ips, err);
        answer(ips);
      }, () => callback(err));
    });
  }
  dns.lookup = lookup;
  installed = { uninstall() { dns.lookup = original; installed = null; } };
  return installed;
}
