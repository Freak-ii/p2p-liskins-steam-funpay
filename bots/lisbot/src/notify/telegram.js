import { sleep, errMsg } from '../util/common.js';

const ICON = { info: 'ℹ️', money: '💰', warn: '⚠️', critical: '🚨', report: '📊', ok: '✅' };
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

/** Уведомления в Telegram + команды управления (только из разрешённого чата). */
export class Telegram {
  constructor({ token, chatId, enabled, dedupeMinutes = 15, log, fetchImpl = globalThis.fetch }) {
    Object.assign(this, { token, chatId: String(chatId || ''), enabled: enabled && !!token && !!chatId, log, fetch: fetchImpl });
    this.dedupeMs = dedupeMinutes * 60_000;
    this.recent = new Map();
    this.queue = Promise.resolve();
    this.commands = new Map();
    this.offset = 0;
    this.polling = false;
    this.subscribers = [];
  }

  /** Подписка на все уведомления (например, системные уведомления десктоп-приложения). */
  subscribe(fn) { this.subscribers.push(fn); return () => { this.subscribers = this.subscribers.filter((f) => f !== fn); }; }

  async api(method, body) {
    const res = await this.fetch(`https://api.telegram.org/bot${this.token}/${method}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: AbortSignal.timeout(method === 'getUpdates' ? 40_000 : 15_000),
    });
    const json = await res.json().catch(() => ({}));
    if (!json.ok) throw new Error(`Telegram ${method}: ${json.description || res.status}`);
    return json.result;
  }

  /** level: info | money | warn | critical | report | ok. dedupeKey — не слать одно и то же чаще dedupeMinutes. */
  send(level, text, { dedupeKey } = {}) {
    if (dedupeKey) {
      const last = this.recent.get(dedupeKey);
      if (last && Date.now() - last < this.dedupeMs) return Promise.resolve(false);
      this.recent.set(dedupeKey, Date.now());
    }
    const msg = `${ICON[level] || ''} ${level === 'critical' ? '<b>КРИТИЧНО</b>\n' : ''}${text}`;
    for (const fn of this.subscribers) { try { fn(level, String(text).replace(/<[^>]+>/g, '').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')); } catch { /* подписчик не должен ронять отправку */ } }
    if (!this.enabled) { this.log.info(`[telegram выкл] ${msg.replace(/<[^>]+>/g, '')}`); return Promise.resolve(false); }
    this.queue = this.queue.then(async () => {
      for (let i = 0; i < 4; i++) {
        try {
          await this.api('sendMessage', { chat_id: this.chatId, text: msg.slice(0, 4000), parse_mode: 'HTML', disable_web_page_preview: true, disable_notification: level === 'info' });
          return true;
        } catch (e) {
          this.log.warn(`Telegram: ${errMsg(e)}`);
          await sleep(2000 * (i + 1));
        }
      }
      return false;
    });
    return this.queue;
  }

  on(cmd, handler, help) { this.commands.set(cmd, { handler, help }); }

  startPolling() {
    if (!this.enabled || this.polling) return;
    this.polling = true;
    (async () => {
      while (this.polling) {
        try {
          const updates = await this.api('getUpdates', { offset: this.offset, timeout: 30, allowed_updates: ['message'] });
          for (const u of updates) {
            this.offset = u.update_id + 1;
            const m = u.message;
            if (!m?.text || String(m.chat.id) !== this.chatId) continue; // чужие чаты игнорируем
            const [raw, ...args] = m.text.trim().split(/\s+/);
            const cmd = raw.replace(/^\//, '').replace(/@.*$/, '').toLowerCase();
            const c = this.commands.get(cmd);
            if (!c) { await this.send('info', this.helpText()); continue; }
            try { const reply = await c.handler(args); if (reply) await this.send('info', reply); } catch (e) { await this.send('warn', `Команда /${cmd}: ${esc(errMsg(e))}`); }
          }
        } catch (e) {
          this.log.warn(`Telegram polling: ${errMsg(e)}`);
          await sleep(10_000);
        }
      }
    })();
  }

  helpText() { return ['Команды:', ...[...this.commands].map(([k, v]) => `/${k} — ${v.help || ''}`)].join('\n'); }

  stop() { this.polling = false; }
}

export { esc };
