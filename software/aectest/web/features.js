// The feature matrix (batch 8, item 5): every capability the module advertises, in four states, with an Exercise button per
// row and per group. The rows come from the engine's `features` command (`aectest bringup features`, agent claude-tech8).
// Row contract: { name, group, state: 'ok' | 'hollow' | 'untested' | 'absent', detail, result, hint }. A row without `state`
// is mapped from `advertised`, `hollow`, `exercised` and `verdict`. Pure: no DOM; selftest parts i and n run it in node.
'use strict';
const FEAT_STATE = { ok: ['pass', 'works'], hollow: ['warn', 'hollow'], untested: ['info', 'not exercised'], absent: ['plain', 'not advertised'] };
const FEAT_ORDER = ['ok', 'hollow', 'untested', 'absent'];
const featEsc = s => String(s === null || s === undefined ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
function featState(r) { // the four states from whatever the row carries
  if (r.state && FEAT_STATE[r.state]) return r.state;
  if (r.advertised === false || r.state === 'not advertised') return 'absent';
  if (r.hollow) return 'hollow';
  if (r.exercised || r.verdict || r.result) return /^(PASS|MEASURED|OK|works)/i.test(String(r.verdict || r.result || '')) || r.ok === true ? 'ok' : r.ok === false || /FAIL|hollow/i.test(String(r.verdict || r.result || '')) ? 'hollow' : 'untested';
  return 'untested';
}
function featureGroups(rows) { // [[group, rows]] in first-seen order; a row without a group lands in 'Other'
  const out = new Map();
  for (const r of rows || []) { const g = r.group || 'Other'; if (!out.has(g)) out.set(g, []); out.get(g).push(r); }
  return [...out.entries()];
}
function featureCounts(rows) { const c = { ok: 0, hollow: 0, untested: 0, absent: 0 }; for (const r of rows || []) c[featState(r)] += 1; return c; }
function featuresHtml(rows, opts = {}) { // the card body: the counts, then one table per group; `canExercise` false hides the buttons
  const e = opts.esc || featEsc, can = opts.canExercise !== false;
  if (!rows || !rows.length) return '<div class="empty"><b>No feature rows</b>Build the matrix from a module first.</div>';
  const c = featureCounts(rows);
  let html = `<div class="chips feat-counts">${FEAT_ORDER.map(s => `<span class="badge ${FEAT_STATE[s][0]}">${c[s]} ${FEAT_STATE[s][1]}</span>`).join('')}</div>`;
  for (const [g, rs] of featureGroups(rows)) {
    const adv = rs.filter(r => featState(r) !== 'absent').length;
    html += `<details class="feat-group" open><summary>${e(g)}<span class="stamp">${adv} of ${rs.length} advertised</span>${can && adv ? `<button type="button" class="btn small" data-exercise-group="${e(g)}">Exercise the group</button>` : ''}</summary>
      <div class="feat-scroll"><table class="t cols feat-table"><tr><th>Feature</th><th>State</th><th>Detail</th><th>Result</th>${can ? '<th></th>' : ''}</tr>${rs.map(r => { const s = featState(r); return `<tr data-feat="${e(r.name)}"><td>${e(r.name)}${r.hint ? ` <span class="hint" role="button" tabindex="0" data-hint="${e(r.hint)}">?</span>` : ''}</td><td><span class="badge ${FEAT_STATE[s][0]}">${FEAT_STATE[s][1]}</span></td><td class="feat-detail">${e(r.detail || r.reg || '')}</td><td class="feat-res">${e(typeof r.result === 'object' && r.result !== null ? JSON.stringify(r.result) : (r.result || r.verdict || ''))}${s === 'hollow' && r.note ? `<div class="stamp">${e(r.note)}</div>` : ''}</td>${can ? `<td>${s === 'absent' ? '' : `<button type="button" class="btn small" data-exercise="${e(r.name)}">Exercise</button>`}</td>` : ''}</tr>`; }).join('')}</table></div></details>`;
  }
  return html;
}
if (typeof module !== 'undefined') module.exports = { featState, featureGroups, featureCounts, featuresHtml, FEAT_STATE };
