import fs from 'node:fs';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import SteamUser from 'steam-user';
import SteamCommunity from 'steamcommunity';
import TradeOfferManager from 'steam-tradeoffer-manager';
import SteamTotp from 'steam-totp';
import { RequestScheduler, RateLimitedError } from '../util/rateLimiter.js';
import { sleep, dayKey, errMsg } from '../util/common.js';
import { installSteamDnsFallback, isNetworkError } from './dns.js';

/**
 * Одна долгоживущая сессия Steam:
 *  - вход по refresh-токену (сохраняется на диск), пароль используется только при первом входе
 *    или протухшем токене — частые логины Steam считает подозрительными;
 *  - суточный лимит логинов, экспоненциальная пауза при RateLimitExceeded;
 *  - все web-запросы идут через две очереди (общая и для цен) с паузами и разбросом,
 *    при 429 — глобальная пауза, при серии 429 — критическая остановка.
 */
export class SteamSession extends EventEmitter {
  constructor({ cfg, secrets, db, log }) {
    super();
    Object.assign(this, { cfg, secrets, db, log });
    this.tokenFile = path.join(cfg.dataDir, 'steam-refresh-token.txt');
    this.user = new SteamUser({ autoRelogin: true, renewRefreshTokens: true, dataDirectory: path.join(cfg.dataDir, 'steam-user') });
    this.community = new SteamCommunity();
    this.manager = new TradeOfferManager({
      steam: this.user, community: this.community, language: 'en',
      pollInterval: Math.max(30, cfg.steam.tradePollIntervalSec) * 1000,
      cancelTime: undefined, // мы не создаём исходящих офферов
      dataDirectory: path.join(cfg.dataDir, 'tradeoffers'), savePollData: true,
    });
    installSteamDnsFallback({ onFallback: (host, ips, err) => this.log.warn(`Steam: системный DNS не нашёл ${host} (${err.code}), адрес взят у запасного DNS: ${ips[0]}`) });
    this.steamId = null;
    this.ready = false;
    this.lastWebSession = 0;
    this.webLogonPending = false;

    const st = cfg.steam;
    const onBackoff = (name) => (ms, n) => {
      this.log.warn(`Steam ${name}: 429/лимит, пауза ${Math.round(ms / 60000)} мин (подряд: ${n})`);
      this.emit('rateLimited', { name, ms, n });
      if (n >= st.maxConsecutiveRateLimits) this.emit('critical', { code: 'STEAM_RATE_LIMIT', message: `Steam ${n} раз подряд ограничил запросы (${name}). Возможна блокировка по частоте — бот остановлен.` });
    };
    this.webSched = new RequestScheduler({
      name: 'steam-web', minDelayMs: st.requestMinDelayMs, jitterMs: st.requestJitterMs, maxPerHour: st.maxRequestsPerHour,
      backoffMs: st.rateLimitBackoffMin * 60_000, maxBackoffMs: st.rateLimitMaxBackoffMin * 60_000, onBackoff: onBackoff('web'),
    });
    this.priceSched = new RequestScheduler({
      name: 'steam-price', minDelayMs: st.priceRequestMinDelayMs, jitterMs: st.requestJitterMs, maxPerHour: st.maxPriceRequestsPerHour,
      backoffMs: st.rateLimitBackoffMin * 60_000, maxBackoffMs: st.rateLimitMaxBackoffMin * 60_000, onBackoff: onBackoff('price'),
    });
    this._wire();
  }

