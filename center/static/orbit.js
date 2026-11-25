// Пульт Орбиты (center/orbit.py): позиции по всем каналам, круги денег и переводы.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (v) => v == null ? '—' : `<span class="${v < 0 ? 'neg' : 'posv'}">${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%</span>`;
const fmtTs = (t) => t ? new Date(t * 1000).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
const STATE = { open: 'на орбите', closed: 'снята', parked: 'нет круга' };
let V = null;

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (r.status === 401) { location.href = '/login'; throw new Error('auth'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) flash(d.message || `HTTP ${r.status}`, true);
  return d;
}
function flash(msg, bad) {
  $('#flash').innerHTML = msg ? `<div class="banner" style="${bad ? 'background:color-mix(in srgb,var(--bad) 14%,transparent)' : ''}">${esc(msg)}</div>` : '';
  if (msg) setTimeout(() => { $('#flash').innerHTML = ''; }, 8000);
}

function chanCard(p, c) {
  const binds = c.bind.map((b) => `<div class="b"><span class="st-${esc(b.status)}">●</span> ${esc(b.title)}: ${esc(b.text)}</div>`).join('');
  const margin = c.sale ? `<div class="b">маржа <input type="number" step="0.1" value="${c.margin}" data-margin="${esc(p.id)}@${esc(c.channel)}"> %
      <span title="откуда цифра">${esc(c.margin_source)}</span></div>
      <div class="b">свой круг: ${c.best ? pct(c.best.pct) + (c.best.with.length ? ' вместе с ' + esc(c.best.with.join(', ')) : '') : '<b>нет</b>'}</div>` : '';
  const canToggle = p.state !== 'closed' || c.state === 'closed';
  const btn = canToggle ? `<button class="btn small" data-ch="${esc(p.id)}|${esc(c.channel)}|${c.state === 'closed' ? 1 : 0}">${c.state === 'closed' ? 'открыть канал' : 'закрыть канал'}</button>` : '';
  return `<div class="ob-ch ${esc(c.state)}" title="${esc(c.why || c.note || '')}">
    <div class="t"><span>${esc(c.title)}</span><span class="ob-state ob-${esc(c.state)}">${STATE[c.state]}</span></div>
    ${c.note ? `<div class="b">${esc(c.note)}</div>` : ''}${margin}${binds}
    <div style="margin-top:4px">${btn}</div></div>`;
}

function posCard(p) {
  const main = p.state === 'closed'
    ? `<button class="btn primary" data-pos="${esc(p.id)}|1">↩️ Вернуть на орбиту</button>`
    : `<input placeholder="причина (необязательно)" id="why_${esc(p.id)}" style="max-width:220px">
       <button class="btn danger" data-pos="${esc(p.id)}|0">⛔ Снять с орбиты везде</button>`;
  const info = p.state === 'closed' ? `снята ${fmtTs(p.changed_at)}${p.reason ? ' — ' + esc(p.reason) : ''}` :
    `лучший свой круг: ${pct(p.best_pct)}${p.losing ? ' · <span class="neg">в минусе</span>' : ''}`;
  return `<div class="card ob-pos">
    <div class="ob-head"><h3>${esc(p.emoji)} ${esc(p.title)}</h3><span class="ob-state ob-${esc(p.state)}">${STATE[p.state]}</span>
      <span class="small muted">${info}</span><span class="spacer" style="flex:1"></span>${main}</div>
    <div class="ob-chans">${p.channels.map((c) => chanCard(p, c)).join('')}</div></div>`;
}

