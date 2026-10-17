import { sleep, jitter, HOUR } from './common.js';

export class RateLimitedError extends Error {
  constructor(msg, retryAfterMs) { super(msg); this.code = 'RATE_LIMITED'; this.retryAfterMs = retryAfterMs; }
}

/**
 * Последовательная очередь запросов:
 *  - не более одного запроса одновременно;
 *  - минимальная пауза между запросами + случайный разброс (человеческий ритм, без «пулемёта»);
 *  - почасовой лимит (скользящее окно);
 *  - глобальная пауза (backoff) после 429, с экспоненциальным ростом.
 */
export class RequestScheduler {
  constructor({ name, minDelayMs = 1000, jitterMs = 0, maxPerHour = Infinity, backoffMs = 600_000, maxBackoffMs = 3 * HOUR, onBackoff } = {}) {
    Object.assign(this, { name, minDelayMs, jitterMs, maxPerHour, baseBackoffMs: backoffMs, maxBackoffMs, onBackoff });
    this.queue = [];
    this.running = false;
    this.lastAt = 0;
    this.history = [];
    this.pausedUntil = 0;
    this.consecutiveLimits = 0;
    this.stopped = false;
  }

  get stats() {
    return { name: this.name, queued: this.queue.length, lastHour: this.history.length, pausedUntil: this.pausedUntil, consecutiveLimits: this.consecutiveLimits };
  }

  schedule(fn, { priority = 0, label = '' } = {}) {
    return new Promise((resolve, reject) => {
      const job = { fn, resolve, reject, priority, label };
      const idx = this.queue.findIndex((j) => j.priority < priority);
      if (idx < 0) this.queue.push(job); else this.queue.splice(idx, 0, job);
      this._pump();
    });
  }

  /** Сообщить о 429/лимите: ставим глобальную паузу с экспоненциальным ростом. */
  rateLimited(retryAfterMs) {
    this.consecutiveLimits++;
    const exp = Math.min(this.maxBackoffMs, this.baseBackoffMs * 2 ** (this.consecutiveLimits - 1));
    const wait = Math.max(exp, retryAfterMs || 0);
    this.pausedUntil = Math.max(this.pausedUntil, Date.now() + wait);
    this.onBackoff?.(wait, this.consecutiveLimits);
    return wait;
  }

  ok() { this.consecutiveLimits = 0; }

  stop() { this.stopped = true; for (const j of this.queue.splice(0)) j.reject(new Error(`${this.name}: остановлено`)); }

  async _pump() {
    if (this.running) return;
    this.running = true;
    try {
      while (this.queue.length && !this.stopped) {
        const t = Date.now();
        this.history = this.history.filter((x) => t - x < HOUR);
        let wait = 0;
        if (this.pausedUntil > t) wait = this.pausedUntil - t;
        else if (this.history.length >= this.maxPerHour) wait = this.history[0] + HOUR - t + 50;
        else {
          const gap = jitter(this.minDelayMs, this.jitterMs);
          if (t - this.lastAt < gap) wait = gap - (t - this.lastAt);
        }
        if (wait > 0) { await sleep(Math.min(wait, 30_000)); continue; }
        const job = this.queue.shift();
        this.lastAt = Date.now();
        this.history.push(this.lastAt);
        try { job.resolve(await job.fn()); } catch (e) { job.reject(e); }
      }
    } finally { this.running = false; }
  }
}