  _wire() {
    this.user.on('loggedOn', () => {
      this.steamId = this.user.steamID.getSteamID64();
      this.log.info(`Steam: вход выполнен (${this.steamId})`);
      this.user.setPersona(SteamUser.EPersonaState.Invisible);
    });
    // баланс кошелька Steam (в валюте кошелька) — нужен мосту ключей для закупа на ТП
    this.user.on('wallet', (hasWallet, currency, balance) => { this.wallet = hasWallet ? { currency, balance: Number(balance), ts: Date.now() } : null; });
    this.user.on('refreshToken', (token) => {
      try { fs.writeFileSync(this.tokenFile, token, { mode: 0o600 }); } catch (e) { this.log.error('не удалось сохранить refresh-токен', e); }
    });
    this.user.on('webSession', (_sid, cookies) => {
      this.community.setCookies(cookies);
      this.manager.setCookies(cookies, (err) => {
        if (err) { this.log.error(`TradeOfferManager.setCookies: ${err.message}`); this.emit('error', err); return; }
        this.lastWebSession = Date.now();
        this.webLogonPending = false;
        if (!this.ready) { this.ready = true; this.lastError = ''; this.emit('ready'); }
        this.emit('webSession');
      });
    });
    this.user.on('error', (err) => {
      this.ready = false;
      this.lastError = err.message;
      if (isNetworkError(err) && err.eresult === undefined) {
        this.emit('critical', { code: 'STEAM_NETWORK', message: `Нет связи со Steam: ${err.message}. Это ошибка сети или DNS на компьютере, а не отказ Steam во входе; бот повторит вход сам.`, fatal: false, network: true });
        return;
      }
      const fatal = ['InvalidPassword', 'AccountLogonDenied', 'AccountLoginDeniedThrottle', 'RateLimitExceeded', 'LoggedInElsewhere', 'InvalidLoginAuthCode', 'TwoFactorCodeMismatch', 'Banned'];
      const name = SteamUser.EResult[err.eresult] || err.message;
      if (/AccessDenied|RefreshToken|Expired/i.test(name)) try { fs.unlinkSync(this.tokenFile); } catch { /* нет файла */ }
      this.emit('critical', { code: 'STEAM_LOGIN', message: `Ошибка входа Steam: ${name}`, fatal: fatal.includes(name) });
    });
    this.user.on('disconnected', (eresult, msg) => {
      this.log.warn(`Steam отключён: ${msg || eresult} — steam-user переподключится сам`);
      this.emit('disconnected');
    });
    this.community.on('sessionExpired', () => this.refreshWebSession('sessionExpired'));
    this.manager.on('sessionExpired', () => this.refreshWebSession('manager.sessionExpired'));
  }

  _countLogin() {
    const key = `steam.logins.${dayKey(Date.now(), this.cfg.timezone)}`;
    const n = (this.db.get(key, 0) || 0) + 1;
    this.db.set(key, n);
    return n;
  }

  /** Отменить учёт попытки входа: бот не дошёл до Steam (сеть/DNS), Steam её не видел. */
  _uncountLogin() {
    const key = `steam.logins.${dayKey(Date.now(), this.cfg.timezone)}`;
    this.db.set(key, Math.max(0, (this.db.get(key, 0) || 0) - 1));
  }

  /** Код Steam Guard из shared_secret с поправкой на расхождение часов компьютера и Steam. */
  authCode() {
    return SteamTotp.generateAuthCode(this.secrets.sharedSecret, this._timeOffset || 0);
  }

  async login() {
    const n = this._countLogin();
    if (n > this.cfg.steam.maxLoginsPerDay) {
      throw Object.assign(new Error(`Превышен суточный лимит логинов в Steam (${this.cfg.steam.maxLoginsPerDay}). Защита от блокировки.`), { code: 'LOGIN_LIMIT' });
    }
    const opts = { machineName: 'lisskins-bot' };
    if (fs.existsSync(this.tokenFile)) {
      opts.refreshToken = fs.readFileSync(this.tokenFile, 'utf8').trim();
      this.log.info('Steam: вход по сохранённому refresh-токену');
    } else {
      // часы компьютера могут отставать: код SDA действует 30 с, поэтому берём поправку у Steam
      await this.syncTime();
      if (Math.abs(this._timeOffset || 0) >= 15) this.log.warn(`Steam: часы компьютера расходятся со Steam на ${this._timeOffset} с, код входа сделан с поправкой`);
      opts.accountName = this.secrets.steamAccountName;
      opts.password = this.secrets.steamPassword;
      opts.twoFactorCode = this.authCode();
      this.log.info('Steam: вход по логину/паролю (первичный)');
    }
    // если Steam попросит код ещё раз (например, токен протух) — генерируем сами
    this.user.removeAllListeners('steamGuard');
    this.user.once('steamGuard', (_domain, cb, lastWrong) => {
      if (lastWrong) { this.emit('critical', { code: 'STEAM_GUARD', message: 'Steam Guard отклонил код. Проверьте shared_secret (из maFile SDA, без шифрования) и время на компьютере', fatal: true }); return; }
      setTimeout(() => cb(this.authCode()), 3000);
    });
    this.user.logOn(opts);
    try {
      await new Promise((resolve, reject) => {
        const done = (fn, v) => { clearTimeout(t); this.off('ready', onReady); this.off('critical', onCritical); fn(v); };
        const onReady = () => done(resolve);
        const onCritical = (c) => {
          if (c.code.startsWith('STEAM_LOGIN') || c.code === 'STEAM_GUARD' || c.network) done(reject, Object.assign(new Error(c.message), { network: !!c.network }));
        };
        const t = setTimeout(() => done(reject, new Error('Steam: таймаут входа (90с)')), 90_000);
        this.on('ready', onReady);
        this.on('critical', onCritical);
      });
    } catch (e) {
      if (e.network) this._uncountLogin();
      throw e;
    }
  }

