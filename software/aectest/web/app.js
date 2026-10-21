'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const hhmmss = t => new Date(t ? t * 1000 : Date.now()).toLocaleTimeString([], { hour12: false });
const api = async (path, body) => {
  const r = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
};
const HEALTH = /^\s*\+\s*([\d.]+) s\s+(?:(\S+): )?state (\S+)\s+temp\s+([\d.-]+) C\s+vcc ([\d.]+) V/;
const TREND = /^\s*\+\s*([\d.]+) s\s+BER ((?:[\d.e+-]+\s+)+)(?:SNR ((?:[\d.]+\s+)+)dB\s+)?(?:LOL (0x[0-9a-f]+)\s+)?/;
const allZero = a => Array.isArray(a) && a.length > 0 && a.every(v => !(+v));
const NO_SNR = 'SNR not reported by this module (every value 0)';
function lolStrip(el, lols) { // one cell per sample; amber = a lane lost lock since the previous sample (14h:139, clear-on-read)
  if (!el) return; if (!lols || !lols.length) { el.hidden = true; return; }
  const n = lols.filter(v => parseInt(v, 16)).length; el.hidden = false;
  el.innerHTML = `<div class="stamp">LOL per second: ${n ? `${n} of ${lols.length} s with a loss of lock` : `none in ${lols.length} s`}</div><div class="lol-strip">${lols.map((v, i) => `<i class="${parseInt(v, 16) ? 'lol' : ''}" title="${i + 1} s: ${parseInt(v, 16) ? 'LOL lanes ' + esc(v) : 'locked'}"></i>`).join('')}</div>`;
}
const COLORS = ['--c1', '--c2', '--c3', '--c4', '--c5', '--c6', '--c7', '--c8'].map(v => getComputedStyle(document.documentElement).getPropertyValue(v).trim() || '#888');
let INFO = { units: [], dry_run: false, budget_w: 6, tech_steps: [] };
let conn = connInit();

