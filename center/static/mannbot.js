// Настройки MannBot из панели центра: те же поля, что в панели самого MannBot (/api/mannbot/settings).
// Секреты (golden_key, пароль и ключи Steam, токен Telegram) центр хранит в зашифрованном виде.
(() => {
  const dlg = document.createElement('dialog');
  dlg.id = 'mannDlg';
  dlg.className = 'wide';
  document.body.appendChild(dlg);
  let data = null, open = new Set(['funpay', 'steam']);

  const fieldHtml = (sec, f) => {
    const name = `${sec}__${f.key}`, help = f.help ? `<div class="muted small">${esc(f.help)}</div>` : '';
    if (f.type === 'bool') return `<label class="check"><input type="checkbox" name="${name}" ${f.value ? 'checked' : ''}> ${esc(f.label)}</label>${help}`;
    let input;
    if (f.type === 'secret') input = `<input name="${name}" type="password" autocomplete="new-password" placeholder="${f.set ? 'задан — оставьте пустым, чтобы не менять' : 'не задан'}">`;
    else if (f.type.startsWith('choice:')) input = `<select name="${name}">${f.type.slice(7).split('|').map((o) => `<option ${o === String(f.value) ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
    else if (f.type === 'list') input = `<textarea name="${name}" rows="3">${esc((f.value || []).join('\n'))}</textarea>`;
    else input = `<input name="${name}" value="${esc(f.value ?? '')}" ${f.type === 'int' || f.type === 'float' ? 'inputmode="decimal"' : ''} autocomplete="off">`;
    return `<label>${esc(f.label)}${f.type === 'secret' && f.set ? ' <span class="muted small">✓ задан</span>' : ''}</label>${input}${help}`;
  };

  const lotRow = (l = {}) => `<div class="row lot"><input placeholder="offer_id лота" value="${esc(l.offer_id ?? '')}" data-k="offer_id" style="flex:2">
    <input placeholder="node_id" value="${esc(l.node_id ?? 1808)}" data-k="node_id" style="flex:1"><input placeholder="game_id" value="${esc(l.game_id ?? 435)}" data-k="game_id" style="flex:1">
    <input placeholder="ключей за 1 шт." value="${esc(l.keys_per_unit ?? 1)}" data-k="keys_per_unit" style="flex:1"></div>`;

  function render() {
    const probs = data.problems?.length ? `<div class="detail">Не хватает: ${data.problems.map(esc).join('; ')}</div>` : '<div class="muted small">Все обязательные настройки заполнены.</div>';
    dlg.innerHTML = `<form method="dialog" id="mannForm">
      <div class="row"><h3>Настройки MannBot</h3><span class="spacer" style="flex:1"></span><button class="btn" value="cancel" formnovalidate aria-label="Закрыть">✕</button></div>
      <p class="muted small">Секреты хранятся в папке центра в зашифрованном виде (Windows: DPAPI текущего пользователя). Сохранение с новыми секретами перезапускает процесс MannBot.</p>
      ${probs}
      <details ${open.has('lots') ? 'open' : ''} data-sec="lots"><summary>Лоты FunPay с ключами</summary>
        <div id="mannLots">${(data.lots.length ? data.lots : [{}]).map(lotRow).join('')}</div>
        <button class="btn" type="button" id="mannAddLot">+ лот</button></details>
      <details ${open.has('mafile') ? 'open' : ''} data-sec="mafile"><summary>maFile Steam Guard</summary>
        <p class="muted small">Файл из Steam Desktop Authenticator (без шифрования): заполнит логин, SteamID, shared_secret и identity_secret.</p>
        <input type="file" id="mannMafile" accept=".maFile,.json,application/json"></details>
      ${data.sections.map((s) => `<details ${open.has(s.id) ? 'open' : ''} data-sec="${esc(s.id)}"><summary>${esc(s.title)}</summary>${s.fields.map((f) => fieldHtml(s.id, f)).join('')}</details>`).join('')}
      <div class="row" style="margin-top:14px"><button class="btn primary" value="save">Сохранить</button><button class="btn" value="cancel" formnovalidate>Отмена</button></div>
    </form>`;
    dlg.querySelector('#mannAddLot').onclick = () => dlg.querySelector('#mannLots').insertAdjacentHTML('beforeend', lotRow());
    dlg.querySelectorAll('details').forEach((d) => d.addEventListener('toggle', () => (d.open ? open.add(d.dataset.sec) : open.delete(d.dataset.sec))));
  }

  window.openMann = async () => {
    flash('Загружаю настройки MannBot…');
    const r = await fetch('/api/mannbot/settings');
    const d = await r.json();
    if (!r.ok) return flash(d.error || 'MannBot не ответил', true);
    flash('');
    data = d;
    render();
    dlg.showModal();
  };

  dlg.addEventListener('close', async () => {
    if (dlg.returnValue !== 'save') return;
    const form = dlg.querySelector('#mannForm'), values = {};
    for (const s of data.sections) {
      for (const f of s.fields) {
        const el = form.elements[`${s.id}__${f.key}`];
        if (!el) continue;
        const v = f.type === 'bool' ? el.checked : el.value;
        if (f.type === 'secret' && !v) continue;
        (values[s.id] ||= {})[f.key] = v;
      }
    }
    const lots = [...dlg.querySelectorAll('.lot')].map((row) => Object.fromEntries([...row.querySelectorAll('input')].map((i) => [i.dataset.k, i.value.trim()]))).filter((l) => l.offer_id);
    const body = { values, lots };
    const file = dlg.querySelector('#mannMafile').files[0];
    if (file) body.mafile = await file.text();
    flash('Сохраняю…');
    const r = await fetch('/api/mannbot/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json();
    flash(d.ok ? d.message + (d.problems?.length ? `. Осталось заполнить: ${d.problems.join('; ')}` : '') : d.error, !d.ok);
    setTimeout(refresh, 1500);
  });
})();
