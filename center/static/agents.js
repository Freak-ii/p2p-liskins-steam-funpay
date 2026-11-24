// Раздел «Агенты» (center/agents/web.py). Карточки всех агентов + очередь постов агента канала.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtTs = (t) => t ? new Date(t * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
const KINDS = { news: 'Новость', season: 'Сезонный повод', promo: 'Акция', milestone: 'Достижение', achievement: 'Развитие проекта', chatter: 'Обычный пост', manual: 'Свой пост' };
const STATUS = { draft: 'Черновики', approved: 'Одобрены', published: 'Опубликованы', rejected: 'Отклонены', failed: 'Ошибки' };
let chStatus = 'draft';

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (r.status === 401) { location.href = '/login'; throw new Error('auth'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok && d.message) flash(d.message, true);
  return d;
}
function flash(msg, bad) {
  $('#flash').innerHTML = msg ? `<div class="banner" style="${bad ? 'background:color-mix(in srgb,var(--bad) 14%,transparent)' : ''}">${esc(msg)}</div>` : '';
  if (msg) setTimeout(() => { $('#flash').innerHTML = ''; }, 8000);
}

async function loadAgents() {
  const d = await api('/api/agents');
  $('#hdr').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU');
  $('#agents').innerHTML = d.agents.map((a) => {
    const c = a.counts || {};
    const extra = a.id === 'channel' ? `
      <div class="small" style="margin-top:6px">Канал: <b>${esc(a.channel || 'не задан')}</b> · бот: ${a.bot_username ? '@' + esc(a.bot_username) : '—'}
        ${a.token_set ? '' : ' · <span class="warn">нет токена бота</span>'} · публикация:
        ${a.publish_enabled ? '<span class="pos">включена</span>' : '<span class="warn">выключена</span>'}</div>
      <div class="small muted">Тексты: ${esc(a.writer)}${a.shop_orders != null ? ' · выполненных заказов магазина: ' + a.shop_orders : ''}</div>
      ${a.news_errors && a.news_errors.errors && a.news_errors.errors.length ? `<div class="small warn">Источники не открылись: ${esc(a.news_errors.errors.join('; '))}</div>` : ''}` : '';
    return `<div class="card">
      <div class="row"><h3>${esc(a.title)}</h3><span class="spacer" style="flex:1"></span>
        <span class="badge ${a.running ? 's-running' : 's-stopped'}">${a.running ? 'работает' : 'остановлен'}</span></div>
      <div class="muted small" style="margin-top:4px">${esc(a.description)}</div>${extra}
      <div class="kpi">
        <div><b>${c.draft || 0}</b><span>ждут одобрения</span></div>
        <div><b>${c.approved || 0}</b><span>одобрены</span></div>
        <div><b>${c.published || 0}</b><span>опубликованы</span></div>
      </div>
      <div class="small muted">Проход раз в ${Math.round(a.interval_minutes)} мин · последний: ${a.last_run ? fmtTs(a.last_run) + ' — ' + esc(a.last_result || a.last_error) : 'ещё не было'}</div>
      ${a.last_error ? `<div class="detail small">${esc(a.last_error)}</div>` : ''}
      <div class="row" style="margin-top:10px">
        ${a.running ? `<button class="btn" onclick="act('${a.id}','stop')">Остановить</button>` : `<button class="btn primary" onclick="act('${a.id}','start')" ${a.enabled ? '' : 'disabled'}>Запустить</button>`}
        <button class="btn" onclick="act('${a.id}','run')">Сделать проход сейчас</button>
      </div></div>`;
  }).join('');
  const ch = d.agents.find((a) => a.id === 'channel');
  if (ch) {
    $('#channel').hidden = false;
    $('#chUpcoming').innerHTML = (ch.upcoming || []).map((u) =>
      `<li><b>${esc(u.title)}</b> — ${new Date(u.date).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' })}${u.approx ? ' (примерно)' : ''}
       <span class="muted small">· через ${u.in_days} дн. · ${esc(u.idea)}</span></li>`).join('') || '<li class="muted">В ближайшие 45 дней поводов нет</li>';
    const counts = ch.counts || {};
    $('#chTabs').innerHTML = Object.entries(STATUS).map(([k, v]) =>
      `<button class="btn ${k === chStatus ? 'on' : ''}" onclick="setTab('${k}')">${v}${counts[k] ? ' · ' + counts[k] : ''}</button>`).join('');
  }
}

