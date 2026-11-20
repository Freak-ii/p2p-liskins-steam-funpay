// Раздел «ИИ-координатор» панели центра: брифинг, предложения на одобрение, вопрос координатору,
// журнал решений, уровни полномочий и ключ Claude API. Данные — /api/coordinator/*.
const CO_STATUS = { done: 'сделано', failed: 'не удалось', proposed: 'ждёт решения', approved: 'одобрено', rejected: 'отклонено',
  expired: 'устарело', manual: 'выполнить вручную', info: '' };
const CO_KIND = { action: 'Сделал сам', proposal: 'Предложение', briefing: 'Брифинг', answer: 'Вопрос', anomaly: 'Аномалия' };
const CO_TIER = { auto: 'делает сам', propose: 'предлагает', confirm: 'нужно подтверждение', manual: 'только вручную', off: 'выключено' };
let coLast = null;

function coProposal(p) {
  const strict = p.tier === 'confirm' || p.tier === 'manual';
  return `<div class="co-prop ${strict ? 'co-strict' : ''}">
    <div class="row"><b>${esc(p.title)}</b><span class="spacer" style="flex:1"></span>
      <span class="badge co-t-${esc(p.tier)}">${esc(CO_TIER[p.tier] || p.tier)}</span></div>
    ${p.reason ? `<div class="muted small">${esc(p.reason)}</div>` : ''}
    <div class="row" style="margin-top:6px">
      <button class="btn ${strict ? 'danger' : 'primary'} small" onclick="coDecide(${p.id}, true, '${esc(p.tier)}')">${p.tier === 'manual' ? 'Одобрить (сделаю сам)' : 'Одобрить'}</button>
      <button class="btn small" onclick="coDecide(${p.id}, false)">Отклонить</button>
      <span class="muted small">${fmtTs(p.ts)} · ${p.source?.startsWith('rule:') ? 'сторож аномалий' : p.source === 'ai' ? 'ИИ' : 'сводка'}</span>
    </div></div>`;
}

