// Вкладка «CRM» общей панели: единый список сделок обоих ботов, покупатели FunPay, карточки, заметки и теги.
// Использует $, esc, api, fmtTs, flash из index.html и tabs из analytics.js.
const CRM = { view: 'deals', q: '', bot: '', group: '', days: 'all', dry: false, sort: 'created_at', dir: 'desc', offset: 0, limit: 50,
  cq: '', risk: '', tag: '', segment: '', csort: 'last_order' };
const RISK = { low: ['низкий', 'pos'], medium: ['средний', 'warn'], high: ['высокий', 'neg'] };
const BOT_T = { lisbot: 'LisBot', mannbot: 'MannBot' };
const cm = (v, cur) => v == null ? '—' : v.toLocaleString('ru-RU', { maximumFractionDigits: 2 }) + (cur === 'USD' ? ' $' : ' ₽');
const cprofit = (v, cur) => v == null ? '—' : `<span class="${v > 0 ? 'pos' : v < 0 ? 'neg' : ''}">${v > 0 ? '+' : ''}${cm(v, cur)}</span>`;
const riskBadge = (r) => `<span class="badge ${RISK[r.level][1]}" title="${esc(r.reasons.map((x) => `${x.points > 0 ? '+' : ''}${x.points}: ${x.text}`).join('\n'))}">${r.score} · ${RISK[r.level][0]}</span>`;
const SEG_CLS = { vip: 'pos', regular: 'pos', new: '', sleeping: 'warn', lost: 'muted', risky: 'neg', prospect: 'muted' };
const segBadge = (g) => g ? `<span class="badge ${SEG_CLS[g.key] || ''}" title="${esc(g.why)}">${esc(g.text)}</span>` : '';
const grpCls = { open: '', attention: 'warn', done: 'pos', failed: 'neg' };
let crmTimer = null;

function crmQuery(extra = {}) {
  const p = new URLSearchParams({ q: CRM.q, bot: CRM.bot, group: CRM.group, days: CRM.days, sort: CRM.sort, dir: CRM.dir, ...extra });
  if (CRM.dry) p.set('dry', '1');
  return p.toString();
}

function crmControls() {
  tabs('#crmView', [['deals', 'Сделки'], ['customers', 'Покупатели FunPay'], ['cohorts', 'Удержание']], CRM.view, (k) => { CRM.view = k; crmLoad(); });
  $('#crmDeals').hidden = CRM.view !== 'deals';
  $('#crmCust').hidden = CRM.view !== 'customers';
  $('#crmCoh').hidden = CRM.view !== 'cohorts';
}

const MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
async function crmCohorts() {
  let d;
  try { d = await api('/api/crm/cohorts?months=6'); } catch { return; }
  const n = d.months - 1;
  const cell = (v) => v == null ? '<td class="muted">·</td>' : `<td style="background:color-mix(in srgb, var(--ok) ${Math.round(Math.min(100, v) * 0.6)}%, transparent)">${v.toLocaleString('ru-RU')}%</td>`;
  $('#crmCohTable').innerHTML = `<thead><tr><th>Когорта</th><th>Новых</th>${Array.from({ length: n }, (_, i) => `<th>Через ${i + 1} мес</th>`).join('')}
    <th>Вернулись</th><th>Принёс</th></tr></thead><tbody>
    ${d.rows.slice().reverse().map((r) => { const [y, m] = r.month.split('-'); return `<tr><td>${MONTHS[+m - 1]} ${y}</td><td>${r.new}</td>
      ${Array.from({ length: n }, (_, i) => i < r.retention.length && r.new ? cell(r.retention[i]) : '<td></td>').join('')}
      <td>${r.repeat == null ? '—' : r.repeat.toLocaleString('ru-RU') + '%'}</td><td>${cm(r.ltv, 'RUB')}</td></tr>`; }).join('')}</tbody>`;
}

