import { readFileSync, statSync, mkdirSync, writeFileSync, renameSync } from 'node:fs';
import { dirname, join } from 'node:path';

/**
 * Орбита центра (Пульт Орбиты, center/orbit.py): что снято с продажи. Центр пишет shared/orbit.json и передаёт
 * путь в CENTER_ORBIT. LisBot смотрит ключи buy.steam (закупка под ТП Steam) и buy.arb (арбитраж на Market.CSGO):
 * закрыто — новые покупки не делаются, а купленное продаётся как обычно. Без файла всё открыто.
 */
export class OrbitGate {
  constructor({ bot = process.env.CENTER_ORBIT_BOT || 'lisbot', path = process.env.CENTER_ORBIT || '', ttlMs = 3000 } = {}) {
    Object.assign(this, { bot, path, ttlMs });
    this.data = {}; this.mtime = null; this.checked = 0; this.ackedVersion = null;
  }

  refresh() {
    if (!this.path) return this.data;
    const now = Date.now();
    if (now - this.checked < this.ttlMs) return this.data;
    this.checked = now;
    let mtime;
    try { mtime = statSync(this.path).mtimeMs; } catch { this.data = {}; this.mtime = null; return this.data; }
    if (mtime === this.mtime) return this.data;
    try { const d = JSON.parse(readFileSync(this.path, 'utf8')); if (d && typeof d === 'object') { this.data = d; this.mtime = mtime; } } catch { /* файл пишется — прошлое решение */ }
    this.ack();
    return this.data;
  }

  closed() { return new Set((this.refresh().closed || {})[this.bot] || []); }
  blocked(...keys) { const c = this.closed(); return keys.some(k => k && c.has(k)); }

  /** Подтверждение центру: закрытые ключи применены (закупка просто перестаёт начинаться). */
  ack() {
    const version = Number(this.data.version || 0);
    if (!this.path || version === this.ackedVersion) return;
    this.ackedVersion = version;
    try {
      const dir = join(dirname(this.path), 'orbit-ack');
      mkdirSync(dir, { recursive: true });
      const applied = Object.fromEntries([...((this.data.closed || {})[this.bot] || [])].map(k => [k, 'ok']));
      const tmp = join(dir, `${this.bot}.json.tmp`);
      writeFileSync(tmp, JSON.stringify({ bot: this.bot, version, ts: Date.now() / 1000, pid: process.pid, applied }));
      renameSync(tmp, join(dir, `${this.bot}.json`));
    } catch { /* подтверждение необязательно */ }
  }
}
