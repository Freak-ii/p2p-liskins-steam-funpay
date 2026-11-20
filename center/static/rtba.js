// Страница RTBA центра: мгновенные покупки LisBot, автоцена MannBot, общий бюджет и лимиты, бэктест правил.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, d = 2) => v == null || v === '' ? '—' : Number(v).toLocaleString('ru-RU', { maximumFractionDigits: d });
const ts = (t) => t ? new Date(t > 1e12 ? t : t * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
const ago = (t) => { if (!t) return '—'; const s = (Date.now() - (t > 1e12 ? t : t * 1000)) / 1000; return s < 90 ? `${Math.round(s)} с назад` : s < 5400 ? `${Math.round(s / 60)} мин назад` : `${(s / 3600).toFixed(1)} ч назад`; };
const ms = (v) => v == null ? '—' : `${Math.round(v)} мс`;
const tile = (v, t) => `<div><b>${v}</b><span>${t}</span></div>`;
const OUTCOME = { buy: ['куплено', 'pos'], dry: ['купил бы (тест)', 'pos'], skip: ['отказ', 'muted'], fail: ['ошибка', 'neg'], gone: ['лот ушёл', 'muted'] };
const LIMIT_RU = { item: 'доля капитала на предмет', hour: 'лимит в час', day: 'лимит в день', exposure: 'сумма в товаре', balance: 'баланс с резервом' };
const ruReason = (t) => String(t).replace(/ограничение: (\w+)/, (m, k) => `ограничение: ${LIMIT_RU[k] || k}`);
const ST = { bot: 'mannbot', budget: null, status: null, bt: {} };

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (r.status === 401) { location.href = '/login'; throw new Error('auth'); }
  return r.json();
}
function flash(msg, bad) { $('#flash').innerHTML = msg ? `<div class="banner" style="${bad ? 'color:var(--bad)' : ''}">${esc(msg)}</div>` : ''; }
const srcBadge = (d, name) => d.live ? '<span class="badge s-running">в работе</span>'
  : `<span class="badge s-stopped" title="${esc(name)} не запущен — показана история из его базы">из базы</span>`;
const mode = (on, dry) => `RTBA <b>${on ? 'включён' : 'выключен'}</b> · ${dry ? 'тестовый режим (ничего не покупает и не продаёт)' : '<b style="color:var(--bad)">боевой режим</b>'}`;