  /** Обновить web-сессию не чаще раза в 2 минуты (Steam не любит частые webLogOn). */
  refreshWebSession(reason) {
    if (this.webLogonPending || Date.now() - this.lastWebSession < 120_000) return;
    this.webLogonPending = true;
    this.log.warn(`Steam: обновляю web-сессию (${reason})`);
    try { this.user.webLogOn(); } catch (e) { this.webLogonPending = false; this.log.error(`webLogOn: ${errMsg(e)}`); }
    setTimeout(() => { this.webLogonPending = false; }, 5 * 60_000).unref();
  }

  sessionId() { return this.community.getSessionID(); }

  /** HTTP-запрос к steamcommunity через очередь с антибот-паузами. kind: 'web' | 'price'. */
  request({ method = 'GET', url, form, json = true, kind = 'web', priority = 0, headers = {}, extra = {} }) {
    const sched = kind === 'price' ? this.priceSched : this.webSched;
    return sched.schedule(() => new Promise((resolve, reject) => {
      const opts = { method, url, json, headers: { Referer: 'https://steamcommunity.com/', ...headers }, timeout: 30_000, ...extra };
      if (form) opts.form = form;
      this.community.httpRequest(opts, (err, res, body) => {
        if (err) {
          const code = err.code || res?.statusCode;
          if (code === 429) { sched.rateLimited(); return reject(new RateLimitedError('Steam 429')); }
          if (code === 401 || /Not Logged In/i.test(err.message)) this.refreshWebSession('401');
          return reject(Object.assign(err, { status: code, body }));
        }
        sched.ok();
        resolve(body);
      }, 'lisskins-bot');
    }), { priority });
  }

  /** Инвентарь (по умолчанию CS2: appid 730, context 2). tradableOnly — только передаваемые предметы. */
  inventory(appid = 730, contextid = 2, tradableOnly = false) {
    return this.webSched.schedule(() => new Promise((resolve, reject) => {
      this.community.getUserInventoryContents(this.steamId, appid, contextid, tradableOnly, 'english', (err, items) => {
        if (err) {
          if (/429/.test(err.message)) { this.webSched.rateLimited(); return reject(new RateLimitedError('Steam inventory 429')); }
          return reject(err);
        }
        this.webSched.ok();
        resolve(items);
      });
    }), { priority: 2 });
  }

  confirmationKey(tag) {
    const time = SteamTotp.time(this._timeOffset || 0);
    return { time, key: SteamTotp.getConfirmationKey(this.secrets.identitySecret, time, tag) };
  }

  async syncTime() {
    this._timeOffset = await new Promise((res) => SteamTotp.getTimeOffset((err, off) => res(err ? (this._timeOffset || 0) : off)));
  }

  getConfirmations() {
    return this.webSched.schedule(() => new Promise((resolve, reject) => {
      const { time, key } = this.confirmationKey('list');
      this.community.getConfirmations(time, { tag: 'list', key }, (err, confs) => (err ? reject(err) : resolve(confs)));
    }), { priority: 6 });
  }

  respondConfirmation(conf, accept) {
    return this.webSched.schedule(async () => {
      await sleep(1100); // ключ подтверждения нельзя генерировать дважды в одну секунду
      const tag = accept ? 'accept' : 'reject';
      const { time, key } = this.confirmationKey(tag);
      return new Promise((resolve, reject) => this.community.respondToConfirmation(conf.id, conf.key, time, { tag, key }, accept, (err) => (err ? reject(err) : resolve())));
    }, { priority: 7 });
  }

  stop() {
    this.webSched.stop();
    this.priceSched.stop();
    try { this.manager.shutdown(); } catch { /* noop */ }
    try { this.user.logOff(); } catch { /* noop */ }
  }

  get stats() {
    return { ready: this.ready, lastError: this.lastError || '', steamId: this.steamId, web: this.webSched.stats, price: this.priceSched.stats, lastWebSession: this.lastWebSession };
  }
}
