import TradeOfferManager from 'steam-tradeoffer-manager';
import { errMsg, randInt, sleep, DAY, fmtUsd } from '../util/common.js';
import { esc } from '../notify/telegram.js';

const EState = TradeOfferManager.ETradeOfferState;
const AWAITING = ['bought', 'trade_sent', 'locked_on_lis'];

/**
 * Обработка обменов Steam.
 * Принимаем ТОЛЬКО офферы, где:
 *   - мы ничего не отдаём (itemsToGive пуст),
 *   - каждый получаемый предмет соответствует купленному и ещё не полученному предмету из CRM.
 * Офферы с запросом наших предметов отклоняются. Любой исходящий оффер (мы их не создаём) =
 * признак компрометации (API-key scam) -> критическая остановка и отмена оффера.
 */
export class TradeHandler {
  constructor({ cfg, db, session, guard, notify, log }) {
    Object.assign(this, { cfg, db, session, guard, notify, log });
    this.manager = session.manager;
    this.processing = new Set();
    this.warned = new Set();
  }

  start() {
    this.manager.on('newOffer', (offer) => this.onNewOffer(offer).catch((e) => this.guard.recordError('steam-trades', e)));
    this.manager.on('unknownOfferSent', (offer) => this.onUnknownOutgoing(offer, 'unknownOfferSent'));
    this.manager.on('sentOfferChanged', (offer) => this.onUnknownOutgoing(offer, 'sentOfferChanged'));
    this.manager.on('receivedOfferChanged', (offer, old) => this.onReceivedChanged(offer, old));
    this.manager.on('pollFailure', (e) => this.guard.recordError('steam-poll', e));
    this.manager.on('pollSuccess', () => this.guard.recordSuccess('steam-poll'));
  }

  /** Сопоставить получаемые предметы с ожидаемыми покупками (мультимножество по названию). */
  matchOffer(offer) {
    const expected = this.db.itemsByStatus(...AWAITING).filter((i) => !i.received_at);
    const pool = new Map();
    for (const it of expected) {
      if (!pool.has(it.name)) pool.set(it.name, []);
      pool.get(it.name).push(it);
    }
    const matched = [];
    for (const item of offer.itemsToReceive) {
      const list = pool.get(item.market_hash_name);
      if (!list?.length) return null;
      // если LIS сообщил id оффера — предпочитаем точное совпадение
      const idx = Math.max(0, list.findIndex((x) => x.trade_offer_id === String(offer.id)));
      matched.push({ crm: list.splice(idx, 1)[0], econ: item });
    }
    return matched;
  }

  async onNewOffer(offer) {
    if (this.processing.has(offer.id)) return;
    this.processing.add(offer.id);
    const partner = offer.partner.getSteamID64();
    this.db.touchCounterparty(partner);
    this.db.event('trade.incoming', { offerId: offer.id, partner, give: offer.itemsToGive.length, receive: offer.itemsToReceive.length });

    if (offer.isGlitched()) { this.log.warn(`Оффер ${offer.id} «глючный» — пропуск`); return; }

    if (offer.itemsToGive.length > 0) {
      this.db.touchCounterparty(partner, 'offers_declined');
      this.guard.warn('OFFER_WANTS_ITEMS', `Входящий оффер ${offer.id} от ${partner} просит отдать ${offer.itemsToGive.length} предм. — отклонён. Если вы его не ждали — возможна попытка мошенничества.`, null, { dedupe: false });
      await this._decline(offer);
      return;
    }

    const matched = this.matchOffer(offer);
    if (!matched) {
      // не помечаем как обработанный: если покупка «найдётся» позже (сетевая ошибка), аудит примет оффер
      this.processing.delete(offer.id);
      if (this.warned.has(offer.id)) return;
      this.warned.add(offer.id);
      this.guard.warn('OFFER_UNKNOWN', `Неожиданный входящий оффер ${offer.id} от ${partner} (${offer.itemsToReceive.map((i) => i.market_hash_name).join(', ')}) — не совпадает с покупками, оставлен без ответа.`);
      return;
    }
    if (!this.guard.canAcceptTrades) { this.log.warn(`Оффер ${offer.id} ожидает: бот остановлен`); this.processing.delete(offer.id); return; }

    await sleep(randInt(4000, 15000)); // не мгновенно — как живой человек
    try {
      const status = await new Promise((res, rej) => offer.accept(true, (err, st) => (err ? rej(err) : res(st))));
      this.log.info(`Оффер ${offer.id} принят (${status}), предметов: ${matched.length}`);
      this.db.touchCounterparty(partner, 'offers_accepted');
      this.db.touchCounterparty(partner, 'items_received', matched.length);
      const protUntil = Date.now() + this.cfg.steam.tradeProtectionDays * DAY + this.cfg.steam.tradeProtectionBufferMin * 60_000;
      for (const m of matched) {
        this.db.setItemStatus(m.crm.id, 'protected', { trade_offer_id: String(offer.id), partner_steamid: partner, received_at: Date.now(), tradable_after: protUntil }, { offerId: offer.id });
      }
      this.guard.recordSuccess('steam-accept');
      if (status === 'pending') this.guard.warn('ACCEPT_NEEDS_CONF', `Оффер ${offer.id} требует подтверждения — проверьте мобильный аутентификатор`);
      this.notify.send('info', `📥 Принят трейд от LIS: ${matched.map((m) => esc(m.crm.name)).join(', ')}\nTrade Protection до ${new Date(protUntil).toLocaleString('ru-RU', { timeZone: this.cfg.timezone })}`);
      await this.assignAssetIds(offer, matched);
    } catch (e) {
      this.processing.delete(offer.id);
      this.guard.recordError('steam-accept', e);
      if (/escrow|hold/i.test(errMsg(e))) this.guard.critical('TRADE_HOLD', `Steam сообщает о холде обменов: ${errMsg(e)}. Проверьте мобильный аутентификатор и ограничения аккаунта.`);
    }
  }

