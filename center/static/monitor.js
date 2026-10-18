// Раздел «Мониторинг» панели центра: аптайм и время ответа ботов, уведомления, сторожевой пинг,
// настройки Telegram и тихих часов, ежедневный отчёт, подтверждение тревог. Данные — /api/monitor/*.
let monHours = 24, monHist = null;
const stripTags = (s) => String(s ?? '').replace(/<[^>]+>/g, '');
const NOTE_STATUS = { sent: 'отправлено', failed: 'ошибка', no_channel: 'только в панели', held: 'тихие часы', duplicate: 'повтор скрыт' };

async function ackAlert(source, id) {
  const r = await api('/api/alerts/ack', { source, id });
  flash(r.message, !r.ok); setTimeout(refresh, 500);
}
async function ackAll() {
  const r = await api('/api/alerts/ack-all', {});
  flash(r.message, !r.ok); setTimeout(refresh, 500);
}

function availColor(b) {
  if (b.avail == null) return 'var(--line)';
  return b.avail >= 0.999 ? 'var(--ok)' : b.avail > 0 ? 'var(--warn)' : 'var(--bad)';
}
function fmtBucket(t, step) {
  const o = step >= 3600 ? { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' } : { hour: '2-digit', minute: '2-digit' };
  return new Date(t * 1000).toLocaleString('ru-RU', o);
}

// Один бот = одна карточка: полоса доступности (статус + подпись в подсказке) и линия времени ответа.
function botChart(name, b, h) {
  const W = 600, H = 90, n = b.buckets.length, cw = W / n;
  const lats = b.buckets.map((x) => x.lat).filter((v) => v != null);
  const max = Math.max(50, ...lats) * 1.15;
  const y = (v) => H - 4 - (v / max) * (H - 12);
  let path = '', pen = false, dots = '';
  b.buckets.forEach((x, i) => {
    if (x.lat == null) { pen = false; return; }
    path += `${pen ? 'L' : 'M'}${(i + 0.5) * cw},${y(x.lat).toFixed(1)}`; pen = true;
    const prev = b.buckets[i - 1]?.lat, next = b.buckets[i + 1]?.lat;
    if (prev == null && next == null) dots += `<circle cx="${(i + 0.5) * cw}" cy="${y(x.lat).toFixed(1)}" r="4" class="mon-dot"/>`;
  });
  const cells = b.buckets.map((x, i) => `<rect x="${i * cw + 0.5}" y="0" width="${Math.max(1, cw - 1)}" height="10" rx="1.5" fill="${availColor(x)}"/>`).join('');
  const grid = [0.5, 1].map((f) => `<line x1="0" x2="${W}" y1="${y(max / 1.15 * f)}" y2="${y(max / 1.15 * f)}" class="mon-grid"/>`).join('');
  return `<div class="card mon-bot">
    <div class="row"><h3>${esc(b.title)}</h3></div>
    <div class="kpi mon-kpi">
      <div><b>${b.uptime == null ? '—' : (b.uptime * 100).toFixed(1) + '%'}</b><span>аптайм</span></div>
      <div><b>${b.latency_avg == null ? '—' : Math.round(b.latency_avg) + ' мс'}</b><span>ответ, среднее</span></div>
      <div><b>${b.latency_p95 == null ? '—' : Math.round(b.latency_p95) + ' мс'}</b><span>ответ, 95%</span></div>
      <div><b>${b.restarts}</b><span>перезапусков</span></div>
      <div><b>${b.crashes + b.unresponsive}</b><span>падений и зависаний</span></div>
    </div>
    <div class="muted small">Доступность</div>
    <svg class="mon-strip" viewBox="0 0 ${W} 10" preserveAspectRatio="none" data-bot="${name}">${cells}</svg>
    <div class="muted small" style="margin-top:6px">Время ответа API, мс <span class="mon-max">до ${Math.round(max / 1.15)}</span></div>
    <svg class="mon-line" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" data-bot="${name}">${grid}<path d="${path}" class="mon-path"/>${dots}<line class="mon-cross" x1="-10" x2="-10" y1="0" y2="${H}"/></svg>
    <div class="row muted small"><span>${fmtBucket(h.since, h.step)}</span><span class="spacer" style="flex:1"></span><span>сейчас</span></div>
  </div>`;
}

function bindHover() {
  const tip = document.getElementById('monTip');
  document.querySelectorAll('#monCharts svg').forEach((svg) => {
    const b = monHist.bots[svg.dataset.bot];
    const move = (ev) => {
      const r = svg.getBoundingClientRect();
      const i = Math.max(0, Math.min(b.buckets.length - 1, Math.floor((ev.clientX - r.left) / r.width * b.buckets.length)));
      const x = b.buckets[i];
      const avail = x.avail == null ? 'не запускался' : x.avail >= 0.999 ? 'отвечал' : x.avail > 0 ? `отвечал ${Math.round(x.avail * 100)}% проверок` : 'не отвечал';
      tip.innerHTML = `<b>${fmtBucket(x.t, monHist.step)}</b><br>${esc(avail)}<br>ответ: ${x.lat == null ? '—' : Math.round(x.lat) + ' мс'}`;
      tip.style.display = 'block';
      tip.style.left = Math.min(window.innerWidth - 180, ev.clientX + 12) + 'px';
      tip.style.top = (ev.clientY + 12) + 'px';
      const cross = svg.querySelector('.mon-cross');
      if (cross) { const cx = (i + 0.5) * 600 / b.buckets.length; cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); }
    };
    svg.addEventListener('mousemove', move);
    svg.addEventListener('mouseleave', () => { tip.style.display = 'none'; svg.querySelector('.mon-cross')?.setAttribute('x1', -10); svg.querySelector('.mon-cross')?.setAttribute('x2', -10); });
  });
}

