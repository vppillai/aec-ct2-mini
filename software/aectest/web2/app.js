// pattern, the decode tree and the records. Shared with web/: conn.js (the heartbeat reducer), labels.js, help.js, md.js.
'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const hhmmss = t => new Date(t ? t * 1000 : Date.now()).toLocaleTimeString([], { hour12: false });
const mmss = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
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
let INFO = { units: [], tech_steps: [] }, STEPS = [], STEP_ORDER = [], CAPS = [];

// ---- small UI: toasts, hints, theme, views, keys
const toasts = $('#toasts'), pop = $('#pop');
function toast(text, ms = 5000) { const t = document.createElement('div'); t.className = 'toast'; t.textContent = text; t.onclick = () => t.remove(); toasts.appendChild(t); setTimeout(() => t.remove(), ms); }
function showHint(el) { pop.textContent = el.dataset.hint; pop.hidden = false; const r = el.getBoundingClientRect(); pop.style.left = Math.min(r.left, window.innerWidth - 300) + 'px'; pop.style.top = (r.bottom + 6) + 'px'; }
document.addEventListener('mouseover', e => { const h = e.target.closest('.hint'); if (h) showHint(h); });
document.addEventListener('mouseout', e => { if (e.target.closest('.hint')) pop.hidden = true; });
document.addEventListener('focusin', e => { const h = e.target.closest('.hint'); if (h) showHint(h); });
document.addEventListener('focusout', e => { if (e.target.closest('.hint')) pop.hidden = true; });
document.addEventListener('click', e => { const h = e.target.closest('.hint'); if (h) { e.preventDefault(); showHint(h); } });
const hb = k => HELP[k] ? `<button type="button" class="hint" data-hint="${esc(HELP[k])}">?</button>` : '';
function setTheme(t) { document.documentElement.dataset.theme = t; localStorage.setItem('theme', t); }
setTheme(localStorage.getItem('theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
$('#theme-toggle').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
function show(view) {
  $$('.view').forEach(v => v.toggleAttribute('data-active', v.id === view));
  $$('#rail .nav').forEach(b => b.dataset.view === view ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current'));
  localStorage.setItem('view', view);
  if (view === 'records') loadRecords();
  window.scrollTo(0, 0);
}
$$('#rail .nav').forEach(b => b.onclick = () => show(b.dataset.view));
const KEYS = { 1: 'cable', 2: 'test', 3: 'explore', 4: 'records', '?': 'help' };
document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea') || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'Escape') { pop.hidden = true; $$('.toast').forEach(t => t.remove()); }
  if (KEYS[e.key]) show(KEYS[e.key]);
});

// ---- connection (conn.js): the heartbeat, a restart reloads, a difference in the unit list reloads the cards
let CONN = connInit(), es = null, seenUnits = null;
function renderConn() {
  const el = $('#conn'); el.className = 'conn ' + CONN.status;
  $('#conn-text').textContent = CONN.status === 'live' ? `Live, ${hhmmss(CONN.lastBeat)}` : CONN.status === 'down' ? 'Server down, reconnecting' : 'Connecting';
  $('#offline').hidden = CONN.status !== 'down';
}
function connect() {
  if (es) es.close();
  es = new EventSource('/api/events');
  es.onmessage = ev => {
    const e = JSON.parse(ev.data);
    const was = CONN; CONN = connReduce(CONN, e); renderConn();
    if (CONN.restarted || (was.status === 'down' && CONN.status === 'live')) { toast(CONN.restarted ? 'The server restarted. Reloaded.' : 'Back online. Reloaded.'); refreshAll(); }
    unitsSeen(e.units || []);
  };
  es.onerror = () => { es.close(); CONN = connReduce(CONN, { type: 'drop' }); renderConn(); setTimeout(() => { CONN = connReduce(CONN, { type: 'retry' }); connect(); }, connBackoffMs(CONN.attempts)); };
}
function unitsSeen(names) { const key = names.join(','); if (seenUnits !== null && key !== seenUnits) { toast(names.length > (seenUnits ? seenUnits.split(',').filter(Boolean).length : 0) ? 'A unit arrived.' : 'A unit left.'); loadUnits(); } seenUnits = key; }
async function refreshAll() { await loadUnits(); if ($('#records[data-active]')) loadRecords(); }
async function loadInfo() { const i = await api('/api/info'); $('#version').textContent = `aectest ${i.version}`; $('#help-version').textContent = `This is aectest ${i.version}. The release notes list every public version.`; $('#help-changelog').innerHTML = md(i.changelog); }

// ---- jobs: the SSE stream of one command
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
    src.onerror = () => { src.close(); reject(Object.assign(new Error('The connection dropped during the run. The last known state is in the log.'), { lost: true, last })); };
  });
}

// ---- errors as sentences (design section 8)
const FIXES = [
  [/open by another process|lock/i, m => ({ text: `${(/unit (\S+) is open/.exec(m) || [])[1] || 'The unit'} is open in another program.`, fix: 'Stop that program, then Refresh units.' })],
  [/confirmations/, () => ({ text: 'This command releases the module from low power.', fix: 'Tick both guard boxes first.' })],
  [/budget|MaxPower/, m => ({ text: m, fix: 'Raise the budget with a reason in the guards, or test a module within the budget.' })],
  [/CMIS 4\.0 < 5\.0|force-cmis|force_cmis/, () => ({ text: 'This module is below CMIS 5.0.', fix: 'Tick "Force on CMIS below 5.0". It may still have no diagnostic pages.' })],
  [/not advertised|13h\/14h|no diagnostic/, m => ({ text: m, fix: 'This module has no diagnostic pages. PRBS and loopback cannot run on it.' })],
  [/No USB device|cannot open|No such device/, m => ({ text: 'The unit does not answer on USB.', fix: 'Check the PC LED and the cable, then Refresh units.' })],
  [/Nack|NACK|does not answer/, () => ({ text: 'The module does not answer.', fix: 'In MAN the MSEL slide at O or H deselects it. Set MODE to AUTO, then Refresh.' })],
  [/no image file|image file path/, () => ({ text: 'No image file yet.', fix: 'Choose the image file first.' })],
  [/no criteria/i, m => ({ text: m, fix: 'Create a criteria file in Explore, Database, then test again.' })],
];
function fixText(msg) {
  const m = String(msg).replace(/^(Refused|Nack|KeyError|ValueError|OSError|SystemExit|RuntimeError|Exception): /, '').replace(/^REFUSED: /, '').replace(/^Error: /, '');
  for (const [re, f] of FIXES) if (re.test(m)) return f(m);
  return { text: m, fix: '' };
}
function alertBox(el, msg, retry) {
  const { text, fix } = fixText(msg);
  el.innerHTML = `<div class="alert"><svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01M10.3 3.9L2.5 17.5a2 2 0 0 0 1.7 3h15.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg><div><b>${esc(text)}</b>${fix ? `<div class="fix">${esc(fix)}</div>` : ''}</div>${retry ? `<div class="row"><button class="btn small" data-retry>${esc(retry.label || 'Try again')}</button></div>` : ''}</div>`;
  if (retry) $('[data-retry]', el).onclick = retry.fn;
}

