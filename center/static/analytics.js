// Вкладка «Аналитика перепродажи» общей панели: P&L центра, дневной график, детали по ботам, экспорт.
// Использует $, esc, api из основного скрипта index.html.
const AN = { days: '30', cur: 'RUB', metric: 'profit', daily: null };
const BOTS = [['lisbot', 'LisBot'], ['mannbot', 'MannBot']];
const money = (v, cur = AN.cur) => v == null ? '—' : v.toLocaleString('ru-RU', { maximumFractionDigits: Math.abs(v) >= 1000 ? 0 : 2 }) + (cur === 'USD' ? ' $' : ' ₽');
const signed = (v, cur) => v == null ? '—' : `<span class="${v > 0 ? 'pos' : v < 0 ? 'neg' : ''}">${v > 0 ? '+' : ''}${money(v, cur)}</span>`;
const pct = (v) => v == null ? '—' : `${v.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
const dur = (s) => s == null ? '—' : s < 90 ? `${Math.round(s)} с` : s < 5400 ? `${(s / 60).toFixed(1)} мин` : `${(s / 3600).toFixed(1)} ч`;
const tabs = (el, opts, cur, fn) => { $(el).innerHTML = opts.map(([k, t]) => `<button class="btn ${k === cur ? 'on' : ''}" data-k="${k}">${t}</button>`).join(''); $(el).onclick = (e) => { const k = e.target.dataset?.k; if (k) fn(k); }; };

function anControls() {
  tabs('#anDays', [['7', '7 дн'], ['30', '30 дн'], ['90', '90 дн'], ['all', 'всё']], AN.days, (k) => { AN.days = k; anLoad(); });
  tabs('#anCur', [['RUB', '₽'], ['USD', '$']], AN.cur, (k) => { AN.cur = k; anLoad(); });
  tabs('#anMetric', [['profit', 'Прибыль'], ['revenue', 'Выручка'], ['deals', 'Сделки']], AN.metric, (k) => { AN.metric = k; anControls(); anChart(); });
  const q = AN.days === 'all' ? '' : `?days=${AN.days}`;
  $('#anCsv').href = `/api/analytics/export.csv${q}`; $('#anXlsx').href = `/api/analytics/export.xlsx${q}`;
}

function anPnl(p) {
  const c = AN.cur;
  const row = (title, b, cls = '') => `<tr class="${cls}"><td>${title}</td><td>${b.deals ?? ''}</td><td>${money(b[c].revenue)}</td>
    <td>${money(b[c].cost)}</td><td>${signed(b[c].profit)}</td><td>${pct(b[c].roi)}</td><td>${money(b[c].capital)}</td>
    <td>${b[c].turnover == null ? '—' : b[c].turnover.toLocaleString('ru-RU') + '×'}</td></tr>`;
  const r = p.rate;
  $('#anPnl').innerHTML = `<table class="pnl"><thead><tr><th>Бот</th><th>Сделок</th><th>Выручка</th><th title="Себестоимость проданного + комиссии площадок">Расходы</th><th>Прибыль</th>
      <th>ROI</th><th>Капитал сейчас</th><th title="Себестоимость проданного за период / капитал">Оборот капитала</th></tr></thead><tbody>
    ${BOTS.map(([k, t]) => row(`<span class="swatch" style="background:var(--s-${k})"></span>${t}`, p.bots[k])).join('')}
    ${row('Весь центр', p.total, 'total')}</tbody></table>
    <div class="muted small" style="margin-top:6px">Курс USD→RUB: ${r.rate.toFixed(2)} (${esc(r.source)}). Сделка считается в день продажи;
      тестовые покупки LisBot и возвраты MannBot не учитываются.</div>`;
  $('#anWarn').innerHTML = p.warnings.map((w) => `<div class="detail" style="margin-bottom:8px">${esc(w)}</div>`).join('');
}

function anChart() {
  const d = AN.daily; if (!d) return;
  const m = AN.metric, unit = m === 'deals' ? '' : d.currency;
  const fmt = (v) => m === 'deals' ? String(v) : money(v, unit);
  const ser = d.series;
  $('#anChartTitle').textContent = { profit: 'Прибыль по дням', revenue: 'Выручка по дням', deals: 'Сделки по дням' }[m];
  $('#anLegend').innerHTML = BOTS.map(([k, t]) => `<span><span class="swatch" style="background:var(--s-${k})"></span>${t}</span>`).join('');
  const W = Math.max(320, $('#anChart').clientWidth || 800), H = 240, L = 56, B = 22, T = 8;
  let hi = 0, lo = 0;
  for (const s of ser) {
    let p = 0, n = 0;
    for (const [k] of BOTS) { const v = s[`${k}_${m}`] || 0; if (v >= 0) p += v; else n += v; }
    hi = Math.max(hi, p); lo = Math.min(lo, n);
  }
  if (hi === lo) hi = 1;
  const span = hi - lo, ph = H - B - T, y = (v) => T + (hi - v) / span * ph;
  const step = (W - L) / ser.length, bw = Math.max(2, Math.min(28, step - 4));
  const ticks = [lo, lo + span / 2, hi].filter((v, i, a) => a.indexOf(v) === i);
  if (lo < 0 && hi > 0 && !ticks.includes(0)) ticks.push(0);
  let svg = ticks.map((v) => `<line x1="${L}" x2="${W}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/>
    <text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${esc(fmt(Math.round(v * 100) / 100))}</text>`).join('');
  const labEvery = Math.ceil(ser.length / 8);
  ser.forEach((s, i) => {
    const x = L + i * step + (step - bw) / 2;
    let up = 0, down = 0;
    for (const [k] of BOTS) {
      const v = s[`${k}_${m}`] || 0; if (!v) continue;
      const a = v >= 0 ? up : down, b = a + v;
      if (v >= 0) up = b; else down = b;
      const top = y(Math.max(a, b)), h = Math.max(1, Math.abs(y(a) - y(b)) - 1);   // 1px зазор между сегментами
      svg += `<rect x="${x}" y="${top}" width="${bw}" height="${h}" rx="2" fill="var(--s-${k})"/>`;
    }
    svg += `<rect class="hit" data-i="${i}" x="${L + i * step}" y="${T}" width="${step}" height="${ph}" fill="transparent"/>`;
    if (i % labEvery === 0) svg += `<text x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${s.day.slice(8)}.${s.day.slice(5, 7)}</text>`;
  });
  svg += `<line x1="${L}" x2="${W}" y1="${y(0)}" y2="${y(0)}" stroke="var(--muted)" stroke-width="1"/>`;
  $('#anChart').innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc($('#anChartTitle').textContent)}">${svg}</svg><div class="tip"></div>`;
  const tip = $('#anChart .tip');
  $('#anChart svg').onmousemove = (e) => {
    const i = e.target.dataset?.i; if (i == null) { tip.style.display = 'none'; return; }
    const s = ser[i];
    tip.innerHTML = `<b>${new Date(s.day).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' })}</b><br>` +
      BOTS.map(([k, t]) => `<span class="swatch" style="background:var(--s-${k})"></span>${t}: ${fmt(s[`${k}_${m}`] || 0)}`).join('<br>') +
      (m === 'deals' ? '' : `<br>Всего: ${fmt(s[m])}`) + (s.lisbot_spent ? `<br><span class="muted">Закупки LisBot: ${money(s.lisbot_spent, unit)}</span>` : '');
    const r = $('#anChart').getBoundingClientRect();
    tip.style.display = 'block';
    tip.style.left = Math.min(e.clientX - r.left + 12, r.width - tip.offsetWidth) + 'px';
    tip.style.top = Math.max(0, e.clientY - r.top - tip.offsetHeight - 8) + 'px';
  };
  $('#anChart svg').onmouseleave = () => { tip.style.display = 'none'; };
}