function render(v) {
  V = v;
  $('#hdr').textContent = `${v.name} · версия решения ${v.version} · ${new Date().toLocaleTimeString('ru-RU')}`;
  $('#advice').style.display = v.advice.length ? '' : 'none';
  $('#advice').innerHTML = `<b>Подсказки Орбиты</b><ul class="ob-adv">${v.advice.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>`;
  const groups = {};
  v.positions.forEach((p) => (groups[p.group] = groups[p.group] || []).push(p));
  $('#groups').innerHTML = Object.entries(groups).map(([g, ps]) => `<h2>${esc(g)}</h2><div class="ob-grid">${ps.map(posCard).join('')}</div>`).join('');
  $('#circHint').textContent = `Всего кругов: ${v.circles_total} (показаны лучшие ${v.circles.length}). Доходность круга — произведение звеньев: маржа продажи и потери на переводах.`;
  $('#circles').innerHTML = `<table><tr><th>Круг</th><th>Позиции</th><th>Путь</th></tr>${v.circles.map((c) => `<tr><td class="nowrap">${pct(c.pct)}</td>
    <td>${esc(c.sales.join(', '))}</td><td class="ob-path">${c.legs.map((l) => `${esc(v.nodes[l.from] || l.from)} → <i>${esc(l.title)} ${l.pct > 0 ? '+' : ''}${l.pct}%</i>`).join(' → ')}</td></tr>`).join('')}</table>`;
  $('#transfers').innerHTML = `<table><tr><th>Вкл.</th><th>Перевод</th><th>Потеря, %</th></tr>${v.transfers.map((t) => `<tr>
    <td><input type="checkbox" data-tr="${esc(t.id)}" ${t.enabled ? 'checked' : ''}></td><td>${esc(t.title)}<div class="small muted">${esc(v.nodes[t.from])} → ${esc(v.nodes[t.to])}</div></td>
    <td><input type="number" step="0.1" value="${t.pct}" data-trpct="${esc(t.id)}" style="width:70px"></td></tr>`).join('')}</table>`;
  $('#settings').innerHTML = `<label class="check"><input type="checkbox" id="autoPark" ${v.auto_park ? 'checked' : ''}> Парковать каналы без круга
      (закрывать у ботов, пока деньгам оттуда некуда вернуться)</label>
    <div style="margin-top:8px">Подсказка «в минусе», если лучший круг ниже <input type="number" step="0.5" id="minLoop" value="${v.min_loop_pct}" style="width:70px"> %</div>
    <div class="small muted" style="margin-top:8px">Файл решения для ботов: <span class="mono">${esc(v.path)}</span>.
      Управление файлом: строка <span class="mono">orbit close mann_keys минус</span> или <span class="mono">orbit open premium</span> в remote_cmd.txt центра.</div>`;
  $('#history').innerHTML = v.history.length ? `<table><tr><th>Когда</th><th>Что</th><th>Кто</th></tr>${v.history.map((h) => `<tr><td class="nowrap">${fmtTs(h.ts)}</td>
    <td>${h.action === 'close' ? '⛔ снята' : '↩️ возвращена'}: ${esc(h.position)}${h.channel ? ' · ' + esc(h.channel) : ''}${h.reason ? ' — ' + esc(h.reason) : ''}</td><td>${esc(h.by)}</td></tr>`).join('')}</table>` : '<span class="muted">Пока ничего не меняли.</span>';
}

async function load() { render(await api('/api/orbit')); }

document.addEventListener('click', async (ev) => {
  const t = ev.target.closest('[data-pos],[data-ch]');
  if (!t) return;
  let body;
  if (t.dataset.pos) {
    const [id, open] = t.dataset.pos.split('|');
    body = { id, open: open === '1', reason: ($('#why_' + id) || {}).value || '' };
  } else {
    const [id, channel, open] = t.dataset.ch.split('|');
    body = { id, channel, open: open === '1' };
  }
  t.disabled = true;
  const d = await api('/api/orbit/position', body);
  if (d.ok) { flash(d.message); render(d.view); }
  t.disabled = false;
});
document.addEventListener('change', async (ev) => {
  const t = ev.target;
  let body = null;
  if (t.dataset.margin) body = { margins: { [t.dataset.margin]: t.value === '' ? null : Number(t.value) } };
  else if (t.dataset.tr) body = { transfers: { [t.dataset.tr]: { enabled: t.checked } } };
  else if (t.dataset.trpct) body = { transfers: { [t.dataset.trpct]: { pct: Number(t.value) } } };
  else if (t.id === 'autoPark') body = { auto_park: t.checked };
  else if (t.id === 'minLoop') body = { min_loop_pct: Number(t.value) };
  if (!body) return;
  const d = await api('/api/orbit/settings', body);
  if (d.ok) { flash('Сохранено, круги пересчитаны'); render(d.view); }
});
load();
setInterval(() => { if (!document.hidden && !document.activeElement?.matches('input')) load(); }, 10000);