async function crmDeals() {
  let d;
  try { d = await api(`/api/crm/deals?${crmQuery({ limit: CRM.limit, offset: CRM.offset })}`); } catch { return; }
  tabs('#crmGroups', [['', `Все ${Object.values(d.groups).reduce((a, b) => a + b, 0)}`],
    ...Object.entries(d.group_names).map(([k, t]) => [k, `${t} ${d.groups[k]}`])], CRM.group, (k) => { CRM.group = k; CRM.offset = 0; crmDeals(); });
  $('#crmCsv').href = `/api/crm/deals.csv?${crmQuery()}`;
  $('#crmTotals').innerHTML = Object.entries(d.totals).map(([c, t]) => `${c === 'USD' ? 'В $' : 'В ₽'}: ${t.deals} сделок, сумма ${cm(t.amount, c)}, выручка ${cm(t.revenue, c)}, прибыль ${cprofit(t.profit, c)}`).join(' · ') || 'Сделок не найдено';
  const sortTh = (k, t) => `<th class="sortable" data-sort="${k}">${t}${CRM.sort === k ? (CRM.dir === 'desc' ? ' ↓' : ' ↑') : ''}</th>`;
  $('#crmTable').innerHTML = `<thead><tr><th>Бот</th><th>ID</th><th>Предмет</th>${sortTh('created_at', 'Создана')}${sortTh('amount', 'Сумма')}
    ${sortTh('profit', 'Прибыль')}<th>Статус</th><th>Контрагент</th><th>Заметка / теги</th></tr></thead><tbody>
    ${d.rows.map((r) => `<tr class="click" data-bot="${r.bot}" data-ref="${esc(r.ref)}">
      <td><span class="swatch" style="background:var(--s-${r.bot})"></span>${BOT_T[r.bot]}</td><td class="mono">${esc(r.id)}</td>
      <td title="${esc(r.name)}">${esc(r.name.length > 36 ? r.name.slice(0, 35) + '…' : r.name)}${r.qty > 1 ? ` <span class="muted">×${r.qty}</span>` : ''}</td>
      <td>${fmtTs(r.created_at)}</td><td>${cm(r.amount, r.currency)}</td><td>${cprofit(r.profit, r.currency)}</td>
      <td class="${grpCls[r.group]}">${esc(r.status_text)}${r.dry_run ? ' <span class="muted">(тест)</span>' : ''}</td>
      <td>${r.customer_id != null ? `<a href="#" data-cust="${r.customer_id}">${esc(r.counterparty)}</a>` : `<span class="mono small">${esc(r.counterparty)}</span>`}</td>
      <td class="small">${esc((r.notes || '').slice(0, 60))}${r.tags ? ` ${r.tags.split(',').filter((t) => t.trim()).map((t) => `<span class="tag">${esc(t.trim())}</span>`).join('')}` : ''}</td></tr>`).join('')
      || '<tr><td colspan="9" class="muted">Сделок не найдено</td></tr>'}</tbody>`;
  const pages = Math.ceil(d.total / CRM.limit), page = Math.floor(CRM.offset / CRM.limit) + 1;
  $('#crmPager').innerHTML = pages > 1 ? `<button class="btn" ${page <= 1 ? 'disabled' : ''} data-p="-1">←</button>
    <span class="muted small">стр. ${page} из ${pages} · всего ${d.total}</span><button class="btn" ${page >= pages ? 'disabled' : ''} data-p="1">→</button>` : '';
}

async function crmCustomers() {
  let d;
  try {
    d = await api(`/api/crm/customers?${new URLSearchParams({ q: CRM.cq, risk: CRM.risk, tag: CRM.tag, segment: CRM.segment, sort: CRM.csort })}`);
  } catch { return; }
  const all = d.levels.low + d.levels.medium + d.levels.high;
  tabs('#crmRisk', [['', `Все ${all}`], ...Object.entries(RISK).map(([k, [t]]) => [k, `${t} риск ${d.levels[k]}`])], CRM.risk, (k) => { CRM.risk = k; crmCustomers(); });
  tabs('#crmSeg', [['', 'Все сегменты'], ...Object.entries(d.segment_names).filter(([k]) => d.segments[k] || k === CRM.segment)
    .map(([k, t]) => [k, `${t} ${d.segments[k]}`])], CRM.segment, (k) => { CRM.segment = k; crmCustomers(); });
  $('#crmTag').innerHTML = `<option value="">все теги</option>${d.tags.map((t) => `<option ${t === CRM.tag ? 'selected' : ''}>${esc(t)}</option>`).join('')}`;
  $('#crmCustTable').innerHTML = `<thead><tr><th>Покупатель</th><th>Заказов</th><th>Подтв.</th><th>Возвр.</th><th>Потратил</th><th>Ключей</th>
    <th>Последний заказ</th><th>Отзывы</th><th>Сегмент</th><th>Риск</th><th>Теги / заметка</th></tr></thead><tbody>
    ${d.rows.map((c) => `<tr class="click" data-cust="${c.funpay_id}"><td>${c.blacklisted ? '⛔ ' : ''}<b>${esc(c.username || c.funpay_id)}</b> <span class="muted small">${c.funpay_id}</span></td>
      <td>${c.stats.orders}</td><td>${c.stats.completed}</td><td class="${c.stats.refunded ? 'neg' : ''}">${c.stats.refunded}</td>
      <td>${cm(c.stats.spent, c.stats.currency)}</td><td>${c.stats.keys}</td><td>${fmtTs(c.stats.last_order || c.last_seen)}</td>
      <td>${c.stats.avg_stars == null ? '—' : `${c.stats.avg_stars}★ (${c.stats.reviews})`}</td><td>${segBadge(c.segment)}</td><td>${riskBadge(c.risk)}</td>
      <td class="small">${c.tags.split(',').filter((t) => t.trim()).map((t) => `<span class="tag">${esc(t.trim())}</span>`).join('')} ${esc(c.notes.slice(0, 50))}</td></tr>`).join('')
      || '<tr><td colspan="11" class="muted">Покупателей пока нет — они появятся после первых заказов MannBot</td></tr>'}</tbody>`;
}

