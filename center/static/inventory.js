// Блок «Склад» в аналитике: что сейчас лежит у ботов. LisBot — предметы с оценкой по рынку и флагами,
// MannBot — ключи и на сколько дней их хватит. Данные — /api/inventory. Использует money/signed/pct из analytics.js.
const INV_LEVEL = { ok: ['lv-ok', 'хватает'], low: ['lv-warn', 'скоро закончатся'], empty: ['lv-critical', 'склад пуст'], unknown: ['muted', 'нет данных'] };
let invAll = false;

async function invLoad() {
  let r;
  try { r = await api('/api/inventory'); } catch { return; }
  const l = r.lisbot, m = r.mannbot, t = r.total[AN.cur];
  const ages = l.buckets.map((b) => `<div><b>${b.n}</b><span>${esc(b.label)} · ${money(b.cost, 'USD')}</span></div>`).join('');
  const list = invAll ? l.items : l.items.slice(0, 8);
  const rows = list.map((i) => `<tr><td>${esc(i.name)}${i.flags.map((f) => ` <span class="tag" style="${f === 'в минусе' ? 'color:var(--bad)' : ''}">${esc(f)}</span>`).join('')}</td>
    <td>${esc(i.status_text)}</td><td>${i.days.toLocaleString('ru-RU')} дн</td>
    <td class="muted">${i.sell_in == null ? '—' : i.sell_in < 1 ? 'скоро' : `≈${Math.round(i.sell_in)} дн`}</td><td>${money(i.cost, 'USD')}</td>
    <td title="${esc(i.value_source)}${i.trend != null ? ` · тренд ${i.trend}%` : ''}">${money(i.value, 'USD')}${i.value_source === 'рынок' ? '' : ' <span class="muted">*</span>'}</td>
    <td>${signed(i.pnl, 'USD')} <span class="muted small">${pct(i.pnl_pct)}</span></td></tr>`).join('')
    || '<tr><td colspan="7" class="muted">Склад пуст: все купленные предметы проданы</td></tr>';
  const more = l.items.length > 8 ? `<button class="btn small" onclick="invAll=!invAll;invLoad()">${invAll ? 'Свернуть' : `Показать все (${l.items.length})`}</button>` : '';
  const [cls, txt] = INV_LEVEL[m.level] || INV_LEVEL.unknown;
  $('#invBody').innerHTML = `
    <div class="kpi">
      <div><b>${money(t.cost)}</b><span>вложено в склад</span></div>
      <div><b>${money(t.value)}</b><span>стоит по рынку</span></div>
      <div><b>${signed(t.pnl)}</b><span>бумажная прибыль LisBot</span></div>
      <div><b>${l.flagged}</b><span>требуют решения</span></div>
      <div><b>${money(AN.cur === 'RUB' && r.rate ? l.forecast.value7 * r.rate : l.forecast.value7, AN.cur === 'RUB' && r.rate ? 'RUB' : 'USD')}</b><span>ожидается продаж за 7 дн (${l.forecast.n7} шт)</span></div>
    </div>
    <div class="grid">
      <div><h3>LisBot · ${l.n} шт</h3>
        <div class="kpi mon-kpi">${ages}</div>
        <div class="tablewrap"><table><thead><tr><th>Предмет</th><th>Где</th><th>Лежит</th><th title="По обычному сроку продажи этого предмета (или всех) за 90 дней">Продастся</th><th>Куплен</th><th>По рынку</th><th>Итог</th></tr></thead><tbody>${rows}</tbody></table></div>
        <div class="row small muted" style="margin-top:6px"><span>«По рынку» — сколько бот получит после комиссии Steam по последней оценке цены; * — оценки нет, взят прогноз при покупке. «Долго лежит» — дольше ${l.old_days.toLocaleString('ru-RU')} дн.</span><span class="spacer" style="flex:1"></span>${more}</div>
      </div>
      <div><h3>MannBot · ключи</h3>
        <div class="kpi mon-kpi">
          <div><b>${m.stock ?? '—'}</b><span>на складе</span></div>
          <div><b>${m.per_day == null ? '—' : m.per_day.toLocaleString('ru-RU')}</b><span>${m.per_day ? `продаётся в день (за ${m.rate_days} дн)` : 'продаётся в день: мало истории'}</span></div>
          <div><b class="${cls}">${m.days_left == null ? '—' : m.days_left.toLocaleString('ru-RU') + ' дн'}</b><span>хватит · ${txt}</span></div>
          <div><b>${money(m.value, 'RUB')}</b><span>по себестоимости</span></div>
        </div>
        <div class="muted small">${m.restock ? `<b>Докупить ${m.restock} ключей</b>${m.restock_cost ? ` (≈${money(m.restock_cost, 'RUB')})` : ''}, чтобы хватило на ${m.target_days} дн продаж${m.bridge_coming ? ` с учётом ${m.bridge_coming} ключей в пути по мосту` : ''}. ` : ''}Если ключей останется меньше чем на ${m.warn_days} дн продаж, центр пришлёт уведомление.</div>
        ${invBridge(r.bridge)}
      </div>
    </div>`;
}

