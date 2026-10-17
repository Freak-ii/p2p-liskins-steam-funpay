import { EventEmitter } from 'node:events';
import WebSocket from 'ws';
import { normalizeListing } from './client.js';
import { sleep } from '../util/common.js';

/**
 * Минимальный клиент Centrifugo (JSON-протокол v2), через который LIS-SKINS
 * отдаёт события маркета в реальном времени. Эмитит:
 *   'listing'  — новый/изменённый лот { id, name, price, unlockAt, ... }, receivedAt
 *   'removed'  — лот снят/куплен { id }
 *   'status'   — 'connected' | 'disconnected'
 * Формат событий LIS может меняться, поэтому разбор максимально терпимый.
 */
export class LisWs extends EventEmitter {
  constructor({ url, channels, getToken, log }) {
    super();
    Object.assign(this, { url, channels, getToken, log });
    this.ws = null;
    this.stopped = false;
    this.cmdId = 0;
    this.connected = false;
    this.stats = { messages: 0, listings: 0, unparsed: 0, reconnects: 0, lastMessageAt: 0 };
  }

  start() { this._loop(); return this; }

  stop() { this.stopped = true; try { this.ws?.close(); } catch { /* noop */ } }

  async _loop() {
    let backoff = 2000;
    while (!this.stopped) {
      try {
        const token = await this.getToken();
        await this._connectOnce(token);
        backoff = 2000;
      } catch (e) {
        this.log.warn(`LIS WS: ${e.message}`);
      }
      if (this.stopped) break;
      this.stats.reconnects++;
      await sleep(backoff + Math.random() * 1000);
      backoff = Math.min(backoff * 2, 120_000);
    }
  }

  _send(obj) { this.ws?.readyState === WebSocket.OPEN && this.ws.send(JSON.stringify({ id: ++this.cmdId, ...obj })); }

  _connectOnce(token) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.url, { handshakeTimeout: 15_000 });
      this.ws = ws;
      let watchdog;
      const resetWatchdog = () => {
        clearTimeout(watchdog);
        // сервер Centrifugo шлёт пинг ~каждые 25с; если тишина 90с — переподключаемся
        watchdog = setTimeout(() => ws.terminate(), 90_000);
      };
      ws.on('open', () => {
        resetWatchdog();
        this._send({ connect: { token, name: 'lisskins-bot' } });
      });
      ws.on('message', (buf) => {
        resetWatchdog();
        for (const line of buf.toString().split('\n')) {
          if (!line.trim()) continue;
          let msg;
          try { msg = JSON.parse(line); } catch { continue; }
          this._handle(msg);
        }
      });
      ws.on('close', (code) => {
        clearTimeout(watchdog);
        if (this.connected) this.emit('status', 'disconnected');
        this.connected = false;
        code === 1000 || this.stopped ? resolve() : reject(new Error(`соединение закрыто (${code})`));
      });
      ws.on('error', (e) => reject(e));
    });
  }

  _handle(msg) {
    this.stats.messages++;
    this.stats.lastMessageAt = Date.now();
    // пинг сервера: пустой объект -> отвечаем пустым объектом
    if (msg && Object.keys(msg).length === 0) { this.ws?.send('{}'); return; }
    if (msg.error) { this.log.warn('LIS WS ошибка', msg.error); if (msg.error.code === 109) this.ws?.close(); return; }
    if (msg.connect) {
      this.connected = true;
      this.emit('status', 'connected');
      this.log.info(`LIS WS подключён, подписка: ${this.channels.join(', ')}`);
      for (const channel of this.channels) this._send({ subscribe: { channel } });
      return;
    }
    const pub = msg.push?.pub;
    if (pub?.data) this._handlePublication(pub.data);
  }

  _handlePublication(data) {
    const receivedAt = Date.now();
    const evt = String(data.event || data.type || data.action || '').toLowerCase();
    const payloads = Array.isArray(data.payload) ? data.payload : Array.isArray(data.data) ? data.data : [data.payload ?? data.data ?? data];
    for (const p of payloads) {
      if (/delet|remov|sold|purchas/.test(evt)) {
        const id = p?.id ?? p?.item_id;
        if (id != null) this.emit('removed', { id: String(id) });
        continue;
      }
      const listing = normalizeListing(p);
      if (!listing) { this.stats.unparsed++; continue; }
      this.stats.listings++;
      this.emit('listing', listing, { event: evt, receivedAt });
    }
  }
}
