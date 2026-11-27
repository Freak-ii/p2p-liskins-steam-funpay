// Схема Орбиты на главной: кто кого снабжает и куда продаёт, со статусом каждой связи (/api/supply/graph).
// Показываются только включённые боты и площадки; выключенное — одной строкой внизу. Использует $, esc, api, flash.
const SL = { ok: ['●', 'lv-ok'], warn: ['●', 'lv-warn'], bad: ['●', 'lv-critical'], down: ['●', 'lv-critical'],
  idle: ['○', 'muted'], off: ['○', 'muted'], manual: ['✋', 'lv-warn'] };
const SL_SRC = { vault: 'хранится в Центре и передаётся ботам сам', adopted: 'взят из настроек магазина и передаётся ботам сам',
  new: 'создан Центром и передаётся ботам сам', memory: 'временный: хранилище Центра недоступно' };

function slLink(l) {
  const [dot, cls] = SL[l.status] || SL.idle;
  const what = l.what ? ` <span class="muted">(${esc(l.what)})</span>` : '';
  return `<li><span class="${cls}" title="${esc(l.status)}">${dot}</span> ${esc(l.to_title)}${what} — <span class="${cls}">${esc(l.text)}</span></li>`;
}

async function refreshSupply() {
  let g;
  try { g = await api('/api/supply/graph'); } catch { return; }
  const titles = Object.fromEntries(g.nodes.map((n) => [n.id, n]));
  titles.manual = { title: 'Оператор', state: 'running', running: true };
  const ORDER = ['starsbot', 'tggift', 'mannbot', 'manual', 'shopbot', 'lisbot'];   // сначала поставщики
  const rank = (id) => (ORDER.indexOf(id) + 1) || 99;
  const from = [...new Set(g.links.map((l) => l.from))].sort((a, b) => rank(a) - rank(b));
  const cards = from.map((id) => {
    const n = titles[id] || { title: id, state: 'stopped' };
    const sup = g.links.filter((l) => l.from === id && l.kind === 'supply');
    const sell = g.links.filter((l) => l.from === id && l.kind === 'sell');
    return `<div class="card"><div class="row"><h3>${esc(n.title)}</h3><span class="spacer" style="flex:1"></span>
      <span class="badge s-${esc(n.state)}">${n.running ? 'работает' : 'не запущен'}</span></div>
      ${sup.length ? `<div class="small muted" style="margin-top:6px">Снабжает</div><ul class="supply">${sup.map(slLink).join('')}</ul>` : ''}
      ${sell.length ? `<div class="small muted" style="margin-top:6px">Продаёт</div><ul class="supply">${sell.map(slLink).join('')}</ul>` : ''}
    </div>`;
  }).join('');
  const bad = g.links.filter((l) => ['bad', 'down'].includes(l.status)).length;
  const head = bad ? `<span class="lv-critical">Связей с проблемой: ${bad}</span>` : '<span class="lv-ok">Связи в порядке</span>';
  const unused = g.unused.length ? `<div class="muted small" style="margin-top:8px">Не используется: ${g.unused.map(esc).join('; ')}</div>` : '';
  const sec = `<div class="muted small" style="margin-top:6px">Общий секрет поставщиков: ${esc(SL_SRC[g.secret.source] || 'ещё не создан')} — вписывать его никуда не нужно.
    <button class="btn small" onclick="supplyNewSecret()">Создать новый</button></div>`;
  $('#supply').innerHTML = `<div class="row"><h2>Схема Орбиты: кто кого снабжает</h2><span class="spacer" style="flex:1"></span>${head}</div>
    <div class="grid">${cards || '<div class="card muted">Включённых ботов нет</div>'}</div>${unused}${sec}`;
}
async function supplyNewSecret() {
  if (!confirm('Создать новый общий секрет поставщиков? Магазин и поставщиков потом нужно перезапустить.')) return;
  const r = await api('/api/supply/secret', {});
  flash(r.message, !r.ok);
  refreshSupply();
}
refreshSupply();
setInterval(refreshSupply, 15000);