// ---- rendering: tables with labels and bubbles, badges, run cards
const label = k => (LABELS[k] || [human(k)])[0];
const fmtNum = v => Number.isInteger(v) ? String(v) : (Math.abs(v) < 1e-3 && v !== 0 ? v.toExponential(2) : String(+v.toFixed(4)));
function stateBadge(st) { return `<span class="badge ${/Ready/.test(st) ? 'pass' : /Fault/.test(st) ? 'fail' : /LowPwr/.test(st) ? 'info' : 'warn'}">${esc(st)}</span>`; }
function verdictBadge(v) { const s = String(v || 'none'); return `<span class="badge ${/^MEASURED|^PASS/.test(s) ? 'pass' : /INCONCLUSIVE/.test(s) ? 'warn' : /FAIL|ABORT|REFUSED|lost/.test(s) ? 'fail' : ''}" title="${esc(s)}">${esc(s.split(/[(:]/)[0].trim())}</span>`; }
const FMT_ANY = { supported_cmds: ids => Array.isArray(ids) ? cdbGroups(ids).map(([f, names]) => `<div class="sub"><span>${esc(f)}</span> ${names.map(n => `<span class="badge plain mono">${esc(n)}</span>`).join(' ')}</div>`).join('') : esc(String(ids)) };
const HTML_FMT = { module_state: v => stateBadge(v), verdict: v => verdictBadge(v), result: v => verdictBadge(v) };
function kvTable(obj, depth = 0) {
  if (obj === null || obj === undefined) return '<span class="stamp">none</span>';
  if (Array.isArray(obj)) {
    if (!obj.length) return '<span class="stamp">empty</span>';
    if (obj.every(x => typeof x !== 'object' || x === null)) return `<span class="mono wrap">${esc(obj.slice(0, 16).map(v => typeof v === 'number' ? fmtNum(v) : String(v)).join(', '))}${obj.length > 16 ? ` and ${obj.length - 16} more` : ''}</span>`;
    if (obj.every(x => x && typeof x === 'object' && !Array.isArray(x))) {
      if (obj.length > 40 || depth > 1) return `<span class="stamp">${obj.length} rows, see the raw JSON</span>`;
      const cols = [...new Set(obj.flatMap(Object.keys))].slice(0, 10);
      return `<div class="tscroll"><table class="t compact"><tr>${cols.map(c => `<th>${esc(label(c))}${hb(c)}</th>`).join('')}</tr>${obj.map(r => `<tr>${cols.map(c => `<td>${kvTable(r[c], depth + 1)}</td>`).join('')}</tr>`).join('')}</table></div>`;
    }
    return `<span class="stamp">${obj.length} items, see the raw JSON</span>`;
  }
  if (typeof obj === 'object') {
    const keys = Object.keys(obj);
    if (depth > 2 || keys.length > 40) return `<span class="stamp">${keys.length} fields, see the raw JSON</span>`;
    const rows = labelRows(obj).map(([k, l, text]) => [k, l, FMT_ANY[k] && obj[k] != null ? FMT_ANY[k](obj[k]) : text !== null ? (HTML_FMT[k] ? HTML_FMT[k](obj[k]) : esc(text)) : kvTable(obj[k], depth + 1)]);
    if (depth >= 1) return `<div class="sub">${rows.map(([k, l, h]) => `<div><span title="${esc(k)}">${esc(l)}</span><span>${h}</span></div>`).join('')}</div>`;
    return `<table class="t">${rows.map(([k, l, h]) => `<tr><th>${esc(l)}${hb(k)}</th><td>${h}</td></tr>`).join('')}</table>`;
  }
  if (typeof obj === 'string' && /^(SUCCESS|FAILED) \(/.test(obj)) return `<span class="badge ${obj.startsWith('SUCCESS') ? 'pass' : 'fail'}" title="${esc(obj)}">${esc(obj.split(' (')[0])}</span>`; // a CDB status: the word, the text on hover (batch 7)
  if (typeof obj === 'string' && obj.length > 160) return `<span class="mono wrap">${esc(obj.slice(0, 160))} and ${obj.length - 160} more characters</span>`;
  if (typeof obj === 'boolean') return `<span class="badge ${obj ? 'pass' : 'plain'}">${obj ? 'yes' : 'no'}</span>`;
  if (typeof obj === 'number') return `<span class="mono">${fmtNum(obj)}</span>`;
  return esc(obj);
}
const berClass = v => v === null || v === undefined || Number.isNaN(+v) ? 'off' : v < 1e-6 ? 'pass' : v <= 2.4e-4 ? 'warn' : 'fail';
const BER_TEXT = { pass: 'good', warn: 'pre-FEC range', fail: 'high', off: 'n/a' };
const fmtBer = v => v > 0 ? (+v).toExponential(1) : v === 0 ? '0' : 'n/a';
function laneTable(ber, mask, snr) {
  const n = (ber || []).length;
  return `<div class="tscroll"><table class="t compact"><tr><th>Lane</th>${Array.from({ length: n }, (_, i) => `<th>L${i + 1}</th>`).join('')}</tr>
    <tr><th>BER</th>${(ber || []).map((v, i) => { const on = mask >> i & 1; const c = on ? berClass(v) : 'off'; return `<td class="num lc ${c}" title="${BER_TEXT[c]}">${on ? fmtBer(v) : 'n/a'}</td>`; }).join('')}</tr>
    ${snr && !allZero(snr) ? `<tr><th>SNR dB</th>${snr.map(v => `<td class="num">${v ? (+v).toFixed(1) : 'n/a'}</td>`).join('')}</tr>` : ''}</table></div>${snr && allZero(snr) ? `<div class="stamp">${esc(NO_SNR)}</div>` : ''}`;
}
function endCard(name, e) {
  const dp = e.dp_states_11h_128 ? [...new Set(e.dp_states_11h_128)] : [];
  return `<div class="mini"><h3>${esc(name)}</h3><div class="id-s">${esc(`${e.vendor || ''} ${e.part_number || ''}`.trim() || 'module')}<small>${esc(e.serial || '')}</small></div><div class="chips" style="margin-top:6px">${e.power_class !== undefined ? `<span class="badge info">class ${esc(e.power_class)}, ${esc(e.max_power_w)} W</span>` : ''}${e.release && e.release.module_ready_observed_s !== undefined ? `<span class="badge">ready after ${esc(e.release.module_ready_observed_s)} s</span>` : ''}${dp.length ? `<span class="badge ${dp.length === 1 && /Activated/.test(dp[0]) ? 'pass' : 'warn'}">${esc(dp.join(', '))}</span>` : ''}</div></div>`;
}
function lolStrip(el, lols) {
  if (!el) return; if (!lols || !lols.length) { el.hidden = true; return; }
  const n = lols.filter(v => parseInt(v, 16)).length; el.hidden = false;
  el.innerHTML = `<div class="stamp">Loss of lock per second: ${n ? `${n} of ${lols.length} s` : `none in ${lols.length} s`}</div><div class="lol-strip">${lols.map((v, i) => `<i class="${parseInt(v, 16) ? 'lol' : ''}" title="${i + 1} s: ${parseInt(v, 16) ? 'lanes ' + esc(v) : 'locked'}"></i>`).join('')}</div>`;
}
function runCards(r) {
  const mask0 = parseInt(String(r.lane_mask || 'ff').slice(0, 4), 16);
  let html = `<div class="chips">${verdictBadge(r.verdict)}${r.pattern ? `<span class="badge plain">${esc(r.pattern)}, ${esc(r.seconds)} s</span>` : ''}${r.lane_mask ? `<span class="badge plain">lanes ${esc(String(r.lane_mask).slice(0, 4))}</span>` : ''}${r.apsel ? `<span class="badge plain">application ${esc(r.apsel)}</span>` : ''}${r.kind ? `<span class="badge plain">${esc(r.kind)}</span>` : ''}</div>`;
  const ends = Object.entries(r.ends || {});
  if (ends.length) html += `<div class="minis">${ends.map(([k, e]) => endCard(k, e)).join('')}</div>`;
  const dirs = r.directions ? Object.entries(r.directions) : r.direction ? [[`${r.direction.generator} to ${r.direction.checker}`, r.direction]] : [];
  if (r.samples && r.samples.length && !r.direction) { const last = r.samples[r.samples.length - 1]; dirs.push([`last sample, ${last.t_s} s`, { media_ber: last.media_ber, verdict: r.verdict, snr: last.media_snr_db }]); }
  for (const [k, d] of dirs) {
    const mask = parseInt(String(d.lane_mask || r.lane_mask || 'ff').slice(0, 4), 16) || mask0;
    html += `<div class="mini" style="margin-top:8px"><h3>${esc(k.replace('_to_', ' to '))} ${verdictBadge(d.verdict)}</h3>${laneTable(d.media_ber, mask, d.snr)}${d.media_lol_before_gate_14h_139 !== undefined ? `<div class="stamp">loss of lock before the gate ${esc(d.media_lol_before_gate_14h_139)}, at the end ${esc(d.media_lol_14h_139)}</div>` : ''}</div>`;
  }
  if (r.samples && r.samples.length && r.samples[0].media_lol !== undefined) html += '<div class="lol-box"></div>';
  if (r.lanes_linked) html += `<div class="lanes">${r.lanes_linked.map((ok, i) => `<div class="lane ${ok ? 'pass' : 'fail'}">L${i + 1}<br>${ok ? 'linked' : 'flag'}</div>`).join('')}</div>`;
  return html;
}
const isRun = r => !!(r && (r.directions || r.direction || r.samples || r.lanes_linked));
function fillLol(container, rec) { const box = $('.lol-box', container); if (box && rec.samples) lolStrip(box, rec.samples.map(s => s.media_lol || '0x00')); }
function summarize(r) {
  if (r && r.pages && r.decoded) return { ...r.decoded, pages: Object.keys(r.pages).join(', ') };
  if (r && r.kind === 'audit') return { module: r.module, score: `${r.score.pass} of ${r.score.applicable} applicable checks pass`, rows: r.rows };
  if (r && r.kind === 'timing') return { verdict: r.verdict, module: r.module, steps: r.steps.map(s => ({ step: s.name, measured: s.measured_s !== undefined ? s.measured_s : s.measured, limit: s.limit_s !== undefined ? s.limit_s : s.limit, within: s.within, note: s.note })) };
  if (r && r.kind === 'flags') return { verdict: r.verdict, module: r.module, checks: r.checks };
  if (r && r.kind === 'i2c-stress') return { verdict: r.verdict, module: r.module, rounds: r.rounds, 'reads of 128 bytes': r.reads_128b, NACKs: r.nacks, 'page read-back mismatches': r.page_readback_mismatches, 'ms per read': r.read_ms, 'bytes per s': r.bytes_per_s, 'deselected read NACKs': r.deselected_read_nacks };
  if (r && r.plan && r.steps) return { result: r.result, image: r.image, plan: r.plan, steps: r.steps };
  if (r && r.features !== undefined && r.active_fw !== undefined) return { 'active firmware': r.active_fw, 'inactive firmware': r.inactive_fw, state: r.module_state, download: r.download, features: r.features, images: r.images };
  return r;
}
function rowsFirst(shown) { // a result with one list of rows (audit rows, timing steps, flag checks, firmware steps): the facts as chips, the rows as the table
  const key = ['rows', 'steps', 'checks'].find(k => Array.isArray(shown[k]) && shown[k].length && shown[k].every(x => x && typeof x === 'object'));
  if (!key) return kvTable(shown);
  const rest = { ...shown }; delete rest[key];
  const chips = Object.entries(rest).filter(([, v]) => v === null || typeof v !== 'object').map(([k, v]) => `<span class="badge ${k === 'verdict' || k === 'result' ? (/PASS|UPDATED|COMMITTED|RUNNING|PLANNED/.test(String(v)) ? 'pass' : /FAIL|ABORT|REFUSED/.test(String(v)) ? 'fail' : 'warn') : 'plain'}">${esc(label(k))}: ${esc(typeof v === 'boolean' ? (v ? 'yes' : 'no') : v ?? 'none')}</span>`).join('');
  const objs = Object.entries(rest).filter(([, v]) => v && typeof v === 'object');
  return `<div class="chips">${chips}</div>${objs.map(([k, v]) => `<h3 style="margin-top:12px">${esc(label(k))}</h3>${kvTable(v)}`).join('')}<h3 style="margin-top:12px">${esc(label(key))}</h3>${kvTable(shown[key])}`;
}
function resultCard(title, obj) {
  const shown = summarize(obj);
  return `<div class="card-head"><h2>${esc(title)}</h2><span class="stamp">read ${hhmmss()}</span></div>${isRun(obj) ? runCards(obj) : rowsFirst(shown)}<details class="raw"><summary>Raw JSON</summary><pre class="mono-box">${esc(JSON.stringify(obj, null, 1))}</pre></details>`;
}

// ---- the plate and the decode tree
function plate(pp) {
  const m = pp.module;
  if (!m) return '<div class="empty"><b>No module in this unit</b>Insert a cable end, then click Read cable.</div>';
  const b = pp.budget || {}, d = pp.diagnostics, ho = pp.hollow || {};
  const apps = pp.applications || [];
  const hollow = [ho.tx_power_identical_on_every_lane && `Tx power reads ${(ho.tx_power_uw || [])[0]} uW on every lane: a placeholder, not a monitor`,
    ho.rx_power_identical_on_every_lane && `Rx power reads ${(ho.rx_power_uw || [])[0]} uW on every lane: a placeholder, not a monitor`,
    ho.vdm_descriptor_page_all_zero && 'VDM advertised, but the descriptor page 20h is all zero: no VDM observables'].filter(Boolean);
  return `<div class="plate">
    <div><div class="id">${esc(m.vendor)} ${esc(m.part_number)}<small>serial ${esc(m.serial)}, rev ${esc(m.revision || '-')}, date ${esc(m.date_code || '-')}</small></div>
      <div class="chips">${stateBadge(m.module_state)}<span class="badge info">CMIS ${esc(m.cmis_revision)}, ${esc(m.memory_model)}</span><span class="badge ${b.within ? 'pass' : 'fail'}">class ${esc(m.power_class)}, ${esc(m.max_power_w)} W ${b.within ? 'within' : 'above'} the ${esc(b.budget_w)} W budget</span><span class="badge plain" title="${esc(m.media_interface_technology_00h_212)}">${esc(String(m.media_interface_technology_00h_212 || '').replace(/^0x[0-9a-fA-F]+ /, ''))}</span></div>
      <dl class="kv"><dt>Temperature${hb('temp_c')}</dt><dd>${(+m.temp_c).toFixed(1)} C</dd><dt>Identifier${hb('identifier_name')}</dt><dd>${esc(m.identifier_name)} (${esc(m.identifier)})</dd>
        <dt>Module state${hb('module_state')}</dt><dd>${esc(m.module_state)}</dd><dt>Memory${hb('memory_model')}</dt><dd>${esc(m.memory_model)}</dd>
        <dt>Diagnostic pages${hb('Diagnostic pages')}</dt><dd>${pp.advertised && pp.advertised.diagnostic_pages ? 'yes' : 'no'}</dd><dt>CDB${hb('CDB')}</dt><dd>${pp.advertised ? pp.advertised.cdb_instances : 0} instance(s)</dd><dt>VDM${hb('VDM')}</dt><dd>${pp.advertised && pp.advertised.vdm ? 'yes' : 'no'}</dd>${flagNames(pp.flags_at_read).length ? `<dt>Flags at read${hb('flags_at_read')}</dt><dd>${esc(flagNames(pp.flags_at_read).join(', '))}</dd>` : ''}</dl></div>
    <div><h3>Applications${hb('application')}</h3>${apps.length ? `<div class="tscroll"><table class="t compact"><tr><th>#</th><th>Host${hb('host')}</th><th>Media${hb('media')}</th><th>Lanes${hb('host_lanes')}</th><th></th></tr>${apps.map(a => `<tr><td>${esc(a.n)}</td><td>${esc(ifaceName('host', a.host_id))}</td><td>${esc(ifaceName('media', a.media_id, m.media_interface_technology_00h_212))}</td><td class="num">${esc(a.host_lanes)} / ${esc(a.media_lanes)}</td><td>${a.n === pp.active_apsel ? '<span class="badge accent">active</span>' : ''}</td></tr>`).join('')}</table></div>` : '<p class="muted small">None advertised.</p>'}
      <h3 style="margin-top:12px">Test features</h3>${d ? kvTable({ 'media generator': d.generator_media.join(', ') || 'none', 'media checker': d.checker_media.join(', ') || 'none', 'host generator': d.generator_host.join(', ') || 'none', loopbacks: d.loopbacks.join(', ') || 'none', 'BER results': d.ber_results, 'SNR (media)': d.snr_media, gating: d.gating }) : '<p class="muted small">No diagnostic pages: no PRBS and no loopback on this cable.</p>'}
      ${hollow.length ? `<h3 style="margin-top:12px">Hollow features</h3><ul class="sub">${hollow.map(h => `<li><span class="badge warn">hollow</span> ${esc(h)}</li>`).join('')}</ul>` : ''}</div></div>
    <details class="raw"><summary>Thresholds and firmware</summary>${kvTable({ thresholds: pp.thresholds, firmware: pp.firmware })}</details>
    <details class="raw"><summary>Raw passport JSON</summary><pre class="mono-box">${esc(JSON.stringify(pp, null, 1))}</pre></details>`;
}
const isLanes = v => Array.isArray(v) && v.length > 0 && v.every(x => x && typeof x === 'object' && 'lane' in x && 'value' in x);
const fmtVal = v => v === null || v === undefined ? 'none' : isLanes(v) ? v.map(x => `L${x.lane} ${fmtVal(x.value)}`).join(', ')
  : Array.isArray(v) ? (v.length ? v.map(x => typeof x === 'number' ? fmtNum(x) : typeof x === 'object' ? fmtVal(x) : String(x)).join(', ') : 'none')
  : typeof v === 'object' ? ('result' in v && typeof v.result === 'string' ? v.result : Object.entries(v).map(([k, x]) => `${k} ${fmtVal(x)}`).join(', '))
  : typeof v === 'number' ? fmtNum(v) : typeof v === 'boolean' ? (v ? 'yes' : 'no') : String(v);
function laneView(rec) { // per-lane fields: a list of {lane, value}, or sibling fields named ...LaneN, as one table with the lanes as columns
  const rows = [];
  for (const [pk, blk] of Object.entries(rec.pages || {})) {
    const groups = {};
    for (const f of blk.fields || []) {
      if (isLanes(f.value)) { rows.push({ page: pk, name: f.name, reg: f.reg, unit: f.unit, vals: f.value.map(x => fmtVal(x.value)) }); continue; }
      const m = /^(.*?)Lane(\d)$/.exec(f.name); if (m && f.value !== null && typeof f.value !== 'object') (groups[m[1]] = groups[m[1]] || { page: pk, name: m[1], reg: f.reg.replace(/:\d+.*$/, ''), unit: f.unit, vals: [] }).vals[+m[2] - 1] = fmtVal(f.value);
      else if (m && f.value && typeof f.value === 'object' && !Array.isArray(f.value)) for (const [k, x] of Object.entries(f.value)) (groups[`${m[1]} ${k}`] = groups[`${m[1]} ${k}`] || { page: pk, name: `${m[1]} ${k}`, reg: f.reg.replace(/:\d+.*$/, ''), unit: null, vals: [] }).vals[+m[2] - 1] = fmtVal(x);
    }
    rows.push(...Object.values(groups).filter(g => g.vals.filter(x => x !== undefined).length > 1));
  }
  if (!rows.length) return '';
  const n = Math.max(...rows.map(r => r.vals.length));
  return `<details class="card flat" open><summary>Lane view: ${rows.length} per-lane fields</summary><div class="tscroll"><table class="t compact"><tr><th>Field</th>${Array.from({ length: n }, (_, i) => `<th>L${i + 1}</th>`).join('')}</tr>${rows.map(r => `<tr><th title="${esc(r.reg)}">${esc(r.page)} ${esc(r.name)}${r.unit ? ` (${esc(r.unit)})` : ''}</th>${Array.from({ length: n }, (_, i) => `<td class="num">${esc(r.vals[i] === undefined ? '' : r.vals[i])}</td>`).join('')}</tr>`).join('')}</table></div></details>`;
}
function treeView(rec) {
  const ident = rec.identity || {};
  let html = `<div class="chips" style="margin-bottom:8px"><span class="badge info">CMIS ${esc(rec.revision)}, ${esc(rec.memory_model)} memory</span>${ident.vendor ? `<span class="badge plain">${esc(ident.vendor)} ${esc(ident.part_number)} ${esc(ident.serial || '')}</span>` : ''}${rec.read_at ? `<span class="stamp">read ${esc(rec.read_at)}</span>` : ''}</div>`;
  html += laneView(rec) + '<div class="tree">';
  for (const [pk, blk] of Object.entries(rec.pages || {})) {
    const state = blk.note ? `<span class="badge plain">${esc(blk.note.split(' (')[0])}</span>` : `<span class="badge pass plain">read</span>`;
    html += `<details${pk === 'lower' ? ' open' : ''}><summary>${esc(blk.title)} ${state}<span class="n">${blk.fields && blk.fields.length ? `${blk.fields.length} fields` : ''}</span></summary><div class="body">`;
    if (blk.fields && blk.fields.length) html += `<div class="tscroll"><table class="t compact">${blk.fields.map(f => `<tr><td>${esc(f.reg)}</td><td>${esc(f.name)}${hb(f.key || f.name)}</td><td class="wrap">${esc(fmtVal(f.value))}${f.unit && f.advertised !== false && !Array.isArray(f.value) ? ` ${esc(f.unit)}` : ''}</td></tr>`).join('')}</table></div>`;
    else if (blk.note) html += `<p class="muted small">${esc(blk.note)}</p>`;
    if (blk.raw) html += `<details class="raw"><summary>Raw bytes</summary><pre class="mono-box">${esc(blk.raw.replace(/(.{32})/g, '$1\n'))}</pre></details>`;
    html += '</div></details>';
  }
  if (rec.vdm) html += `<details><summary>VDM: ${esc(rec.vdm.instances.length)} instance(s)${rec.vdm.descriptor_page_all_zero ? ' <span class="badge warn">hollow</span>' : ''}</summary><div class="body">${rec.vdm.instances.length ? kvTable(rec.vdm.instances.slice(0, 40)) : '<p class="muted small">No instance in use.</p>'}</div></details>`;
  if (rec.cdb_query) html += `<details><summary>CDB queries</summary><div class="body">${kvTable(rec.cdb_query)}</div></details>`;
  if (rec.not_read && Object.keys(rec.not_read).length) html += `<details><summary>Not read: ${Object.keys(rec.not_read).length} page(s) without a verified gate bit</summary><div class="body">${kvTable(rec.not_read)}</div></details>`;
  return html + `</div><details class="raw"><summary>Raw decode JSON</summary><pre class="mono-box">${esc(JSON.stringify(rec, null, 1))}</pre></details>`;
}

// ---- Cable view
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
const clockChip = serial => { const c = CLOCKS[serial]; return c ? `<span class="badge ${c.i2c_ramp && c.i2c_ramp.startsWith('fell') ? 'warn' : 'plain'}" title="${esc(c.i2c_ramp || '')}">I2C ${Math.round(c.i2c_hz / 1000)} kHz</span>${hb('i2c_clock')}` : ''; };
async function rampClock(serial) {
  try { const { job } = await api('/api/i2c-ramp', { serial }); CLOCKS[serial] = await stream(job, null); const el = $(`#clock-${CSS.escape(serial)}`); if (el) el.innerHTML = clockChip(serial); }
  catch (e) { toast(`Clock ramp on ${serial}: ${e.message}`, 6000); }
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
  card.innerHTML = `<div class="card-head"><h2>${esc(u.serial)}</h2><span class="stamp" id="fru-${esc(u.serial)}"></span><span class="chips" id="pins-${esc(u.serial)}"></span><span class="chips" id="clock-${esc(u.serial)}">${clockChip(u.serial)}</span>
    <div class="actions"><button class="btn primary" data-act="read">Read cable</button><button class="btn" data-act="decode">Decode all pages</button><button class="btn" data-act="save">Save passport</button><button class="btn" data-act="pins">Refresh pins</button></div></div>
    <div id="plate-${esc(u.serial)}"><div class="empty"><b>Not read yet</b>Click Read cable to read the module in this unit.</div></div><div id="tree-${esc(u.serial)}"></div><details class="unit-fru" id="unit-${esc(u.serial)}"><summary>Unit ${esc(u.serial)}</summary><div class="empty"><b>Not read yet</b>The FRU area shows after the first pin read.</div></details>`;
  const q = id => $(`#${id}-${CSS.escape(u.serial)}`);
  const pins = async () => {
    const el = q('pins');
    try {
      const s = await api(`/api/status?serial=${encodeURIComponent(u.serial)}`);
      const unread = !$('.plate', q('plate')), flags = FLAGS_AT_READ[u.serial];
      const intBadge = !s.interrupt ? `<span class="badge plain">no interrupt</span>${flags && flags.length ? `<span class="stamp" title="latched flags, cleared by the read">flags at read: ${esc(flags.join(', '))}</span>` : ''}`
        : unread ? '<span class="badge info" title="IntL 0 before any register read">interrupt latched since insert</span><span class="stamp">Read the cable to clear it.</span>'
        : `<span class="badge warn" title="IntL stays low after the flags were read">interrupt pending${flags && flags.length ? ': ' + esc(flags.join(', ')) : ''}</span>`;
      el.innerHTML = s.ModPrsL === 0 && s.IntL === 0 && unread
        ? '<span class="badge info" title="ModPrsL 0, IntL 0">pins low: module present or rail off</span><span class="stamp">Turn MOD PWR on and read the cable.</span>'
        : `<span class="badge ${s.module_present ? 'pass' : 'plain'}">${s.module_present ? 'module present' : 'no module'}</span>${intBadge}<span class="stamp">read ${hhmmss()}</span>`;
      q('fru').textContent = s.fru && s.fru !== 'none' ? `FRU ${s.fru}` : 'FRU none'; unitFruCard(u.serial, s);
    } catch (e) { el.innerHTML = ''; alertBox(q('plate'), e.message, { label: 'Refresh units', fn: loadUnits }); }
  };
  const run = async (act) => {
    const box = act === 'decode' ? q('tree') : q('plate'), btns = $$('button', card); btns.forEach(b => b.disabled = true);
    box.innerHTML = `<div class="empty"><b>${act === 'decode' ? 'Decoding every advertised page' : 'Reading'}</b>${act === 'decode' ? 'Lower memory, the upper pages, the banks, VDM and the CDB queries.' : 'Identity, pages, capabilities and firmware info.'}</div>`;
    try {
      const { job } = await api(act === 'decode' ? '/api/decode' : act === 'save' ? '/api/passport' : '/api/cable', { serial: u.serial });
      const r = await stream(job, null);
      if (act === 'decode') box.innerHTML = `<div class="card-head" style="margin-top:16px"><h2>Every page</h2></div>${treeView(r)}`;
      else { FLAGS_AT_READ[u.serial] = flagNames(r.flags_at_read || (r.module && r.module.flags_at_read)); box.innerHTML = plate(r); MODULE_SN[u.serial] = r.module && r.module.serial; fillLabel(r.module && r.module.serial); if (act === 'save') toast('Passport saved: ' + r.files.map(f => f.split('/').pop()).join(', '), 6000); }
      pins(); toast(`${act === 'decode' ? 'Decoded' : 'Read'} ${u.serial}`);
      if (act !== 'decode' && r.module) rampClock(u.serial);
    } catch (e) { box.innerHTML = ''; alertBox(box, e.message, { fn: () => run(act) }); }
    btns.forEach(b => b.disabled = false);
  };
  $$('[data-act]', card).forEach(b => b.onclick = () => b.dataset.act === 'pins' ? pins() : run(b.dataset.act));
  card.readCable = () => run('read');
  pins();
}
$('#cable-read-all').onclick = () => $$('#cable-cards .card').forEach(c => c.readCable && c.readCable());
$('#cable-refresh').onclick = () => loadUnits().then(() => toast('Units refreshed'));
async function loadUnits() {
  INFO = await api('/api/units');
  INFO.units.sort((a, b) => String(a.serial).localeCompare(String(b.serial)));
  Object.assign(CLOCKS, INFO.i2c || {});
  $('#mode').textContent = (INFO.dry_run ? 'Fake backend' : 'Hardware') + (INFO.units.length ? ', ' + INFO.units.map(u => u.serial).join(', ') : ', no unit');
  const opts = INFO.units.map(u => `<option>${esc(u.serial)}</option>`).join('');
  $$('select[data-units]').forEach(s => { const was = s.value; s.innerHTML = opts; if (s.dataset.second !== undefined && INFO.units[1]) s.value = INFO.units[1].serial; if (was && INFO.units.some(u => u.serial === was)) s.value = was; });
  pairLines();
  $$('input[data-budget]').forEach(i => { if (!i.value) i.value = INFO.budget_w; });
  const op = $('[name=operator]', $('#tech-form')); if (!op.value) op.value = INFO.operator || '';
  $('#tech-mode').value = INFO.units.length >= 2 ? 'both' : 'one'; techMode();
  $$('.need-two').forEach(el => el.hidden = INFO.units.length < 2);
  const cards = $('#cable-cards'); cards.innerHTML = '';
  if (!INFO.units.length) cards.innerHTML = '<div class="empty"><b>No unit on the host</b>Plug the PC port of a stick into this computer, then Refresh units.<div class="row"><button class="btn" onclick="loadUnits()">Refresh units</button></div></div>';
  for (const u of INFO.units) { const c = document.createElement('div'); c.className = 'card'; cards.appendChild(c); cableCard(u, c); }
  $('#settings-list').innerHTML = Object.entries({ backend: INFO.dry_run ? 'fake (training)' : 'hardware', 'server instance': INFO.instance, started: new Date(INFO.started * 1000).toLocaleString(), 'budget W': INFO.budget_w, units: INFO.units.map(u => `${u.serial}: ${u.fru ? u.fru.fru : 'FRU not read yet'}`).join('; ') || 'none', 'record folders': (INFO.record_roots || []).join('\n'), 'command of record': 'aectest web2 --open', 'first front end': 'aectest web --open' }).map(([k, v]) => `<dt>${esc(k)}</dt><dd class="wrap">${esc(v)}</dd>`).join('');
}

// ---- Test view: setup
const SKIPPED = new Map();
let MATCH = null;
let techJob = null, techBack = false, stepSeen = false, stepTimer = null, stepStart = 0;
function fillLabel(serial) { const i = $('[name=label_sn]', $('#tech-form')); if (!serial || (i.value && i.dataset.auto !== i.value)) return; i.value = serial; i.dataset.auto = serial; }
function techMode() { const one = $('#tech-mode').value === 'one'; $('.need-b', $('#tech-form')).hidden = one; planText(); }
$('#tech-mode').onchange = techMode;
function planText() {
  const one = $('#tech-mode').value === 'one', el = $('#tech-plan'); if (!STEPS.length) return;
  const steps = STEPS.filter(s => s.name !== 'open' && !(one && s.name === 'prbs'));
  const secs = steps.reduce((a, s) => a + (s.duration_s || 0), 0);
  el.hidden = false; el.textContent = `${steps.length} steps: ${steps.map(s => s.title).join(', ')}. About ${mmss(secs)} at most${one ? ', the PRBS step between two ends does not run' : ''}.`;
}
$('#tech-read').onclick = async () => {
  const f = Object.fromEntries(new FormData($('#tech-form'))), msg = $('#tech-msg'), box = $('#tech-match');
  msg.textContent = 'Reading the cable in ' + f.a; box.hidden = true; $('#tech-start').disabled = true;
  try {
    const { job } = await api('/api/match-sku', { serial: f.a });
    const m = await stream(job, null); MATCH = m;
    fillLabel(m.identity.serial);
    box.hidden = false;
    box.innerHTML = `<div class="row between"><div><b>${esc(m.identity.vendor)} ${esc(m.identity.part_number)}</b><div class="muted small">serial ${esc(m.identity.serial)}, CMIS ${esc(m.identity.cmis_revision)}, class ${esc(m.identity.power_class)}, ${esc(m.identity.max_power_w)} W</div></div>
      ${m.matched ? `<span class="badge pass">criteria ${esc(m.sku)}${m.proposed ? ', PROPOSED' : ''}</span>` : '<span class="badge warn">no criteria file</span>'}</div>
      ${m.matched ? '' : '<p class="small muted" style="margin-top:8px">The test runs without limits and the verdict reads NO-CRITERIA. Create a file from this cable in Explore, Database, when the cable is known good.</p>'}`;
    msg.textContent = ''; $('#tech-start').disabled = false; planText();
  } catch (e) { msg.textContent = ''; box.hidden = false; box.innerHTML = ''; alertBox(box, e.message, { fn: () => $('#tech-read').click() }); }
};
// ---- Test view: the run
const PHASES = [[/LPMode low|waiting for ModuleReady/, 'releasing low power'], [/^soak/, 'holding high power'], [/measuring|gate/, 'measuring'], [/^ModuleReady/, 'module ready'], [/^STOP|^THROTTLE/, 'stopping'], [/wait.*insert/i, 'waiting for the cable'], [/^PRBS/, 'pattern running']];
function stepInfo(name) { return STEPS.find(s => s.name === name) || { title: name, what: '', judges: '', duration_s: 0 }; }
function stepCard(name, idx) {
  const s = stepInfo(name), card = $('#tech-step'); card.hidden = false; $('#tech-ask').hidden = true;
  $('#tech-step-name').textContent = `Step ${idx + 1} of ${STEP_ORDER.length}: ${s.title}`;
  $('#tech-blurb').innerHTML = `<b>What this step does.</b> ${esc(s.what)} <b>It judges:</b> ${esc(s.judges)}`;
  $('#tech-phase').hidden = true; $('#tech-ticker').textContent = ''; delete $('#tech-ticker').dataset.lines;
  stepStart = Date.now(); clearInterval(stepTimer);
  const tick = () => { const el = (Date.now() - stepStart) / 1000; $('#tech-timer').textContent = `${mmss(el)}${s.duration_s ? ` of about ${mmss(s.duration_s)}` : ''}`; };
  tick(); stepTimer = setInterval(tick, 500);
}
function ticker(text) { const t = $('#tech-ticker'); const lines = (t.dataset.lines ? JSON.parse(t.dataset.lines) : []).concat([text]).slice(-5); t.dataset.lines = JSON.stringify(lines); t.innerHTML = lines.map(l => `<div>${esc(l)}</div>`).join(''); const ph = PHASES.find(([re]) => re.test(text)); if (ph) { $('#tech-phase').hidden = false; $('#tech-phase').textContent = ph[1]; } }
function askCard(e) {
  clearInterval(stepTimer); $('#tech-step').hidden = true;
  const m = /press (\w+)\.?\s*$/i.exec(e.text); $('#tech-ask-text').textContent = e.text.replace(/\s*Then press \w+\.?\s*$/i, '');
  $('#tech-yes').textContent = m && !/enter/i.test(m[1]) ? m[1] : 'Done'; $('#tech-ask').hidden = false;
  $('#tech-skip').hidden = $('#tech-skip-reason').hidden = !e.skippable; $('#tech-skip-reason').value = ''; $('#tech-skip').disabled = true;
  $('#tech-back').hidden = stepSeen; $('#tech-yes').focus();
}
function precheckCard(ev) {
  const box = $('#tech-prechecks');
  let card = $(`[data-unit="${CSS.escape(ev.unit)}"]`, box); if (!card) { card = document.createElement('div'); card.className = 'card soft precheck'; card.dataset.unit = ev.unit; box.appendChild(card); }
  if (ev.ok) { card.className = 'card soft precheck'; card.innerHTML = `<div class="pre-row"><span class="badge pass">Unit ${esc(ev.unit)} ready</span>${(ev.checks || []).map(c => `<span class="stamp">${esc(c.name)}: ${esc(c.got)}</span>`).join('')}</div>`; return; }
  card.innerHTML = `<h3>Unit ${esc(ev.unit)}: not ready</h3>` + (ev.checks || []).map(c => `<div class="pre-row"><span class="badge ${c.ok ? 'pass' : 'warn'}">${esc(c.name)}</span><span>${c.ok ? esc(c.got) : `${esc(c.got)}, want ${esc(c.want)}`}</span>${!c.ok && c.hint ? `<span class="stamp">${esc(c.hint)}</span>` : ''}</div>`).join('');
}
const STEP_DATA = new Map(); let stepCur = null;
function stepButtons(steps, now, done, data, skipped) {
  return steps.map((s, i) => { const can = data.has(s); return `<button type="button" class="step ${skipped.has(s) ? 'skip' : i < now ? 'done' : i === now && !done ? 'now' : ''}" data-step="${esc(s)}"${can ? '' : ' disabled'} title="${skipped.has(s) ? 'skipped: ' + esc(skipped.get(s)) : can ? 'Show the results of this step.' : 'Not run yet.'}">${i + 1}. ${esc(stepInfo(s).title)}</button>`; }).join('');
}
function stepper(now, done) {
  const el = $('#tech-stepper'); el.classList.toggle('collapsed', !!done);
  el.innerHTML = stepButtons(STEP_ORDER, now, done, STEP_DATA, SKIPPED);
}
function stepView(data, container) { // one step's results: the events by type, the health samples as a table, the lines behind a toggle
  const byType = new Map(); for (const e of data.events) { if (!byType.has(e.type)) byType.set(e.type, []); byType.get(e.type).push(e); }
  let html = '';
  for (const [t, evs] of byType) {
    if (t === 'health') { const rows = evs.map(e => ({ end: e.end, t_s: e.data.t_s, module_state: e.data.state_name, temp_c: e.data.temp_c, vcc_v: e.data.vcc_v })); html += `<h3>Health, ${rows.length} samples</h3>${rows.length > 12 ? `<div class="stamp">The first and the last of ${rows.length}. The chart holds every sample.</div>` + kvTable([rows[0], rows[rows.length - 1]]) : kvTable(rows)}`; continue; }
    html += evs.map(e => `<h3>${esc(label(t))}${e.end ? `, end ${esc(e.end)}` : ''}</h3>${kvTable(e.data)}`).join('');
  }
  if (!html) html = '<div class="empty"><b>No data</b>This step logged lines only.</div>';
  container.innerHTML = html + (data.lines.length ? `<details class="raw"><summary>Lines (${data.lines.length})</summary><pre class="mono-box">${esc(data.lines.join('\n'))}</pre></details>` : '');
}
function showStep(name) {
  const d = STEP_DATA.get(name); if (!d) return; $('#tech-step-view').hidden = false;
  $('#tech-step-view-title').textContent = `Step ${STEP_ORDER.indexOf(name) + 1}: ${stepInfo(name).title}`;
  stepView(d, $('#tech-step-view-body')); $$('button.step', $('#tech-stepper')).forEach(b => b.setAttribute('aria-selected', b.dataset.step === name));
}
function stepperKeys(el, open) {
  el.onclick = ev => { const b = ev.target.closest('button.step'); if (b && !b.disabled) open(b.dataset.step); };
  el.onkeydown = ev => { if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return; const bs = $$('button.step:not(:disabled)', el); if (!bs.length) return; const i = bs.indexOf(document.activeElement); const j = (i + (ev.key === 'ArrowRight' ? 1 : -1) + bs.length) % bs.length; bs[j].focus(); open(bs[j].dataset.step); ev.preventDefault(); };
}
function stepCollect(text) {
  if (!stepCur || !STEP_DATA.has(stepCur)) return; const d = STEP_DATA.get(stepCur);
  if (text.startsWith('event {')) { try { const ev = JSON.parse(text.slice(6)); if (ev.type !== 'step') d.events.push(ev); } catch (e) { d.lines.push(text); } }
  else if (!/^step \d+: /.test(text)) d.lines.push(text);
}
stepperKeys($('#tech-stepper'), showStep);
$('#tech-step-view-close').onclick = () => { $('#tech-step-view').hidden = true; $$('button.step', $('#tech-stepper')).forEach(b => b.removeAttribute('aria-selected')); };
const KN = { ends_same_sn: 'Both ends share one serial', label_sn_judged_on: 'Label serial judged on end', mode: 'Ends tested', max_power_w: 'MaxPower W', temp_max_A: 'Temperature max A (C)', temp_max_B: 'Temperature max B (C)',
  mgmt_init_ms_A: 'MgmtInit A (ms)', mgmt_init_ms_B: 'MgmtInit B (ms)', vdm_recorded: 'VDM pages recorded', prbs_caveat_AB: 'PRBS note A to B', prbs_caveat_BA: 'PRBS note B to A', worst_errors_AB: 'Worst errors A to B', worst_errors_BA: 'Worst errors B to A',
  worst_ber_AB: 'Worst BER A to B', worst_ber_BA: 'Worst BER B to A', vcc_min_v: 'Vcc min (V)', vcc_max_v: 'Vcc max (V)', sku: 'Criteria file' };
function keyNumbers(kn) { return `<table class="t">${Object.entries(kn).filter(([, v]) => v !== null && v !== undefined && typeof v !== 'object').map(([k, v]) => `<tr><th>${esc(KN[k] || label(k))}</th><td class="num">${esc(typeof v === 'boolean' ? (v ? 'yes' : 'no') : typeof v === 'number' ? fmtNum(v) : v)}</td></tr>`).join('')}</table>`; }
const NEXT = { PASS: 'Put the cable in the pass bin. The record is saved.', FAIL: 'Put the cable in the fail bin. Hand the session folder to engineering.', INCONCLUSIVE: 'Check the reason, fix the setup, then test the cable again.', STOPPED: 'The record holds the steps that ran. Test the cable again when ready.', PROPOSED: 'Ask engineering to confirm the criteria file. Keep the cable out of the fail bin.' };
function verdictCard(status, why, meta, result, nextText) {
  const v = $('#tech-verdict'); v.hidden = false;
  v.innerHTML = `<div class="verdict-card ${esc(status)}"><div class="v-big">${esc(status)}</div><div><div class="why">${esc(why)}</div><div class="next"><b>Next:</b> ${esc(nextText || NEXT[status] || NEXT.INCONCLUSIVE)}</div></div>
    <div class="meta">${meta.map(m => `<span class="badge plain">${esc(m)}</span>`).join('')}</div>
    <div class="actions"><button class="btn primary big" id="tech-again">Test another cable</button>${result && result.report ? `<a class="btn" href="/api/record?path=${encodeURIComponent(result.report)}" target="_blank">Open the record</a><button class="btn" id="tech-copy-report">Copy the report</button>` : ''}<button class="btn" id="tech-show-log">Show the log</button></div></div>`;
  $('#tech-again').onclick = () => { $('#test-run').hidden = true; $('#test-setup').hidden = false; $('#tech-match').hidden = true; $('#tech-start').disabled = true; $('#test-sub').textContent = 'One guided test for one cable. Read the cable, start, watch, read the verdict.'; };
  const cp = $('#tech-copy-report'); if (cp) cp.onclick = () => copyText($('#tech-report').dataset.raw || $('#tech-report').textContent).then(() => toast('Report copied'));
  $('#tech-show-log').onclick = () => { const d = $('#tech-log').closest('details'); d.open = true; d.scrollIntoView({ behavior: 'smooth' }); };
  v.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
$('#tech-form').onsubmit = async ev => {
  ev.preventDefault();
  const f = Object.fromEntries(new FormData(ev.target));
  if (f.mode === 'one') delete f.b;
  f.label_sn = (f.label_sn || '').trim() || null; if (!(f.operator || '').trim()) delete f.operator;
  $('#test-setup').hidden = true; $('#test-run').hidden = false; $('#test-stop').hidden = false; SKIPPED.clear(); techBack = false; stepSeen = false;
  const log = $('#tech-log'); log.textContent = ''; $('#tech-verdict').hidden = true; $('#tech-numbers').hidden = true; $('#tech-report-card').hidden = true; $('#tech-live').hidden = true; $('#tech-step').hidden = true;
  $('#tech-prechecks').innerHTML = ''; $$('[data-log]').forEach(rawLog); STEP_DATA.clear(); stepCur = null; $('#tech-step-view').hidden = true;
  const match = MATCH ? `${MATCH.identity.vendor} ${MATCH.identity.part_number}, serial ${MATCH.identity.serial}` : 'Cable';
  $('#test-sub').textContent = `${match}, ${f.mode === 'one' ? 'one end in ' + f.a : 'both ends in ' + f.a + ' and ' + f.b}. Operator ${f.operator || INFO.operator}.`;
  let stepNo = 0; stepper(0); stepCard('open', -1); $('#tech-step-name').textContent = 'Pre-check'; const t0 = Date.now();
  const points = {};
  try {
    const { job } = await api('/api/tech', f); techJob = job;
    const result = await stream(job, log, e => {
      if (e.type === 'ask') askCard(e);
      if (e.type !== 'line') return;
      stepCollect(e.text);
      if (e.text.startsWith('event {')) { try { const ev2 = JSON.parse(e.text.slice(6)); if (ev2.type === 'precheck') precheckCard({ ...ev2.data, unit: ev2.data.unit || ev2.end }); } catch (e2) { /* a line */ } return; }
      const sm = /^step (\d+): (\w+)( skipped(?: \((.*)\))?)?/.exec(e.text);
      if (sm) { stepSeen = true; if (sm[3]) SKIPPED.set(sm[2], sm[4] || 'one end tested'); stepNo = STEP_ORDER.indexOf(sm[2]); stepCur = sm[2]; STEP_DATA.set(stepCur, { events: [], lines: [] }); stepper(sm[3] ? stepNo + 1 : stepNo); if (!sm[3]) stepCard(sm[2], stepNo); return; }
      ticker(e.text);
      const mm = HEALTH.exec(e.text); if (mm) { $('#tech-live').hidden = false; const ek = endName(mm[2] || 'module', f); (points[ek] = points[ek] || []).push([+mm[1], +mm[4], +mm[5]]); lineChart($('#tech-chart'), healthSeries(points), { axes: ['left', 'right'], legendEl: $('#tech-legend') }); }
    });
    clearInterval(stepTimer); $('#tech-step').hidden = true; $('#tech-ask').hidden = true; $('#test-stop').hidden = true;
    if (techBack) { $('#test-run').hidden = true; $('#test-setup').hidden = false; toast('Back to the setup. The stopped run is on record.'); return; }
    const reasons = (result.verdict.reasons || []).map(r => (typeof r === 'string' ? r : (r.code || '') + (r.text ? ': ' + r.text : '')).replace(/ got None$/, ''));
    const stopped = reasons.some(r => /stopped by the operator|ABORTED/.test(r));
    const proposed = reasons.some(r => /CRITERIA-PROPOSED/.test(r));
    const status = stopped ? 'STOPPED' : result.verdict.status;
    const kn = result.verdict.key_numbers || {};
    const why = stopped ? (reasons.find(r => /stopped/.test(r)) || 'Stopped by the operator.').replace(/^\S+: /, '') : status === 'PASS' ? `Every judged value is within the limits${kn.sku ? ' of ' + kn.sku : ''}.` : reasons[0] ? reasons.join('. ') : 'Every step passed.';
    const meta = [f.label_sn ? 'serial ' + f.label_sn : null, f.mode === 'one' ? 'one end, PRBS skipped' : 'both ends', mmss((Date.now() - t0) / 1000), kn.temp_max_A !== undefined && kn.temp_max_A !== null ? `temp max ${(+kn.temp_max_A).toFixed(1)} C` : null, kn.max_power_w ? `MaxPower ${kn.max_power_w} W` : null].filter(Boolean);
    stepper(stopped ? stepNo : STEP_ORDER.length, true); $('#tech-prechecks').innerHTML = '';
    verdictCard(status, proposed ? 'Criteria proposed, not confirmed. ' + (reasons.find(r => !/CRITERIA-PROPOSED/.test(r)) || 'Every measured value is on record.') : why, meta, result, proposed ? NEXT.PROPOSED : null);
    if (Object.keys(kn).length) { $('#tech-numbers').hidden = false; $('#tech-numbers-body').innerHTML = keyNumbers(kn) + (reasons.length ? `<h3 style="margin-top:12px">Reasons</h3><ul class="sub">${reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>` : ''); }
    if (result.report) { const r = await api(`/api/record?path=${encodeURIComponent(result.report)}`); $('#tech-report').textContent = r.text; $('#tech-report-card').hidden = false; $('#tech-report-link').href = `/api/record?path=${encodeURIComponent(result.report)}`; }
    toast('Test finished: ' + status);
  } catch (e) {
    clearInterval(stepTimer); $('#tech-step').hidden = true; $('#tech-ask').hidden = true; $('#test-stop').hidden = true;
    const { text, fix } = fixText(e.message);
    verdictCard('STOPPED', e.lost ? 'The connection dropped during the run.' : text, [fix || 'The run did not finish.'], null);
  }
};
const answer = (yes, extra = {}) => async () => { $('#tech-ask').hidden = true; await api('/api/answer', { job: techJob, yes, ...extra }); };
$('#tech-yes').onclick = answer(true); $('#tech-no').onclick = answer(false);
$('#test-stop').onclick = () => { if (!$('#tech-ask').hidden) return answer(false)(); toast('Stop takes effect at the next question of the engine.'); return answer(false)(); };
$('#tech-back').onclick = () => { techBack = true; answer(false)(); };
$('#tech-skip-reason').oninput = ev => { $('#tech-skip').disabled = !ev.target.value.trim(); };
$('#tech-skip').onclick = () => answer(false, { skip: true, reason: $('#tech-skip-reason').value.trim() })();
function healthSeries(points) { const s = []; for (const [name, p] of Object.entries(points)) { s.push({ name: `${name} temp C`, points: p.map(x => [x[0], x[1]]) }); s.push({ name: `${name} Vcc V`, points: p.map(x => [x[0], x[2]]) }); } return s; }

// ---- Explore
function pairLines() {
  const f = $('#f-run'), a = $('[name=a]', f), b = $('[name=b]', f), line = $('[data-pair-line]', f); if (!a || !b || !line) return;
  line.textContent = a.value && b.value ? `Pair: ${a.value} is end A, ${b.value} is end B.` : 'Pair: pick two units.';
}
$('#f-run').addEventListener('change', ev => { if (ev.target.matches('[name=a], [name=b]')) pairLines(); });
$('[data-swap]', $('#f-run')).onclick = () => { const f = $('#f-run'), a = $('[name=a]', f), b = $('[name=b]', f); [a.value, b.value] = [b.value, a.value]; pairLines(); };
const GROUP_FORM = { Read: '#f-read', Control: '#f-control', Run: '#f-run', Query: '#f-query', Firmware: '#f-fw', Database: '#f-db', Features: '#f-features' };
function featuresCard(r, body) {
  const chips = `<div class="chips">${r.module ? `<span class="badge info">${esc(`${r.module.vendor || ''} ${r.module.part_number || ''}`.trim())}</span>` : ''}${body && body.exercise ? `<span class="badge plain">exercised: ${esc(body.exercise)}</span>` : ''}${r.file ? `<span class="stamp">saved to ${esc(r.file)}</span>` : ''}</div>`;
  return chips + (r.status === 'not available' ? `<div class="note"><b>Feature matrix:</b> not available. ${esc(r.reason || '')}.</div>` : featuresHtml(r.rows, { esc }));
}
$('#explore-result').addEventListener('click', ev => {
  const b = ev.target.closest('button[data-exercise], button[data-exercise-group]'); if (!b) return;
  const body = formBody($('#f-features')), name = b.dataset.exercise || b.dataset.exerciseGroup;
  if (body.confirm.length < 2) { alertBox($('#explore-banner'), 'Tick both guard boxes on the Features form: an exercise may release the module.'); return; }
  runExplore('features', { serial: body.serial, exercise: name, confirm: body.confirm, budget_w: body.budget_w, reason: body.reason }, `Exercise: ${name}`, r => featuresCard(r, { exercise: name }));
});
function renderCaps() {
  for (const [group, sel] of Object.entries(GROUP_FORM)) {
    const box = $('.cmds', $(sel)); if (!box) continue;
    box.innerHTML = CAPS.filter(c => c.group === group).map(c => `<div class="cmd"><div><b>${esc(c.title)}</b><span class="what">${esc(c.what)}</span><span class="needs">${c.needs.map(n => `<span class="badge plain">${esc(n)}</span>`).join('')}</span></div><button type="button" class="btn ${/confirm|care|gate/.test(c.needs.join()) ? 'danger' : ''}" data-cmd="${esc(c.cmd)}">Run</button></div>`).join('');
  }
  $$('#explore button[data-cmd]').forEach(b => b.onclick = () => exploreCmd(b.dataset.cmd, b.closest('form')));
  if (typeof fwImageState === 'function') fwImageState();
}
function formBody(form) {
  const f = Object.fromEntries(new FormData(form));
  const confirm = [f.c1 && 'release-lpmode', f.c2 && 'usbc-power'].filter(Boolean);
  return { serial: f.serial || f.a, page: f.page !== undefined ? parseInt(f.page, 16) : undefined, off: f.off, n: f.n, hex: f.hex, hold: f.hold, budget_w: f.budget_w, reason: f.reason, confirm,
    pair: f.a && f.b ? `${f.a}:${f.b}` : undefined, time: f.time, pattern: f.pattern, apsel: f.apsel ? +f.apsel : null, force_cmis: !!f.force_cmis, count: f.count,
    image: f.image, header_bytes: f.header_bytes, image_version: f.image_version, force: !!f.force, run_image: !!f.run_image, confirm_text: f.confirm, owner: f.owner, note: f.note, password: f.password || null };
}
async function exploreCmd(cmd, form) {
  if (!form.checkValidity()) { form.reportValidity(); return; }
  const b = formBody(form), cap = CAPS.find(c => c.cmd === cmd) || { title: cmd };
  if (cmd === 'reg-write' && !b.hex) { alertBox($('#explore-banner'), 'Give the bytes to write as hex.'); return; }
  if (cap.needs.includes('two units') && (!b.pair || b.pair.split(':')[0] === b.pair.split(':')[1])) { alertBox($('#explore-banner'), 'A two-unit run needs two different units on the host.'); return; }
  if (cmd === 'fw-update') { if (!(b.image || '').trim()) { alertBox($('#explore-banner'), 'Choose the image file first.'); return; } return fwUpdate(b, 'plan'); }
  if (cmd === 'cdb') return runExplore('cdb', { serial: b.serial, args: ['query'] }, 'CDB query');
  if (cmd === 'derive') return runExplore('derive', { serial: b.serial, confirm: b.confirm, owner: b.owner, note: b.note, budget_w: b.budget_w }, 'Derive criteria', derivePanel);
  if (cmd === 'match-sku') return runExplore('match-sku', { serial: b.serial }, 'Match the database');
  if (cmd === 'decode') return runExplore('decode', { serial: b.serial }, 'Decode all pages', r => treeView(r));
  if (cmd === 'features') return runExplore('features', { serial: b.serial, exercise: null }, 'Feature matrix', r => featuresCard(r, null));
  return runExplore(cmd, b, cap.title);
}
let exploreTimer = null;
function liveExplore(ends) {
  const points = {}, ber = {}, snr = {}, lols = [], marks = [], labels = [];
  let run = 0, offset = 0, lastT = -1, maxT = 0;
  $('#explore-live').hidden = true; $('#explore-live2').hidden = true; $('#explore-lol').hidden = true; $('#explore-snr-note').hidden = true;
  const t0 = Date.now(); clearInterval(exploreTimer); exploreTimer = setInterval(() => { $('#explore-timer').textContent = mmss((Date.now() - t0) / 1000); }, 500);
  return e => {
    if (e.type !== 'line') return;
    const dm = /media-side (\S+) -> (\S+):/.exec(e.text); if (dm) labels.push(`${dm[1]} to ${dm[2]}`);
    const h = HEALTH.exec(e.text);
    if (h) {
      $('#explore-live').hidden = false; const t = +h[1];
      if (t < lastT) { run += 1; offset = maxT + 2; marks.push({ x: offset, label: labels[run] || `run ${run + 1}` }); if (run === 1) marks.unshift({ x: 0, label: labels[0] || 'run 1' }); }
      lastT = t; maxT = Math.max(maxT, offset + t);
      const ek = endName(h[2] || 'unit', ends); (points[ek] = points[ek] || []).push([offset + t, +h[4], +h[5]]);
      lineChart($('#explore-chart'), healthSeries(points), { axes: ['left', 'right'], legendEl: $('#explore-legend'), marks }); return;
    }
    const t = TREND.exec(e.text);
    if (t) {
      $('#explore-live').hidden = false;
      t[2].trim().split(/\s+/).forEach((v, i) => (ber[i] = ber[i] || []).push([+t[1], +v]));
      lineChart($('#explore-chart'), Object.entries(ber).map(([i, p]) => ({ name: `lane ${+i + 1} BER`, points: p })), { ylog: true, legendEl: $('#explore-legend') });
      if (t[3]) {
        t[3].trim().split(/\s+/).forEach((v, i) => (snr[i] = snr[i] || []).push([+t[1], +v]));
        const dead = Object.values(snr).every(p => allZero(p.map(x => x[1])));
        $('#explore-live2').hidden = dead; $('#explore-snr-note').hidden = !dead; $('#explore-snr-note').textContent = NO_SNR;
        if (!dead) lineChart($('#explore-chart2'), Object.entries(snr).map(([i, p]) => ({ name: `lane ${+i + 1} SNR dB`, points: p })), { minSpan: 2, legendEl: $('#explore-legend2') });
      }
      lols.push(t[4] || '0x00'); lolStrip($('#explore-lol'), lols);
    }
  };
}
function showBer(rec) {
  const dirs = rec.directions || (rec.direction ? { [`${rec.direction.generator} to ${rec.direction.checker}`]: rec.direction } : {});
  const first = Object.values(dirs)[0]; if (!first || !first.media_ber) return;
  $('#explore-ber').hidden = false;
  const mask = parseInt((first.lane_mask || 'ff').slice(0, 4), 16);
  barChart($('#explore-bars'), Object.entries(dirs).map(([k, d]) => ({ name: k.replace('_to_', ' to '), values: (d.media_ber || []).map((v, i) => ({ label: `L${i + 1}`, v, off: !(mask >> i & 1) })) })), $('#explore-bars-legend'));
}
async function runExplore(cmd, body, title, render, onLine) {
  const res = $('#explore-result'), log = $('#explore-log'); log.textContent = ''; $('#explore-banner').innerHTML = ''; $('#explore-ber').hidden = true;
  res.innerHTML = `<div class="card-head"><h2>${esc(title)}</h2><span class="pulse"></span><span class="stamp">running</span></div><div class="empty"><b>Running</b>The log fills below. The result appears here when the command ends.</div>`;
  $$('#explore button[data-cmd]').forEach(b => b.disabled = true);
  const live = liveExplore(body), onEv = e => { live(e); if (onLine && e.type === 'line') onLine(e.text); };
  try {
    const { job } = await api(`/api/${cmd}`, body);
    const result = await stream(job, log, onEv);
    clearInterval(exploreTimer);
    res.innerHTML = render ? `<div class="card-head"><h2>${esc(title)}</h2><span class="stamp">read ${hhmmss()}</span></div>${render(result)}` : resultCard(title, result);
    if (result) fillLol(res, result);
    if (result && (result.directions || result.samples)) showBer(result);
    toast(`${title} finished`);
    return result;
  } catch (e) {
    clearInterval(exploreTimer);
    res.innerHTML = '<div class="empty"><b>No result</b>The command did not finish. The reason is above.</div>';
    alertBox($('#explore-banner'), e.message); if (e.refused) $('#explore-logbox').open = true;
    return null;
  } finally { $$('#explore button[data-cmd]').forEach(b => b.disabled = false); loadUnits().catch(() => {}); }
}
const FW_STAGES = { plan: { plan: true }, update: {}, run: { run_only: true, run_image: true }, commit: { run_only: true, commit: true } };
let fwSteps = [];
function fwLine(text) { // the engine's lines drive the bar and the step rows; the password value never appears in a line
  const box = $('#fw-progress-box'); if (box) box.hidden = false;
  const w = /write (\d+)\/(\d+) bytes, (\d+) blocks sent.*?([\d.]+) s/.exec(text);
  if (w) { const done = +w[1], total = +w[2], el = +w[4], pct = Math.round(100 * done / total), eta = el * (total - done) / Math.max(done, 1); $('#fw-progress').value = pct; $('#fw-progress-text').textContent = `${pct} %: ${w[3]} blocks, ${done} of ${total} bytes, ${Math.round(el)} s, about ${Math.round(eta)} s left`; }
  const c = /^CDB (0x[0-9a-fA-F]{4}) [\w-]+: (SUCCESS|FAILED[^,]*)/.exec(text);
  if (c) { const last = fwSteps[fwSteps.length - 1]; if (last && last[0] === cdbName(c[1]) && last[1] === c[2]) last[2] += 1; else fwSteps.push([cdbName(c[1]), c[2], 1]); } // repeated block writes fold into one row with a count
  if (/^password entry written/.test(text)) fwSteps.push(['password entry, page 00h bytes 122 to 125', 'written', 1]);
  if (c || /^password entry written/.test(text)) $('#fw-steps').innerHTML = fwSteps.map(([n, st, k]) => `<div class="pre-row"><span class="badge ${/SUCCESS|written/.test(st) ? 'pass' : 'fail'}">${esc(st.split(' ')[0])}</span><span>${esc(n)}${k > 1 ? ` x ${k}` : ''}</span></div>`).join('');
  if (/^ABORTED|^Run refused|did not answer/.test(text)) $('#fw-progress-text').textContent = text;
}
async function fwUpdate(b, stage) {
  if (stage === 'commit' && !b.confirm_text) { alertBox($('#explore-banner'), 'Type the commit confirmation first.'); return null; }
  if ((stage === 'run' || stage === 'commit') && !b.image_version) { alertBox($('#explore-banner'), 'Give the image version. Run and Commit find the bank by it.'); return null; }
  fwSteps = []; const pb = $('#fw-progress'); if (pb) { pb.value = 0; $('#fw-progress-text').textContent = ''; $('#fw-steps').innerHTML = ''; $('#fw-progress-box').hidden = stage === 'plan'; }
  const body = { serial: b.serial, image: b.image, header_bytes: b.header_bytes, image_version: b.image_version, force: b.force, password: b.password || null, confirm: b.confirm_text, ...FW_STAGES[stage] };
  const titles = { plan: 'Firmware update plan', update: 'Firmware download', run: 'Firmware run', commit: 'Firmware commit' };
  return runExplore('fw-update', body, titles[stage], res => fwPanel(res, b), stage === 'plan' ? null : fwLine);
}
function fwAfter(res, b) { // what the operator does next, from the engine's result word
  const r = (res && res.result) || '', m = (res && res.module) || {};
  if (/^(UPDATED|RUNNING|COMMITTED)$/.test(r) && $('#fw-progress')) { $('#fw-progress').value = 100; $('#fw-progress-text').textContent = '100 %: the bank holds the image'; }
  if (/ABORTED/.test(r)) return `<div class="note"><b>Aborted before the run.</b> The committed image in bank ${esc(m.running || '?')} is untouched. The step rows name the command that failed.</div>`;
  if (r === 'PLANNED') return `<div class="note"><b>Plan read.</b> Nothing was written. Click Download when the gate FW-1 go is on record.</div><div class="row" style="margin-top:12px"><button class="btn danger" id="fw-go">Download</button></div>`;
  if (r === 'UPDATED') return `<div class="note"><b>The inactive bank holds the new image.</b> Click Run to start it. The module resets.</div><div class="row" style="margin-top:12px"><button class="btn danger" id="fw-run">Run</button></div>`;
  if (r === 'RUNNING') return `<div class="note"><b>The new image runs, uncommitted.</b> Run Test a cable on it. Then type the commit confirmation and click Commit.</div><div class="row" style="margin-top:12px"><button class="btn danger solid" id="fw-commit">Commit</button></div>`;
  if (r === 'COMMITTED') return '<div class="note"><b>Committed.</b> The module boots the new image at every reset. The old image stays in the other bank.</div>';
  return `<div class="note"><b>${esc(r)}.</b> Read the log for the engine's note.</div>`;
}
function fwPanel(res, b) {
  const plan = res.plan || {};
  let html = `<div class="chips"><span class="badge ${/ABORT|REFUSED|SILENT/.test(res.result) ? 'fail' : /COMMITTED|UPDATED|RUNNING/.test(res.result) ? 'pass' : 'info'}">${esc(res.result)}</span><span class="badge plain">${esc(res.image || '')}</span></div>`;
  html += fwAfter(res, b);
  html += `<h3 style="margin-top:12px">Plan</h3>${kvTable(plan)}`;
  if (res.steps && res.steps.length) html += `<h3 style="margin-top:12px">Steps</h3>${kvTable(res.steps)}`;
  setTimeout(() => { const go = $('#fw-go'); if (go) go.onclick = () => fwUpdate(b, 'update'); const rn = $('#fw-run'); if (rn) rn.onclick = () => fwUpdate(b, 'run'); const cm = $('#fw-commit'); if (cm) cm.onclick = () => fwUpdate(b, 'commit'); }, 0);
  return html + `<details class="raw"><summary>Raw JSON</summary><pre class="mono-box">${esc(JSON.stringify(res, null, 1))}</pre></details>`;
}
function fwImageState() { const has = !!(($('[name=image]', $('#f-fw')) || {}).value || '').trim(); $$('#f-fw button[data-cmd="fw-update"]').forEach(b => b.disabled = !has); }
$('#fw-file').onchange = async ev => { // the browser's file -> /api/fw/image -> the server path in the box
  const file = ev.target.files && ev.target.files[0], info = $('#fw-file-info'); if (!file) return;
  info.textContent = `Uploading ${file.name}, ${file.size} bytes`;
  try {
    const buf = new Uint8Array(await file.arrayBuffer()); let bin = ''; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    const r = await api('/api/fw/image', { name: file.name, data_b64: btoa(bin) });
    $('[name=image]', $('#f-fw')).value = r.path; info.textContent = `${r.name}, ${r.size} bytes, sha256 ${r.sha256.slice(0, 16)}`; fwImageState(); toast('Image uploaded');
  } catch (e) { info.textContent = ''; alertBox($('#explore-banner'), e.message); }
};
$('[name=image]', $('#f-fw')).oninput = fwImageState;
$('#fw-pw-show').onclick = ev => { const i = $('[name=password]', $('#f-fw')), show = i.type === 'password'; i.type = show ? 'text' : 'password'; ev.currentTarget.setAttribute('aria-pressed', String(show)); ev.currentTarget.textContent = show ? 'Hide' : 'Show'; };
const EXP_RESULTS = {}, EXP_EMPTY = '<div class="empty"><b>No result yet</b>Run a command on the left. Its result appears here as cards.</div>'; let expTab = ($('#explore-tabs button[aria-selected="true"]') || { dataset: {} }).dataset.tab || null;
$$('#explore-tabs button').forEach(b => b.onclick = () => { $$('#explore-tabs button').forEach(x => x.setAttribute('aria-selected', x === b)); $$('#explore .tab').forEach(t => t.toggleAttribute('data-active', t.dataset.tab === b.dataset.tab)); if (b.dataset.tab === 'db') loadDb(); const res = $('#explore-result'); if (expTab !== b.dataset.tab) { res.innerHTML = tabSwap(EXP_RESULTS, expTab, b.dataset.tab, res.innerHTML, EXP_EMPTY); $('#explore-log').textContent = ''; $('#explore-banner').innerHTML = ''; } expTab = b.dataset.tab; });
async function loadDb() {
  try {
    const d = await api('/api/skus');
    $('#db-list').innerHTML = d.skus.length ? `<table class="t compact"><tr><th>SKU</th><th>Vendor</th><th>Part</th><th>Status</th><th>Author</th></tr>${d.skus.map(s => `<tr><td class="nowrap">${esc(s.sku)}</td><td>${esc(s.vendor || '')}</td><td>${esc(s.part_number || '')}</td><td>${s.proposed ? '<span class="badge warn">PROPOSED</span>' : '<span class="badge pass">reviewed</span>'}</td><td class="${s.owner && s.owner !== '<name>' ? '' : 'muted'}">${esc(s.owner && s.owner !== '<name>' ? s.owner : 'unset')}</td></tr>`).join('')}</table>` : '<div class="empty"><b>No criteria file yet</b>Derive one from a known good cable.</div>';
  } catch (e) { alertBox($('#explore-banner'), e.message); }
}
function derivePanel(d) {
  const line = `git add ${d.rel}`;
  setTimeout(() => { const b = $('#derive-copy'); if (b) b.onclick = () => copyText(line).then(() => toast('Copied')); loadDb(); }, 0);
  return `<div class="chips"><span class="badge ${d.existed ? 'warn' : 'pass'}">${d.existed ? 'proposed beside an existing file' : 'new file'}</span><span class="badge plain">${esc(d.sku)}</span>${d.timing ? '<span class="badge plain">with timing</span>' : ''}</div>
    <p class="small muted" style="margin:8px 0">${esc(d.rel)}. A .proposed.yaml beside an existing file waits for a review; the live file stays as it was.</p>
    <div class="row"><code>${esc(line)}</code><button class="btn small" id="derive-copy">Copy git add line</button></div><pre class="mono-box">${esc(d.text.split('\n').slice(0, 24).join('\n'))}</pre>`;
}
async function copyText(text) { try { await navigator.clipboard.writeText(text); } catch (e) { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); } }
async function copyRich(html, text) {
  try { if (navigator.clipboard && window.ClipboardItem) { await navigator.clipboard.write([new ClipboardItem({ 'text/html': new Blob([html], { type: 'text/html' }), 'text/plain': new Blob([text], { type: 'text/plain' }) })]); return 'rich'; } } catch (e) { /* fall through */ }
  await copyText(text); return 'plain';
}
function rawLog(pre) { if (pre.dataset.raw !== undefined) { pre.textContent = pre.dataset.raw; delete pre.dataset.raw; pre.classList.remove('rendered'); } }
$$('[data-render]').forEach(b => b.onclick = () => { const pre = $(b.dataset.render); if (pre.dataset.raw !== undefined) { rawLog(pre); b.textContent = 'Render'; } else { pre.dataset.raw = pre.textContent; pre.innerHTML = md(pre.textContent); pre.classList.add('rendered'); b.textContent = 'Raw'; } });
$$('[data-copy-log]').forEach(b => b.onclick = async () => { const pre = $(b.dataset.copyLog), msg = $('.copy-msg', b.parentElement); const kind = pre.dataset.raw !== undefined ? await copyRich(pre.innerHTML, pre.dataset.raw) : (await copyText(pre.textContent), 'plain'); msg.textContent = kind === 'rich' ? 'Copied as rich text' : 'Copied'; setTimeout(() => { msg.textContent = ''; }, 1500); });

// ---- Records
let FOLDERS = [], CABLES = [], UNASSIGNED = [], REC_MODE = localStorage.getItem('recmode') || 'cable';
const when = t => new Date(t * 1000).toLocaleString();
function fileRow(r) {
  const li = document.createElement('li'); li.innerHTML = `<span class="kind">${esc(r.kind)}</span><span>${esc(r.name)}<small>${when(r.time || r.mtime)}${r.unit ? `, unit ${esc(r.unit)}` : ''}${REC_MODE === 'cable' ? `, ${esc(r.folder)}` : ''}</small></span>`;
  li.onclick = () => { $$('#record-list li').forEach(x => x.removeAttribute('aria-selected')); li.setAttribute('aria-selected', 'true'); showRecord(r); };
  return li;
}
function renderRecords() {
  const q = ($('#record-filter').value || '').toLowerCase(), box = $('#record-list'); box.innerHTML = '';
  $('#rec-by-cable').setAttribute('aria-pressed', REC_MODE === 'cable'); $('#rec-by-folder').setAttribute('aria-pressed', REC_MODE !== 'cable');
  if (REC_MODE === 'cable') {
    const hit = c => !q || [c.vendor, c.part_number, ...c.serials].join(' ').toLowerCase().includes(q);
    const shown = CABLES.map(c => ({ ...c, records: hit(c) ? c.records : c.records.filter(r => r.name.toLowerCase().includes(q)) })).filter(c => c.records.length);
    if (!shown.length && !UNASSIGNED.length) { box.innerHTML = `<div class="empty"><b>${CABLES.length ? 'No cable matches' : 'No records yet'}</b>${CABLES.length ? 'Clear the filter to see every cable.' : 'Read a cable or run a test.'}</div>`; return; }
    shown.forEach((c, i) => {
      const d = document.createElement('details'); d.className = 'folder'; d.open = i === 0 || !!q;
      const what = c.kind === 'module' ? 'module' : 'cable';
      d.innerHTML = `<summary><span class="cable-id">${esc(c.vendor || '?')} ${esc(c.part_number)}<small>${esc(what)} ${esc(c.serials.join(', ') || 'no serial')}</small></span><span class="chips">${Object.entries(c.kinds).map(([k, n]) => `<span class="kind">${esc(k)} ${n}</span>`).join('')}</span></summary><div class="stamp cable-meta">${what === 'module' ? 'One module with its own serial.' : 'One cable with its end serials.'} First seen ${when(c.first_seen)}, last ${when(c.last_seen)}, units ${esc(c.units.join(', ') || '-')}</div><ul class="list"></ul>`;
      const ul = $('ul', d); c.records.forEach(r => ul.appendChild(fileRow(r))); box.appendChild(d);
    });
    const un = UNASSIGNED.filter(r => !q || r.name.toLowerCase().includes(q));
    if (un.length) { const d = document.createElement('details'); d.className = 'folder'; d.innerHTML = `<summary>No cable identity<span class="badge plain">${un.length}</span></summary><ul class="list"></ul>`; const ul = $('ul', d); un.forEach(r => ul.appendChild(fileRow(r))); box.appendChild(d); }
    return;
  }
  const shown = FOLDERS.map(f => ({ ...f, files: f.files.filter(r => !q || r.name.toLowerCase().includes(q)) })).filter(f => f.files.length);
  if (!shown.length) { box.innerHTML = `<div class="empty"><b>${FOLDERS.length ? 'No file matches' : 'No records yet'}</b>${FOLDERS.length ? 'Clear the filter to see every file.' : 'Run a command first.'}</div>`; return; }
  shown.forEach((f, i) => { const d = document.createElement('details'); d.className = 'folder'; d.open = i === 0 || !!q; d.innerHTML = `<summary>${esc(f.folder === '.' ? 'top level' : f.folder)}<span class="badge plain">${f.files.length}</span></summary><ul class="list"></ul>`; const ul = $('ul', d); f.files.forEach(r => ul.appendChild(fileRow(r))); box.appendChild(d); });
}
async function loadRecords() {
  try { const idx = await api('/api/records'); FOLDERS = idx.folders; CABLES = idx.cables || []; UNASSIGNED = idx.unassigned || []; } catch (e) { alertBox($('#records-banner'), e.message); return; }
  renderRecords();
}
$('#records-reload').onclick = loadRecords; $('#record-filter').oninput = renderRecords;
$('#rec-by-cable').onclick = () => { REC_MODE = 'cable'; localStorage.setItem('recmode', REC_MODE); renderRecords(); };
$('#rec-by-folder').onclick = () => { REC_MODE = 'folder'; localStorage.setItem('recmode', REC_MODE); renderRecords(); };
async function showRecord(r) {
  const pane = $('#record-pane');
  let d; try { d = await api(`/api/record?path=${encodeURIComponent(r.path)}`); } catch (e) { alertBox(pane, e.message); return; }
  const head = `<div class="card-head"><div><h2>${esc(r.name)}</h2><span class="stamp">${esc(r.folder === '.' ? 'top level' : r.folder)}, ${esc(r.kind)}</span></div><div class="actions"><a class="btn small" href="/api/record?path=${encodeURIComponent(r.path)}" target="_blank">Open the file</a></div></div>`;
  const charts = '<svg class="chart" id="rc1"></svg><ul class="legend" id="rl1"></ul><div id="rc2box" hidden><svg class="chart" id="rc2"></svg><ul class="legend" id="rl2"></ul></div>';
  if (d.rows) {
    pane.innerHTML = head + charts + `<details class="raw"><summary>Header</summary><pre class="mono-box">${esc(Object.entries(d.meta).map(([k, v]) => `${k}: ${v}`).join('\n'))}</pre></details>`;
    const rows = d.rows.filter(x => x.t_s);
    if (rows[0] && rows[0].lane1_ber !== undefined) {
      lineChart($('#rc1'), [1, 2, 3, 4, 5, 6, 7, 8].map(i => ({ name: `lane ${i} BER`, points: rows.filter(x => x[`lane${i}_ber`]).map(x => [+x.t_s, +x[`lane${i}_ber`]]) })).filter(s => s.points.length), { ylog: true, legendEl: $('#rl1') });
      const snr = [1, 2, 3, 4, 5, 6, 7, 8].map(i => ({ name: `lane ${i} SNR dB`, points: rows.filter(x => +x[`lane${i}_snr_db`]).map(x => [+x.t_s, +x[`lane${i}_snr_db`]]) })).filter(s => s.points.length);
      if (snr.length) { $('#rc2box').hidden = false; lineChart($('#rc2'), snr, { minSpan: 2, legendEl: $('#rl2') }); }
      else if (rows.some(x => x.lane1_snr_db !== undefined && x.lane1_snr_db !== '')) $('#rc1').insertAdjacentHTML('afterend', `<div class="stamp">${esc(NO_SNR)}</div>`);
      if (rows[0].media_lol !== undefined) { $('#rc2box').insertAdjacentHTML('beforebegin', '<div class="lol-box"></div>'); lolStrip($('.lol-box', pane), rows.map(x => x.media_lol || '0x00')); }
    } else lineChart($('#rc1'), healthSeries({ module: rows.filter(x => x.temp_c).map(x => [+x.t_s, +x.temp_c, +x.vcc_v]) }), { axes: ['left', 'right'], legendEl: $('#rl1') });
  } else if (d.directions) {
    const points = {}; const dirs = Object.keys(d.directions);
    for (const [k, dir] of Object.entries(d.directions)) for (const [name, hs] of Object.entries(dir.health || {})) points[`${dirs.length > 1 ? `run ${dirs.indexOf(k) + 1}, ` : ''}${name}`] = hs.map(h => [h.t_s, h.temp_c, h.vcc_v]);
    pane.innerHTML = head + runCards(d) + charts + `<details class="raw"><summary>Raw JSON</summary><pre class="mono-box">${esc(JSON.stringify(d, null, 1))}</pre></details>`;
    lineChart($('#rc1'), healthSeries(points), { axes: ['left', 'right'], legendEl: $('#rl1') });
    $('#rc2box').hidden = false; const mask = parseInt((Object.values(d.directions)[0].lane_mask || 'ff').slice(0, 4), 16);
    barChart($('#rc2'), Object.entries(d.directions).map(([k, v]) => ({ name: k.replace('_to_', ' to '), values: (v.media_ber || []).map((b, i) => ({ label: `L${i + 1}`, v: b, off: !(mask >> i & 1) })) })), $('#rl2'));
  } else if (d.samples) {
    pane.innerHTML = head + runCards(d) + charts + `<details class="raw"><summary>Raw JSON</summary><pre class="mono-box">${esc(JSON.stringify(d, null, 1))}</pre></details>`;
    fillLol(pane, d);
    lineChart($('#rc1'), [0, 1, 2, 3, 4, 5, 6, 7].map(i => ({ name: `lane ${i + 1} BER`, points: d.samples.filter(s => s.media_ber && s.media_ber[i] > 0).map(s => [s.t_s, s.media_ber[i]]) })).filter(s => s.points.length), { ylog: true, legendEl: $('#rl1') });
    const snr = [0, 1, 2, 3, 4, 5, 6, 7].map(i => ({ name: `lane ${i + 1} SNR dB`, points: d.samples.filter(s => s.media_snr_db && s.media_snr_db[i]).map(s => [s.t_s, s.media_snr_db[i]]) })).filter(s => s.points.length);
    if (snr.length) { $('#rc2box').hidden = false; lineChart($('#rc2'), snr, { minSpan: 2, legendEl: $('#rl2') }); }
    else if (d.samples.some(s => s.media_snr_db)) $('#rc1').insertAdjacentHTML('afterend', `<div class="stamp">${esc(NO_SNR)}</div>`);
  } else if (d.lower && d.pages && !d.decoded) { pane.innerHTML = head + '<div class="empty"><b>Decoding</b></div>'; try { const t = await api(`/api/decode?path=${encodeURIComponent(r.path)}`); pane.innerHTML = head + treeView(t); } catch (e) { alertBox(pane, e.message); } }
  else if (d.lower && d.pages) { pane.innerHTML = head + `<div class="row" style="margin-bottom:8px"><button class="btn small" id="rec-decode">Decode every page</button></div><div id="rec-decode-box">${kvTable(summarize(d))}</div><details class="raw"><summary>Raw JSON</summary><pre class="mono-box">${esc(JSON.stringify(d, null, 1))}</pre></details>`; $('#rec-decode').onclick = async () => { try { const t = await api(`/api/decode?path=${encodeURIComponent(r.path)}`); $('#rec-decode-box').innerHTML = treeView(t); } catch (e) { alertBox(pane, e.message); } }; }
  else if (d.module !== undefined && d.unit) pane.innerHTML = head + plate(d);
  else if (d.text !== undefined) { pane.innerHTML = head + `<div class="row" style="margin:8px 0"><button type="button" class="btn small" data-render="#rec-text">Render</button><button type="button" class="btn small" data-copy-log="#rec-text">Copy</button><span class="stamp copy-msg"></span></div><pre class="mono-box" id="rec-text" data-log>${esc(d.text)}</pre>`; if (/report\.md$/.test(r.path)) recordStepper(r.path.replace(/report\.md$/, 'events.jsonl'), pane); }
  else pane.innerHTML = head + resultCard(r.name, d.decoded || d).replace(/^<div class="card-head">.*?<\/div>/, '');
  $$('[data-render]', pane).forEach(b => b.onclick = () => { const pre = $(b.dataset.render); if (pre.dataset.raw !== undefined) { rawLog(pre); b.textContent = 'Render'; } else { pre.dataset.raw = pre.textContent; pre.innerHTML = md(pre.textContent); pre.classList.add('rendered'); b.textContent = 'Raw'; } });
  $$('[data-copy-log]', pane).forEach(b => b.onclick = async () => { const pre = $(b.dataset.copyLog), msg = $('.copy-msg', b.parentElement); const kind = pre.dataset.raw !== undefined ? await copyRich(pre.innerHTML, pre.dataset.raw) : (await copyText(pre.textContent), 'plain'); msg.textContent = kind === 'rich' ? 'Copied as rich text' : 'Copied'; setTimeout(() => { msg.textContent = ''; }, 1500); });
}

// ---- Help: the step blurbs and the glossary
async function recordStepper(path, pane) {
  let ev; try { ev = await api(`/api/record?path=${encodeURIComponent(path)}`); } catch (e) { return; }
  const data = new Map(), skipped = new Map(); let cur = null;
  for (const line of (ev.text || '').split('\n')) { if (!line.trim()) continue; let e; try { e = JSON.parse(line); } catch (e2) { continue; }
    if (e.type === 'step') { cur = e.data.name; data.set(cur, { events: [], lines: [] }); continue; }
    if (e.type === 'skip') { skipped.set(e.data.name, e.data.reason || 'skipped'); continue; }
    if (cur) data.get(cur).events.push(e); }
  if (!data.size) return;
  const steps = STEP_ORDER.filter(s => data.has(s) || skipped.has(s));
  const box = document.createElement('div'); box.innerHTML = `<h3>Steps</h3><div class="stepper" id="rec-stepper"></div><div id="rec-step-body"></div>`;
  pane.insertBefore(box, pane.children[1] || null);
  const el = $('#rec-stepper', box); el.innerHTML = stepButtons(steps, steps.length, true, data, skipped);
  const open = name => { const d = data.get(name); if (!d) return; stepView(d, $('#rec-step-body', box)); $$('button.step', el).forEach(b => b.setAttribute('aria-selected', b.dataset.step === name)); };
  stepperKeys(el, open);
}
function renderHelpSteps() {
  $('#help-steps-body').innerHTML = STEPS.map((s, i) => `<div class="card soft"><h3>${s.name === 'open' ? 'Before step 1' : `Step ${STEP_ORDER.indexOf(s.name) + 1}`}: ${esc(s.title)}</h3><p class="small">${esc(s.what)}</p><p class="small muted"><b>It judges:</b> ${esc(s.judges)}${s.duration_s ? ` <b>About:</b> ${mmss(s.duration_s)}.` : ''}${s.waits ? ` <b>Waits for:</b> ${esc(s.waits)}.` : ''}</p></div>`).join('');
}
$('#glossary').innerHTML = Object.entries(HELP).map(([k, v]) => `<dt>${esc(label(k))}</dt><dd>${esc(v)}</dd>`).join('');

// ---- start
connect(); renderConn();
loadInfo().catch(() => {});
(async () => {
  try {
    const [st, caps] = await Promise.all([api('/api/steps'), api('/api/caps')]);
    STEPS = st.steps; STEP_ORDER = st.order; CAPS = caps.caps; renderCaps(); renderHelpSteps(); planText();
    await loadUnits();
  } catch (e) { alertBox($('#cable-banner'), e.message, { label: 'Try again', fn: () => location.reload() }); }
  show(localStorage.getItem('view') && $(`#${localStorage.getItem('view')}`) ? localStorage.getItem('view') : 'cable');
})();