async function refreshMonitor() {
  let st;
  try { [st, monHist] = await Promise.all([api('/api/monitor/status'), api(`/api/monitor/history?hours=${monHours}`)]); } catch { return; }
  const tg = st.telegram.ok ? `<span class="lv-ok">✓ Telegram</span> <span class="muted">(${esc(st.telegram.source)})</span>` : '<span class="lv-warn">⚠ Telegram не настроен</span> <span class="muted">— уведомления видны только здесь</span>';
  const hb = st.heartbeat.ok == null ? `<span class="muted">сторожевой пинг: ${esc(st.heartbeat.info)}</span>`
    : st.heartbeat.ok ? `<span class="lv-ok">✓ сторожевой пинг</span> <span class="muted">${fmtTs(st.heartbeat.at)}</span>` : `<span class="lv-critical">✗ сторожевой пинг: ${esc(st.heartbeat.info)}</span>`;
  const quiet = st.quiet_hours ? `тихие часы ${esc(st.quiet_hours)}${st.quiet_now ? ` (сейчас, отложено ${st.held})` : ''}` : 'без тихих часов';
  const disk = (st.disk?.disks || []).map((x) => `<span class="${x.low ? 'lv-warn' : 'muted'}" title="${esc(x.path)}">${x.low ? '⚠ ' : ''}${esc(x.label)}: свободно ${x.free_gb.toLocaleString('ru-RU')} ГБ</span>`).join(' · ');
  const rep = st.daily_report.enabled ? `отчёт в ${esc(st.daily_report.time)}` : 'отчёт выключен';
  const tabs = [[24, '24 ч'], [168, '7 дней'], [720, '30 дней']].map(([h, t]) => `<button class="btn ${h === monHours ? 'on' : ''}" onclick="monHours=${h};refreshMonitor()">${t}</button>`).join('');
  const notes = st.notifications.map((x) => `<tr><td>${fmtTs(x.ts)}</td><td class="lv-${esc(x.level)}">${esc(NOTE_STATUS[x.status] || x.status)}</td><td>${esc(stripTags(x.text)).replace(/\n/g, ' · ')}</td></tr>`).join('')
    || '<tr><td colspan="3" class="muted">Уведомлений пока не было</td></tr>';
  $('#monitor').innerHTML = `
    <div class="card"><div class="row small">${tg} · ${hb} · <span class="muted">${quiet} · ${rep}</span>${disk ? ` · ${disk}` : ''}
      <span class="spacer" style="flex:1"></span>
      <button class="btn" onclick="openMonSettings()">Настройки уведомлений</button>
      <button class="btn" onclick="monTest()">Проверить</button>
      <button class="btn" onclick="openReport()">Отчёт за сутки</button>
      <button class="btn" onclick="openReport('week')">За неделю</button></div></div>
    <div class="tabs" style="margin-top:10px">${tabs}</div>
    <div class="grid" id="monCharts">${Object.entries(monHist.bots).map(([n, b]) => botChart(n, b, monHist)).join('')}</div>
    <details class="card" style="margin-top:14px"><summary>Уведомления центра (${st.notifications.length})</summary>
      <table><thead><tr><th>Время</th><th>Статус</th><th>Текст</th></tr></thead><tbody>${notes}</tbody></table></details>`;
  bindHover();
}

async function monTest() { flash('Отправляю…'); const r = await api('/api/monitor/test', {}); flash(r.message, !r.ok); refreshMonitor(); }

let repPeriod = '';
async function openReport(period = '') {
  repPeriod = period;
  const r = await api(`/api/monitor/report${period ? '?period=' + period : ''}`);
  $('#repTitle').textContent = period === 'week' ? 'Отчёт центра за неделю' : 'Отчёт центра за сутки';
  $('#repText').innerHTML = esc(r.text).replace(/&lt;(\/?)(b|i|code)&gt;/g, '<$1$2>');
  $('#repDlg').showModal();
}

