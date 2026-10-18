// Карточка «Готовность к боевому режиму»: что не настроено или настроено опасно (одинаковый аккаунт Steam у ботов,
// общий Telegram-бот, нет ключей). Свёрнута, когда проблем нет. Использует $, esc, api из index.html.
const RD = { fail: ['⛔', 'lv-critical'], warn: ['⚠', 'lv-warn'], info: ['ℹ', 'muted'], ok: ['✓', 'lv-ok'] };

async function refreshReadiness() {
  let r;
  try { r = await api('/api/readiness'); } catch { return; }
  if (r.demo) { $('#readiness').innerHTML = ''; return; }
  const c = r.counts;
  const head = c.fail ? `<span class="lv-critical">⛔ К боевому режиму не готово: проблем ${c.fail}</span>`
    : c.warn ? `<span class="lv-warn">⚠ К боевому режиму готово, но стоит проверить: ${c.warn}</span>`
      : '<span class="lv-ok">✓ К боевому режиму всё готово</span>';
  const rows = r.items.map((i) => `<li class="${RD[i.level][1]}">${RD[i.level][0]} ${esc(i.title)}
    ${i.detail ? `<div class="muted small">${esc(i.detail)}</div>` : ''}${i.where ? `<div class="muted small">Где: ${esc(i.where)}</div>` : ''}</li>`).join('');
  const open = $('#readiness details')?.open ?? (c.fail > 0);
  $('#readiness').innerHTML = `<details class="card" style="margin-top:14px" ${open ? 'open' : ''}><summary>${head}</summary>
    <ul class="readiness">${rows}</ul></details>`;
}
refreshReadiness();
setInterval(refreshReadiness, 60000);

// Новая версия центра (/api/update): полоска со ссылкой на выпуск, центр сам ничего не скачивает
async function refreshUpdate() {
  let u;
  try { u = await api('/api/update'); } catch { return; }
  $('#update').innerHTML = u.newer ? `<div class="card" style="margin-top:14px">⬆ Вышла версия центра <b>${esc(u.latest)}</b> (у вас ${esc(u.current)}).
    <a href="${esc(u.url)}" target="_blank" rel="noopener">Скачать</a> — выйдите из центра и распакуйте архив туда же с заменой файлов: настройки и базы в папке <code>data</code> останутся.</div>` : '';
}
refreshUpdate();
setInterval(refreshUpdate, 3600000);