function crmLoad() {
  crmControls();
  return CRM.view === 'deals' ? crmDeals() : CRM.view === 'cohorts' ? crmCohorts() : crmCustomers();
}

// ---------------------------------------------------------------- карточки
const kv = (v, t) => `<div><b>${v}</b><span>${t}</span></div>`;
const evData = (d) => typeof d === 'object' && d ? Object.entries(d).map(([k, v]) => `${esc(k)}: ${esc(typeof v === 'object' ? JSON.stringify(v) : v)}`).join(', ') : esc(d ?? '');
const msgList = (ms) => ms.length ? `<div class="msgs">${ms.map((m) => `<div class="msg ${m.direction === 'in' ? 'in' : 'out'}"><span class="muted small">${fmtTs(m.ts)}${m.order_id ? ' · #' + esc(m.order_id) : ''}</span><br>${esc(m.text)}</div>`).join('')}</div>` : '<p class="muted">Сообщений нет</p>';

function crmShow(html) {
  $('#crmBody').innerHTML = html;
  if (!$('#crmDlg').open) $('#crmDlg').showModal();
}

async function openDeal(bot, ref) {
  const d = await api(`/api/crm/deals/${bot}/${encodeURIComponent(ref)}`);
  if (d.ok === false) { flash(d.message, true); return; }
  const x = d.deal, raw = d.raw, c = x.currency;
  const stats = bot === 'lisbot'
    ? kv(cm(x.amount, c), 'куплен за') + kv(cm(raw.expected_net_usd, c), 'прогноз выручки') + kv(cm(x.revenue, c), 'продан за')
      + kv(cprofit(x.profit, c), 'прибыль') + kv(esc(x.mode || '—'), 'режим') + kv(raw.relists ?? 0, 'перевыставлений')
    : kv(cm(x.amount, c), 'цена') + kv(x.qty, 'ключей') + kv(cprofit(x.profit, c), 'прибыль') + kv(raw.risk_score ?? 0, 'риск антифрода')
      + kv(raw.review_stars ? raw.review_stars + '★' : '—', 'отзыв') + kv(esc(raw.partner_name || '—'), 'Steam-ник');
  const cust = d.customer ? `<div class="row" style="margin:6px 0">Покупатель: <a href="#" data-cust="${d.customer.funpay_id}"><b>${esc(d.customer.username)}</b></a>
    ${riskBadge(d.customer.risk)} <span class="muted small">${d.customer.stats.orders} заказов, ${cm(d.customer.stats.spent, d.customer.stats.currency)}</span></div>` : '';
  const risk = (raw.risk_reasons || []).length ? `<div class="detail small">${raw.risk_reasons.map(esc).join('<br>')}</div>` : '';
  crmShow(`<div class="row"><h3><span class="swatch" style="background:var(--s-${bot})"></span>${BOT_T[bot]} · ${esc(x.id)}</h3><span class="spacer" style="flex:1"></span>
      <span class="badge ${grpCls[x.group]}">${esc(x.status_text)}</span></div>
    <div style="margin:4px 0"><b>${esc(x.name)}</b></div>
    <div class="muted small">создана ${fmtTs(x.created_at)}${x.closed_at ? ` · закрыта ${fmtTs(x.closed_at)}` : ''}${x.counterparty ? ` · контрагент ${esc(x.counterparty)}` : ''}${raw.fail_reason ? ` · причина: ${esc(raw.fail_reason)}` : ''}</div>
    ${cust}<div class="stat">${stats}</div>${risk}
    <form id="crmNoteForm"><label>Заметка</label><textarea name="notes" rows="3" maxlength="2000">${esc(x.notes)}</textarea>
      ${bot === 'lisbot' ? `<label>Теги (через запятую)</label><input name="tags" maxlength="200" value="${esc(x.tags)}">` : '<div class="muted small">Теги ставятся покупателю в его карточке.</div>'}
      <div class="row" style="margin-top:8px"><button class="btn primary" type="submit">Сохранить</button><span id="crmSaveMsg" class="small"></span></div></form>
    ${bot === 'mannbot' ? `<h4>Переписка</h4>${msgList(d.messages)}` : ''}
    <h4>История</h4><div class="scroll"><table>${d.events.map((e) => `<tr><td class="nowrap">${fmtTs(e.ts)}</td><td>${esc(e.type)}</td><td class="small">${evData(e.data)}</td></tr>`).join('') || '<tr><td class="muted">Событий нет</td></tr>'}</table></div>`);
  $('#crmNoteForm').onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target, body = { notes: f.notes.value };
    if (f.tags) body.tags = f.tags.value;
    const r = await api(`/api/crm/deals/${bot}/${encodeURIComponent(ref)}`, body);
    $('#crmSaveMsg').innerHTML = `<span class="${r.ok ? 'pos' : 'neg'}">${esc(r.message)}</span>`;
    if (r.ok) crmLoad();
  };
}