// ---------------------------------------------------------------- LisBot
function renderLis(d) {
  const ws = d.ws, b = d.buyer, lat = d.latency || {};
  const rt = (d.byMode || []).filter((m) => m.mode === 'rtba');
  const rtBuys = rt.reduce((s, m) => s + m.n, 0), rtSpent = rt.reduce((s, m) => s + m.spent, 0);
  const skips = Object.entries(b?.skips || {}).sort((x, y) => y[1] - x[1]).slice(0, 5);
  const recent = (d.recent || []).map((r) => {
    const [t, cls] = OUTCOME[r.outcome] || [r.outcome, ''];
    return `<tr><td>${ts(r.ts)}</td><td title="${esc(r.name)}">${esc(r.name)}</td><td>${num(r.price)} $</td><td class="${cls}">${t}</td><td class="small">${esc(ruReason(r.reason))}</td><td>${ms(r.latencyMs)}</td></tr>`;
  }).join('');
  const buys = (d.buys || []).map((r) => `<tr><td>${ts(r.ts)}</td><td>${esc(r.name)}</td><td>${num(r.price)} $</td><td>${num(r.profit)} $</td><td>${ms(r.latencyMs)}</td><td>${r.dry ? 'тест' : ''}</td></tr>`).join('');
  $('#lis').innerHTML = `<div class="row"><h3><span class="swatch" style="background:var(--s-lisbot)"></span>LisBot · мгновенные покупки</h3><span class="spacer" style="flex:1"></span>${srcBadge(d, 'LisBot')}</div>
    <div class="small" style="margin-top:4px">${mode(d.enabled, d.dryRun)}</div>
    <div class="stat">
      ${tile(ws ? (ws.connected ? '<span class="pos">онлайн</span>' : '<span class="neg">нет связи</span>') : '—', 'WebSocket LIS')}
      ${tile(ws ? ago(ws.lastMessageAt) : '—', 'последнее сообщение')}
      ${tile(ws ? num(ws.listings, 0) : '—', 'лотов из потока')}
      ${tile(ws ? num(ws.reconnects, 0) : '—', 'переподключений')}
      ${tile(b ? num(b.rtbaSeen, 0) : '—', 'лотов вотчлиста с запуска')}
      ${tile(b ? `${num(b.rtbaBuys, 0)} / ${num(b.scanBuys, 0)}` : '—', 'покупок RTBA / скан')}
      ${tile(`${num(rtBuys, 0)} · ${num(rtSpent)} $`, `RTBA за ${d.days} дн`)}
      ${tile(`${ms(lat.p50)} / ${ms(lat.p90)}`, `задержка p50 / p90 (${lat.n || 0})`)}
    </div>
    ${skips.length ? `<div class="small muted">Частые отказы с запуска: ${skips.map(([k, n]) => `${esc(ruReason(k))} — ${n}`).join(' · ')}</div>` : ''}
    <h3 style="margin-top:12px;font-size:13px">Последние решения RTBA</h3>
    <div class="scroll"><table><thead><tr><th>Время</th><th>Предмет</th><th>Цена</th><th>Решение</th><th>Причина</th><th>Задержка</th></tr></thead>
      <tbody>${recent || `<tr><td colspan="6" class="muted">${d.live ? 'С запуска бота новых лотов из вотчлиста не было' : 'Решения видны, пока LisBot запущен'}</td></tr>`}</tbody></table></div>
    <h3 style="margin-top:12px;font-size:13px">Покупки RTBA за ${d.days} дн</h3>
    <div class="scroll" style="max-height:220px"><table><thead><tr><th>Время</th><th>Предмет</th><th>Цена</th><th>Ожид. прибыль</th><th>Задержка</th><th></th></tr></thead>
      <tbody>${buys || '<tr><td colspan="6" class="muted">Покупок по WebSocket пока не было</td></tr>'}</tbody></table></div>
    <div class="muted small" style="margin-top:6px">Задержка — от получения лота из WebSocket до ответа LIS на покупку.</div>`;
}

// ---------------------------------------------------------------- MannBot
function priceChart(hist, markup) {
  const pts = [...hist].reverse().filter((h) => h.new_price != null);
  if (pts.length < 2) return '<div class="muted small">Для графика нужно хотя бы две проверки рынка.</div>';
  const W = 640, H = 170, L = 46, B = 18, T = 6;
  const k = markup || 1;
  const vals = pts.flatMap((p) => [p.new_price * k, p.best_competitor]).filter((v) => v != null);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi === lo) { hi += 1; lo -= 1; }
  const x = (i) => L + i / (pts.length - 1) * (W - L - 4), y = (v) => T + (hi - v) / (hi - lo) * (H - B - T);
  const line = (f, color, dash = '') => `<polyline fill="none" stroke="${color}" stroke-width="2" ${dash} points="${pts.map((p, i) => f(p) == null ? '' : `${x(i).toFixed(1)},${y(f(p)).toFixed(1)}`).join(' ')}"/>`;
  const ticks = [lo, (lo + hi) / 2, hi].map((v) => `<line x1="${L}" x2="${W}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${L - 5}" y="${y(v) + 4}" text-anchor="end">${num(v, 0)}</text>`).join('');
  const lab = [0, pts.length - 1].map((i) => `<text x="${x(i)}" y="${H - 4}" text-anchor="${i ? 'end' : 'start'}">${ts(pts[i].ts).slice(0, 11)}</text>`).join('');
  return `<div class="legend"><span><span class="swatch" style="background:var(--s-mannbot)"></span>наша цена${markup && markup !== 1 ? ' (для покупателя)' : ''}</span><span><span class="swatch" style="background:var(--muted)"></span>лидер рынка</span></div>
    <div class="chart"><svg viewBox="0 0 ${W} ${H}" style="height:170px" role="img" aria-label="Цена лота и лидер рынка">${ticks}${lab}
      ${line((p) => p.best_competitor, 'var(--muted)', 'stroke-dasharray="4 3"')}${line((p) => p.new_price * k, 'var(--s-mannbot)')}</svg></div>`;
}