function anLis(d) {
  const st = (v, t) => `<div><b>${v}</b><span>${t}</span></div>`;
  const f = d.forecast, open = Object.entries(d.funnel).filter(([k]) => !['sold', 'failed', 'reversed', 'dry_run'].includes(k)).reduce((a, [, n]) => a + n, 0);
  $('#anLis').innerHTML = `<h3><span class="swatch" style="background:var(--s-lisbot)"></span>LisBot · предметы</h3>
    <div class="stat">${st(pct(f.fact_to_forecast), 'факт / прогноз цены')}${st(pct(f.mean_abs_error), 'ошибка прогноза')}
      ${st(d.hold.median_days == null ? '—' : d.hold.median_days + ' дн', 'медиана удержания')}${st(open, 'в работе сейчас')}</div>
    ${(d.by_type || []).length > 1 ? `<table class="pnl" style="margin-bottom:12px"><thead><tr><th>Тип</th><th>Предметов</th><th>Куплено</th><th>Продано</th><th>Прибыль</th><th title="Прибыль к цене покупки проданных">ROI</th></tr></thead><tbody>
      ${d.by_type.map((t) => `<tr><td>${esc(t.type)}</td><td>${t.names}</td><td>${t.bought}</td><td>${t.sold || 0}</td><td>${signed(t.profit, 'USD')}</td><td>${pct(t.roi)}</td></tr>`).join('')}</tbody></table>` : ''}
    <div class="scroll"><table class="pnl"><thead><tr><th>Предмет</th><th>Куплено</th><th>Продано</th><th>Прибыль</th><th>ROI</th><th>Удерж.</th><th>Прогноз</th></tr></thead><tbody>
    ${d.items.map((i) => `<tr><td title="${esc(i.name)}">${esc(i.name.length > 34 ? i.name.slice(0, 33) + '…' : i.name)}</td><td>${i.bought}</td><td>${i.sold || 0}</td>
      <td>${signed(i.profit, 'USD')}</td><td>${pct(i.roi)}</td><td>${i.hold_days == null ? '—' : i.hold_days + ' дн'}</td><td>${pct(i.accuracy)}</td></tr>`).join('')
      || '<tr><td colspan="7" class="muted">Сделок пока нет</td></tr>'}</tbody></table></div>
    <div class="muted small" style="margin-top:6px">Суммы в $. «Прогноз» — фактическая выручка в % от оценки при покупке.</div>
    ${(d.roi_cut || []).length ? `<h3 style="margin-top:14px">Если бы минимальный ROI был другим</h3>
    <table class="pnl"><thead><tr><th>Мин. ROI</th><th>Сделок</th><th>В минус</th><th>Прибыль</th><th>ROI</th></tr></thead><tbody>
      ${d.roi_cut.map((r) => `<tr${r.current ? ' style="font-weight:600"' : ''}><td>${Math.round(r.min_roi * 100)}%${r.current ? ' · сейчас' : ''}</td><td>${r.deals}</td>
        <td class="${r.losers ? 'neg' : ''}">${r.losers}</td><td>${signed(r.profit, 'USD')}</td><td>${pct(r.roi)}</td></tr>`).join('')}</tbody></table>
    <div class="muted small" style="margin-top:6px">Проданные сделки, у которых прогноз ROI при покупке был не ниже порога (strategy.minRoi LisBot). Поднимать порог стоит, если при этом уходят в основном убыточные сделки.</div>` : ''}`;
}