async function openCustomer(cid) {
  const c = await api(`/api/crm/customers/${cid}`);
  if (c.ok === false) { flash(c.message, true); return; }
  const s = c.stats, cur = s.currency;
  crmShow(`<div class="row"><h3>${c.blacklisted ? '⛔ ' : ''}${esc(c.username || c.funpay_id)}</h3><span class="muted small">FunPay ID ${c.funpay_id}</span>
      <span class="spacer" style="flex:1"></span>${segBadge(c.segment)} ${riskBadge(c.risk)}</div>
    <div class="muted small">первый заказ ${fmtTs(s.first_order) || '—'} · последний ${fmtTs(s.last_order) || '—'}</div>
    <div class="stat">${kv(s.orders, 'заказов')}${kv(s.completed, 'подтвердил')}${kv(s.refunded, 'возвратов')}${kv(s.active, 'в работе')}
      ${kv(cm(s.spent, cur), 'потратил')}${kv(cm(s.avg_check, cur), 'средний чек')}${kv(s.keys, 'ключей')}
      ${kv(s.confirm_rate == null ? '—' : s.confirm_rate + '%', 'подтверждает')}${kv(s.avg_stars == null ? '—' : s.avg_stars + '★', `отзывов: ${s.reviews}`)}</div>
    <h4>Риск-скоринг: ${c.risk.score} из 100</h4>
    ${c.risk.reasons.length ? `<ul class="small">${c.risk.reasons.map((r) => `<li>${r.points > 0 ? '+' : ''}${r.points} — ${esc(r.text)}</li>`).join('')}</ul>` : '<p class="muted small">Тревожных признаков нет.</p>'}
    ${c.shared_with.length ? `<p class="small">Тот же Steam-аккаунт у: ${c.shared_with.map((u) => `<a href="#" data-cust="${u.funpay_id}">${esc(u.username || u.funpay_id)}</a>`).join(', ')}</p>` : ''}
    <p class="small muted">Steam ID: ${c.steam_ids.map((x) => `<a href="https://steamcommunity.com/profiles/${esc(x)}" target="_blank" rel="noopener" class="mono">${esc(x)}</a>`).join(', ') || '—'}</p>
    ${c.winback ? `<h4>Вернуть покупателя</h4><p class="muted small">${esc(c.segment.why)}. Текст для чата FunPay (центр сам ничего не отправляет):</p>
      <textarea id="crmWinback" rows="3" readonly>${esc(c.winback)}</textarea>
      <div class="row" style="margin-top:4px"><button class="btn small" type="button" id="crmWinCopy">Скопировать</button><span id="crmWinMsg" class="small muted"></span></div>` : ''}
    <form id="crmCustForm"><label>Заметка</label><textarea name="notes" rows="3" maxlength="2000">${esc(c.notes)}</textarea>
      <label>Теги (через запятую)</label><input name="tags" maxlength="200" value="${esc(c.tags)}" placeholder="VIP, опт, проблемный">
      <label class="check"><input type="checkbox" name="blacklisted" ${c.blacklisted ? 'checked' : ''}> В чёрном списке (MannBot не будет выдавать автоматически)</label>
      <div class="row" style="margin-top:8px"><button class="btn primary" type="submit">Сохранить</button><span id="crmSaveMsg" class="small"></span></div></form>
    <h4>Заказы</h4><div class="scroll"><table><thead><tr><th>Заказ</th><th>Дата</th><th>Сумма</th><th>Ключей</th><th>Статус</th><th>Риск</th><th>Отзыв</th></tr></thead><tbody>
      ${c.orders.map((o) => `<tr class="click" data-bot="mannbot" data-ref="${esc(o.order_id)}"><td class="mono">#${esc(o.order_id)}</td><td>${fmtTs(o.created_at)}</td>
        <td>${cm(o.price, o.currency)}</td><td>${o.keys ?? ''}</td><td>${esc(o.status_text)}</td><td>${o.risk}</td><td>${o.stars ? o.stars + '★' : ''}</td></tr>`).join('')}</tbody></table></div>
    <h4>Переписка</h4>${msgList(c.messages)}`);
  if (c.winback) $('#crmWinCopy').onclick = async () => {
    try { await navigator.clipboard.writeText(c.winback); $('#crmWinMsg').textContent = 'скопировано'; }
    catch { $('#crmWinback').select(); $('#crmWinMsg').textContent = 'выделено — нажмите Ctrl+C'; }
  };
  $('#crmCustForm').onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.blacklisted.checked && !c.blacklisted && !confirm(`Добавить ${c.username} в чёрный список MannBot?`)) return;
    const r = await api(`/api/crm/customers/${cid}`, { notes: f.notes.value, tags: f.tags.value, blacklisted: f.blacklisted.checked });
    $('#crmSaveMsg').innerHTML = `<span class="${r.ok ? 'pos' : 'neg'}">${esc(r.message)}</span>`;
    if (r.ok) { c.blacklisted = f.blacklisted.checked; crmLoad(); }
  };
}