function renderMann(d) {
  const c = d.config || {};
  const hist = d.history || [];
  const rows = hist.slice(0, 60).map((h) => `<tr style="${h.applied ? '' : 'opacity:.6'}"><td>${ts(h.ts)}</td><td>${num(h.old_price)} → <b>${num(h.new_price)}</b></td>
    <td>${num(h.best_competitor)}</td><td>${h.position ?? '—'}</td><td>${h.applied ? 'да' : 'нет'}</td><td class="small">${esc(h.reason)}</td></tr>`).join('');
  const comps = (d.competitors || []).slice(0, 12).map((l) => `<tr><td>${esc(l.seller)}</td><td>${num(l.price)}</td><td>${num(l.amount, 0)}</td><td>${l.online === false ? 'нет' : 'да'}</td></tr>`).join('');
  $('#mann').innerHTML = `<div class="row"><h3><span class="swatch" style="background:var(--s-mannbot)"></span>MannBot · автоцена лота FunPay</h3><span class="spacer" style="flex:1"></span>${srcBadge(d, 'MannBot')}</div>
    <div class="small" style="margin-top:4px">${mode(c.enabled, d.dry_run)}</div>
    <div class="stat">
      ${tile(esc({ top1: 'дешевле лидера', topN: `позиция ${c.target_position || 1}`, median: 'медиана рынка' }[c.strategy] || c.strategy || '—'), 'стратегия')}
      ${tile(`${num(d.floor)} ₽`, 'пол цены (себест. + маржа)')}
      ${tile(`${num(c.min_margin_pct, 1)}%`, 'мин. маржа')}
      ${tile(d.markup ? `×${num(d.markup, 3)}` : '—', 'наценка FunPay')}
      ${tile(`${num(d.day?.applied, 0)} / ${num(d.day?.checks, 0)}`, 'изменений / проверок за 24 ч')}
      ${tile(d.updates_last_hour == null ? '—' : `${d.updates_last_hour} / ${num(c.max_updates_per_hour, 0)}`, 'изменений за час / лимит')}
      ${tile(d.stock == null ? '—' : num(d.stock, 0), 'ключей на складе')}
    </div>
    ${d.restart_required ? '<div class="detail">MannBot ждёт перезапуска, чтобы применить часть настроек.</div>' : ''}
    ${priceChart(hist, d.markup)}
    <h3 style="margin-top:12px;font-size:13px">Решения автоцены</h3>
    <div class="scroll"><table><thead><tr><th>Время</th><th>Цена лота</th><th>Лидер</th><th>Поз.</th><th>Применено</th><th>Причина</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="muted">Автоцена ещё не работала: включите RTBA в настройках MannBot</td></tr>'}</tbody></table></div>
    <h3 style="margin-top:12px;font-size:13px">Рынок FunPay ${d.competitors_ts ? `на ${ts(d.competitors_ts)}` : ''}</h3>
    <div class="scroll" style="max-height:220px"><table><thead><tr><th>Продавец</th><th>Цена</th><th>Ключей</th><th>Онлайн</th></tr></thead>
      <tbody>${comps || '<tr><td colspan="4" class="muted">Снимков рынка пока нет</td></tr>'}</tbody></table></div>`;
}