async function act(id, verb) {
  const d = await api(`/api/agents/${id}/${verb}`, {});
  if (d.ok) flash(d.message);
  await loadAgents(); await loadItems();
}
function setTab(s) { chStatus = s; loadAgents(); loadItems(); }

async function loadItems() {
  const d = await api('/api/agents/channel/items?status=' + chStatus);
  const items = d.items || [];
  $('#chItems').innerHTML = items.length ? items.map(itemHtml).join('') : `<div class="muted">Пусто. ${chStatus === 'draft' ? 'Нажмите «Сделать проход сейчас» или добавьте свой пост.' : ''}</div>`;
}
function itemHtml(it) {
  const editable = ['draft', 'failed', 'rejected', 'approved'].includes(it.status);
  const issues = (it.issues || []).length ? `<ul class="ag-issues small">${it.issues.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : '';
  const when = it.published_at ? 'опубликован ' + fmtTs(it.published_at) : it.scheduled_at ? 'запланирован на ' + fmtTs(it.scheduled_at) : 'создан ' + fmtTs(it.created_at);
  const src = it.source_url ? ` · <a href="${esc(it.source_url)}" target="_blank" rel="noopener">источник</a>` : '';
  const forDate = it.meta && it.meta.for_date ? ` · к дате ${new Date(it.meta.for_date).toLocaleDateString('ru-RU')}` : '';
  return `<div class="ag-item" id="it${it.id}">
    <div class="row"><span class="ag-kind">${esc(KINDS[it.kind] || it.kind)}</span><b>${esc(it.title)}</b>
      <span class="spacer" style="flex:1"></span><span class="small ag-s-${it.status}">${esc(STATUS[it.status] || it.status)}</span></div>
    <div class="muted small">${when}${forDate}${src}${it.meta && it.meta.model ? ' · ' + esc(it.meta.model) : ''}${it.error ? ' · <span class="neg">' + esc(it.error) + '</span>' : ''}</div>
    ${editable ? `<textarea id="tx${it.id}" rows="${Math.min(14, Math.max(4, it.text.split('\n').length + 1))}" style="margin-top:8px">${esc(it.text)}</textarea>` : `<div class="ag-text">${esc(it.text)}</div>`}
    ${issues}
    ${editable ? `<div class="row" style="margin-top:6px">
      <button class="btn" onclick="saveItem(${it.id})">Сохранить правку</button>
      ${it.status !== 'approved' ? `<button class="btn primary" onclick="approve(${it.id})" ${(it.issues || []).length ? 'disabled title="Сначала уберите замечания фильтра"' : ''}>Одобрить и опубликовать</button>
      <input type="datetime-local" id="at${it.id}" aria-label="Время публикации"><button class="btn" onclick="approve(${it.id}, true)" ${(it.issues || []).length ? 'disabled' : ''}>Одобрить на время</button>` :
      `<button class="btn primary" onclick="publish(${it.id})">Опубликовать сейчас</button>`}
      ${it.status !== 'rejected' ? `<button class="btn danger" onclick="reject(${it.id})">Отклонить</button>` : ''}
    </div>` : ''}
  </div>`;
}
async function saveItem(id) {
  const d = await api(`/api/agents/channel/items/${id}/edit`, { text: $('#tx' + id).value });
  if (d.ok) flash(d.message);
  await loadItems(); await loadAgents();
}
async function approve(id, timed) {
  const body = {};
  if (timed) {
    const v = $('#at' + id).value;
    if (!v) { flash('Выберите время публикации', true); return; }
    body.at = v;
  }
  const t = $('#tx' + id);
  if (t) await api(`/api/agents/channel/items/${id}/edit`, { text: t.value });
  const d = await api(`/api/agents/channel/items/${id}/approve`, body);
  if (d.ok) flash(d.message);
  await loadItems(); await loadAgents();
}
async function publish(id) {
  const d = await api(`/api/agents/channel/items/${id}/publish`, {});
  if (d.ok) flash(d.message);
  await loadItems(); await loadAgents();
}
async function reject(id) {
  const d = await api(`/api/agents/channel/items/${id}/reject`, {});
  if (d.ok) flash(d.message);
  await loadItems(); await loadAgents();
}

// ------------------------------------------------------------ настройки
const BOOLS = ['publish_enabled', 'button', 'ai', 'share_milestones', 'notify_drafts'];
const LISTS = ['sources', 'keywords', 'achievements', 'forbidden_words'];
const NUMS = ['interval_minutes', 'chatter_every_days', 'season_lead_days', 'news_per_day'];
function promoRow(p = {}) {
  const div = document.createElement('div');
  div.className = 'ag-promo';
  div.innerHTML = `<input placeholder="Название" data-k="title" value="${esc(p.title)}"><input placeholder="Промокод" data-k="code" value="${esc(p.code)}">
    <input placeholder="Скидка %" type="number" step="0.5" data-k="discount_pct" value="${esc(p.discount_pct)}">
    <input placeholder="Начало" type="date" data-k="start" value="${esc(p.start)}"><input placeholder="Конец" type="date" data-k="end" value="${esc(p.end)}">
    <button class="btn small" type="button" aria-label="Удалить акцию">✕</button>`;
  div.dataset.id = p.id || '';
  div.dataset.text = p.text || '';
  div.querySelector('button').onclick = () => div.remove();
  return div;
}
async function loadSettings() {
  const s = await api('/api/agents/channel/settings');
  BOOLS.forEach((k) => { $('#f_' + k).checked = !!s[k]; });
  LISTS.forEach((k) => { $('#f_' + k).value = (s[k] || []).map((x) => typeof x === 'object' ? x.text : x).join('\n'); });
  NUMS.forEach((k) => { $('#f_' + k).value = s[k] ?? ''; });
  const box = $('#f_promos'); box.innerHTML = '';
  (s.promos || []).forEach((p) => box.appendChild(promoRow(p)));
}
$('#addPromo').onclick = () => $('#f_promos').appendChild(promoRow());
$('#chForm').onsubmit = async (e) => {
  e.preventDefault();
  const body = {};
  BOOLS.forEach((k) => { body[k] = $('#f_' + k).checked; });
  LISTS.forEach((k) => { body[k] = $('#f_' + k).value; });
  NUMS.forEach((k) => { if ($('#f_' + k).value !== '') body[k] = Number($('#f_' + k).value); });
  body.promos = [...document.querySelectorAll('#f_promos .ag-promo')].map((row) => {
    const p = { id: row.dataset.id || undefined, text: row.dataset.text || undefined };
    row.querySelectorAll('input').forEach((i) => { if (i.value !== '') p[i.dataset.k] = i.dataset.k === 'discount_pct' ? Number(i.value) : i.value; });
    return p;
  }).filter((p) => p.title || p.code);
  const d = await api('/api/agents/channel/settings', body);
  if (d.ok) { flash(d.message); loadSettings(); loadAgents(); }
};
$('#chNewBtn').onclick = () => { $('#chNew').hidden = !$('#chNew').hidden; };
$('#chNewSave').onclick = async () => {
  const d = await api('/api/agents/channel/items', { title: $('#chNewTitle').value, text: $('#chNewText').value });
  if (d.ok) { flash(d.message); $('#chNewText').value = ''; $('#chNewTitle').value = ''; $('#chNew').hidden = true; chStatus = 'draft'; await loadAgents(); await loadItems(); }
};

// ------------------------------------------------------------ партнёры
let ptDays = 30;
async function loadSources() {
  $('#ptDays').innerHTML = [7, 30, 90].map((d) => `<button class="btn ${d === ptDays ? 'on' : ''}" onclick="ptDays=${d};loadSources()">${d} дн.</button>`).join('');
  const d = await api('/api/agents/channel/sources?days=' + ptDays);
  if (!d.ok) { $('#ptTable').innerHTML = `<div class="muted small">${esc(d.message)}</div>`; return; }
  const rows = d.rows || [];
  $('#ptTable').innerHTML = rows.length ? `<table><thead><tr><th>Метка</th><th>Запуски бота</th><th>Покупатели</th><th>Заказы</th><th>Выручка, ₽</th></tr></thead><tbody>${
    rows.map((r) => `<tr><td>${esc(r.source)}</td><td>${r.starts ?? 0}</td><td>${r.buyers ?? 0}</td><td>${r.orders ?? 0}</td><td>${Math.round(r.revenue_rub || 0).toLocaleString('ru-RU')}</td></tr>`).join('')}</tbody></table>`
    : '<div class="muted small">Пока никто не пришёл по меткам</div>';
}
$('#ptMake').onclick = async () => {
  const d = await api('/api/agents/channel/links', { tag: $('#ptTag').value });
  if (d.ok) $('#ptLinks').innerHTML = `Бот: ${esc(d.bot)}<br>Сразу к звёздам: ${esc(d.bot_stars)}`;
};

loadAgents().then(loadItems).then(loadSettings).then(loadSources);
setInterval(loadAgents, 30000);