  /** Новые assetid после обмена (с повторами — Steam отдаёт их не сразу). */
  async assignAssetIds(offer, matched) {
    for (let attempt = 0; attempt < 5; attempt++) {
      await sleep(15_000 * (attempt + 1));
      try {
        const items = await new Promise((res, rej) => offer.getExchangeDetails(false, (err, status, tradeInitTime, received) => (err ? rej(err) : res(received || []))));
        if (!items.length) continue;
        const used = new Set();
        for (const m of matched) {
          const got = items.find((i) => i.market_hash_name === m.crm.name && !used.has(i.new_assetid || i.assetid));
          if (!got) continue;
          const aid = String(got.new_assetid || got.assetid);
          used.add(aid);
          this.db.updateItem(m.crm.id, { asset_id: aid });
        }
        return;
      } catch (e) {
        this.log.debug?.(`getExchangeDetails: ${errMsg(e)}`);
      }
    }
    this.log.warn(`Оффер ${offer.id}: assetid не получены — их найдёт синхронизация инвентаря`);
  }

  onReceivedChanged(offer, oldState) {
    this.db.event('trade.changed', { offerId: offer.id, from: EState[oldState], to: EState[offer.state] });
    if ([EState.Canceled, EState.InvalidItems, EState.Declined].includes(offer.state)) {
      const items = this.db.itemsByStatus(...AWAITING).filter((i) => i.trade_offer_id === String(offer.id));
      if (items.length) this.guard.warn('LIS_OFFER_CANCELED', `Оффер LIS ${offer.id} отменён (${EState[offer.state]}). LIS обычно отправляет повторно или возвращает деньги.`);
    }
  }

  async onUnknownOutgoing(offer, source) {
    if (offer.state !== EState.Active && offer.state !== EState.CreatedNeedsConfirmation) return;
    this.guard.critical('UNKNOWN_OUTGOING_TRADE', `Обнаружен исходящий обмен ${offer.id}, который бот не создавал (${source}): отдаём ${offer.itemsToGive.length} предм. пользователю ${offer.partner.getSteamID64()}. Возможна компрометация аккаунта/API-ключа! Пробую отменить. СРОЧНО смените пароль и отзовите Web API ключ.`);
    try { await new Promise((res, rej) => offer.cancel((err) => (err ? rej(err) : res()))); this.notify.send('warn', `Подозрительный исходящий оффер ${offer.id} отменён`); } catch (e) { this.log.error(`Не удалось отменить оффер ${offer.id}: ${errMsg(e)}`); }
  }

  /**
   * Периодическая проверка (раз в 10 мин):
   *  - нет ли активных исходящих офферов (на случай пропущенного события);
   *  - нет ли входящих офферов LIS, которые не приняли (бот был остановлен/перезапущен).
   */
  async auditOutgoing() {
    const [sent, received] = await new Promise((res, rej) => this.manager.getOffers(TradeOfferManager.EOfferFilter.ActiveOnly, (err, s, r) => (err ? rej(err) : res([s || [], r || []]))));
    for (const o of sent) await this.onUnknownOutgoing(o, 'audit');
    if (!this.guard.canAcceptTrades) return;
    for (const o of received) if (o.state === EState.Active && !this.processing.has(o.id)) await this.onNewOffer(o);
  }

  async _decline(offer) {
    try { await new Promise((res, rej) => offer.decline((err) => (err ? rej(err) : res()))); } catch (e) { this.log.warn(`decline ${offer.id}: ${errMsg(e)}`); }
  }
}

export { fmtUsd };
