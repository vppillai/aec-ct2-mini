//   lineChart(svg, series, opts)  series = [{name, points: [[x, y], ...]}]; opts: {ylog, axes: ['left'] | ['left','right'], marks: [{x, label}], legendEl, minSpan}
//   barChart(svg, groups, legendEl) groups = [{name, values: [{label, v, off}]}], log scale for BER
//   The eight series colours come from the CSS tokens --c1..--c8 so both themes read right.
'use strict';
const chartColors = () => ['--c1', '--c2', '--c3', '--c4', '--c5', '--c6', '--c7', '--c8'].map(v => getComputedStyle(document.documentElement).getPropertyValue(v).trim() || '#888');
const cEsc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
function chartLegend(el, series) {
  if (!el) return;
  const C = chartColors();
  el.innerHTML = series.map((s, i) => `<li title="${cEsc(s.name)}"><i style="background:${C[i % 8]}"></i>${cEsc(s.name)}</li>`).join('');
}
function fmtTick(v, log, span) { // enough decimals to tell neighbouring ticks apart
  if (log) return v.toExponential(0);
  const d = span ? Math.max(0, Math.min(4, Math.ceil(-Math.log10(span / 4)))) : (Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 10 ? 1 : 2);
  return v.toFixed(d);
}
function lineChart(svg, series, o = {}) {
  const C = chartColors();
  const W = svg.clientWidth || 800, H = svg.clientHeight || 220, L = 56, R = o.axes && o.axes.length > 1 ? 56 : 16, T = 12, B = 28;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const all = series.flatMap(s => s.points);
  if (!all.length) { svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">no points yet</text>`; chartLegend(o.legendEl, series); return; }
  const xs = all.map(p => p[0]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs, x0 + 1);
  const axisOf = i => (o.axes && o.axes.length > 1 ? i % 2 : 0); // two axes: even series left, odd right
  const yr = [0, 1].map(ax => {
    const pts = series.filter((s, i) => axisOf(i) === ax).flatMap(s => s.points.map(p => p[1])).filter(v => !o.ylog || v > 0);
    if (!pts.length) return null;
    let lo = Math.min(...pts), hi = Math.max(...pts);
    if (o.ylog) { lo = Math.log10(lo); hi = Math.log10(hi); }
    const span = Math.max(hi - lo, o.minSpan || (o.ylog ? 1 : 0.3));
    const mid = (lo + hi) / 2;
    return [mid - span / 2, mid + span / 2];
  });
  const X = x => L + (x - x0) / (x1 - x0) * (W - L - R);
  const Y = (v, ax) => { const r = yr[ax] || yr[0]; const y = o.ylog ? Math.log10(v) : v; return T + (1 - (y - r[0]) / (r[1] - r[0])) * (H - T - B); };
  let g = `<line class="axis" x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}"/>`;
  for (let i = 0; i <= 4; i++) {
    const y = T + i / 4 * (H - T - B);
    g += `<line class="grid" x1="${L}" y1="${y}" x2="${W - R}" y2="${y}"/>`;
    for (const ax of [0, 1]) {
      const r = yr[ax]; if (!r || (ax === 1 && !(o.axes && o.axes.length > 1))) continue;
      const v = r[1] - i / 4 * (r[1] - r[0]);
      g += `<text x="${ax ? W - R + 6 : L - 6}" y="${y + 4}" text-anchor="${ax ? 'start' : 'end'}">${fmtTick(o.ylog ? 10 ** v : v, o.ylog, r[1] - r[0])}</text>`;
    }
  }
  for (let i = 0; i <= 4; i++) { const x = x0 + i / 4 * (x1 - x0); g += `<text x="${X(x)}" y="${H - 8}" text-anchor="middle">${fmtTick(x, false, x1 - x0).replace(/\.0+$/, '')} s</text>`; }
  (o.marks || []).forEach(m => { g += `<line class="axis" x1="${X(m.x)}" y1="${T}" x2="${X(m.x)}" y2="${H - B}" stroke-dasharray="4 3"/><text x="${X(m.x) + 4}" y="${T + 10}">${cEsc(m.label)}</text>`; });
  series.forEach((s, i) => {
    const pts = s.points.filter(p => !o.ylog || p[1] > 0);
    if (!pts.length) return;
    const d = pts.map(p => `${X(p[0]).toFixed(1)},${Y(p[1], axisOf(i)).toFixed(1)}`).join(' ');
    g += `<polyline fill="none" stroke="${C[i % 8]}" stroke-width="2" points="${d}"><title>${cEsc(s.name)}</title></polyline>`;
    if (pts.length === 1) g += `<circle cx="${X(pts[0][0])}" cy="${Y(pts[0][1], axisOf(i))}" r="3" fill="${C[i % 8]}"/>`;
  });
  svg.innerHTML = g;
  chartLegend(o.legendEl, series);
}
function barChart(svg, groups, legendEl) {
  const C = chartColors();
  const W = svg.clientWidth || 800, H = svg.clientHeight || 220, L = 56, R = 16, T = 12, B = 28;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const vals = groups.flatMap(gr => gr.values).filter(v => !v.off && v.v > 0).map(v => Math.log10(v.v));
  const lo = Math.floor(Math.min(...vals, -12)), hi = Math.ceil(Math.max(...vals, -3));
  const labels = groups[0] ? groups[0].values.map(v => v.label) : [];
  const n = Math.max(labels.length, 1), gw = (W - L - R) / n, bw = Math.max(4, gw / (groups.length + 1));
  const Y = v => T + (1 - (Math.log10(v) - lo) / (hi - lo)) * (H - T - B);
  let g = `<line class="axis" x1="${L}" y1="${H - B}" x2="${W - R}" y2="${H - B}"/>`;
  for (let e = lo; e <= hi; e += Math.max(1, Math.round((hi - lo) / 4))) { const y = Y(10 ** e); g += `<line class="grid" x1="${L}" y1="${y}" x2="${W - R}" y2="${y}"/><text x="${L - 6}" y="${y + 4}" text-anchor="end">1e${e}</text>`; }
  labels.forEach((lb, i) => { g += `<text x="${L + gw * (i + 0.5)}" y="${H - 8}" text-anchor="middle">${cEsc(lb)}</text>`; });
  groups.forEach((gr, gi) => gr.values.forEach((v, i) => {
    const x = L + gw * i + bw * (gi + 0.5);
    if (v.off || !(v.v > 0)) { g += `<text x="${x + bw / 2}" y="${H - B - 4}" text-anchor="middle">${v.off ? 'n/a' : '0'}</text>`; return; }
    g += `<rect x="${x}" y="${Y(v.v)}" width="${bw - 2}" height="${H - B - Y(v.v)}" fill="${C[gi % 8]}"><title>${cEsc(gr.name)} ${cEsc(v.label)}: ${v.v.toExponential(2)}</title></rect>`;
  }));
  svg.innerHTML = g;
  chartLegend(legendEl, groups);
}
if (typeof module !== 'undefined') module.exports = { lineChart, barChart, fmtTick };