const MON_FIELDS = ['telegram_enabled', 'telegram_token', 'telegram_chat_id', 'telegram_commands', 'dedup_minutes', 'quiet_hours', 'quiet_allow_critical',
  'bot_alert_levels', 'heartbeat_url', 'heartbeat_seconds', 'fail_when_bot_down', 'daily_report_enabled', 'daily_report_time'];
async function openMonSettings() {
  const s = await api('/api/monitor/settings');
  const f = $('#monForm');
  for (const k of MON_FIELDS) {
    const el = f.elements[k];
    if (el.type === 'checkbox') el.checked = !!s[k]; else el.value = Array.isArray(s[k]) ? s[k].join(', ') : (s[k] ?? '');
  }
  $('#monTokHint').textContent = s.telegram_token_set ? '(сохранён — оставьте пустым, чтобы не менять)' : s.telegram_source === 'LisBot' ? '(сейчас берётся из подключений LisBot)' : '';
  const cs = s.telegram_commands_state || {};
  $('#monCmdHint').textContent = cs.info ? `(${cs.info})` : '';
  $('#monDlg').showModal();
}

document.body.insertAdjacentHTML('beforeend', `
<div id="monTip" class="mon-tip"></div>
<dialog id="monDlg"><form method="dialog" id="monForm">
  <h3>Уведомления центра</h3>
  <p class="muted small">Падения ботов, «бот не отвечает», критические тревоги и ежедневный отчёт. Если токен и chat id пусты, используются те, что заданы в подключениях LisBot.</p>
  <label><input type="checkbox" name="telegram_enabled" style="width:auto"> Отправлять в Telegram</label>
  <label>Токен Telegram-бота <span class="muted small" id="monTokHint"></span></label><input name="telegram_token" autocomplete="off">
  <label>Chat id (можно несколько через запятую)</label><input name="telegram_chat_id" autocomplete="off">
  <label><input type="checkbox" name="telegram_commands" style="width:auto"> Команды и кнопки одобрения в Telegram <span class="muted small" id="monCmdHint"></span></label>
  <p class="muted small" style="margin:2px 0 0">Работают со своим Telegram-ботом центра (не тем, что у LisBot) и только из указанных chat id: /status, /report, /stock, /proposals, /pause, /resume, /buypause, /buyresume, /backup, /goal, /ready.</p>
  <label>Не повторять одинаковое уведомление чаще, чем раз в N минут</label><input name="dedup_minutes" type="number" min="0">
  <label>Тихие часы, например 23:00-08:00 (пусто — без тихих часов)</label><input name="quiet_hours" placeholder="23:00-08:00">
  <label><input type="checkbox" name="quiet_allow_critical" style="width:auto"> Критичное приходит и в тихие часы</label>
  <label>Какие тревоги ботов пересылать (critical, warn, info)</label><input name="bot_alert_levels" placeholder="critical">
  <label>Сторожевой URL (healthchecks.io и подобные)</label><input name="heartbeat_url" placeholder="https://hc-ping.com/…">
  <label>Пинговать каждые N секунд</label><input name="heartbeat_seconds" type="number" min="10">
  <label><input type="checkbox" name="fail_when_bot_down" style="width:auto"> Сообщать сервису о сбое (/fail), пока бот не работает</label>
  <label><input type="checkbox" name="daily_report_enabled" style="width:auto"> Ежедневный отчёт</label>
  <label>Время отчёта</label><input name="daily_report_time" placeholder="09:00">
  <div class="row" style="margin-top:14px">
    <button class="btn primary" value="save">Сохранить</button>
    <button class="btn" value="cancel" formnovalidate>Отмена</button>
  </div>
</form></dialog>
<dialog id="repDlg"><form method="dialog">
  <h3 id="repTitle">Отчёт центра за сутки</h3>
  <pre id="repText" class="mon-report"></pre>
  <div class="row" style="margin-top:14px">
    <button class="btn primary" value="send">Отправить в Telegram</button>
    <button class="btn" value="cancel">Закрыть</button>
  </div>
</form></dialog>`);

$('#monDlg').addEventListener('close', async () => {
  if ($('#monDlg').returnValue !== 'save') return;
  const f = $('#monForm'), data = {};
  for (const k of MON_FIELDS) { const el = f.elements[k]; data[k] = el.type === 'checkbox' ? el.checked : el.value; }
  const r = await api('/api/monitor/settings', data);
  flash(r.message, !r.ok); refreshMonitor();
});
$('#repDlg').addEventListener('close', async () => {
  if ($('#repDlg').returnValue !== 'send') return;
  const r = await api(`/api/monitor/report${repPeriod ? '?period=' + repPeriod : ''}`, {});
  flash(r.message, !r.ok); refreshMonitor();
});

refreshMonitor();
setInterval(refreshMonitor, 30000);