// ---------------------------------------------------------------- события
function crmClick(e) {
  const cust = e.target.closest('[data-cust]');
  if (cust) { e.preventDefault(); openCustomer(cust.dataset.cust); return; }
  const th = e.target.closest('th[data-sort]');
  if (th) { CRM.dir = CRM.sort === th.dataset.sort && CRM.dir === 'desc' ? 'asc' : 'desc'; CRM.sort = th.dataset.sort; crmDeals(); return; }
  const row = e.target.closest('tr[data-ref]');
  if (row) openDeal(row.dataset.bot, row.dataset.ref);
}
$('#crm').addEventListener('click', crmClick);
$('#crmBody').addEventListener('click', crmClick);
$('#crmPager').addEventListener('click', (e) => { const p = e.target.dataset?.p; if (p) { CRM.offset = Math.max(0, CRM.offset + Number(p) * CRM.limit); crmDeals(); } });
$('#crmSearch').addEventListener('input', (e) => { clearTimeout(crmTimer); crmTimer = setTimeout(() => { CRM.q = e.target.value; CRM.offset = 0; crmDeals(); }, 300); });
$('#crmCustSearch').addEventListener('input', (e) => { clearTimeout(crmTimer); crmTimer = setTimeout(() => { CRM.cq = e.target.value; crmCustomers(); }, 300); });
$('#crmBot').onchange = (e) => { CRM.bot = e.target.value; CRM.offset = 0; crmDeals(); };
$('#crmDays').onchange = (e) => { CRM.days = e.target.value; CRM.offset = 0; crmDeals(); };
$('#crmDry').onchange = (e) => { CRM.dry = e.target.checked; CRM.offset = 0; crmDeals(); };
$('#crmTag').onchange = (e) => { CRM.tag = e.target.value; crmCustomers(); };
$('#crmCsort').onchange = (e) => { CRM.csort = e.target.value; crmCustomers(); };
crmLoad();
setInterval(() => { if (!$('#crmDlg').open) crmLoad(); }, 30000);