// ---------------------------------------------------------------- бюджет и лимиты
const showVal = (r, v) => v == null || v === '' ? '—' : r.unit === 'доля' ? `${num(v * 100, 1)}%` : typeof v === 'number' ? `${num(v)} ${esc(r.unit)}` : esc(v);
function planInput(r) {
  const own = r.source === 'план центра' ? r.planned : '';
  if (r.key === 'mannbot.strategy') {
    return `<select data-k="${r.key}" class="btn"><option value="">как сейчас</option>${['top1', 'topN', 'median'].map((s) => `<option ${own === s ? 'selected' : ''}>${s}</option>`).join('')}</select>`;
  }
  const ph = r.planned != null && r.source !== 'план центра' ? r.planned : 'как сейчас';
  return `<input data-k="${r.key}" inputmode="decimal" value="${own ?? ''}" placeholder="${esc(ph)}" style="max-width:150px">`;
}
function renderBudget(p) {
  ST.budget = p;
  const diff = p.rows.filter((r) => r.differs);
  $('#budget').innerHTML = `<div class="row">
      <label style="margin:0">Общий капитал центра, $</label>
      <input id="bTotal" inputmode="decimal" value="${p.total_usd || ''}" placeholder="не задан" style="max-width:140px">
      <span class="muted small">LisBot получает его за вычетом ключей на складе MannBot.</span></div>
    <div class="stat">${tile(`${num(p.capital.lisbot_usd)} $`, 'LisBot: баланс + товар')}${tile(`${num(p.capital.mannbot_usd)} $`, 'MannBot: ключи по себестоимости')}
      ${tile(`${num(p.capital.total_usd)} $`, 'весь центр сейчас')}${tile(num(p.rate), 'курс $ → ₽')}</div>
    <div style="overflow-x:auto"><table><thead><tr><th>Лимит</th><th>Сейчас у бота</th><th>В плане центра</th><th>Итог плана</th><th></th></tr></thead><tbody>
    ${p.rows.map((r) => `<tr><td>${esc(r.label)}${r.unit === 'доля' ? ' <span class="muted small">(0.1 = 10%)</span>' : ''}</td><td>${showVal(r, r.current)}</td><td>${planInput(r)}</td>
      <td>${r.planned == null ? '<span class="muted">не меняется</span>' : showVal(r, r.planned)}${r.source && r.source !== 'план центра' ? `<div class="muted small">${esc(r.source)}</div>` : ''}</td>
      <td>${r.differs ? '<span class="badge s-starting">отличается</span>' : ''}</td></tr>`).join('')}
    </tbody></table></div>
    <div class="row" style="margin-top:10px">
      <button class="btn" id="bSave">Сохранить план</button>
      <button class="btn primary" id="bApply" ${diff.length ? '' : 'disabled'}>Применить к ботам${diff.length ? ` (${diff.length})` : ''}</button>
      <span class="muted small">План хранится в center.yaml и ничего не меняет у ботов, пока вы не нажмёте «Применить». Работает: LisBot ${p.running.lisbot ? 'да' : 'нет'}, MannBot ${p.running.mannbot ? 'да' : 'нет'}.</span>
    </div>
    ${p.last_apply?.rows?.length ? `<div class="row small" style="margin-top:8px"><span class="muted">Применено ${ts(p.last_apply.ts)}: ${p.last_apply.rows.map((r) => `${esc(r.label)} ${showVal(r, r.previous)} → ${showVal(r, r.applied)}`).join('; ')}.</span>
      <button class="btn small" id="bUndo">Вернуть как было</button></div>` : ''}`;
  $('#bSave').onclick = savePlan;
  if ($('#bUndo')) {
    $('#bUndo').onclick = async () => {
      if (!confirm('Вернуть ботам значения, которые были до последнего «Применить»? План центра не изменится.')) return;
      const r = await api('/api/rtba/undo', {});
      flash(r.message, !r.ok);
      loadBudget(); setTimeout(loadStatus, 1000);
    };
  }
  $('#bApply').onclick = () => {
    $('#applyList').innerHTML = `<table><tbody>${ST.budget.rows.filter((r) => r.differs).map((r) => `<tr><td>${esc(r.label)}</td><td>${showVal(r, r.current)} → <b>${showVal(r, r.planned)}</b></td></tr>`).join('')}</tbody></table>`;
    $('#applyDlg').showModal();
  };
}
async function savePlan() {
  const plan = {};
  for (const el of document.querySelectorAll('#budget [data-k]')) plan[el.dataset.k] = el.value.trim() === '' ? null : el.value.trim().replace(',', '.');
  const r = await api('/api/rtba/budget', { total_usd: $('#bTotal').value.replace(',', '.') || 0, plan });
  if (r.ok === false) return flash(r.message, true);
  renderBudget(r);
  flash('План сохранён. Боты получат его после «Применить к ботам».');
}
$('#applyDlg').addEventListener('close', async () => {
  if ($('#applyDlg').returnValue !== 'apply') return;
  flash('Применяю…');
  const r = await api('/api/rtba/apply', {});
  flash(r.message, !r.ok);
  loadBudget(); setTimeout(loadStatus, 1000);
});