// Мост ключей LisBot → MannBot: куплено за кошелёк Steam, ждут 7 дней, в обмене, принято MannBot
function invBridge(b) {
  if (!b || (!b.enabled && !b.bought)) return '<div class="muted small" style="margin-top:8px">Мост ключей выключен: LisBot может покупать ключи за кошелёк Steam и передавать их MannBot (настройки LisBot → «Мост ключей»).</div>';
  const cur = esc(b.currency || '');
  return `<h3 style="margin-top:12px">Мост ключей${b.enabled ? '' : ' <span class="tag">выключен</span>'}</h3>
    <div class="kpi mon-kpi">
      <div><b>${b.bought}</b><span>куплено${b.avg_price != null ? ` · ≈${b.avg_price.toLocaleString('ru-RU')} ${cur} за ключ` : ''}</span></div>
      <div><b>${b.waiting}</b><span>ждут 7 дней блокировки</span></div>
      <div><b>${b.in_offers}</b><span>в обмене на MannBot</span></div>
      <div><b>${b.delivered}</b><span>принято MannBot</span></div>
    </div>
    <div class="muted small">${!b.enabled ? 'Мост выключен в настройках LisBot: ключи не покупаются и не передаются. ' : b.buying ? `Закупка включена: до ${b.per_day ?? '—'} ключей в день, не дороже ${b.max_price ?? '—'} ${cur}${b.buy_order ? ', ордер на ТП стоит' : ''}.` : 'Закупка за кошелёк выключена, мост только передаёт ключи, которые уже есть у LisBot.'}${b.spent ? ` Потрачено с кошелька ${b.spent.toLocaleString('ru-RU')} ${cur}.` : ''}${b.per_wallet_rub ? ` Ключ продаётся на FunPay за ≈${money(b.sell_net_per_key, 'RUB')} после комиссии: <b>${b.per_wallet_rub.toLocaleString('ru-RU')} ₽ с FunPay за 1 ₽ кошелька</b> (по ${esc(b.price_basis)}, продажи за 30 дн).` : ''}</div>`;
}

// Цель прибыли на месяц: прогресс и прогноз по темпу (/api/analytics/goal)
async function goalLoad() {
  let g;
  try { g = await api('/api/analytics/goal'); } catch { return; }
  const bar = g.goal ? `<div class="goalbar"><div style="width:${Math.min(100, g.progress || 0)}%"></div><span style="left:${Math.min(100, g.elapsed / g.days * 100)}%" title="сегодня"></span></div>` : '';
  const ahead = g.goal && g.forecast_pct != null ? (g.forecast_pct >= 100 ? '<span class="pos">идём с опережением</span>' : '<span class="neg">отстаём от плана</span>') : '';
  $('#goalBody').innerHTML = g.goal
    ? `<div class="row small"><b>${money(g.profit, 'RUB')}</b>&nbsp;из ${money(g.goal, 'RUB')} · ${pct(g.progress)} · прогноз на месяц ${money(g.forecast, 'RUB')} (${pct(g.forecast_pct)}) · ${ahead}${g.need_per_day ? ` · нужно ${money(g.need_per_day, 'RUB')} в день` : ''}
       <span class="spacer" style="flex:1"></span><button class="btn small" onclick="setGoal(${g.goal})">Изменить</button></div>${bar}`
    : `<div class="row small">Прибыль с начала месяца ${money(g.profit, 'RUB')}, прогноз по темпу ${money(g.forecast, 'RUB')}.<span class="spacer" style="flex:1"></span><button class="btn small" onclick="setGoal(0)">Задать цель</button></div>`;
}
async function setGoal(cur) {
  const v = prompt('Цель прибыли центра на месяц, ₽ (0 — без цели):', cur || '');
  if (v == null) return;
  const r = await api('/api/analytics/goal', { goal: Number(String(v).replace(/\s/g, '').replace(',', '.')) || 0 });
  if (r.ok === false) flash(r.message, true);
  goalLoad();
}
document.querySelector('#anPnl').closest('.card').insertAdjacentHTML('beforebegin',
  '<div class="card" style="margin-bottom:14px"><div class="row"><h3>Цель месяца</h3></div><div id="goalBody" class="muted small">Загрузка…</div></div>');
goalLoad();
setInterval(goalLoad, 60000);

document.querySelector('#analytics').insertAdjacentHTML('beforeend',
  '<div class="card" style="margin-top:14px"><div class="row"><h3>Склад сейчас</h3></div><div id="invBody" class="muted small">Загрузка…</div></div>');
document.querySelector('#anCur').addEventListener('click', () => setTimeout(invLoad, 0));
invLoad();
setInterval(invLoad, 60000);
