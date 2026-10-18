import { errMsg } from '../util/common.js';

/**
 * Один аккаунт Steam с MannBot. MannBot перед отправкой ключей покупателю записывает обмен в реестр
 * центра (получатель + assetid, затем номер обмена), см. center/steamshare.py. Защита LisBot спрашивает
 * центр о каждом исходящем обмене, который создал не он: «свой» только обмен с ровно записанными
 * предметами и получателем, где от нас ничего не просят взамен. Нет ответа центра, ошибка, любое
 * расхождение — не свой, дальше обычная тревога UNKNOWN_OUTGOING_TRADE / UNKNOWN_CONFIRMATION.
 * Включается, только если центр запустил LisBot с CENTER_STEAM_SHARED=1 (адрес и токен тоже от центра).
 */
export class SharedSteam {
  constructor({ url = '', token = '', shared = false, log, fetchImpl = globalThis.fetch } = {}) {
    Object.assign(this, { url: String(url).replace(/\/+$/, ''), token, log, fetchImpl });
    this.enabled = !!(shared && this.url && this.token);
    this.own = new Map(); // offerId -> ref заказа MannBot (уже подтверждённые центром)
  }

  static fromEnv(env, log) {
    return new SharedSteam({ url: env.CENTER_STEAM_URL, token: env.CENTER_STEAM_TOKEN, shared: env.CENTER_STEAM_SHARED === '1', log });
  }

  /** Обмен записан MannBot в центре? offer — объект steam-tradeoffer-manager. */
  async isOwn(offer) {
    if (!this.enabled || !offer?.id) return false;
    const id = String(offer.id);
    if (this.own.has(id)) return true;
    const give = (offer.itemsToGive || []).map((i) => ({ appid: Number(i.appid), assetid: String(i.assetid ?? i.id ?? '') }));
    const body = { offer_id: id, partner: offer.partner?.getSteamID64?.() || '', give, receive_count: (offer.itemsToReceive || []).length };
    if (!give.length || body.receive_count) return false;
    try {
      const r = await this.fetchImpl(`${this.url}/check`, {
        method: 'POST', body: JSON.stringify(body), signal: AbortSignal.timeout(8000),
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.token}`, 'X-Bot': 'lisbot' },
      });
      const d = r.ok ? await r.json() : null;
      if (d?.own === true && d.bot === 'mannbot') { this.own.set(id, d.ref); return true; }
      return false;
    } catch (e) {
      this.log?.warn?.(`Реестр обменов центра недоступен: ${errMsg(e)} — обмен ${id} считаю чужим`);
      return false;
    }
  }
}
