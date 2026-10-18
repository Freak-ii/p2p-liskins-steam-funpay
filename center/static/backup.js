// Раздел «Резервные копии» панели центра: последняя копия, список с загрузкой, копия сейчас,
// расписание и папка, восстановление (центр останавливается и восстанавливает копию при следующем запуске).
const fmtSize = (n) => n == null ? '—' : n < 1e6 ? `${Math.max(1, Math.round(n / 1e3))} КБ` : `${(n / 1e6).toFixed(1)} МБ`;
const BK_LABEL = { '': 'по расписанию', manual: 'вручную', 'before-restore': 'перед восстановлением' };

async function refreshBackup() {
  let st;
  try { st = await api('/api/backup'); } catch { return; }
  const l = st.last;
  const last = !l ? '<span class="lv-warn">⚠ копий ещё не было</span>'
    : l.ok ? `<span class="lv-ok">✓ последняя копия</span> <span class="muted">${fmtTs(l.at)} · ${fmtSize(l.size)} · файлов ${l.files}</span>`
      : `<span class="lv-critical">✗ копия не удалась ${fmtTs(l.at)}: ${esc(l.error || (l.problems || []).join('; '))}</span>`;
  const plan = st.enabled ? `каждый день в ${esc(st.time)}, хранить ${st.keep}` : 'по расписанию выключено';
  const rows = st.backups.map((b) => `<tr><td>${fmtTs(b.ts)}</td><td>${esc(BK_LABEL[b.label] ?? b.label)}</td><td>${fmtSize(b.size)}</td>
    <td><a class="btn small" href="/api/backup/download/${encodeURIComponent(b.name)}">Скачать</a>
    <button class="btn small" onclick="restoreBackup('${esc(b.name)}', ${b.ts})">Восстановить</button></td></tr>`).join('')
    || '<tr><td colspan="4" class="muted">Копий нет</td></tr>';
  $('#backup').innerHTML = `<div class="card">
    <div class="row small">${last} · <span class="muted">${plan}</span><span class="spacer" style="flex:1"></span>
      <button class="btn primary" onclick="runBackup()" ${st.running ? 'disabled' : ''}>${st.running ? 'Делается…' : 'Сделать копию сейчас'}</button>
      <button class="btn" onclick="openBackupSettings()">Расписание и папка</button></div>
    <div class="muted small" style="margin-top:6px">Папка: ${esc(st.dir_effective)}. В копии базы и настройки центра и обоих ботов; ключи хранятся зашифрованными.</div>
    <details style="margin-top:8px"><summary>Копии (${st.backups.length})</summary>
      <div class="tablewrap"><table><thead><tr><th>Когда</th><th>Вид</th><th>Размер</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></details>
  </div>`;
}

async function runBackup() {
  flash('Делаю резервную копию…');
  const r = await api('/api/backup/run', {});
  flash(r.message, !r.ok); refreshBackup();
}

async function restoreBackup(name, ts) {
  const when = fmtTs(ts);
  if (!confirm(`Восстановить копию от ${when}?\n\nЦентр остановит ботов и закроется. При следующем запуске данные центра и ботов вернутся к состоянию на ${when}; текущие данные сохранятся отдельной копией.`)) return;
  const r = await api('/api/backup/restore', { name, confirm: 'ВОССТАНОВИТЬ' });
  if (!r.ok) { flash(r.message, true); return; }
  document.body.innerHTML = `<main><div class="card">${esc(r.message)}</div></main>`;
}

async function openBackupSettings() {
  const s = await api('/api/backup/settings');
  const f = $('#bkForm');
  f.elements.enabled.checked = s.enabled; f.elements.time.value = s.time; f.elements.keep.value = s.keep; f.elements.dir.value = s.dir;
  f.elements.dir.placeholder = s.dir_effective;
  $('#bkDlg').showModal();
}

document.body.insertAdjacentHTML('beforeend', `
<dialog id="bkDlg"><form method="dialog" id="bkForm">
  <h3>Резервные копии</h3>
  <p class="muted small">Центр каждый день сохраняет базы и настройки центра и обоих ботов в zip-файл. Если компьютер был выключен в это время, копия делается при следующем запуске.</p>
  <label><input type="checkbox" name="enabled" style="width:auto"> Делать копию каждый день</label>
  <label>Время</label><input name="time" placeholder="04:00">
  <label>Сколько копий хранить</label><input name="keep" type="number" min="1" max="365">
  <label>Папка для копий (пусто — папка backups в данных центра). Папка Яндекс Диска или Google Drive сохранит копии и при поломке компьютера.</label><input name="dir" autocomplete="off">
  <div class="row" style="margin-top:14px">
    <button class="btn primary" value="save">Сохранить</button>
    <button class="btn" value="cancel" formnovalidate>Отмена</button>
  </div>
</form></dialog>`);

$('#bkDlg').addEventListener('close', async () => {
  if ($('#bkDlg').returnValue !== 'save') return;
  const f = $('#bkForm');
  const r = await api('/api/backup/settings', { enabled: f.elements.enabled.checked, time: f.elements.time.value, keep: Number(f.elements.keep.value), dir: f.elements.dir.value });
  flash(r.message, !r.ok); refreshBackup();
});

refreshBackup();
setInterval(refreshBackup, 60000);
