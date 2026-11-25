// Раздел «Экономика»: /api/economy/summary (сводка, круги, позиции) и /api/economy/ledger (журнал операций).
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const rub = (x, d = 0) => x == null ? '—' : `${Number(x).toLocaleString('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d })} ₽`;
const sgn = (x, d = 0) => x == null ? '—' : `<span class="${x > 0 ? 'pos' : x < 0 ? 'neg' : ''}">${x > 0 ? '+' : ''}${rub(x, d)}</span>`;
const pct = (x) => x == null ? '—' : `<span class="${x > 0 ? 'pos' : x < 0 ? 'neg' : ''}">${x > 0 ? '+' : ''}${Number(x).toFixed(1)}%</span>`;
const when = (ts) => new Date(ts * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
const EC = { days: localGet('ec.days') || 'all', rep: null, ops: [], show: 100 };

function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { /* приватный режим */ } }

async function api(path) {
  const r = await fetch(path);
  if (r.status === 401) { location.href = '/login'; throw new Error('401'); }
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

function tabs() {
  const opts = [['1', 'сутки'], ['7', '7 дн'], ['30', '30 дн'], ['all', 'всё']];
  $('#ecDays').innerHTML = opts.map(([k, t]) => `<button class="btn ${k === EC.days ? 'on' : ''}" data-k="${k}">${t}</button>`).join('');
  $('#ecDays').onclick = (e) => { const k = e.target.dataset?.k; if (k) { EC.days = k; localSet('ec.days', k); tabs(); load(); } };
}

function kpi(r) {
  const t = r.totals;
  const tile = (v, label) => `<div><b>${v}</b><span>${label}</span></div>`;
  $('#kpi').innerHTML = [
    tile(rub(t.revenue), `выручка, ${t.orders} заказ(ов)`),
    tile(rub(t.cost + t.gas + t.fees), 'закупка, газ и комиссии'),
    tile(`${sgn(t.gross)} <span class="small">${pct(t.gross_pct)}</span>`, 'прибыль до вывода'),
    tile(`${sgn(t.net)} <span class="small">${pct(t.net_pct)}</span>`, `чистыми после вывода (${rub(t.fee_estimate)} оценка)`),
    tile(r.rates.next_buy ? `${Number(r.rates.next_buy.rub).toFixed(2)} ₽` : '—', r.rates.next_buy ? `TON сейчас: GRAM ${r.rates.next_buy.gram_usdt} $ × USDT ${r.rates.next_buy.usdt_rub} ₽` : 'цена TON сейчас (нет данных)'),
    tile(rub(t.p2p_premium), 'наценка P2P к биржевому доллару'),
    tile(rub(t.revenue_pending), 'FunPay ещё держит'),
    tile(rub(r.capital_rub), 'деньги и товар в работе'),
  ].join('');
  $('#warnCard').hidden = !r.warnings.length;
  $('#warn').innerHTML = r.warnings.map((w) => `<li>${esc(w)}</li>`).join('');
}

function circles(r) {
  if (!r.circles.length) { $('#circles').innerHTML = '<div class="card muted">За этот период кругов с операциями нет.</div>'; return; }
  const N = r.titles.nodes;
  $('#circles').innerHTML = r.circles.map((c) => {
    // цепочка узлов: первый шаг начинается с его from, дальше каждый шаг ведёт в свой to
    let flow = `<span class="node">${esc(N[c.legs[0]?.from] || c.legs[0]?.from || '')}</span>`;
    for (const l of c.legs) {
      const cls = l.kind === 'sale' ? 'pos' : (l.rub || 0) < 0 ? 'neg' : 'mid';
      flow += `<div class="leg ${cls}" title="${esc(l.note || '')}"><b>${l.kind === 'sale' ? '+' : ''}${rub(l.rub)}</b><div class="arrow"></div>`
        + `<span>${esc(l.title)}</span><span class="muted">${l.pct == null ? '' : `${Number(l.pct).toFixed(1)}% выручки`}</span></div>`
        + `<span class="node">${esc(N[l.to] || l.to)}</span>`;
    }
    const rev = c.revenue || 0;
    const costs = c.legs.filter((l) => l.kind !== 'sale' && (l.rub || 0) < 0);
    const wf = costs.map((l) => {
      const w = rev ? Math.min(100, -l.rub / rev * 100) : 0;
      return `<div class="wf-row"><span>${esc(l.title)}</span><div class="wf-track"><i style="left:0;width:${w}%;background:var(--bad);opacity:.75"></i></div><b class="neg">−${w.toFixed(1)} ₽</b></div>`;
    }).join('');
    const left = rev ? c.net / rev * 100 : 0;
    const res = `<div class="wf-row"><span><b>остаётся из 100 ₽</b></span><div class="wf-track"><i style="left:0;width:${Math.min(100, Math.abs(left))}%;min-width:3px;background:var(--${left >= 0 ? 'ok' : 'bad'})"></i></div><b class="${left >= 0 ? 'pos' : 'neg'}">${left >= 0 ? '+' : ''}${left.toFixed(2)} ₽</b></div>`;
    return `<div class="card circle"><div class="circle-head"><h3>${esc(c.title)}</h3>
        <span class="badge ${c.net >= 0 ? 'pos' : 'neg'}">${c.net >= 0 ? 'прибыль' : 'убыток'} ${sgn(c.net)} · ${pct(c.net_pct)}</span>
        <span class="muted small">${c.orders} заказ(ов), ${when(c.first_ts)} — ${when(c.last_ts)}; до вывода ${sgn(c.gross)} (${pct(c.gross_pct)})</span></div>
      <div class="flow">${flow}</div>
      ${c.legs.filter((l) => l.note).map((l) => `<div class="muted small">• ${esc(l.title)}: ${esc(l.note)}</div>`).join('')}
      <div class="wf"><div class="muted small">Из каждых 100 ₽ выручки:</div>${wf}${res}</div>
      <div class="muted small mono" style="margin-top:6px">${esc(c.id)}</div></div>`;
  }).join('');
}

function dirs(r) {
  const rows = r.directions.map((d) => `<tr><td>${esc(d.title)}</td><td class="num">${d.orders}</td><td class="num">${rub(d.revenue)}</td>
    <td class="num">${rub(d.cost + d.gas + d.fees)}</td><td class="num">${sgn(d.gross)}</td><td class="num">${rub(d.fee_estimate)}</td>
    <td class="num">${sgn(d.net)}</td><td class="num">${pct(d.net_pct)}</td></tr>`).join('');
  const t = r.totals;
  $('#dirs').innerHTML = `<tr><th>Направление</th><th class="num">Заказы</th><th class="num">Выручка</th><th class="num">Закупка и комиссии</th>
    <th class="num">До вывода</th><th class="num">Вывод (оценка)</th><th class="num">Чистыми</th><th class="num">Маржа</th></tr>${rows}
    <tr style="font-weight:600"><td>Всего</td><td class="num">${t.orders}</td><td class="num">${rub(t.revenue)}</td><td class="num">${rub(t.cost + t.gas + t.fees)}</td>
    <td class="num">${sgn(t.gross)}</td><td class="num">${rub(t.fee_estimate)}</td><td class="num">${sgn(t.net)}</td><td class="num">${pct(t.net_pct)}</td></tr>`;
}

function days(r) {
  const d = r.daily.slice(-45);
  const max = Math.max(1, ...d.map((x) => Math.abs(x.net || 0)));
  $('#days').innerHTML = d.length ? d.map((x) => `<div title="${x.day}: выручка ${rub(x.revenue)}, чистыми ${rub(x.net)}">
    <span>${x.net > 0 ? '+' : ''}${Math.round(x.net)}</span><i style="height:${Math.abs(x.net) / max * 100}%;background:var(--${x.net >= 0 ? 'ok' : 'bad'})"></i>
    <span>${x.day.slice(8)}.${x.day.slice(5, 7)}</span></div>`).join('') : '<span class="muted">нет операций</span>';
}

function side(r) {
  $('#positions').innerHTML = r.positions.length ? `<tr><th>Где</th><th class="num">Сколько</th><th class="num">≈ ₽</th></tr>` + r.positions.map((p) =>
    `<tr><td>${esc(p.title)}${p.idle ? ' <span class="tag">без оборота</span>' : ''}</td><td class="num">${p.qty} ${esc(p.unit)}</td><td class="num">${rub(p.value_rub)}</td></tr>`).join('')
    : '<tr><td class="muted">нет данных</td></tr>';
  $('#wallets').innerHTML = r.wallets.map((w) => `<p style="margin:8px 0 2px"><b>${esc(w.title)}</b>: было ${w.start} + пришло ${w.in} − ушло ${w.out} = ${(w.start + w.in - w.out).toFixed(4)}, сейчас ${w.end} ${esc(w.asset)}</p>
    <div class="muted small">${esc(w.note)}</div>`).join('') || '<p class="muted">нет данных</p>';
  const tp = r.rates.topups || [];
  $('#topups').innerHTML = tp.length ? `<tr><th>Когда</th><th class="num">₽</th><th class="num">TON</th><th class="num">₽/TON</th><th class="num">к P2P-ориентиру</th></tr>` + tp.map((t) =>
    `<tr><td>${when(t.ts)} <span class="muted">${esc(t.note)}</span></td><td class="num">${rub(t.rub)}</td><td class="num">${t.ton}</td><td class="num">${t.rate}</td>
    <td class="num ${t.over_market_pct > 0 ? 'neg' : 'pos'}">${t.over_market_pct > 0 ? '+' : ''}${t.over_market_pct}%</td></tr>`).join('')
    + `<tr><td colspan="5" class="muted small">P2P-ориентир (GRAM × USDT на P2P) ≈ ${r.rates.ton_market_rub} ₽/TON, в среднем платим ${r.rates.ton_paid_rub}; по биржевому доллару было бы ${r.rates.ton_forex_rub}</td></tr>` : '<tr><td class="muted">пополнений не записано</td></tr>';
}

function filters(r) {
  const fill = (sel, map) => { const el = $(sel); const cur = el.value; el.innerHTML = el.options[0].outerHTML + Object.entries(map).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join(''); el.value = cur; };
  fill('#fbot', r.titles.bots); fill('#fdir', r.titles.directions); fill('#fkind', r.titles.kinds);
}

async function ledger() {
  const p = new URLSearchParams({ days: EC.days });
  for (const [id, k] of [['#fq', 'q'], ['#fbot', 'bot'], ['#fdir', 'direction'], ['#fkind', 'kind'], ['#fst', 'status']]) if ($(id).value) p.set(k, $(id).value);
  $('#csv').href = `/api/economy/export?days=${EC.days}`;
  const d = await api(`/api/economy/ledger?${p}`);
  const T = EC.rep?.titles || { bots: {}, directions: {}, kinds: {} };
  const st = { done: 'проведено', pending: 'ждёт FunPay', estimate: 'оценка' };
  const rows = d.ops.slice(0, EC.show).map((o) => `<tr class="st-${o.status}"><td>${when(o.ts)}</td><td>${esc(T.bots[o.bot] || o.bot)}</td>
    <td>${esc(T.kinds[o.kind] || o.kind)}</td><td class="num">${o.amount} ${esc(o.currency)}</td><td class="num">${sgn(o.rub, 2)}</td>
    <td>${esc(st[o.status] || o.status)}</td><td class="mono">${esc(o.ref)}</td><td style="white-space:normal;min-width:260px">${esc(o.note)}</td></tr>`).join('');
  $('#ledger').innerHTML = `<tr><th>Время</th><th>Бот</th><th>Операция</th><th class="num">Сумма</th><th class="num">₽ для системы</th><th>Статус</th><th>Заказ</th><th>Что произошло</th></tr>${rows}`;
  $('#ledgerInfo').textContent = `${d.total} операций, новые сверху. «₽ для системы»: + пришло, − ушло, по фактической цене закупки (FIFO).`;
  $('#more').hidden = d.total <= EC.show;
  $('#more').textContent = `Показать ещё (${Math.min(100, d.total - EC.show)} из ${d.total - EC.show})`;
}

async function load() {
  try {
    const r = await api(`/api/economy/summary?days=${EC.days}&ops=1`);
    EC.rep = r;
    kpi(r); circles(r); dirs(r); days(r); side(r); filters(r);
    $('#hdr').textContent = `обновлено ${new Date().toLocaleTimeString('ru-RU')}, $ = ${r.rates.usd_rub} ₽`;
    $('#flash').innerHTML = '';
    await ledger();
  } catch (e) {
    $('#flash').innerHTML = `<div class="banner lv-error">Не удалось загрузить отчёт: ${esc(e.message)}</div>`;
  }
}

let t;
for (const id of ['#fbot', '#fdir', '#fkind', '#fst']) $(id).onchange = () => { EC.show = 100; ledger(); };
$('#more').onclick = () => { EC.show += 100; ledger(); };
$('#fq').oninput = () => { clearTimeout(t); t = setTimeout(() => { EC.show = 100; ledger(); }, 300); };
tabs();
load();
setInterval(() => { if (!document.hidden) load(); }, 60000);