function anMann(d) {
  const st = (v, t) => `<div><b>${v}</b><span>${t}</span></div>`;
  const t = d.times;
  $('#anMann').innerHTML = `<h3><span class="swatch" style="background:var(--s-mannbot)"></span>MannBot · заказы FunPay</h3>
    <div class="stat">${st(d.orders, 'заказов')}${st(d.keys_sold, 'ключей выдано')}${st(pct(d.confirm_rate), 'подтверждают')}
      ${st(pct(d.refund_rate), 'возвратов')}${st(dur(t.total), 'оплата → выдача')}${st(dur(t.confirm), 'выдача → подтверждение')}</div>
    <table><tbody>${d.funnel.map((f) => `<tr><td style="width:40%">${esc(f.stage)}</td><td style="width:15%">${f.n}</td>
      <td><div class="bar" style="width:${f.pct || 0}%" title="${pct(f.pct)}"></div></td><td style="width:12%" class="muted">${pct(f.pct)}</td></tr>`).join('')}</tbody></table>
    <div class="muted small" style="margin-top:6px">Медианы: ссылка ${dur(t.link)} · трейд ${dur(t.offer)} · принятие ${dur(t.accept)}.</div>
    ${(d.problems || []).length ? `<h4 style="margin:12px 0 4px">Почему заказы не дошли до выдачи</h4><table><tbody>
      ${d.problems.map((p) => `<tr><td>${esc(p.reason)}</td><td style="width:12%">${p.n}</td><td class="muted small">${Object.entries(p.statuses).map(([k, v]) => `${esc(k)} ${v}`).join(', ')}</td></tr>`).join('')}</tbody></table>` : ''}
    ${d.orders ? whenBuy(d.by_hour, d.by_weekday) : ''}`;
}

// Когда покупают: столбики по часам суток и дням недели (местное время компьютера с центром)
function whenBuy(hours, days) {
  const bars = (arr, labels) => {
    const mx = Math.max(1, ...arr);
    return `<div class="when">${arr.map((n, i) => `<div title="${labels[i]}: ${n}"><i style="height:${Math.round(n / mx * 100)}%"></i><span>${labels[i]}</span></div>`).join('')}</div>`;
  };
  const top = hours.map((n, h) => [n, h]).sort((a, b) => b[0] - a[0]).slice(0, 3).filter(([n]) => n).map(([, h]) => `${h}:00`);
  return `<h4 style="margin:12px 0 4px">Когда покупают</h4>
    ${bars(hours, hours.map((_, h) => String(h)))}${bars(days, ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'])}
    <div class="muted small">${top.length ? `Больше всего заказов в ${top.join(', ')}. Держите ключи на складе и не ставьте на эти часы тихие часы.` : ''}</div>`;
}

async function anLoad() {
  anControls();
  const q = AN.days === 'all' ? 'days=all' : `days=${AN.days}`;
  const chartDays = AN.days === 'all' ? 90 : Math.max(7, Number(AN.days));
  try {
    const [p, daily, lis, mann] = await Promise.all([
      api(`/api/analytics/pnl?${q}`), api(`/api/analytics/daily?days=${chartDays}&currency=${AN.cur}`),
      api(`/api/analytics/bot/lisbot?${q}`), api(`/api/analytics/bot/mannbot?${q}`)]);
    anPnl(p); AN.daily = daily; anChart(); anLis(lis); anMann(mann);
  } catch { /* центр перезапускается */ }
}
window.addEventListener('resize', () => anChart());
anLoad();
setInterval(anLoad, 60000);
