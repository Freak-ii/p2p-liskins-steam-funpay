import { errMsg, HOUR, fmtUsd } from '../util/common.js';
import { esc } from '../notify/telegram.js';

/** Сопоставление статусов LIS с нашими. Терпимо к разным формулировкам. */
export function mapLisStatus(s) {
  const v = String(s || '').toLowerCase();
  if (!v) return 'pending';
  if (/return|refund|cancel|fail|error|unavailable|declin|expired|reject/.test(v)) return 'failed';
  if (/accept|success|complete|deliver|withdrawn|finish|done/.test(v)) return 'delivered';
  if (/lock|hold/.test(v)) return 'locked';
  if (/sent|trade|offer|wait/.test(v)) return 'trade_sent';
  return 'pending';
}

/**
 * Следит за покупками на LIS: статусы, отправка трейда, возвраты, вывод разблокированных предметов.
 */
export class PurchaseTracker {
  constructor({ cfg, db, lis, guard, log, notify }) {
    Object.assign(this, { cfg, db, lis, guard, log, notify });
  }

  async poll() {
    const pending = this.db.pendingPurchases();
    if (!pending.length) return;
    for (let i = 0; i < pending.length; i += 100) {
      const chunk = pending.slice(i, i + 100);
      let info;
      try {
        info = await this.lis.purchaseInfo({ customIds: chunk.map((p) => p.custom_id) });
        this.guard.recordSuccess('lis-info');
      } catch (e) { this.guard.recordError('lis-info', e); return; }
      const byCustom = new Map((Array.isArray(info) ? info : []).map((p) => [String(p.custom_id), p]));
      for (const p of chunk) await this.apply(p, byCustom.get(p.custom_id));
    }
  }

  async apply(purchase, info) {
    const items = this.db.itemsByPurchase(purchase.custom_id);
    if (!info) {
      // покупка с сетевой ошибкой и без записи на LIS — значит не прошла
      if (!items.length && Date.now() - purchase.ts > 10 * 60_000) this.db.updatePurchase(purchase.custom_id, { status: 'failed', error: 'не найдена на LIS' });
      return;
    }
    if (!purchase.purchase_id && info.purchase_id) this.db.updatePurchase(purchase.custom_id, { purchase_id: String(info.purchase_id) });
    const skins = info.skins || info.items || [];

    // покупка «потерялась» при сетевой ошибке, но прошла — создаём записи предметов
    if (!items.length && skins.length) {
      for (const s of skins) {
        const v = this.db.valuation(s.name);
        this.db.insertItem({
          lis_item_id: String(s.id), custom_id: purchase.custom_id, purchase_id: String(info.purchase_id ?? ''), name: s.name, mode: purchase.mode,
          status: 'bought', buy_price_usd: Number(s.price), fx_at_buy: v?.fx, expected_net_usd: v?.net_usd, expected_profit_usd: v?.net_usd ? v.net_usd - Number(s.price) : null,
        });
      }
      const total = skins.reduce((a, s) => a + Number(s.price || 0), 0);
      this.db.updatePurchase(purchase.custom_id, { total_usd: total });
      // если сверка баланса уже прошла после этой покупки — сообщаем ей о «поздней» трате
      if ((this.db.get('balance.last')?.ts || 0) >= purchase.ts) this.db.set('balance.lateSpend', (this.db.get('balance.lateSpend', 0) || 0) + total);
      this.guard.warn('BUY_RECOVERED', `Покупка ${purchase.custom_id} прошла несмотря на сетевую ошибку — учтена`);
      return;
    }

    for (const it of this.db.itemsByPurchase(purchase.custom_id)) {
      const s = skins.find((x) => String(x.id) === it.lis_item_id);
      if (!s) continue;
      const st = mapLisStatus(s.status);
      if (s.status !== it.lis_status) this.db.updateItem(it.id, { lis_status: s.status, trade_offer_id: s.trade_offer_id ? String(s.trade_offer_id) : it.trade_offer_id });
      if (['protected', 'ready', 'listing', 'listed', 'delivering', 'settling', 'sold', 'failed', 'reversed'].includes(it.status)) continue;
      if (st === 'failed') {
        this.db.setItemStatus(it.id, 'failed', {}, { lisStatus: s.status });
        this.db.set('refundCredit', (this.db.get('refundCredit', 0) || 0) + it.buy_price_usd);
        this.notify.send('warn', `Покупка «${esc(it.name)}» не состоялась (${esc(s.status)}). Ожидается возврат ${fmtUsd(it.buy_price_usd)} на баланс LIS.`);
        continue;
      }
      if (st === 'locked' && it.status !== 'locked_on_lis') this.db.setItemStatus(it.id, 'locked_on_lis');
      if (st === 'trade_sent' && it.status === 'bought') this.db.setItemStatus(it.id, 'trade_sent', { trade_offer_id: s.trade_offer_id ? String(s.trade_offer_id) : null });
      // delivered — финальный статус ставит обработчик трейдов (после фактического принятия обмена)
      if (it.status === 'locked_on_lis' && it.lis_unlock_at && it.lis_unlock_at < Date.now()) await this.withdraw(purchase, it);
      const age = Date.now() - it.created_at;
      if (age > this.cfg.risk.maxPurchasePendingHours * HOUR && ['bought', 'trade_sent'].includes(it.status)) {
        this.guard.warn('PURCHASE_STUCK', `«${it.name}» висит без доставки ${Math.round(age / HOUR)} ч (статус LIS: ${s.status}). Проверьте заказ ${purchase.custom_id}.`);
      }
    }
    const all = this.db.itemsByPurchase(purchase.custom_id);
    const remaining = all.filter((x) => ['bought', 'trade_sent', 'locked_on_lis'].includes(x.status)).length;
    if (!remaining) this.db.updatePurchase(purchase.custom_id, { status: all.some((x) => x.status !== 'failed') ? 'done' : 'failed' });
  }

  async withdraw(purchase, item) {
    const key = `withdraw.${purchase.custom_id}`;
    if (Date.now() - (this.db.get(key, 0) || 0) < HOUR) return;
    this.db.set(key, Date.now());
    try {
      await this.lis.withdraw({ customId: purchase.custom_id, ...this.cfg.trade });
      this.db.setItemStatus(item.id, 'bought', {}, { withdraw: true });
      this.log.info(`Запрошен вывод разблокированного «${item.name}»`);
    } catch (e) {
      this.log.warn(`Вывод «${item.name}»: ${errMsg(e)}`);
    }
  }
}