// ---- toasts, hints, banners
function toast(text, ms = 5000, sticky = false) { const t = document.createElement('div'); t.className = 'toast' + (sticky ? ' sticky' : ''); t.textContent = text + (sticky ? ' (click to close)' : ''); t.onclick = () => t.remove(); $('#toasts').appendChild(t); if (!sticky) setTimeout(() => t.remove(), ms); }
const pop = $('#pop');
function showHint(el) { pop.textContent = el.dataset.hint; pop.hidden = false; const r = el.getBoundingClientRect(); pop.style.left = Math.min(r.left, innerWidth - 300) + 'px'; pop.style.top = (r.bottom + 6) + 'px'; }
document.addEventListener('click', e => { const h = e.target.closest('.hint'); if (h) { e.preventDefault(); e.stopPropagation(); pop.hidden ? showHint(h) : (pop.hidden = true); } else pop.hidden = true; });
document.addEventListener('focusin', e => { if (e.target.classList && e.target.classList.contains('hint')) showHint(e.target); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') pop.hidden = true; });
const FIRST = {
  cable: 'Start here. Click Read cable on a unit to see what is plugged in.',
  test: 'This is the guided test. It asks you one question at a time and ends with a verdict.',
  eng: 'Every command of the tool. Red buttons change the module state and need the confirmations.',
  records: 'Files written by the tester, on this computer. Nothing here changes hardware.',
};
function firstUse(view) {
  const box = $(`#${view}-banner`); if (!box || !FIRST[view] || localStorage.getItem('seen:' + view)) return;
  box.innerHTML = `<div class="banner"><span>${esc(FIRST[view])}</span><button class="btn x">Got it</button></div>`;
  $('button', box).onclick = () => { localStorage.setItem('seen:' + view, '1'); box.innerHTML = ''; };
}
$('#hints-reset').onclick = () => { Object.keys(FIRST).forEach(k => localStorage.removeItem('seen:' + k)); Object.keys(FIRST).forEach(firstUse); toast('Banners restored'); };

// ---- theme
function setTheme(t) { document.documentElement.dataset.theme = t; localStorage.setItem('theme', t); }
setTheme(localStorage.getItem('theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
$('#theme-toggle').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

// ---- views
function show(view) {
  $$('#rail .nav').forEach(b => b.dataset.view === view ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current'));
  $$('.view').forEach(v => v.id === view ? v.setAttribute('data-active', '') : v.removeAttribute('data-active'));
  firstUse(view);
  if (view === 'records') loadRecords();
}
$$('#rail .nav').forEach(b => b.onclick = () => show(b.dataset.view));

// ---- connection: heartbeat, restart detection, stale stamps
let es = null, retryTimer = null;
function renderConn() {
  const c = $('#conn'); c.className = 'conn ' + conn.status;
  $('#conn-text').textContent = conn.status === 'live' ? `Live, ${hhmmss(conn.lastBeat)}` : conn.status === 'down' ? 'Server not reachable' : 'Connecting';
  $('#offline').hidden = conn.status === 'live';
  document.title = (conn.status === 'live' ? '' : '(offline) ') + 'AEC-CT2-MINI tester';
}
function connect() {
  if (es) es.close();
  es = new EventSource('/api/events');
  es.onmessage = ev => {
    const e = JSON.parse(ev.data);
    const before = conn; conn = connReduce(conn, e);
    if (conn.restarted) { toast('Server restarted, data refreshed', 0, true); refreshAll(); }
    else if (before.status !== 'live' && before.instance !== null) { toast('Connected again'); }
    renderConn();
    $('#mode').textContent = `${INFO.dry_run ? 'Simulated units' : 'Hardware'}, up ${Math.round(e.uptime_s)} s`;
    if (e.units && !conn.restarted) unitsSeen(e.units);
  };
  es.onerror = () => {
    es.close(); es = null;
    conn = connReduce(conn, { type: 'drop' }); renderConn();
    clearTimeout(retryTimer);
    retryTimer = setTimeout(() => { conn = connReduce(conn, { type: 'retry' }); connect(); }, connBackoffMs(conn.attempts));
  };
}
setInterval(() => {
  const now = Date.now();
  $$('[data-read-at]').forEach(el => el.classList.toggle('stale', connStale(conn.status, +el.dataset.readAt, +el.dataset.interval || 15000, now)));
}, 1000);
function stamp(el, interval = 15000) { el.dataset.readAt = Date.now(); el.dataset.interval = interval; }
async function refreshAll() { await loadUnits(); if ($('#records[data-active]')) loadRecords(); }
async function loadInfo() { const i = await api('/api/info'); $('#version').textContent = `aectest ${i.version}`; $('#help-version').textContent = `This is aectest ${i.version}. The release notes list every public version.`; $('#help-changelog').innerHTML = md(i.changelog); }
function unitsSeen(names) {
  const known = INFO.units.map(u => u.serial);
  const came = names.filter(n => !known.includes(n)), went = known.filter(n => !names.includes(n));
  if (!came.length && !went.length) return;
  came.forEach(n => toast(`Unit ${n} connected`)); went.forEach(n => toast(`Unit ${n} disconnected`));
  loadUnits().catch(() => {});
}

// ---- charts (inline SVG, legend as an HTML list, no overlapping text)
function fit(name, n = 18) { return name.length > n ? name.slice(0, n - 1) + '…' : name; }
function legend(el, series) { if (!el) return; el.innerHTML = series.map((s, i) => `<li title="${esc(s.name)}"><i style="background:${COLORS[i % 8]}"></i>${esc(s.name)}</li>`).join(''); }
function lineChart(svg, series, o = {}) {
  const W = svg.clientWidth || 600, H = svg.clientHeight || 240, m = { l: 56, r: o.axes && o.axes[1] ? 56 : 16, t: 12, b: 26 };
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.innerHTML = '';
  const pts = series.flatMap(s => s.points);
  if (!pts.length) { svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">no data yet</text>`; legend(o.legendEl, []); return; }
  const xs = pts.map(p => p[0]), x0 = Math.min(...xs), x1 = Math.max(...xs) || 1;
  const axes = o.axes || ['left'], scale = {};
  for (const ax of axes) {
    const ys = series.filter(s => (s.axis || 'left') === ax).flatMap(s => s.points.map(p => p[1])).filter(v => !o.ylog || v > 0);
    let y0 = Math.min(...ys), y1 = Math.max(...ys);
    if (o.ylog) { y0 = Math.floor(Math.log10(y0)); y1 = Math.ceil(Math.log10(y1)); if (y1 - y0 < 1) y1 = y0 + 1; }
    else { const span = Math.max(y1 - y0, o.minSpan || 0.5), mid = (y0 + y1) / 2; y0 = mid - span * 0.6; y1 = mid + span * 0.6; }
    scale[ax] = v => H - m.b - ((o.ylog ? Math.log10(v) : v) - y0) / (y1 - y0) * (H - m.t - m.b);
    scale[ax].range = [y0, y1];
  }
  const sx = x => m.l + (x - x0) / (x1 - x0 || 1) * (W - m.l - m.r);
  let g = `<line class="axis" x1="${m.l}" y1="${H - m.b}" x2="${W - m.r}" y2="${H - m.b}"/>`;
  for (let i = 0; i <= 4; i++) {
    const [a0, a1] = scale[axes[0]].range, v = a0 + (a1 - a0) * i / 4, y = H - m.b - (H - m.t - m.b) * i / 4;
    g += `<line class="grid" x1="${m.l}" y1="${y}" x2="${W - m.r}" y2="${y}"/><text x="${m.l - 6}" y="${y + 4}" text-anchor="end">${o.ylog ? (10 ** v).toExponential(1) : v.toFixed(2)}</text>`;
    if (axes[1]) { const [z0, z1] = scale[axes[1]].range; g += `<text x="${W - m.r + 6}" y="${y + 4}">${(z0 + (z1 - z0) * i / 4).toFixed(2)}</text>`; }
  }
  for (let i = 0; i <= 4; i++) { const x = m.l + (W - m.l - m.r) * i / 4; g += `<text x="${x}" y="${H - 8}" text-anchor="middle">${(x0 + (x1 - x0) * i / 4).toFixed(0)}${o.xunit || ' s'}</text>`; }
  for (const mk of o.marks || []) g += `<line class="axis" x1="${sx(mk.x)}" y1="${m.t}" x2="${sx(mk.x)}" y2="${H - m.b}" stroke-dasharray="4 3"/><text x="${sx(mk.x) + 4}" y="${m.t + 10}">${esc(mk.label)}</text>`;
  series.forEach((s, i) => {
    const sy = scale[s.axis || 'left'];
    const d = s.points.filter(p => !o.ylog || p[1] > 0).map((p, k) => `${k ? 'L' : 'M'}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(' ');
    g += `<path d="${d}" fill="none" stroke="${COLORS[i % 8]}" stroke-width="2" stroke-dasharray="${s.axis === 'right' ? '5 3' : ''}"/>`;
  });
  svg.innerHTML = g; legend(o.legendEl, series);
}
function barChart(svg, groups, legendEl) { // groups: [{name, values:[{label, v, off}]}], log scale
  const W = svg.clientWidth || 600, H = svg.clientHeight || 240, m = { l: 56, r: 12, t: 12, b: 26 };
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.innerHTML = '';
  const vals = groups.flatMap(g => g.values.map(x => x.v)).filter(v => v > 0);
  if (!vals.length) { svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">no BER yet</text>`; legend(legendEl, []); return; }
  const lo = Math.floor(Math.log10(Math.min(...vals))) - 1, hi = Math.ceil(Math.log10(Math.max(...vals)));
  const sy = v => H - m.b - (Math.log10(v) - lo) / (hi - lo) * (H - m.t - m.b);
  let g = '';
  for (let e = lo; e <= hi; e++) g += `<line class="grid" x1="${m.l}" y1="${sy(10 ** e)}" x2="${W - m.r}" y2="${sy(10 ** e)}"/><text x="${m.l - 6}" y="${sy(10 ** e) + 4}" text-anchor="end">1e${e}</text>`;
  const n = groups.reduce((a, gr) => a + gr.values.length, 0), bw = (W - m.l - m.r) / (n + groups.length);
  let x = m.l + bw / 2;
  groups.forEach((gr, gi) => {
    gr.values.forEach(v => {
      if (v.v > 0) g += `<rect x="${x}" y="${sy(v.v)}" width="${bw * 0.8}" height="${H - m.b - sy(v.v)}" fill="${COLORS[gi % 8]}"><title>${esc(gr.name)} ${esc(v.label)}: ${v.v.toExponential(2)}</title></rect>`;
      g += `<text x="${x + bw * 0.4}" y="${H - 8}" text-anchor="middle">${v.off ? 'n/a' : esc(v.label)}</text>`;
      x += bw;
    });
    x += bw;
  });
  svg.innerHTML = g; legend(legendEl, groups);
}
function laneGrid(el, lanes) { el.innerHTML = lanes.map((l, i) => `<div class="lane ${l.cls}" title="lane ${i + 1}: ${esc(l.text)}">L${i + 1}<br>${esc(l.text)}</div>`).join(''); }
function healthSeries(points) { const s = []; for (const [name, p] of Object.entries(points)) { s.push({ name: `${name} temp C`, points: p.map(x => [x[0], x[1]]) }); s.push({ name: `${name} Vcc V`, axis: 'right', points: p.map(x => [x[0], x[2]]) }); } return s; }

// ---- jobs
function stream(jobId, logEl, onEvent) {
  return new Promise((resolve, reject) => {
    const src = new EventSource(`/api/stream?job=${jobId}`);
    let last = null;
    src.onmessage = ev => {
      const e = JSON.parse(ev.data); last = e;
      if (e.type === 'line' && logEl) { const sp = document.createElement('span'); sp.className = e.err ? 'err' : ''; sp.textContent = e.text + '\n'; logEl.appendChild(sp); logEl.scrollTop = logEl.scrollHeight; }
      onEvent && onEvent(e);
      if (e.type === 'done') { src.close(); resolve(e.result); }
      if (e.type === 'error') { src.close(); reject(Object.assign(new Error(e.text), { refused: true })); }
    };
    src.onerror = () => { src.close(); reject(Object.assign(new Error('Connection lost during the run. The last known state is in the log.'), { lost: true, last })); };
  });
}
const FIXES = [
  [/CRITERIA-PROPOSED/, 'Ask engineering to confirm the criteria file. Keep the cable out of the fail bin.'],
  [/No such device|disconnected|unit gone|USBError|FtdiError/, 'The unit left the USB bus. Unplug the host cable and the charger, wait 30 s, plug the host cable, then click Refresh units.'],
  [/cable removed|REMOVED-MID-TEST/, 'The cable came out during the test. Seat it again and start the test over.'],
  [/Connection lost/, 'The server stopped during the run. Wait for Live in the corner, then start the test again.'],
  [/THROTTLE|THERMAL/, 'The module got too hot. Let it cool, put air over the sink, then run again.'],
  [/NO-ACK|no ACK/, 'The module stopped answering. Check MODE AUTO and MOD PWR ON on the unit, then run again.'],
  [/JIG-UNIT-MISSING|cannot open unit/, 'The unit is not on the host, or another program holds it. Check the PC port, close the other program, then Refresh units.'],
  [/stopped by the operator/, 'Start the test again when the cable and the unit are ready.'],
  [/confirmations/, 'Tick both confirmation boxes before a high-power command.'],
  [/CMIS 4\.0 < 5\.0|force-cmis/, 'Tick "Force on CMIS below 5.0" for a CMIS 4.x module.'],
  [/budget|MaxPower/, 'Raise the budget with a reason, or use a module within the budget.'],
  [/not advertised|13h\/14h/, 'This module has no diagnostic pages. The PRBS and loopback tests cannot run on it.'],
  [/No USB device|cannot open|lock/, 'Check the unit is on the host and no other program holds it.'],
  [/Nack|NACK/, 'In MAN the MSEL slide at O or H deselects the module. Set MODE to AUTO, then Refresh.'],
];
const fixFor = msg => (FIXES.find(([re]) => re.test(msg)) || [])[1];
function alertBox(el, msg) { const fix = fixFor(msg) || 'Open the log for the last line, fix the cause, then run again.'; el.innerHTML = `<div class="alert"><b>${esc(msg.replace(/^Refused: (REFUSED: )?/, ''))}</b><div class="fix">${esc(fix)}</div></div>`; }

const label = k => (LABELS[k] || [human(k)])[0];
const hb = k => HELP[k] ? `<span class="hint" role="button" tabindex="0" data-hint="${esc(HELP[k])}">?</span>` : '';
const HTML_FMT = { module_state: v => stateBadge(v), verdict: v => verdictBadge(v) };
const FMT_ANY = { supported_cmds: ids => Array.isArray(ids) ? cdbGroups(ids).map(([f, names]) => `<div class="sub"><span>${esc(f)}</span> ${names.map(n => `<span class="badge mono">${esc(n)}</span>`).join(' ')}</div>`).join('') : esc(String(ids)) };
const fmtNum = v => Number.isInteger(v) ? String(v) : (Math.abs(v) < 1e-3 && v !== 0 ? v.toExponential(2) : String(+v.toFixed(4)));
function kvTable(obj, depth = 0) {
  if (obj === null || obj === undefined) return '<span class="stamp">none</span>';
  if (Array.isArray(obj)) {
    if (!obj.length) return '<span class="stamp">empty</span>';
    if (obj.every(x => typeof x !== 'object' || x === null)) {
      const shown = obj.slice(0, 16).map(v => typeof v === 'number' ? fmtNum(v) : String(v));
      return `<span class="mono">${esc(shown.join(', '))}${obj.length > 16 ? ` ... (${obj.length} values, see the raw JSON)` : ''}</span>`;
    }
    if (obj.every(x => x && typeof x === 'object' && !Array.isArray(x))) {
      if (obj.length > 12 || depth > 1) return `<span class="stamp">${obj.length} rows, see the raw JSON</span>`;
      const cols = [...new Set(obj.flatMap(Object.keys))].slice(0, 10);
      return `<table class="t cols"><tr>${cols.map(c => `<th title="${esc(c)}">${esc(label(c))}${hb(c)}</th>`).join('')}</tr>${obj.map(r => `<tr>${cols.map(c => `<td>${kvTable(r[c], depth + 1)}</td>`).join('')}</tr>`).join('')}</table>`;
    }
    return `<span class="stamp">${obj.length} items, see the raw JSON</span>`;
  }
  if (typeof obj === 'object') {
    const keys = Object.keys(obj);
    if (depth > 2 || keys.length > 40) return `<span class="stamp">${keys.length} fields, see the raw JSON</span>`;
    const rows = labelRows(obj).map(([k, l, text]) => [k, l, FMT_ANY[k] && obj[k] != null ? FMT_ANY[k](obj[k]) : text !== null ? (HTML_FMT[k] ? HTML_FMT[k](obj[k]) : esc(text)) : kvTable(obj[k], depth + 1)]);
    if (depth >= 1) return `<div class="sub">${rows.map(([k, l, h]) => `<div><span title="${esc(k)}">${esc(l)}</span> ${h}</div>`).join('')}</div>`;
    return `<table class="t">${rows.map(([k, l, h]) => `<tr><th title="${esc(k)}">${esc(l)}${hb(k)}</th><td>${h}</td></tr>`).join('')}</table>`;
  }
  if (typeof obj === 'string' && /^(SUCCESS|FAILED) \(/.test(obj)) return `<span class="badge ${obj.startsWith('SUCCESS') ? 'pass' : 'fail'}" title="${esc(obj)}">${esc(obj.split(' (')[0])}</span>`; // a CDB status: the word, the text on hover (batch 7)
  if (typeof obj === 'string' && obj.length > 160) return `<span class="mono">${esc(obj.slice(0, 160))}... (${obj.length} chars)</span>`;
  if (typeof obj === 'boolean') return `<span class="badge ${obj ? 'pass' : ''}">${obj ? 'yes' : 'no'}</span>`;
  if (typeof obj === 'number') return `<span class="mono">${Number.isInteger(obj) ? obj : (Math.abs(obj) < 1e-3 && obj !== 0 ? obj.toExponential(2) : +obj.toFixed(4))}</span>`;
  return esc(obj);
}
function summarize(r) { // the readable part of a run record; the full record stays behind the raw toggle
  if (r && r.directions) return { verdict: r.verdict, pattern: r.pattern, seconds: r.seconds, 'lane mask': r.lane_mask, 'media lane mask': r.media_lane_mask, application: r.apsel,
    ends: Object.fromEntries(Object.entries(r.ends || {}).map(([k, e]) => [k, { module: `${e.vendor || ''} ${e.part_number || ''} ${e.serial || ''}`, class: e.power_class, 'MaxPower W': e.max_power_w, 'ready after s': e.release && e.release.module_ready_observed_s, 'DP states': e.dp_states_11h_128 && [...new Set(e.dp_states_11h_128)].join(', ') }])),
    directions: Object.fromEntries(Object.entries(r.directions).map(([k, d]) => [k.replace('_to_', ' to '), { verdict: d.verdict, 'BER per lane': d.media_ber, 'LOL at the end': d.media_lol_14h_139, 'LOL before the gate': d.media_lol_before_gate_14h_139, samples: (d.health && Object.values(d.health)[0] || []).length }])) };
  if (r && r.samples) return { verdict: r.verdict, kind: r.kind, pattern: r.pattern, seconds: r.seconds, samples: r.samples.length, 'lane mask': r.lane_mask, 'loopback end': r.loopback_end, 'SNR advertised': r.snr_advertised_13h_130_5,
    'last BER per lane': (r.samples[r.samples.length - 1] || {}).media_ber, 'last SNR dB per lane': snrOrNote((r.samples[r.samples.length - 1] || {}).media_snr_db), csv: r.csv };
  if (r && r.lanes_linked) return { verdict: r.verdict, 'lanes linked': r.lanes_linked, samples: r.samples, ends: Object.fromEntries(Object.entries(r.ends || {}).map(([k, e]) => [k, { module: `${e.vendor || ''} ${e.part_number || ''}`, 'DP states': e.dp_states_11h_128 && [...new Set(e.dp_states_11h_128)].join(', '), 'media flags OR': e.media_flags_or }])) };
  if (r && r.pages) return { ...r.decoded, pages: Object.keys(r.pages).join(', '), flags: Object.fromEntries(Object.entries(r.flags || {}).filter(([, v]) => v.status === 'CHECK').map(([k, v]) => [k, v.value])) };
  return r;
}
const snrOrNote = snr => allZero(snr) ? NO_SNR : snr;
const berClass = v => v === null || v === undefined || Number.isNaN(+v) ? 'off' : v < 1e-6 ? 'pass' : v <= 2.4e-4 ? 'warn' : 'fail';
const BER_TEXT = { pass: 'good', warn: 'pre-FEC range', fail: 'high', off: 'n/a' };
const fmtBer = v => v > 0 ? (+v).toExponential(1) : v === 0 ? '0' : 'n/a';
function verdictBadge(v) { const s = String(v || 'none'); return `<span class="badge ${/^MEASURED|^PASS/.test(s) ? 'pass' : /INCONCLUSIVE/.test(s) ? 'warn' : /FAIL|ABORT|REFUSED|lost/.test(s) ? 'fail' : ''}" title="${esc(s)}">${esc(s.split(/[(:]/)[0].trim())}</span>`; }
function laneTable(ber, mask) {
  return `<table class="t lanes-t"><tr><th>Lane</th><th>BER</th><th>Status</th></tr>${(ber || []).map((v, i) => { const on = mask >> i & 1; const c = on ? berClass(v) : 'off'; return `<tr><td>L${i + 1}</td><td class="mono">${on ? fmtBer(v) : 'n/a'}</td><td><span class="badge ${c === 'off' ? '' : c}">${BER_TEXT[c]}</span></td></tr>`; }).join('')}</table>`;
}
function endCard(name, e) {
  const dp = e.dp_states_11h_128 ? [...new Set(e.dp_states_11h_128)] : [];
  return `<div class="mini"><h3>${esc(name)}</h3><div class="id-s">${esc(`${e.vendor || ''} ${e.part_number || ''}`.trim() || 'module')}<small>${esc(e.serial || '')}</small></div><div class="chips">${e.power_class !== undefined ? `<span class="badge info">class ${esc(e.power_class)}, ${esc(e.max_power_w)} W</span>` : ''}${e.release && e.release.module_ready_observed_s !== undefined ? `<span class="badge">ready after ${esc(e.release.module_ready_observed_s)} s</span>` : ''}${dp.map(d => `<span class="badge ${d === 'DPActivated' ? 'pass' : 'warn'}">${esc(d)}</span>`).join('')}${e.media_flags_or ? `<span class="badge">flags ${esc(Object.values(e.media_flags_or).join(' '))}</span>` : ''}</div></div>`;
}
function runCards(r) {
  const mask0 = parseInt(String(r.lane_mask || 'ff').slice(0, 4), 16);
  let html = `<div class="chips">${verdictBadge(r.verdict)}${r.pattern ? `<span class="badge">${esc(r.pattern)}, ${esc(r.seconds)} s</span>` : ''}${r.lane_mask ? `<span class="badge" title="${esc(r.lane_mask)}">lanes ${esc(String(r.lane_mask).slice(0, 4))}</span>` : ''}${r.apsel ? `<span class="badge">application ${esc(r.apsel)}</span>` : ''}${r.kind ? `<span class="badge">${esc(r.kind)}</span>` : ''}</div>`;
  const ends = Object.entries(r.ends || {});
  if (ends.length) html += `<h3>Ends</h3><div class="minis">${ends.map(([k, e]) => endCard(k, e)).join('')}</div>`;
  const dirs = r.directions ? Object.entries(r.directions) : r.direction ? [[`${r.direction.generator} to ${r.direction.checker}`, r.direction]] : [];
  if (r.samples && r.samples.length && !r.direction) { const last = r.samples[r.samples.length - 1]; dirs.push([`last sample, ${last.t_s} s`, { media_ber: last.media_ber, verdict: r.verdict, snr: last.media_snr_db, samples: r.samples.length }]); }
  if (dirs.length) html += `<div class="minis">${dirs.map(([k, d]) => { const mask = parseInt(String(d.lane_mask || r.lane_mask || 'ff').slice(0, 4), 16) || mask0;
    return `<div class="mini"><h3>${esc(k.replace('_to_', ' to '))}</h3>${verdictBadge(d.verdict)}${laneTable(d.media_ber, mask)}${d.snr ? `<div class="stamp">${allZero(d.snr) ? esc(NO_SNR) : 'SNR dB: ' + esc(d.snr.map(x => x ? (+x).toFixed(1) : 'n/a').join(', '))}</div>` : ''}<div class="stamp">${d.media_lol_before_gate_14h_139 !== undefined ? `LOL before the gate ${esc(d.media_lol_before_gate_14h_139)}, ` : ''}${d.media_lol_14h_139 !== undefined ? `LOL at the end ${esc(d.media_lol_14h_139)}, ` : ''}samples ${esc(d.samples !== undefined ? d.samples : r.samples ? r.samples.length : (d.health && Object.values(d.health)[0] || []).length)}</div></div>`; }).join('')}</div>`;
  if (r.samples && r.samples.length && r.samples[0].media_lol !== undefined) html += `<div class="lol-box"></div>`;
  if (r.lanes_linked) html += `<div class="lanes">${r.lanes_linked.map((ok, i) => `<div class="lane ${ok ? 'pass' : 'fail'}">L${i + 1}<br>${ok ? 'linked' : 'flag'}</div>`).join('')}</div>`;
  return html;
}
const isRun = r => !!(r && (r.directions || r.direction || r.samples || r.lanes_linked));
function resultCard(title, obj, extra = '') {
  const shown = summarize(obj);
  return `<div class="row" style="justify-content:space-between"><h2 style="margin:0">${esc(title)}</h2><span class="stamp" data-read-at="${Date.now()}" data-interval="3600000">read ${hhmmss()}</span></div>${extra}${isRun(obj) ? runCards(obj) : kvTable(shown)}<details class="raw"><summary>Raw JSON</summary><pre class="mono-box">${esc(JSON.stringify(obj, null, 1))}</pre></details>`;
}
function fillLol(container, rec) { const box = $('.lol-box', container); if (box && rec.samples) lolStrip(box, rec.samples.map(s => s.media_lol || '0x00')); }

// ---- Cable view
function stateBadge(st) { return `<span class="badge ${/Ready/.test(st) ? 'pass' : /Fault/.test(st) ? 'fail' : /LowPwr/.test(st) ? 'info' : 'warn'}">${esc(st || 'unknown')}</span>`; }
function plate(pp) {
  const m = pp.module;
  if (!m) return `<div class="empty"><b>No module in this unit</b>Insert a cable end, then click Read cable.</div>`;
  const b = pp.budget || {}, d = pp.diagnostics, fw = pp.firmware, ho = pp.hollow || {};
  const apps = (pp.applications || []).map(a => ({ ...a, active: a.n === pp.active_apsel }));
  const hollow = [
    ho.tx_power_identical_on_every_lane && `Tx power reads ${(ho.tx_power_uw || [])[0]} uW on every lane: a placeholder, not a monitor`,
    ho.rx_power_identical_on_every_lane && `Rx power reads ${(ho.rx_power_uw || [])[0]} uW on every lane: a placeholder, not a monitor`,
    ho.vdm_descriptor_page_all_zero && 'VDM advertised, but the descriptor page 20h is all zero: no VDM observables'].filter(Boolean);
  return `
  <div class="plate">
    <div><div class="id">${esc(m.vendor)} ${esc(m.part_number)}<small>serial ${esc(m.serial)}, rev ${esc(m.revision || '-')}, date ${esc(m.date_code || '-')}</small></div>
      <div class="chips">${stateBadge(m.module_state)}${hb('module_state')}<span class="badge info">CMIS ${esc(m.cmis_revision)}, ${esc(m.memory_model)}</span>${hb('memory_model')}
        <span class="badge ${b.within ? 'pass' : 'fail'}">class ${m.power_class}, ${m.max_power_w} W ${b.within ? 'within' : 'above'} the ${b.budget_w} W budget</span>${hb('power_class')}
        <span class="badge">${esc(m.media_interface_technology_00h_212)}</span>${hb('media_interface_technology_00h_212')}</div>
      <dl class="kv"><dt>Temperature${hb('temp_c')}</dt><dd>${(+m.temp_c).toFixed(1)} C</dd><dt>Identifier${hb('identifier_name')}</dt><dd>${esc(m.identifier_name)} (${esc(m.identifier)})</dd>
        <dt>Diagnostic pages${hb('Diagnostic pages')}</dt><dd>${pp.advertised && pp.advertised.diagnostic_pages ? 'yes' : 'no'}</dd><dt>CDB${hb('CDB')}</dt><dd>${pp.advertised ? pp.advertised.cdb_instances : 0} instance(s)</dd>
        <dt>VDM${hb('VDM')}</dt><dd>${pp.advertised && pp.advertised.vdm ? 'yes' : 'no'}</dd>${flagNames(pp.flags_at_read).length ? `<dt>Flags at read${hb('flags_at_read')}</dt><dd>${esc(flagNames(pp.flags_at_read).join(', '))}</dd>` : ''}</dl></div>
    <div>
      <h3>Applications (active ${pp.active_apsel || '-'})${hb('application')}</h3>${apps.length ? kvTable(apps.map(a => ({ application: a.n, host: ifaceName('host', a.host_id), media: ifaceName('media', a.media_id, m.media_interface_technology_00h_212), host_lanes: a.host_lanes, media_lanes: a.media_lanes, active: a.active }))) : '<span class="stamp">none advertised</span>'}
      <h3 style="margin-top:12px">Test features</h3>${d ? kvTable({ 'media generator': d.generator_media.join(', ') || 'none', 'media checker': d.checker_media.join(', ') || 'none', 'host generator': d.generator_host.join(', ') || 'none', loopbacks: d.loopbacks.join(', ') || 'none', 'BER results': d.ber_results, 'SNR (media)': d.snr_media, gating: d.gating }) : '<span class="stamp">no diagnostic pages: no PRBS, no loopback</span>'}
      ${hollow.length ? `<h3 style="margin-top:12px">Hollow features</h3><ul class="hollow">${hollow.map(h => `<li><span class="badge warn">hollow</span> ${esc(h)}</li>`).join('')}</ul>` : ''}
    </div>
  </div>
  <details class="raw"><summary>Thresholds and firmware</summary>${kvTable({ thresholds: pp.thresholds, firmware: fw })}</details>
  <details class="raw"><summary>Raw passport JSON</summary><pre class="mono-box">${esc(JSON.stringify(pp, null, 1))}</pre></details>`;
}
function unitFruCard(serial, s) {
  const box = $(`#unit-${CSS.escape(serial)}`); if (!box) return;
  const d = s.fru_decoded;
  const head = `<summary>Unit ${esc(serial)}: ${d ? esc(`rev ${(d.rev || '').replace(/^REV /, '')}, order ${d.order || '?'}`) : 'no FRU area'}${s.fru_product ? ` <span class="stamp">${esc(s.fru_product)}</span>` : ''}</summary>`;
  if (!d) { box.innerHTML = head + '<div class="empty"><b>No FRU area</b>This unit EEPROM carries no board area. Program it with the EEPROM tool.</div>'; return; }
  const rows = { fru_manufacturer: d.manufacturer, fru_product: d.product, fru_serial: d.serial, fru_part: d.part, fru_file_id: d.file_id, fru_rev: d.rev, fru_order: d.order, fru_case: d.case || 'none', fru_mfg_date: d.mfg_date, fru_product_string: s.fru_product || 'none' };
  const text = Object.entries(rows).map(([k, v]) => `${label(k)}: ${v}`).join('\n');
  box.innerHTML = head + kvTable(rows) + `<div class="row" style="margin-top:8px"><button type="button" class="btn small" data-copy-unit>Copy</button><span class="stamp copy-msg"></span></div>` + (s.fru_raw_hex ? `<details class="raw"><summary>Raw area bytes${hb('fru_raw')}</summary><pre class="mono-box">${esc(s.fru_raw_hex.replace(/(.{32})/g, '$1\n'))}</pre></details>` : '');
  $('[data-copy-unit]', box).onclick = () => copyText(text).then(() => { $('.copy-msg', box).textContent = 'copied'; });
}
async function cableCard(u, card) {
  card.innerHTML = `<div class="row" style="justify-content:space-between"><div><h2 style="margin:0">${esc(u.serial)}</h2><span class="stamp" id="fru-${esc(u.serial)}"></span></div>
    <div class="row"><button class="btn primary" data-act="read">Read cable</button><button class="btn" data-act="save">Save passport</button><button class="btn" data-act="decode">Decode every page</button><span class="hint" role="button" tabindex="0" data-hint="Every page the module advertises, decoded field by field for its CMIS revision. The lane view comes first.">?</span><button class="btn" data-act="pins">Refresh pins</button></div></div>
    <div class="chips" id="pins-${esc(u.serial)}"></div><div class="chips" id="clock-${esc(u.serial)}"></div><div id="plate-${esc(u.serial)}"><div class="empty"><b>Not read yet</b>Click Read cable to read the module in this unit.</div></div><div id="decode-${esc(u.serial)}" hidden></div><details class="unit-fru" id="unit-${esc(u.serial)}"><summary>Unit ${esc(u.serial)}</summary><div class="empty"><b>Not read yet</b>The FRU area shows after the first pin read.</div></details>`;
  const pins = async () => {
    const el = $(`#pins-${CSS.escape(u.serial)}`);
    try {
      const s = await api(`/api/status?serial=${encodeURIComponent(u.serial)}`);
      const drv = [s['ModSelL(drv)'], s['ResetL(drv)'], s['LPMode(drv)']];
      const unread = !$(`#plate-${CSS.escape(u.serial)} .plate`);
      const flags = FLAGS_AT_READ[u.serial];
      const intBadge = !s.interrupt ? `<span class="badge">no interrupt</span>${flags && flags.length ? `<span class="stamp" title="latched flags, cleared by the read">flags at read: ${esc(flags.join(', '))}</span>` : ''}`
        : unread ? `<span class="badge info" title="IntL 0 before any register read">interrupt latched since insert</span><span class="stamp">Read the cable to clear it.</span>`
        : `<span class="badge warn" title="IntL stays low after the flags were read">interrupt pending${flags && flags.length ? ': ' + esc(flags.join(', ')) : ''}</span>`;
      const pinBadges = s.ModPrsL === 0 && s.IntL === 0 && unread
        ? `<span class="badge info" title="ModPrsL 0, IntL 0">pins low: module present or rail off</span><span class="stamp">Turn MOD PWR on and read the cable.</span>`
        : `<span class="badge ${s.module_present ? 'pass' : ''}">${s.module_present ? 'module present' : 'no module'}</span>${intBadge}`;
      el.innerHTML = `${pinBadges}<span class="badge">${drv.every(x => x === 1) ? 'driving ModSelL, ResetL, LPMode high' : `driving ModSelL ${drv[0]}, ResetL ${drv[1]}, LPMode ${drv[2]}`}</span><span class="stamp">read ${hhmmss()}</span>`;
      stamp(el, 20000); $(`#fru-${CSS.escape(u.serial)}`).textContent = s.fru && s.fru !== 'none' ? `FRU ${s.fru}` : 'FRU none'; unitFruCard(u.serial, s);
    } catch (e) { el.innerHTML = `<span class="badge fail">${esc(e.message)}</span>`; }
  };
  const read = async (save) => {
    const box = $(`#plate-${CSS.escape(u.serial)}`), btns = $$('button', card); btns.forEach(b => b.disabled = true);
    box.innerHTML = '<div class="empty"><b>Reading</b>Identity, pages, capabilities and firmware info.</div>';
    try {
      const { job } = await api(save ? '/api/passport' : '/api/cable', { serial: u.serial });
      const pp = await stream(job, null);
      FLAGS_AT_READ[u.serial] = flagNames(pp.flags_at_read || (pp.module && pp.module.flags_at_read));
      box.innerHTML = plate(pp); stamp(box, 300000); pins(); MODULE_SN[u.serial] = pp.module && pp.module.serial; fillLabel(pp.module && pp.module.serial);
      if (save) toast('Passport saved: ' + pp.files.map(f => f.split('/').pop()).join(', '), 6000);
      toast(`Read ${u.serial}`);
      if (pp.module) rampClock(u.serial);
    } catch (e) { box.innerHTML = ''; alertBox(box, e.message); }
    btns.forEach(b => b.disabled = false);
  };
  const decode = async () => {
    const box = $(`#decode-${CSS.escape(u.serial)}`), btns = $$('button', card); btns.forEach(b => b.disabled = true);
    box.hidden = false; box.innerHTML = '<div class="empty"><b>Decoding</b>Every advertised page, field by field for this CMIS revision.</div>';
    try { const { job } = await api('/api/decode', { serial: u.serial }); const rec = await stream(job, null); box.innerHTML = decodeCard(`Decode of ${u.serial}`, rec); stamp(box, 300000); }
    catch (e) { box.innerHTML = ''; alertBox(box, e.message); }
    btns.forEach(b => b.disabled = false);
  };
  $('[data-act=read]', card).onclick = () => read(false); $('[data-act=save]', card).onclick = () => read(true); $('[data-act=pins]', card).onclick = pins; $('[data-act=decode]', card).onclick = decode;
  card.readCable = () => read(false);
  pins();
}
$('#cable-read-all').onclick = () => $$('#cable-cards .card').forEach(c => c.readCable && c.readCable());
const MODULE_SN = {}; // unit serial -> the module serial of the last cable read (the chart legend names the ends with both)
function endName(k, ends) { const unit = ends && ends[String(k).toLowerCase()]; return unit ? `${k} ${unit}${MODULE_SN[unit] ? ' ' + MODULE_SN[unit] : ''}` : k; }
const FLAGS_AT_READ = {};
function flagNames(v) { // the engine shape is open until it lands: a list of names, {names: [...]}, {set: [...]}, or {name: true}
  if (!v) return [];
  if (Array.isArray(v)) return v.map(x => typeof x === 'string' ? x : (x && (x.name || x.flag)) || JSON.stringify(x));
  if (Array.isArray(v.names)) return v.names; if (Array.isArray(v.set)) return v.set;
  return Object.entries(v).filter(([, x]) => x === true || (x && x.set)).map(([k]) => k);
}

const CLOCKS = {};
function clockText(serial) { const c = CLOCKS[serial]; return c ? `I2C ${Math.round(c.i2c_hz / 1000)} kHz` : 'I2C clock not read'; }
function renderClocks() {
  for (const [serial, c] of Object.entries(CLOCKS)) {
    const el = $(`#clock-${CSS.escape(serial)}`); if (!el) continue;
    const ok = c.i2c_ramp && c.i2c_ramp.startsWith('ramped'), fell = c.i2c_ramp && c.i2c_ramp.startsWith('fell');
    el.innerHTML = `<span class="badge ${ok ? 'pass' : fell ? 'warn' : 'info'}" title="${esc(c.i2c_ramp || '')}">${esc(clockText(serial))}</span>${hb('i2c_clock')}`;
  }
  const names = Object.keys(CLOCKS);
  $('#mode').textContent = (INFO.dry_run ? 'Simulated units' : 'Hardware') + (names.length ? ' · ' + names.map(s => `${s} ${clockText(s).replace('I2C ', '')}`).join(', ') : '');
}
async function rampClock(serial) {
  try {
    const { job } = await api('/api/i2c-ramp', { serial });
    const c = await stream(job, null);
    CLOCKS[serial] = c; renderClocks();
  } catch (e) { toast(`Clock ramp on ${serial}: ${e.message}`, 6000); }
}
async function loadUnits() {
  INFO = await api('/api/units');
  INFO.units.sort((a, b) => String(a.serial).localeCompare(String(b.serial)));
  Object.assign(CLOCKS, INFO.i2c || {});
  $('#mode').textContent = INFO.dry_run ? 'Simulated units' : 'Hardware';
  const opts = INFO.units.map(u => `<option>${esc(u.serial)}</option>`).join('');
  $$('select[data-units]').forEach(s => { const was = s.value; s.innerHTML = opts; if (s.dataset.second !== undefined && INFO.units[1]) s.value = INFO.units[1].serial; if (was && INFO.units.some(u => u.serial === was)) s.value = was; });
  pairLines();
  $$('input[data-budget]').forEach(i => { if (!i.value) i.value = INFO.budget_w; });
  const fwn = $('#fw-note'); fwn.hidden = INFO.firmware_engine !== false; fwn.textContent = 'The firmware update engine is not in this build yet. The two buttons answer "not available". Firmware info and features still read through the CDB card above.';
  const op = $('[name=operator]', $('#tech-form')); if (!op.value) op.value = INFO.operator || '';
  needTwo(INFO.units.length >= 2);
  $('#tech-mode').value = INFO.units.length >= 2 ? 'both' : 'one'; techMode();
  const cards = $('#cable-cards'); cards.innerHTML = '';
  if (!INFO.units.length) cards.innerHTML = '<div class="empty"><b>No unit on the host</b>Plug a stick into this computer with its PC port, then reload.</div>';
  for (const u of INFO.units) { const c = document.createElement('div'); c.className = 'card'; cards.appendChild(c); cableCard(u, c); }
  renderClocks();
  const rr = $('#ramp-rounds'); if (rr && !rr.value) rr.value = INFO.i2c_ramp_rounds || 20;
  $('#settings-list').innerHTML = kvTable({ backend: INFO.dry_run ? 'simulated' : 'hardware', 'server instance': INFO.instance, 'started': new Date(INFO.started * 1000).toLocaleString(), 'budget W': INFO.budget_w, units: INFO.units.map(u => `${u.serial}: ${u.fru ? u.fru.fru : 'FRU not read yet'}`).join('; ') || 'none', 'record folders': INFO.record_roots }).replace('<table class="t">', '<table class="t">');
}

$('#ramp-rounds-save').onclick = async () => {
  try { const r = await api('/api/settings', { i2c_ramp_rounds: parseInt($('#ramp-rounds').value, 10) }); toast(`Clock ramp rounds: ${r.i2c_ramp_rounds}`); }
  catch (e) { toast(`Settings: ${e.message}`, 6000); }
};
function needTwo(two) { for (const f of ['#f-pair', '#f-trend']) { $('.need-two', $(f)).hidden = !two; $('.need-one', $(f)).hidden = two; } }
function pairLines() {
  for (const f of $$('#f-pair, #f-trend')) {
    const a = $('[name=a]', f), b = $('[name=b]', f), line = $('[data-pair-line]', f); if (!a || !b || !line) continue;
    line.textContent = a.value && b.value ? `Pair: ${a.value} is end A, ${b.value} is end B.` : 'Pair: pick two units.';
  }
}
$$('#f-pair, #f-trend').forEach(f => { f.addEventListener('change', ev => { if (ev.target.matches('[name=a], [name=b]')) pairLines(); }); const sw = $('[data-swap]', f); if (sw) sw.onclick = () => { const a = $('[name=a]', f), b = $('[name=b]', f); [a.value, b.value] = [b.value, a.value]; pairLines(); }; });
$('#cable-refresh').onclick = () => loadUnits().then(() => toast(`${INFO.units.length} unit(s) on the host`)).catch(e => alertBox($('#cable-banner'), e.message));

// ---- Test a cable (the technician flow)
let techJob = null, techBack = false, stepSeen = false;
const SKIPPED = new Map();
function fillLabel(serial) {
  const i = $('[name=label_sn]', $('#tech-form')); if (!serial || (i.value && i.dataset.auto !== i.value)) return;
  i.value = serial; i.dataset.auto = serial;
}
function askCard(e) {
  const m = /press (\w+)\.?\s*$/i.exec(e.text); $('#tech-ask-text').textContent = e.text.replace(/\s*Then press \w+\.?\s*$/i, '');
  $('#tech-yes').textContent = m && !/enter/i.test(m[1]) ? m[1] : 'Done'; $('#tech-ask').hidden = false;
  $('#tech-skip').hidden = $('#tech-skip-reason').hidden = !e.skippable; $('#tech-skip-reason').value = ''; $('#tech-skip').disabled = true;
  $('#tech-back').hidden = stepSeen;
}
function precheckCard(ev) {
  const box = $('#tech-prechecks'); box.hidden = false;
  let card = $(`[data-unit="${CSS.escape(ev.unit)}"]`, box); if (!card) { card = document.createElement('div'); card.className = 'card precheck'; card.dataset.unit = ev.unit; box.appendChild(card); }
  card.innerHTML = `<h3>Unit ${esc(ev.unit)}: ${ev.ok ? 'ready' : 'not ready'}</h3>` + (ev.checks || []).map(c => `<div class="pre-row"><span class="badge ${c.ok ? 'pass' : 'warn'}">${esc(c.name)}</span><span>${c.ok ? esc(c.got) : `${esc(c.got)}, want ${esc(c.want)}`}</span>${!c.ok && c.hint ? `<span class="stamp">${esc(c.hint)}</span>` : ''}</div>`).join('');
}
const STEP_DATA = new Map(); let stepCur = null;
function stepButtons(steps, now, data, skipped) { // every step is a button: a step with data opens its results, a future step is disabled
  return steps.map((s, i) => { const can = data.has(s); return `<button type="button" class="step ${skipped.has(s) ? 'skip' : i < now ? 'done' : i === now ? 'now' : ''}" data-step="${esc(s)}"${can ? '' : ' disabled'} title="${skipped.has(s) ? 'skipped: ' + esc(skipped.get(s)) : can ? 'Show the results of this step.' : 'Not run yet.'}">${i + 1}. ${esc((STEP_INFO[s] || {}).title || s)}</button>`; }).join('');
}
function stepper(now) { $('#tech-stepper').innerHTML = stepButtons(INFO.tech_steps, now, STEP_DATA, SKIPPED); }
function stepView(data, container) { // the result cards of one step: the events by type, the health samples as a table, the lines behind a toggle
  const byType = new Map(); for (const e of data.events) { if (!byType.has(e.type)) byType.set(e.type, []); byType.get(e.type).push(e); }
  let html = '';
  for (const [t, evs] of byType) {
    if (t === 'health') { const rows = evs.map(e => ({ end: e.end, t_s: e.data.t_s, module_state: e.data.state_name, temp_c: e.data.temp_c, vcc_v: e.data.vcc_v })); html += `<h4>Health, ${rows.length} samples</h4>${rows.length > 12 ? `<div class="stamp">The first and the last of ${rows.length}. The chart holds every sample.</div>` + kvTable([rows[0], rows[rows.length - 1]]) : kvTable(rows)}`; continue; }
    html += evs.map(e => `<h4>${esc(label(t))}${e.end ? `, end ${esc(e.end)}` : ''}</h4>${kvTable(e.data)}`).join('');
  }
  if (!html) html = '<div class="empty"><b>No data</b>This step logged lines only.</div>';
  container.innerHTML = html + (data.lines.length ? `<details class="raw"><summary>Lines (${data.lines.length})</summary><pre class="mono-box">${esc(data.lines.join('\n'))}</pre></details>` : '');
}
function showStep(name) {
  const d = STEP_DATA.get(name); if (!d) return; const v = $('#tech-step-view'); v.hidden = false;
  $('#tech-step-view-title').textContent = `Step ${INFO.tech_steps.indexOf(name) + 1}: ${(STEP_INFO[name] || {}).title || name}`;
  stepView(d, $('#tech-step-view-body')); $$('button.step', $('#tech-stepper')).forEach(b => b.setAttribute('aria-selected', b.dataset.step === name));
}
function stepperKeys(el, open) { // left and right move between the steps that have data
  el.onclick = ev => { const b = ev.target.closest('button.step'); if (b && !b.disabled) open(b.dataset.step); };
  el.onkeydown = ev => { if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return; const bs = $$('button.step:not(:disabled)', el); if (!bs.length) return; const i = bs.indexOf(document.activeElement); const j = (i + (ev.key === 'ArrowRight' ? 1 : -1) + bs.length) % bs.length; bs[j].focus(); open(bs[j].dataset.step); ev.preventDefault(); };
}
function stepCollect(text) { // one line of the run goes to the step it belongs to; a step line opens the next bucket
  if (!stepCur || !STEP_DATA.has(stepCur)) return; const d = STEP_DATA.get(stepCur);
  if (text.startsWith('event {')) { try { const ev = JSON.parse(text.slice(6)); if (ev.type !== 'step') d.events.push(ev); } catch (e) { d.lines.push(text); } }
  else if (!/^step \d+: /.test(text)) d.lines.push(text);
}
stepperKeys($('#tech-stepper'), showStep);
$('#tech-step-view-close').onclick = () => { $('#tech-step-view').hidden = true; $$('button.step', $('#tech-stepper')).forEach(b => b.removeAttribute('aria-selected')); };
let stepTimer = null, stepStart = 0;
function stepNow(name) {
  const i = STEP_INFO[name], d = $('#tech-now'); if (!i) { d.hidden = true; return; }
  d.hidden = false; d.classList.remove('ended'); const n = INFO.tech_steps.indexOf(name) + 1;
  $('#tech-now-title').textContent = `Step ${n} of ${INFO.tech_steps.length}: ${i.title}`;
  $('#tech-now-wait').textContent = (i.wait || '').replace('{timeout}', INFO.tech_timeout_s || 20); $('#tech-now-activity').textContent = '';
  $('#tech-now-does').innerHTML = `<b>What it does:</b> ${esc(i.does)}`; $('#tech-now-judges').innerHTML = `<b>What it judges:</b> ${esc(i.judges)}`; $('#tech-now-time').innerHTML = `<b>How long:</b> ${esc(i.time)}`;
  stepStart = Date.now(); clearInterval(stepTimer); $('#tech-now-elapsed').textContent = '0 s';
  stepTimer = setInterval(() => { $('#tech-now-elapsed').textContent = `${Math.round((Date.now() - stepStart) / 1000)} s`; }, 1000);
}
function stepActivity(text) { if (!$('#tech-now').hidden && text && !text.startsWith('event {')) $('#tech-now-activity').textContent = text.length > 160 ? text.slice(0, 159) + '…' : text; }
function stepEnded() { clearInterval(stepTimer); stepTimer = null; $('#tech-now').classList.add('ended'); }
const NEXT_BY_STATUS = { PASS: 'Label the cable. Put it in the pass bin.', FAIL: 'Put the cable in the fail bin. Hand the session folder to engineering.', INCONCLUSIVE: 'Fix the cause named above, then run the test again.' };
function endCard(o) {
  const c = $('#tech-end'); c.hidden = false; c.className = `card end ${o.status}`;
  $('#tech-end-status').textContent = o.head; $('#tech-end-reason').textContent = o.reason; $('#tech-end-next').innerHTML = `<b>Next:</b> ${esc(o.next)}`;
  $('#tech-end-chips').innerHTML = (o.chips || []).filter(Boolean).map(t => `<span class="badge">${esc(t)}</span>`).join('');
  const more = $('#tech-end-reasons'); more.hidden = (o.reasons || []).length < 2; $('summary', more).textContent = `All reasons (${(o.reasons || []).length})`; $('ul', more).innerHTML = (o.reasons || []).map(r => `<li>${esc(r)}</li>`).join('');
  const link = $('#tech-report-link'), copy = $('#tech-copy-report'), rc = $('#tech-report-card');
  link.hidden = copy.hidden = rc.hidden = !o.report;
  if (o.report) { link.href = `/api/record?path=${encodeURIComponent(o.path)}`; rawLog($('#tech-report')); $('#tech-report').textContent = o.report; }
  $('#tech-stepper').classList.add('done'); $('#tech-ask').hidden = true; $('#tech-now').hidden = true;
  c.scrollIntoView({ block: 'nearest' });
}
function techMode() { const one = $('#tech-mode').value === 'one'; $('.need-b', $('#tech-form')).hidden = one; }
$('#tech-mode').onchange = techMode;
$('#tech-form').onsubmit = async ev => {
  ev.preventDefault();
  const f = Object.fromEntries(new FormData(ev.target));
  if (f.mode === 'one') delete f.b;
  f.label_sn = (f.label_sn || '').trim() || null; if (!(f.operator || '').trim()) delete f.operator;
  $('#test-setup').hidden = true; $('#test-run').hidden = false; SKIPPED.clear(); techBack = false; stepSeen = false;
  const log = $('#tech-log'); log.textContent = ''; $('#tech-end').hidden = true; $('#tech-stepper').classList.remove('done'); $('#tech-now').hidden = true; $('#tech-live').hidden = true; $('#tech-confirm').hidden = true;
  $('#tech-prechecks').hidden = true; $('#tech-prechecks').innerHTML = ''; $$('[data-log]').forEach(rawLog); STEP_DATA.clear(); stepCur = null; $('#tech-step-view').hidden = true;
  let stepNo = 0; stepper(0);
  const points = {};
  try {
    const { job } = await api('/api/tech', f); techJob = job;
    toast(`Test started, ${f.mode === 'one' ? 'one end' : 'both ends'}`);
    const result = await stream(job, log, e => {
      if (e.type === 'ask') askCard(e);
      if (e.type === 'line') {
        if (e.text.startsWith('event {')) { try { const ev2 = JSON.parse(e.text.slice(6)); if (ev2.type === 'precheck') precheckCard({ ...ev2.data, unit: ev2.data.unit || ev2.end }); } catch (e2) { /* a line, not an event */ } }
        const sm = /^step (\d+): (\w+)( skipped(?: \((.*)\))?)?/.exec(e.text); if (sm) { stepSeen = true; if (sm[3]) SKIPPED.set(sm[2], sm[4] || 'one end tested'); stepNo = INFO.tech_steps.indexOf(sm[2]); stepCur = sm[2]; STEP_DATA.set(stepCur, { events: [], lines: [] }); stepper(sm[3] ? stepNo + 1 : stepNo); if (!sm[3]) stepNow(sm[2]); }
        stepCollect(e.text);
        stepActivity(e.text); // item 8: the latest engine line under the step title, so a waiting step never reads as idle
        if (/^confirm: /.test(e.text)) { const c = $('#tech-confirm'); c.hidden = false; c.textContent = 'Read back: ' + e.text.slice(9); }
        const mm = HEALTH.exec(e.text); if (mm) { $('#tech-live').hidden = false; const ek = endName(mm[2] || 'module', f); (points[ek] = points[ek] || []).push([+mm[1], +mm[4], +mm[5]]); lineChart($('#tech-chart'), healthSeries(points), { axes: ['left', 'right'], legendEl: $('#tech-legend') }); }
      }
    });
    if (techBack) { $('#test-run').hidden = true; $('#test-setup').hidden = false; toast('Back to the setup. The stopped run is on record.'); return; }
    const reasons = (result.verdict.reasons || []).map(r => (typeof r === 'string' ? r : (r.code || '') + (r.text ? ': ' + r.text : '')).replace(/ got None$/, ''));
    const stopped = reasons.some(r => /stopped by the operator/.test(r)), status = result.verdict.status, proposed = reasons.some(r => /CRITERIA-PROPOSED/.test(r));
    const atStep = /before step 1/.test(reasons.join(' ')) ? `before step 1 of ${INFO.tech_steps.length}` : `at step ${stepNo + 1} of ${INFO.tech_steps.length}`;
    stepper(stopped ? (/before step 1/.test(reasons.join(' ')) ? 0 : stepNo) : INFO.tech_steps.length); // a stopped run does not show every step as done
    let report = null, next = null;
    if (result.report) { const r = await api(`/api/record?path=${encodeURIComponent(result.report)}`); report = r.text; const m = /## Next step\s*\n+([^\n]+)/.exec(report || ''); if (m) next = m[1].trim(); }
    endCard({ status, head: stopped ? 'Stopped' : status, reason: stopped ? `Stopped by the operator ${atStep}.` : proposed ? 'Criteria proposed, not confirmed. ' + (reasons.find(r => !/CRITERIA-PROPOSED/.test(r)) || 'Every measured value is on record.') : reasons[0] || 'Every step passed.',
      next: (proposed && fixFor('CRITERIA-PROPOSED')) || (status === 'INCONCLUSIVE' && fixFor(reasons.join(' '))) || next || NEXT_BY_STATUS[status] || '', reasons, report, path: result.report,
      chips: [f.label_sn ? `label ${f.label_sn}` : null, f.sku && f.sku !== 'auto' && f.sku !== 'none' ? `criteria ${f.sku}` : f.sku === 'none' ? 'no criteria' : null, f.mode === 'one' ? 'one end' : 'both ends', f.a ? `unit ${f.a}${f.b ? ' and ' + f.b : ''}` : null] });
    toast('Test finished: ' + status);
  } catch (e) {
    endCard({ status: 'INCONCLUSIVE', head: e.lost ? 'Connection lost' : 'Stopped', reason: e.message.replace(/^Refused: (REFUSED: )?/, ''), next: fixFor(e.message) || 'Open the log, fix the cause, then start the test again.', reasons: [], chips: [] });
  }
  stepEnded(); $('#tech-ask').hidden = true; $('#tech-now').hidden = true;
};
const answer = (yes, extra = {}) => async () => { $('#tech-ask').hidden = true; await api('/api/answer', { job: techJob, yes, ...extra }); };
$('#tech-yes').onclick = answer(true); $('#tech-no').onclick = answer(false);
$('#tech-back').onclick = () => { techBack = true; answer(false)(); };
$('#tech-skip-reason').oninput = ev => { $('#tech-skip').disabled = !ev.target.value.trim(); };
$('#tech-skip').onclick = () => answer(false, { skip: true, reason: $('#tech-skip-reason').value.trim() })();
$('#tech-again').onclick = () => { $('#test-run').hidden = true; $('#test-setup').hidden = false; $('#tech-end').hidden = true; $('#tech-stepper').classList.remove('done'); stepper(0); techReset(); };
function techReset() { $('#tech-match').hidden = true; $('#tech-start').disabled = true; $('[name=sku]', $('#tech-form')).value = 'auto'; $('#tech-msg').textContent = ''; }
$('#tech-read').onclick = async () => {
  const f = Object.fromEntries(new FormData($('#tech-form')));
  const box = $('#tech-match'); box.hidden = false; box.innerHTML = '<div class="empty"><b>Reading the cable</b>Identity from unit ' + esc(f.a || '?') + '.</div>'; $('#tech-start').disabled = true;
  $('#test-banner').innerHTML = '';
  try {
    const { job } = await api('/api/match-sku', { serial: f.a });
    const m = await stream(job, $('#tech-log'), () => {});
    const id = m.identity || {};
    const who = `${esc(id.vendor || '?')} ${esc(id.part_number || '?')}, serial ${esc(id.serial || '?')}`;
    fillLabel(id.serial);
    if (m.matched) {
      box.innerHTML = `<div class="cable-id">Cable: ${who}</div><div>Matched to criteria <b>${esc(m.sku)}</b>${m.proposed ? ' (PROPOSED limits, not yet reviewed)' : ''}.</div>`;
      $('[name=sku]', $('#tech-form')).value = m.sku; $('#tech-start').disabled = false;
    } else {
      box.innerHTML = `<div class="cable-id">Cable: ${who}</div><div>No criteria for this cable type yet.${m.candidates.length ? ' Near: ' + esc(m.candidates.join(', ')) + '.' : ''}</div>
        <div class="row"><button type="button" class="btn" id="tech-nocrit">Test without criteria</button><button type="button" class="btn" id="tech-create">Create criteria from this cable</button></div>`;
      $('#tech-nocrit').onclick = () => { $('[name=sku]', $('#tech-form')).value = 'none'; $('#tech-start').disabled = false; toast('The verdict will carry NO-CRITERIA'); };
      $('#tech-create').onclick = async () => {
        $('#tech-create').disabled = true;
        try {
          const r = await api('/api/derive', { serial: f.a }); const d = await stream(r.job, $('#tech-log'), () => {});
          toast(`Wrote ${d.rel}. Commit it after review.`, 0, true);
          $('#test-banner').innerHTML = derivePanel(d);
          $('#tech-read').click();
        } catch (e) { alertBox($('#test-banner'), e.message); }
      };
    }
  } catch (e) { box.innerHTML = ''; box.hidden = true; alertBox($('#test-banner'), e.message); }
};

const ENG_RESULTS = {}; let engTab = ($('#eng-tabs button[aria-selected="true"]') || {}).dataset ? $('#eng-tabs button[aria-selected="true"]').dataset.tab : null;
const ENG_EMPTY = '<div class="empty"><b>No result yet</b>Run a command on the left. Its result appears here as a card.</div>';
$$('#eng-tabs button').forEach(b => b.onclick = () => { $$('#eng-tabs button').forEach(x => x.setAttribute('aria-selected', x === b)); $$('#eng .tab').forEach(t => t.dataset.tab === b.dataset.tab ? t.setAttribute('data-active', '') : t.removeAttribute('data-active'));  const res = $('#eng-result'); if (engTab !== b.dataset.tab) { res.innerHTML = tabSwap(ENG_RESULTS, engTab, b.dataset.tab, res.innerHTML, ENG_EMPTY); $('#eng-log').textContent = ''; $('#eng-banner').innerHTML = ''; } engTab = b.dataset.tab; });
function liveEng(ends) {
  const points = {}, ber = {}, snr = {}, lols = [], marks = [];
  let run = 0, offset = 0, lastT = -1, maxT = 0;
  $('#eng-live').hidden = true; $('#eng-live2').hidden = true; $('#eng-lol').hidden = true; $('#eng-snr-note').hidden = true;
  const labels = []; // the direction lines name the segments: 'prbs31q media-side A -> B: ...'
  return e => {
    if (e.type !== 'line') return;
    const dm = /media-side (\S+) -> (\S+):/.exec(e.text); if (dm) labels.push(`${dm[1]} to ${dm[2]}`);
    const h = HEALTH.exec(e.text);
    if (h) {
      $('#eng-live').hidden = false;
      const t = +h[1];
      if (t < lastT) { run += 1; offset = maxT + 2; marks.push({ x: offset, label: labels[run] || `run ${run + 1}` }); if (run === 1) marks.unshift({ x: 0, label: labels[0] || 'run 1' }); }
      lastT = t; maxT = Math.max(maxT, offset + t);
      const ek = endName(h[2] || 'unit', ends); (points[ek] = points[ek] || []).push([offset + t, +h[4], +h[5]]);
      lineChart($('#eng-chart'), healthSeries(points), { axes: ['left', 'right'], legendEl: $('#eng-legend'), marks }); return;
    }
    const t = TREND.exec(e.text);
    if (t) {
      $('#eng-live').hidden = false;
      t[2].trim().split(/\s+/).forEach((v, i) => (ber[i] = ber[i] || []).push([+t[1], +v]));
      lineChart($('#eng-chart'), Object.entries(ber).map(([i, p]) => ({ name: `lane ${+i + 1} BER`, points: p })), { ylog: true, legendEl: $('#eng-legend') });
      if (t[3]) {
        const vals = t[3].trim().split(/\s+/); vals.forEach((v, i) => (snr[i] = snr[i] || []).push([+t[1], +v]));
        const dead = Object.values(snr).every(p => allZero(p.map(x => x[1])));
        $('#eng-live2').hidden = dead; $('#eng-snr-note').hidden = !dead; $('#eng-snr-note').textContent = NO_SNR;
        if (!dead) lineChart($('#eng-chart2'), Object.entries(snr).map(([i, p]) => ({ name: `lane ${+i + 1} SNR dB`, points: p })), { minSpan: 2, legendEl: $('#eng-legend2') });
      }
      lols.push(t[4] || '0x00'); lolStrip($('#eng-lol'), lols);
    }
  };
}
function showBer(rec) {
  const dirs = rec.directions || (rec.direction ? { [`${rec.direction.generator} to ${rec.direction.checker}`]: rec.direction } : {});
  const first = Object.values(dirs)[0]; if (!first || !first.media_ber) return;
  $('#eng-ber').hidden = false;
  const mask = parseInt((first.lane_mask || 'ff').slice(0, 4), 16);
  barChart($('#eng-bars'), Object.entries(dirs).map(([k, d]) => ({ name: k.replace('_to_', ' to '), values: (d.media_ber || []).map((v, i) => ({ label: `L${i + 1}`, v, off: !(mask >> i & 1) })) })), $('#eng-bars-legend'));
  laneGrid($('#eng-lanes'), (first.media_ber || []).map((v, i) => ({ cls: !(mask >> i & 1) ? 'off' : (first.verdict || '').startsWith('MEASURED') ? 'pass' : 'fail', text: !(mask >> i & 1) ? 'n/a' : v > 0 ? v.toExponential(1) : '0' })));
}
async function runEng(cmd, body, title, form, onLine) {
  const res = $('#eng-result'), log = $('#eng-log'); log.textContent = ''; $('#eng-banner').innerHTML = ''; $('#eng-ber').hidden = true;
  const last = form && $('.last-run', form);
  if (last && body.budget_w) last.textContent = `last run: budget ${body.budget_w} W${body.reason ? ', reason: ' + body.reason : ''}`;
  res.innerHTML = '<div class="empty"><b>Running</b>The log fills below. The result appears here when the command ends.</div>';
  $$('#eng button[data-cmd], #eng button[data-cdb], #eng button[data-fw]').forEach(b => b.disabled = true);
  const live = liveEng(body), onEv = e => { live(e); if (onLine && e.type === 'line') onLine(e.text); };
  let result = null;
  try {
    toast(`${title} started`);
    const { job } = await api(`/api/${cmd}`, body);
    result = await stream(job, log, onEv);
    if (result && result.schema && String(result.schema).startsWith('aectest.cmis.decode')) res.innerHTML = decodeCard(title, result);
    else if (result && result.status === 'not available') res.innerHTML = `<div class="note"><b>${esc(title)}:</b> not available. ${esc(result.reason || '')}.</div>`;
    else if (cmd === 'features' && result && Array.isArray(result.rows)) res.innerHTML = featuresCard(title, result, body);
    else res.innerHTML = resultCard(title, result);
    if (result) fillLol(res, result);
    if (result && (result.directions || result.samples)) showBer(result);
    toast(`${title} finished`);
  } catch (e) {
    res.innerHTML = '<div class="empty"><b>No result</b>The command did not finish.</div>';
    alertBox($('#eng-banner'), e.message); if (e.refused) $('#eng-logbox').open = true;
  }
  loadUnits().then(() => {}); // the pair selects keep their values only if the units list is unchanged; cheap refresh of availability
  $$('#eng button[data-cmd], #eng button[data-cdb], #eng button[data-fw]').forEach(b => b.disabled = false);
  if (typeof fwImageState === 'function') fwImageState();
  return result;
}
function formBody(form) {
  const f = Object.fromEntries(new FormData(form));
  const confirm = [f.c1 && 'release-lpmode', f.c2 && 'usbc-power'].filter(Boolean);
  return { serial: f.serial, page: f.page !== undefined ? parseInt(f.page, 16) : undefined, off: f.off, n: f.n, hex: f.hex, hold: f.hold, budget_w: f.budget_w, reason: f.reason, confirm,
    pair: f.a ? `${f.a}:${f.b}` : undefined, time: f.time, pattern: f.pattern, apsel: f.apsel ? +f.apsel : null, force_cmis: !!f.force_cmis, save: !!f.save };
}
async function loadDb() {
  const d = await api('/api/skus');
  $('#db-list').innerHTML = d.skus.length ? `<table class="t"><thead><tr><th>File</th><th>Vendor</th><th>Part number</th><th>Author</th><th>State</th></tr></thead><tbody>` +
    d.skus.map(s => `<tr><td title="${esc(s.file)}">${esc(s.name)}</td><td title="${esc(s.vendor || '')}">${esc(s.vendor || '-')}</td><td title="${esc(s.part_number || '')}">${esc(s.part_number || '-')}</td><td>${!s.owner || s.owner === '<name>' ? '<span class="stamp" title="the <name> placeholder: put your name in the file">unset</span>' : esc(s.owner)}</td><td>${s.candidate ? '<span class="badge">candidate</span>' : s.proposed ? '<span class="badge warn">PROPOSED</span>' : '<span class="badge ok">reviewed</span>'}${s.schema_ok ? '' : ' <span class="badge fail">schema</span>'}</td></tr>`).join('') + '</tbody></table>'
    : '<div class="empty"><b>No criteria file</b>Derive one from a cable below.</div>';
  $('#db-msg').textContent = `${d.skus.length} file(s) in ${d.dir}`; $('#db-msg').className = 'msg info'; // a count, not an error
}
$('#db-reload').onclick = () => loadDb().catch(e => alertBox($('#eng-banner'), e.message));
$('#db-derive').onclick = async () => {
  const b = formBody($('#f-db')); const f = Object.fromEntries(new FormData($('#f-db')));
  const pv = $('#db-preview'); pv.hidden = false; pv.innerHTML = '<div class="empty"><b>Reading the cable</b>Identity, advertisement, capabilities, thresholds.</div>';
  $('#db-derive').disabled = true;
  try {
    const { job } = await api('/api/derive', { serial: b.serial, confirm: b.confirm, owner: f.owner, note: f.note });
    const d = await stream(job, $('#eng-log'), () => {});
    pv.innerHTML = derivePanel(d);
    toast(`Wrote ${d.rel}. Commit it after review.`, 0, true);
    loadDb();
  } catch (e) { pv.hidden = true; alertBox($('#eng-banner'), e.message); }
  $('#db-derive').disabled = false;
};
function derivePanel(d) {
  const lines = String(d.text || '').split('\n'), head = lines.slice(0, 20).join('\n'), more = lines.length - 20;
  const note = d.existed
    ? `A file for this cable exists. The new figures went to the .proposed.yaml beside it. Compare the two, merge what you accept into the committed file, then delete the proposal.`
    : `A new file. Every limit is marked PROPOSED. Review it, remove the PROPOSED tags you accept, then commit.`;
  return `<div class="card"><div class="row"><h3 style="margin:0" class="mono">${esc(d.rel)}</h3>${d.existed ? '<span class="badge warn">proposal beside the existing file</span>' : '<span class="badge ok">new file</span>'}${d.timing ? '<span class="badge">ready time measured</span>' : ''}</div>
    <p class="stamp">${note}</p>
    <div class="row"><code class="mono">git add ${esc(d.rel)}</code><button type="button" class="btn" data-copy="git add ${esc(d.rel)}">Copy git add line</button><span class="stamp copy-msg"></span></div>
    <pre class="mono-box">${esc(head)}${more > 0 ? `\n... ${more} more line(s) in the file` : ''}</pre></div>`;
}
async function copyText(text) { // the clipboard API needs a secure context (localhost is one); the textarea path covers the rest
  try { await Promise.race([navigator.clipboard.writeText(text), new Promise((_, rej) => setTimeout(() => rej(new Error('clipboard timeout')), 1500))]); return true; }
  catch (e) { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); let ok = false; try { ok = document.execCommand('copy'); } catch (e2) { ok = false; } ta.remove(); return ok; }
}
document.addEventListener('click', async ev => {
  const b = ev.target.closest('button[data-copy]'); if (!b) return;
  const ok = await copyText(b.dataset.copy); const msg = b.parentElement && $('.copy-msg', b.parentElement); if (msg) msg.textContent = ok ? 'copied' : 'copy failed: select the line and copy it';
});
// clipboard, or rich text (text/html + text/plain) while rendered. The textarea fallback stays plain.
function rawLog(pre) { if (pre.dataset.raw !== undefined) { pre.textContent = pre.dataset.raw; delete pre.dataset.raw; pre.classList.remove('rendered'); } const b = pre.parentElement && $('[data-render]', pre.parentElement); if (b) b.textContent = 'Render'; }
async function copyRich(html, text) {
  try { if (!navigator.clipboard.write || !window.ClipboardItem) throw new Error('no rich clipboard'); await navigator.clipboard.write([new ClipboardItem({ 'text/html': new Blob([html], { type: 'text/html' }), 'text/plain': new Blob([text], { type: 'text/plain' }) })]); return true; }
  catch (e) { return copyText(text); }
}
document.addEventListener('click', async ev => {
  const b = ev.target.closest('button[data-render],button[data-copy-log]'); if (!b) return;
  const pre = $(b.dataset.render || b.dataset.copyLog), msg = $('.copy-msg', b.parentElement);
  if (b.dataset.render !== undefined) { if (pre.dataset.raw !== undefined) rawLog(pre); else { pre.dataset.raw = pre.textContent; pre.innerHTML = md(pre.textContent); pre.classList.add('rendered'); b.textContent = 'Raw'; } return; }
  const rendered = pre.dataset.raw !== undefined, text = rendered ? pre.dataset.raw : pre.textContent;
  const ok = rendered ? await copyRich(pre.innerHTML, text) : await copyText(text);
  if (msg) msg.textContent = ok ? (rendered ? 'copied as rich text' : 'copied') : 'copy failed: select the text and copy it';
});
$$('#eng-tabs button[data-tab="db"]').forEach(b => b.addEventListener('click', () => loadDb().catch(() => {})));

const TITLES = { features: 'Feature matrix', status: 'Pins', id: 'Identity', decode: 'Decode every page', dump: 'Dump', reset: 'Reset timing', tristate: 'Tristate', 'reg-read': 'Register read', 'reg-write': 'Register write', release: 'Release and hold', prbs: 'PRBS both ways', 'media-link': 'Media link check', trend: 'Trend', loopback: 'Loopback', 'fw/info': 'Firmware info', 'fw/update': 'Firmware update' };
$$('#eng button[data-cmd]').forEach(b => b.onclick = () => {
  const form = b.closest('form'), msg = $('.msg', form);
  if (!form.checkValidity()) { if (msg) msg.textContent = 'Check the highlighted fields.'; form.reportValidity(); return; }
  if (msg) msg.textContent = '';
  const body = formBody(form);
  if (body.pair && body.pair.split(':')[0] === body.pair.split(':')[1]) { if (msg) msg.textContent = 'End A and End B must be two different units.'; return; }
  if (b.dataset.cmd === 'reg-write' && !body.hex) { msg.textContent = 'Give the bytes to write as hex.'; return; }
  runEng(b.dataset.cmd, body, TITLES[b.dataset.cmd] || b.dataset.cmd, form);
});
$$('#eng button[data-cdb]').forEach(b => b.onclick = () => runEng('cdb', { serial: $('#f-cdb [name=serial]').value, args: [b.dataset.cdb] }, 'CDB ' + b.textContent.trim()));
function featuresBody(exercise) { const b = formBody($('#f-features')); return { serial: b.serial, exercise: exercise || null, confirm: b.confirm, budget_w: b.budget_w, reason: b.reason }; }
function featuresCard(title, r, body) {
  const head = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">${esc(title)}</h2><span class="stamp" data-read-at="${Date.now()}" data-interval="3600000">read ${hhmmss()}</span></div>`;
  const chips = `<div class="chips">${r.module ? `<span class="badge info">${esc(`${r.module.vendor || ''} ${r.module.part_number || ''}`.trim())}</span>` : ''}${body && body.exercise ? `<span class="badge">exercised: ${esc(body.exercise)}</span>` : ''}${r.file ? `<span class="stamp">saved to ${esc(r.file)}</span>` : ''}</div>`;
  return head + chips + featuresHtml(r.rows, { esc }) + `<details class="raw"><summary>Raw JSON</summary><pre class="mono-box">${esc(JSON.stringify(r, null, 1))}</pre></details>`;
}
$$('#f-features button[data-feat]').forEach(b => b.onclick = () => {
  const msg = $('#features-msg'), body = featuresBody(b.dataset.feat === 'all' ? 'all' : null); msg.textContent = '';
  if (body.exercise && body.confirm.length < 2) { msg.textContent = 'Tick both confirmations: an exercise may release the module.'; return; }
  runEng('features', body, b.dataset.feat === 'all' ? 'Features, every one exercised' : 'Feature matrix', $('#f-features'));
});
$('#eng-result').addEventListener('click', ev => { // Exercise on a row or a group of the matrix card
  const b = ev.target.closest('button[data-exercise], button[data-exercise-group]'); if (!b) return;
  const body = featuresBody(b.dataset.exercise || b.dataset.exerciseGroup);
  if (body.confirm.length < 2) { alertBox($('#eng-banner'), 'Tick both confirmations on the Features form: an exercise may release the module.'); return; }
  runEng('features', body, `Exercise: ${body.exercise}`, $('#f-features'));
});
const FW_STAGE = { info: {}, plan: { dry_run: true }, download: { dry_run: false }, run: { dry_run: false, run_only: true, run_image: true }, commit: { dry_run: false, run_only: true, commit: true } };
let fwSteps = [];
function fwLine(text) { // the engine's lines drive the bar and the step rows; the password value never appears in a line
  const w = /write (\d+)\/(\d+) bytes, (\d+) blocks sent.*?([\d.]+) s/.exec(text);
  if (w) { const done = +w[1], total = +w[2], el = +w[4], pct = Math.round(100 * done / total), eta = el * (total - done) / Math.max(done, 1); $('#fw-progress').value = pct; $('#fw-progress-text').textContent = `${pct} %: ${w[3]} blocks, ${done} of ${total} bytes, ${Math.round(el)} s, about ${Math.round(eta)} s left`; }
  const c = /^CDB (0x[0-9a-fA-F]{4}) [\w-]+: (SUCCESS|FAILED[^,]*)/.exec(text);
  if (c) { const last = fwSteps[fwSteps.length - 1]; if (last && last[0] === cdbName(c[1]) && last[1] === c[2]) last[2] += 1; else fwSteps.push([cdbName(c[1]), c[2], 1]); } // repeated block writes fold into one row with a count
  if (/^password entry written/.test(text)) fwSteps.push(['password entry, page 00h bytes 122 to 125', 'written', 1]);
  if (c || /^password entry written/.test(text)) $('#fw-steps').innerHTML = fwSteps.map(([n, st, k]) => `<div class="pre-row"><span class="badge ${/SUCCESS|written/.test(st) ? 'pass' : 'fail'}">${esc(st.split(' ')[0])}</span><span>${esc(n)}${k > 1 ? ` x ${k}` : ''}</span></div>`).join('');
  if (/^ABORTED|^Run refused|did not answer/.test(text)) $('#fw-progress-text').textContent = text;
}
function fwAfter(res) { // what the operator does next, from the engine's result word
  const a = $('#fw-after'), r = (res && res.result) || '', m = (res && res.module) || {}; a.hidden = false;
  if (/^(UPDATED|RUNNING|COMMITTED)$/.test(r) && $('#fw-progress')) { $('#fw-progress').value = 100; $('#fw-progress-text').textContent = '100 %: the bank holds the image'; }
  a.innerHTML = /ABORTED/.test(r) ? `<b>Aborted before the run.</b> The committed image in bank ${esc(m.running || '?')} is untouched. The step rows name the command that failed.`
    : r === 'PLANNED' ? '<b>Plan read.</b> Nothing was written. Click Download when the gate FW-1 go is on record.'
    : r === 'UPDATED' ? '<b>The inactive bank holds the new image.</b> Click Run to start it. The module resets.'
    : r === 'RUNNING' ? '<b>The new image runs, uncommitted.</b> Run Test a cable on it. Then type the commit confirmation and click Commit.'
    : r === 'COMMITTED' ? '<b>Committed.</b> The module boots the new image at every reset. The old image stays in the other bank.'
    : `<b>${esc(r)}.</b> Read the log for the engine's note.`;
}
$$('#eng button[data-fw]').forEach(b => b.onclick = async () => {
  const form = b.closest('form'), f = Object.fromEntries(new FormData(form)), msg = $('.msg', form), stage = b.dataset.fw; msg.textContent = '';
  if (stage !== 'info' && !(f.image || '').trim()) { msg.textContent = 'Choose the image file first.'; return; }
  if (stage !== 'info' && stage !== 'plan' && (!f.c1 || !f.c2)) { msg.textContent = 'Tick both confirmations.'; return; }
  if ((stage === 'run' || stage === 'commit') && !f.image_version) { msg.textContent = 'Give the image version. Run and Commit find the bank by it.'; return; }
  if (stage === 'commit' && !f.confirm_text) { msg.textContent = 'Type the commit confirmation.'; return; }
  fwSteps = []; $('#fw-progress').value = 0; $('#fw-progress-text').textContent = ''; $('#fw-steps').innerHTML = ''; $('#fw-progress-box').hidden = stage === 'info' || stage === 'plan'; $('#fw-after').hidden = true;
  const cmd = stage === 'info' ? 'fw/info' : 'fw/update';
  const res = await runEng(cmd, { serial: f.serial, image: f.image, ...FW_STAGE[stage], password: f.password || null, image_version: f.image_version || null, header_bytes: f.header_bytes || null,
    confirm_text: f.confirm_text || null, confirm: [f.c1 && 'image-for-part', f.c2 && 'usbc-power'].filter(Boolean) }, TITLES[cmd] + (stage === 'info' ? '' : `, ${stage}`), form, stage === 'info' ? null : fwLine);
  if (res && stage !== 'info') fwAfter(res);
});
function fwImageState() { const has = !!$('[name=image]', $('#f-fw')).value.trim(); $$('#f-fw button[data-fw="plan"], #f-fw button[data-fw="download"]').forEach(b => b.disabled = !has); }
$('[name=image]', $('#f-fw')).oninput = fwImageState; fwImageState();
$('#fw-file').onchange = async ev => { // the browser's file -> /api/fw/image -> the server path in the box, with the name, size and sha256 on the card
  const file = ev.target.files && ev.target.files[0], info = $('#fw-file-info'); if (!file) return;
  info.textContent = `Uploading ${file.name}, ${file.size} bytes`;
  try {
    const buf = new Uint8Array(await file.arrayBuffer()); let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    const r = await api('/api/fw/image', { name: file.name, data_b64: btoa(bin) });
    $('[name=image]', $('#f-fw')).value = r.path; info.textContent = `${r.name}, ${r.size} bytes, sha256 ${r.sha256.slice(0, 16)}`; fwImageState(); toast('Image uploaded');
  } catch (e) { info.textContent = ''; $('#fw-msg').textContent = e.message; }
};
$('#fw-pw-show').onclick = ev => { const i = $('[name=password]', $('#f-fw')), show = i.type === 'password'; i.type = show ? 'text' : 'password'; ev.currentTarget.setAttribute('aria-pressed', String(show)); ev.currentTarget.textContent = show ? 'Hide' : 'Show'; };

const fmtScalar = x => typeof x === 'number' ? fmtNum(x) : typeof x === 'boolean' ? (x ? 'yes' : 'no') : String(x);
const isLaneList = v => Array.isArray(v) && v.length > 0 && v.every(x => x && typeof x === 'object' && 'lane' in x);
function fmtVal(v, unit) {
  if (v === null || v === undefined) return '<span class="na">-</span>';
  if (v === 'not advertised') return '<span class="na">not advertised</span>';
  if (Array.isArray(v)) {
    if (!v.length) return '<span class="na">none</span>';
    if (isLaneList(v)) return `<span class="lanes-mini">${v.map(x => `<span title="lane ${x.lane}">L${x.lane} ${esc(fmtScalar(x.value))}</span>`).join('')}</span>`;
    return esc(v.map(fmtScalar).join(', '));
  }
  if (typeof v === 'object') {
    if ('ok' in v && 'stored' in v) return `<span class="badge ${v.ok ? 'pass' : 'fail'}">${v.ok ? 'checksum ok' : 'checksum bad'}</span> <span class="mono">${esc(v.stored)} stored, ${esc(v.computed)} computed</span>`;
    return esc(Object.entries(v).filter(([, x]) => x !== null && x !== undefined).map(([k, x]) => `${k.replace(/_/g, ' ')} ${fmtScalar(x)}`).join(', '));
  }
  return esc(fmtScalar(v)) + (unit ? ` <span class="u">${esc(unit)}</span>` : '');
}
const hexLines = hex => (hex.match(/.{1,32}/g) || []).map((l, i) => `${(i * 16).toString().padStart(3, ' ')}  ${l.match(/.{2}/g).join(' ')}`).join('\n');
function pageBlock(blk, open) {
  const off = !!blk.note, n = (blk.fields || []).length;
  const rows = (blk.fields || []).map(f => `<tr><th title="${esc(f.reg)}">${esc(f.name)} <span class="stamp">${esc(f.reg)}</span></th><td>${fmtVal(f.value, f.advertised === false ? null : f.unit)}</td></tr>`).join('');
  return `<details class="pg${off ? ' off' : ''}"${open && !off ? ' open' : ''}><summary>${esc(blk.title)}<span class="stamp">${off ? esc(blk.note) : `${n} fields`}</span></summary>${rows ? `<table class="t">${rows}</table>` : ''}${blk.raw ? `<details class="raw"><summary>Raw bytes</summary><pre class="mono-box">${esc(hexLines(blk.raw))}</pre></details>` : ''}</details>`;
}
function laneView(rec) { // the status page 11h as one table: a row per lane field, a column per lane; set flags amber, good states green
  const blocks = Object.entries(rec.pages || {}).filter(([k]) => k === '11h' || k.startsWith('11h.b'));
  const rows = [];
  for (const [k, blk] of blocks) for (const f of blk.fields || []) {
    const name = f.name.replace(/\s*\(.*\)$/, '') + (k.includes('.b') ? ` (${k})` : '');
    if (isLaneList(f.value) && f.value.length === 8 && typeof f.value[0].value !== 'object') rows.push({ name, cells: f.value.map(x => ({ t: fmtScalar(x.value), c: /Activated|Success|TxOn|Initialized$/.test(String(x.value)) ? 'ok' : /Fail|Reject|Deactivated|Fault/.test(String(x.value)) ? 'bad' : '' })) });
    else if (Array.isArray(f.value) && /lanes set|lanes valid/.test(f.name) && f.advertised !== false) rows.push({ name, cells: [1, 2, 3, 4, 5, 6, 7, 8].map(l => f.value.includes(l) ? { t: 'set', c: 'set' } : { t: '-', c: 'clear' }) });
  }
  if (!rows.length) return '';
  return `<h3>Lane view (page 11h)${hb('dp_states')}</h3><div class="lane-wrap"><table class="lane-view"><tr><th>Field</th>${[1, 2, 3, 4, 5, 6, 7, 8].map(l => `<th>L${l}</th>`).join('')}</tr>${rows.map(r => `<tr><td>${esc(r.name)}</td>${r.cells.map(c => `<td class="${c.c}">${esc(c.t)}</td>`).join('')}</tr>`).join('')}</table></div>`;
}
function decodeCard(title, rec) {
  const id = rec.identity || {}, adv = rec.advertised || {}, order = Object.keys(rec.pages || {});
  const chips = [`CMIS ${rec.revision}`, `${rec.memory_model} memory`, adv.banks > 1 ? `${adv.banks} banks` : null, adv.diag ? 'diagnostic pages' : 'no diagnostic pages', adv.vdm ? 'VDM' : null, adv.cdb ? `CDB x${adv.cdb_instances || 1}` : null].filter(Boolean);
  const hollow = Object.entries(rec.hollow || {}).filter(([, v]) => v === true).map(([k]) => k.replace(/_/g, ' '));
  const vdm = rec.vdm && rec.vdm.instances && rec.vdm.instances.length ? `<details class="pg"><summary>VDM instances<span class="stamp">${rec.vdm.instances.length} in use${rec.vdm.descriptor_page_all_zero ? ', descriptor page all zero (hollow)' : ''}</span></summary><table class="t cols"><tr><th>#</th><th>Name</th><th>Resource</th><th>Sample</th><th>Unit</th></tr>${rec.vdm.instances.slice(0, 64).map(r => `<tr><td>${r.instance}</td><td>${esc(r.name)}</td><td>${esc(r.resource)}</td><td class="mono">${esc(fmtScalar(r.sample))}</td><td>${esc(r.unit || '')}</td></tr>`).join('')}</table></details>` : '';
  const cdb = rec.cdb_query ? `<details class="pg"><summary>CDB queries<span class="stamp">${Object.keys(rec.cdb_query).join(', ')}</span></summary>${kvTable(rec.cdb_query)}</details>` : '';
  return `<div class="row" style="justify-content:space-between"><h2 style="margin:0">${esc(title)}</h2><span class="stamp" data-read-at="${Date.now()}" data-interval="3600000">read ${hhmmss(rec.read_at)}</span></div>
    <div class="decode-head">${chips.map(c => `<span class="badge info">${esc(c)}</span>`).join('')}${id.vendor ? `<span class="id-s">${esc(id.vendor)} ${esc(id.part_number || '')}<small>serial ${esc(id.serial || '-')}, ${esc(id.module_state || '')}, class ${esc(id.power_class)} ${esc(id.max_power_w)} W</small></span>` : ''}</div>
    ${hollow.length ? `<div class="chips">${hollow.map(h => `<span class="badge warn">hollow: ${esc(h)}</span>`).join('')}</div>` : ''}
    ${rec.file ? `<div class="stamp">saved to ${esc(rec.file)}</div>` : ''}
    ${laneView(rec)}
    <h3>Pages</h3>${order.map(k => pageBlock(rec.pages[k], ['lower', '00h'].includes(k))).join('')}${vdm}${cdb}
    ${rec.not_read && Object.keys(rec.not_read).length ? `<p class="stamp">Not read (no verified gate bit): ${esc(Object.entries(rec.not_read).map(([k, v]) => `${k} ${v}`).join('; '))}</p>` : ''}
    <details class="raw"><summary>Raw JSON</summary><pre class="mono-box">${esc(JSON.stringify(rec, null, 1))}</pre></details>`;
}

// ---- Records
let FOLDERS = [], CABLES = [], UNASSIGNED = [], REC_MODE = localStorage.getItem('recmode') || 'cable';
const when = t => new Date(t * 1000).toLocaleString();
function fileRow(r) {
  const li = document.createElement('li'); li.innerHTML = `<span class="kind">${esc(r.kind)}</span>${esc(r.name)}<small>${when(r.time || r.mtime)}${r.unit ? `, unit ${esc(r.unit)}` : ''}${r.by_time ? ', by time' : ''}${REC_MODE === 'cable' ? `, ${esc(r.folder)}` : ''}</small>`;
  li.onclick = () => { $$('#record-list li').forEach(x => x.removeAttribute('aria-selected')); li.setAttribute('aria-selected', 'true'); showRecord(r); };
  return li;
}
function renderCables(q, box) {
  const hit = c => !q || [c.vendor, c.part_number, ...c.serials].join(' ').toLowerCase().includes(q);
  const shown = CABLES.map(c => ({ ...c, records: hit(c) ? c.records : c.records.filter(r => r.name.toLowerCase().includes(q)) })).filter(c => c.records.length);
  if (!shown.length && !UNASSIGNED.length) { box.innerHTML = `<div class="empty"><b>${CABLES.length ? 'No cable matches' : 'No cable records yet'}</b>${CABLES.length ? 'Clear the filter to see every cable.' : 'Read a cable or run a test first.'}</div>`; return; }
  shown.forEach((c, i) => {
    const d = document.createElement('details'); d.className = 'folder cable'; d.open = i === 0 || !!q;
    const what = c.kind === 'module' ? 'module' : 'cable';
    d.innerHTML = `<summary><span class="cable-id">${esc(c.vendor || '?')} ${esc(c.part_number)}<small>${esc(what)} ${esc(c.serials.join(', ') || 'no serial')}</small></span><span class="chips">${Object.entries(c.kinds).map(([k, n]) => `<span class="kind">${esc(k)} ${n}</span>`).join('')}</span></summary>
      <div class="stamp cable-meta">${what === 'module' ? 'One module with its own serial. A two-end record is listed under both modules.' : 'One cable with its end serials.'} First seen ${when(c.first_seen)}, last seen ${when(c.last_seen)}, units ${esc(c.units.join(', ') || '-')}</div><ul class="list"></ul>`;
    const ul = $('ul', d); c.records.forEach(r => ul.appendChild(fileRow(r))); box.appendChild(d);
  });
  const un = UNASSIGNED.filter(r => !q || r.name.toLowerCase().includes(q));
  if (un.length) { const d = document.createElement('details'); d.className = 'folder'; d.innerHTML = `<summary>No cable identity<span class="badge">${un.length}</span></summary><ul class="list"></ul>`; const ul = $('ul', d); un.forEach(r => ul.appendChild(fileRow(r))); box.appendChild(d); }
}
function renderRecords() {
  const q = ($('#record-filter').value || '').toLowerCase(), box = $('#record-list'); box.innerHTML = '';
  $('#rec-by-cable').setAttribute('aria-pressed', REC_MODE === 'cable'); $('#rec-by-folder').setAttribute('aria-pressed', REC_MODE !== 'cable');
  if (REC_MODE === 'cable') return renderCables(q, box);
  const shown = FOLDERS.map(f => ({ ...f, files: f.files.filter(r => !q || r.name.toLowerCase().includes(q)) })).filter(f => f.files.length);
  if (!shown.length) { box.innerHTML = `<div class="empty"><b>${FOLDERS.length ? 'No file matches' : 'No records yet'}</b>${FOLDERS.length ? 'Clear the filter to see every file.' : 'Run a command first.'}</div>`; return; }
  shown.forEach((f, i) => {
    const d = document.createElement('details'); d.className = 'folder'; d.open = i === 0 || !!q;
    d.innerHTML = `<summary>${esc(f.folder === '.' ? 'top level' : f.folder)}<span class="badge">${f.files.length}</span></summary><ul class="list"></ul>`;
    const ul = $('ul', d); f.files.forEach(r => ul.appendChild(fileRow(r))); box.appendChild(d);
  });
}
async function loadRecords() {
  try { const idx = await api('/api/records'); FOLDERS = idx.folders; CABLES = idx.cables || []; UNASSIGNED = idx.unassigned || []; } catch (e) { alertBox($('#records-banner'), e.message); return; }
  renderRecords();
}
$('#records-reload').onclick = loadRecords;
$('#record-filter').oninput = renderRecords;
$('#rec-by-cable').onclick = () => { REC_MODE = 'cable'; localStorage.setItem('recmode', REC_MODE); renderRecords(); };
$('#rec-by-folder').onclick = () => { REC_MODE = 'folder'; localStorage.setItem('recmode', REC_MODE); renderRecords(); };
async function showRecord(r) {
  const pane = $('#record-pane');
  let d; try { d = await api(`/api/record?path=${encodeURIComponent(r.path)}`); } catch (e) { alertBox(pane, e.message); return; }
  const head = `<div class="row" style="justify-content:space-between"><div><h2 style="margin:0">${esc(r.name)}</h2><span class="stamp">${esc(r.folder === '.' ? 'top level' : r.folder)}, ${esc(r.kind)}</span></div><a class="btn" href="/api/record?path=${encodeURIComponent(r.path)}" target="_blank">Open the file</a></div>`;
  const charts = `<svg class="chart" id="rc1"></svg><ul class="legend" id="rl1"></ul><div id="rc2box" hidden><svg class="chart" id="rc2"></svg><ul class="legend" id="rl2"></ul></div><div class="lanes" id="rlanes"></div>`;
  if (d.rows) { // CSV: T7 trend or T11 trend
    pane.innerHTML = head + charts + `<details class="raw"><summary>Header</summary><pre class="mono-box">${esc(Object.entries(d.meta).map(([k, v]) => `${k}: ${v}`).join('\n'))}</pre></details>`;
    const rows = d.rows.filter(x => x.t_s);
    if (rows[0] && rows[0].lane1_ber !== undefined) {
      lineChart($('#rc1'), [1, 2, 3, 4, 5, 6, 7, 8].map(i => ({ name: `lane ${i} BER`, points: rows.filter(x => x[`lane${i}_ber`]).map(x => [+x.t_s, +x[`lane${i}_ber`]]) })).filter(s => s.points.length), { ylog: true, legendEl: $('#rl1') });
      const snr = [1, 2, 3, 4, 5, 6, 7, 8].map(i => ({ name: `lane ${i} SNR dB`, points: rows.filter(x => +x[`lane${i}_snr_db`]).map(x => [+x.t_s, +x[`lane${i}_snr_db`]]) })).filter(s => s.points.length);
      if (snr.length) { $('#rc2box').hidden = false; lineChart($('#rc2'), snr, { minSpan: 2, legendEl: $('#rl2') }); }
      else if (rows.some(x => x.lane1_snr_db !== undefined && x.lane1_snr_db !== '')) $('#rc1').insertAdjacentHTML('afterend', `<div class="stamp">${esc(NO_SNR)}</div>`);
      if (rows[0].media_lol !== undefined) { $('#rlanes').insertAdjacentHTML('beforebegin', '<div class="lol-box"></div>'); lolStrip($('.lol-box', pane), rows.map(x => x.media_lol || '0x00')); }
    } else lineChart($('#rc1'), healthSeries({ module: rows.filter(x => x.temp_c).map(x => [+x.t_s, +x.temp_c, +x.vcc_v]) }), { axes: ['left', 'right'], legendEl: $('#rl1') });
  } else if (d.directions) { // T11 PRBS
    const points = {};
    const dirs = Object.keys(d.directions);
    for (const [k, dir] of Object.entries(d.directions)) for (const [name, hs] of Object.entries(dir.health || {})) points[`${dirs.length > 1 ? `run ${dirs.indexOf(k) + 1}, ` : ''}${name}`] = hs.map(h => [h.t_s, h.temp_c, h.vcc_v]);
    pane.innerHTML = head + runCards(d) + charts + `<details class="raw"><summary>Raw JSON</summary><pre class="mono-box">${esc(JSON.stringify(d, null, 1))}</pre></details>`;
    lineChart($('#rc1'), healthSeries(points), { axes: ['left', 'right'], legendEl: $('#rl1') });
    $('#rc2box').hidden = false; const mask = parseInt((Object.values(d.directions)[0].lane_mask || 'ff').slice(0, 4), 16);
    barChart($('#rc2'), Object.entries(d.directions).map(([k, v]) => ({ name: k.replace('_to_', ' to '), values: (v.media_ber || []).map((b, i) => ({ label: `L${i + 1}`, v: b, off: !(mask >> i & 1) })) })), $('#rl2'));
  } else if (d.samples) { // trend / loopback JSON
    pane.innerHTML = head + runCards(d) + charts + `<details class="raw"><summary>Raw JSON</summary><pre class="mono-box">${esc(JSON.stringify(d, null, 1))}</pre></details>`;
    fillLol(pane, d);
    lineChart($('#rc1'), [0, 1, 2, 3, 4, 5, 6, 7].map(i => ({ name: `lane ${i + 1} BER`, points: d.samples.filter(s => s.media_ber && s.media_ber[i] > 0).map(s => [s.t_s, s.media_ber[i]]) })).filter(s => s.points.length), { ylog: true, legendEl: $('#rl1') });
    const snr = [0, 1, 2, 3, 4, 5, 6, 7].map(i => ({ name: `lane ${i + 1} SNR dB`, points: d.samples.filter(s => s.media_snr_db && s.media_snr_db[i]).map(s => [s.t_s, s.media_snr_db[i]]) })).filter(s => s.points.length);
    if (snr.length) { $('#rc2box').hidden = false; lineChart($('#rc2'), snr, { minSpan: 2, legendEl: $('#rl2') }); }
    else if (d.samples.some(s => s.media_snr_db)) $('#rc1').insertAdjacentHTML('afterend', `<div class="stamp">${esc(NO_SNR)}</div>`);
  } else if (d.schema && String(d.schema).startsWith('aectest.cmis.decode')) { pane.innerHTML = head + decodeCard(r.name, d); }
  else if (d.module !== undefined && d.unit) { pane.innerHTML = head + plate(d); }
  else if (d.text !== undefined) { pane.innerHTML = head + `<pre class="mono-box">${esc(d.text)}</pre>`; if (/report\.md$/.test(r.path)) recordStepper(r.path.replace(/report\.md$/, 'events.jsonl'), pane); }
  else pane.innerHTML = head + resultCard(r.name, d.decoded || d);
}

async function recordStepper(path, pane) {
  let ev; try { ev = await api(`/api/record?path=${encodeURIComponent(path)}`); } catch (e) { return; }
  const data = new Map(), skipped = new Map(); let cur = null;
  for (const line of (ev.text || '').split('\n')) { if (!line.trim()) continue; let e; try { e = JSON.parse(line); } catch (e2) { continue; }
    if (e.type === 'step') { cur = e.data.name; data.set(cur, { events: [], lines: [] }); continue; }
    if (e.type === 'skip') { skipped.set(e.data.name, e.data.reason || 'skipped'); continue; }
    if (cur) data.get(cur).events.push(e); }
  if (!data.size) return;
  const steps = INFO.tech_steps.filter(s => data.has(s) || skipped.has(s));
  const box = document.createElement('div'); box.innerHTML = `<h3>Steps</h3><div class="stepper" id="rec-stepper"></div><div id="rec-step-body"></div>`;
  pane.insertBefore(box, pane.children[1] || null);
  const el = $('#rec-stepper', box); el.innerHTML = stepButtons(steps, steps.length, data, skipped);
  const open = name => { const d = data.get(name); if (!d) return; stepView(d, $('#rec-step-body', box)); $$('button.step', el).forEach(b => b.setAttribute('aria-selected', b.dataset.step === name)); };
  stepperKeys(el, open);
}

// ---- start
connect();
loadInfo().catch(() => {});
renderConn();
$('#glossary').innerHTML = Object.entries(HELP).map(([k, v]) => `<dt>${esc(label(k))}</dt><dd>${esc(v)}</dd>`).join('');
$('#help-steps').innerHTML = Object.values(STEP_INFO).map(i => `<dt>${esc(i.title)}</dt><dd>${esc(i.does)} ${esc(i.judges)} ${esc(i.time)}</dd>`).join('');
loadUnits().then(() => firstUse('cable')).catch(e => { alertBox($('#cable-banner'), e.message); });