function coBriefing(b) {
  if (!b) return '<div class="muted">Брифингов ещё не было. Нажмите «Брифинг сейчас».</div>';
  const hl = b.data?.highlights || [];
  const recos = (coLast?.journal || []).filter((d) => (b.data?.recommendations || []).includes(d.id) && d.status === 'info');
  return `<div class="muted small">${fmtTs(b.ts)} · ${b.source === 'ai' ? esc(b.data?.meta?.model || 'ИИ') : 'без ИИ'}${b.data?.meta?.error ? ' · ' + esc(b.data.meta.error) : ''}</div>
    <p style="margin:6px 0">${esc(b.result)}</p>
    ${hl.length ? `<ul class="co-list">${hl.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    ${recos.length ? `<div class="muted small">Советы без действия в центре:</div><ul class="co-list">${recos.map((r) => `<li>${esc(r.title)}${r.reason ? ` <span class="muted">— ${esc(r.reason)}</span>` : ''}</li>`).join('')}</ul>` : ''}`;
}

async function refreshCoord() {
  let s;
  try { s = await api('/api/coordinator/status'); } catch { return; }
  coLast = s;
  const el = $('#coordinator');
  if (!s.enabled) { el.innerHTML = '<div class="card muted">Координатор выключен в center.yaml (coordinator.enabled).</div>'; return; }
  const journal = s.journal.filter((d) => d.kind !== 'briefing').map((d) => `<tr>
    <td>${fmtTs(d.ts)}</td><td>${esc(CO_KIND[d.kind] || d.kind)}</td>
    <td>${esc(d.title)}${d.reason ? `<div class="muted small">${esc(d.reason)}</div>` : ''}${d.kind === 'answer' ? `<div class="small">${esc(d.result)}</div>` : ''}</td>
    <td class="co-s-${esc(d.status)}">${esc(CO_STATUS[d.status] ?? d.status)}${d.result && d.kind !== 'answer' ? `<div class="muted small">${esc(d.result)}</div>` : ''}</td></tr>`).join('');
  const lc = s.last_check?.at ? `проверка ${fmtTs(s.last_check.at)}: ${s.last_check.anomalies.length ? esc(s.last_check.anomalies.join(', ')) : 'аномалий нет'}` : 'сторож аномалий запускается';
  el.innerHTML = `
    <div class="row" style="margin-bottom:8px">
      <span class="badge ${s.ai ? 's-running' : 's-stopped'}">${esc(s.mode)}</span>
      <span class="muted small">${lc}</span><span class="spacer" style="flex:1"></span>
      <button class="btn" onclick="coBrief()" ${s.busy ? 'disabled' : ''}>Брифинг сейчас</button>
      <button class="btn" onclick="coCheck()">Проверить аномалии</button>
      <button class="btn" onclick="openCoSettings()">Полномочия и ключ</button>
    </div>
    <div class="grid">
      <div class="card"><h3>Брифинг</h3>${coBriefing(s.briefing)}</div>
      <div class="card"><h3>Ждут вашего решения (${s.proposals.length})</h3>
        ${s.proposals.map(coProposal).join('') || '<div class="muted">Предложений нет</div>'}</div>
    </div>
    <div class="card" style="margin-top:14px">
      <form class="row" onsubmit="coAsk(event)"><input id="coQ" placeholder="Спросить координатора: почему упала прибыль? что поменять в стратегии?" style="flex:1;min-width:200px">
        <button class="btn primary">Спросить</button></form>
      <div id="coAnswer"></div>
    </div>
    <details class="card" style="margin-top:14px"><summary>Журнал решений координатора (${s.journal.length})</summary>
      <div class="scroll"><table><thead><tr><th>Время</th><th>Тип</th><th>Что</th><th>Итог</th></tr></thead><tbody>${journal}</tbody></table></div></details>`;
}

async function coDecide(id, approve, tier) {
  const body = {};
  if (approve && tier === 'confirm' && !confirm('Это действие меняет боевой режим или лимиты. Подтверждаете?')) return;
  if (approve && tier === 'manual' && !confirm('Центр это не выполняет. Отметить как одобренное, чтобы сделать вручную?')) return;
  if (approve && (tier === 'confirm' || tier === 'manual')) body.confirm = true;
  const r = await api(`/api/coordinator/decisions/${id}/${approve ? 'approve' : 'reject'}`, body);
  flash(r.message, !r.ok); refreshCoord(); setTimeout(refresh, 800);
}
async function coBrief() {
  flash('Координатор готовит брифинг…');
  const r = await api('/api/coordinator/briefing', { send: false });
  flash(r.ok ? 'Брифинг готов' : r.message, !r.ok); refreshCoord();
}
async function coCheck() { const r = await api('/api/coordinator/check', {}); flash(r.message, !r.ok); refreshCoord(); }
async function coAsk(ev) {
  ev.preventDefault();
  const q = $('#coQ').value.trim();
  if (!q) return;
  $('#coAnswer').innerHTML = '<div class="muted small" style="margin-top:8px">Думаю…</div>';
  const r = await api('/api/coordinator/ask', { question: q });
  if (!r.ok) { flash(r.message, true); $('#coAnswer').innerHTML = ''; return; }
  const props = r.recommendations.filter((p) => p && p.status === 'proposed');
  await refreshCoord();
  $('#coQ').value = q;
  $('#coAnswer').innerHTML = `<p class="co-answer">${esc(r.answer.result)}</p>${props.length ? `<div class="muted small">Добавлено в «Ждут вашего решения»: ${props.map((p) => esc(p.title)).join('; ')}</div>` : ''}`;
}

async function openCoSettings() {
  const s = await api('/api/coordinator/settings');
  const f = $('#coForm');
  f.elements.api_key.value = '';
  $('#coKeyHint').textContent = s.ai.api_key_set ? `(сохранён ${s.ai.api_key_hint} — оставьте пустым, чтобы не менять)` : '(не задан — брифинги без ИИ)';
  f.elements.ai_enabled.checked = s.ai.enabled;
  f.elements.model.value = s.ai.model;
  f.elements.effort.value = s.ai.effort;
  f.elements.br_enabled.checked = s.briefing.enabled !== false;
  f.elements.br_time.value = s.briefing.time || '09:05';
  f.elements.br_tg.checked = s.briefing.telegram !== false;
  $('#coPerms').innerHTML = s.permissions.map((p) => {
    const opts = ['auto', 'propose', 'confirm', 'manual', 'off']
      .filter((t) => (t !== 'auto' || p.auto_allowed) && (!p.locked || ['confirm', 'manual', 'off'].includes(t)) && !(p.tool === 'withdraw' && t === 'confirm'))
      .map((t) => `<option value="${t}" ${t === p.tier ? 'selected' : ''}>${CO_TIER[t]}</option>`).join('');
    return `<tr><td>${esc(p.title)}${p.locked ? ' <span class="muted small">🔒</span>' : ''}</td><td><select name="perm_${p.tool}">${opts}</select></td></tr>`;
  }).join('');
  $('#coDlg').showModal();
}

document.body.insertAdjacentHTML('beforeend', `
<dialog id="coDlg"><form method="dialog" id="coForm">
  <h3>ИИ-координатор</h3>
  <p class="muted small">ИИ сам ничего не выполняет: его советы становятся предложениями. Сам координатор делает только то, что помечено «делает сам», и только при явной аномалии (по умолчанию — пауза закупок LisBot). Боевой режим и лимиты всегда требуют вашего подтверждения, вывод денег — только вручную (🔒).</p>
  <label>Ключ Claude API <span class="muted small" id="coKeyHint"></span></label><input name="api_key" autocomplete="off" placeholder="sk-ant-…">
  <label><input type="checkbox" name="ai_enabled" style="width:auto"> Использовать ИИ (иначе брифинг по правилам)</label>
  <div class="row"><div style="flex:2"><label>Модель</label><input name="model"></div>
    <div style="flex:1"><label>Глубина (effort)</label><select name="effort"><option>low</option><option>medium</option><option>high</option><option>xhigh</option><option>max</option></select></div></div>
  <label><input type="checkbox" name="br_enabled" style="width:auto"> Ежедневный брифинг</label>
  <div class="row"><div style="flex:1"><label>Время брифинга</label><input name="br_time" placeholder="09:05"></div>
    <label style="flex:2;margin-top:24px"><input type="checkbox" name="br_tg" style="width:auto"> Присылать в Telegram</label></div>
  <h3 style="margin-top:14px">Полномочия</h3>
  <table><tbody id="coPerms"></tbody></table>
  <div class="row" style="margin-top:14px">
    <button class="btn primary" value="save">Сохранить</button>
    <button class="btn" value="cancel" formnovalidate>Отмена</button>
  </div>
</form></dialog>`);

$('#coDlg').addEventListener('close', async () => {
  if ($('#coDlg').returnValue !== 'save') return;
  const f = $('#coForm');
  const permissions = {};
  for (const el of f.elements) if (el.name?.startsWith('perm_')) permissions[el.name.slice(5)] = el.value;
  const ai = { enabled: f.elements.ai_enabled.checked, model: f.elements.model.value, effort: f.elements.effort.value };
  if (f.elements.api_key.value.trim()) ai.api_key = f.elements.api_key.value.trim();
  const r = await api('/api/coordinator/settings', { ai, permissions,
    briefing: { enabled: f.elements.br_enabled.checked, time: f.elements.br_time.value, telegram: f.elements.br_tg.checked } });
  flash(r.message, !r.ok); refreshCoord();
});

refreshCoord();
setInterval(refreshCoord, 20000);