// ---------------------------------------------------------------- бэктест
const BT = {
  lisbot: { days: 7, hint: 'История пола LIS за 7 дней (столько хранит LisBot), правила planBuys самого бота.',
    fields: [['strategy.minRoi', 'Мин. ROI (0.1 = 10%)'], ['strategy.bottomTolerancePct', 'Коридор от минимума (0.02)'], ['strategy.maxSpendPerDayUsd', 'Лимит трат в день, $']] },
  mannbot: { days: 30, hint: 'Снимки рынка FunPay, правила RTBAEngine MannBot. Выручка — оценка при том же спросе.',
    fields: [['strategy', 'Стратегия'], ['min_margin_pct', 'Мин. маржа, %'], ['undercut_abs', 'Дешевле конкурента на, ₽'], ['target_position', 'Позиция (topN)']] },
};
function btForm() {
  const conf = BT[ST.bot];
  $('#btBot').innerHTML = [['mannbot', 'MannBot · автоцена'], ['lisbot', 'LisBot · закупка']].map(([k, t]) => `<button class="btn ${k === ST.bot ? 'on' : ''}" data-k="${k}">${t}</button>`).join('');
  $('#btBot').onclick = (e) => { const k = e.target.dataset?.k; if (k) { ST.bot = k; btForm(); $('#btOut').innerHTML = ''; } };
  $('#btHint').textContent = conf.hint;
  const inp = (v, [f]) => f === 'strategy'
    ? `<select data-f="${f}" class="btn"><option value="">как сейчас</option><option>top1</option><option>topN</option><option>median</option></select>`
    : `<input data-f="${f}" inputmode="decimal" placeholder="как сейчас" style="max-width:130px">`;
  $('#btForm').innerHTML = `<div style="overflow-x:auto"><table><thead><tr><th>Вариант</th>${conf.fields.map(([, t]) => `<th>${t}</th>`).join('')}</tr></thead><tbody>
    <tr><td class="muted">Текущие правила</td>${conf.fields.map(() => '<td class="muted">—</td>').join('')}</tr>
    ${[1, 2, 3].map((v) => `<tr data-v="${v}"><td>Вариант ${v}</td>${conf.fields.map((f) => `<td>${inp(v, f)}</td>`).join('')}</tr>`).join('')}
    </tbody></table></div>
    <div class="row" style="margin-top:8px"><label style="margin:0">Период, дней</label><input id="btDays" inputmode="numeric" value="${conf.days}" style="max-width:80px">
      <button class="btn primary" id="btRun">Запустить бэктест</button></div>`;
  $('#btRun').onclick = runBacktest;
}
function btVariants() {
  const out = [{ label: 'текущие правила', changes: {} }];
  for (const tr of document.querySelectorAll('#btForm tr[data-v]')) {
    const changes = {};
    for (const el of tr.querySelectorAll('[data-f]')) {
      const v = el.value.trim().replace(',', '.');
      if (v !== '') changes[el.dataset.f] = el.dataset.f === 'strategy' ? v : Number(v);
    }
    if (Object.keys(changes).length) out.push({ label: `Вариант ${tr.dataset.v}`, changes });
  }
  return out;
}
async function runBacktest() {
  $('#btOut').innerHTML = '<div class="muted">Считаю…</div>';
  const r = await api('/api/rtba/backtest', { bot: ST.bot, variants: btVariants(), days: Number($('#btDays').value) || BT[ST.bot].days });
  if (r.error) { $('#btOut').innerHTML = `<div class="detail">${esc(r.error)}</div>`; return; }
  ST.bt = r;
  const chg = (c) => Object.entries(c).map(([k, v]) => `${esc(k.split('.').pop())}=${esc(v)}`).join(', ') || '—';
  const toPlan = (i) => Object.keys(r.variants[i].changes).length ? `<button class="btn" data-plan="${i}">В план</button>` : '';
  let html;
  if (ST.bot === 'lisbot') {
    html = `<div class="muted small">${num(r.samples, 0)} замеров пола по ${r.items} предметам, ${ts(r.from)} — ${ts(r.to)}. Прибыль — по текущей оценке Steam; в стакане учтён только самый дешёвый лот.</div>
      <div style="overflow-x:auto"><table class="pnl"><thead><tr><th>Вариант</th><th>Изменения</th><th>Покупок</th><th>Потрачено</th><th>Ожид. прибыль</th><th>ROI</th><th>Макс. в товаре</th><th>Частые отказы</th><th></th></tr></thead><tbody>
      ${r.variants.map((v, i) => `<tr><td>${esc(v.label)}</td><td class="small">${chg(v.changes)}</td><td>${v.buys}</td><td>${num(v.spent)} $</td>
        <td class="${v.expectedProfit > 0 ? 'pos' : ''}">${num(v.expectedProfit)} $</td><td>${v.roi == null ? '—' : num(v.roi, 1) + '%'}</td><td>${num(v.maxExposure)} $</td>
        <td class="small" style="text-align:left;white-space:normal">${Object.entries(v.skips).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([k, n]) => `${esc(ruReason(k))} (${n})`).join('; ') || '—'}</td><td>${toPlan(i)}</td></tr>`).join('')}
      </tbody></table></div>`;
  } else {
    const a = r.actual;
    html = `<div class="muted small">${num(r.snapshots, 0)} снимков рынка и ${r.orders} заказов, ${ts(r.from)} — ${ts(r.to)}. Фактически: выручка ${num(a.revenue)} ₽, изменений цены ${a.updates}, средняя позиция ${num(a.avg_position)}.</div>
      <div style="overflow-x:auto"><table class="pnl"><thead><tr><th>Вариант</th><th>Изменения</th><th>Смен цены</th><th>Ср. цена</th><th>Ср. позиция</th><th>Первые</th><th>На полу</th>
        <th title="Цена варианта в момент каждого заказа × ключи">Оценка выручки</th><th>Оценка прибыли</th><th>Маржа / ключ</th><th>Заказы не первыми</th><th></th></tr></thead><tbody>
      ${r.variants.map((v, i) => `<tr><td>${esc(v.label)}</td><td class="small">${chg(v.changes)}</td><td>${v.updates}</td><td>${num(v.avg_price)}</td><td>${num(v.avg_position)}</td>
        <td>${num(v.share_first, 1)}%</td><td>${v.floor_hits}</td><td>${num(v.est_revenue)} ₽</td><td>${v.est_profit == null ? '—' : num(v.est_profit) + ' ₽'}</td>
        <td>${v.margin_per_key == null ? '—' : num(v.margin_per_key) + ' ₽'}</td><td>${v.orders_not_first}</td><td>${toPlan(i)}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="muted small" style="margin-top:6px">«Заказы не первыми» — сколько продаж пришлось на моменты, когда вариант стоял бы не первым: там спрос мог уйти к конкуренту.</div>`;
  }
  $('#btOut').innerHTML = html;
  $('#btOut').onclick = (e) => { const i = e.target.dataset?.plan; if (i != null) variantToPlan(r.variants[i]); };
}
async function variantToPlan(v) {
  const keyOf = Object.fromEntries((ST.budget?.rows || []).filter((r) => r.bot === ST.bot).map((r) => [r.setting, r.key]));
  const plan = {}, skipped = [];
  for (const [k, val] of Object.entries(v.changes)) (keyOf[k] ? (plan[keyOf[k]] = val) : skipped.push(k));
  if (!Object.keys(plan).length) return flash(`Эти параметры (${skipped.join(', ')}) центр в план не берёт — их можно поменять в панели бота.`, true);
  const r = await api('/api/rtba/budget', { plan });
  if (r.ok === false) return flash(r.message, true);
  renderBudget(r);
  flash(`«${v.label}» добавлен в план${skipped.length ? ` (кроме ${skipped.join(', ')})` : ''}. Нажмите «Применить к ботам», чтобы отправить.`);
  $('#budget').scrollIntoView({ behavior: 'smooth' });
}

// ---------------------------------------------------------------- загрузка
async function loadStatus() {
  try { ST.status = await api('/api/rtba?days=7'); } catch { return; }
  renderLis(ST.status.lisbot); renderMann(ST.status.mannbot);
  $('#hdr').textContent = `обновлено ${new Date().toLocaleTimeString('ru-RU')}`;
}
async function loadBudget() {
  const active = document.activeElement;
  if (active && $('#budget').contains(active) && active.tagName !== 'BUTTON') return;   // не сбивать ввод
  try { renderBudget(await api('/api/rtba/budget')); } catch { /* центр перезапускается */ }
}
btForm(); loadStatus(); loadBudget();
setInterval(loadStatus, 5000); setInterval(loadBudget, 30000);
