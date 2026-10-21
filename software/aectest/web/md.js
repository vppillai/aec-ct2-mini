// which is what report.md uses. Pure: no DOM. app.js calls md(text); selftest part i runs it in node.
'use strict';
const mdEsc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
function md(text) {
  const inline = t => mdEsc(t).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  const out = []; let list = false, table = false, para = [];
  const flush = () => {
    if (para.length) { out.push(`<p>${inline(para.join(' '))}</p>`); para = []; }
    if (list) { out.push('</ul>'); list = false; }
    if (table) { out.push('</table>'); table = false; }
  };
  for (const line of text.split('\n')) {
    const h = /^(#{1,6}) (.*)/.exec(line), li = /^[-*] (.*)/.exec(line), tr = /^\|(.*)\|\s*$/.exec(line);
    if (h) { flush(); const n = h[1].length + 1; out.push(`<h${n}>${inline(h[2])}</h${n}>`); }
    else if (tr) {
      if (/^\|[\s:|-]+\|$/.test(line)) continue; // the header rule
      const cells = tr[1].split('|').map(c => c.trim());
      if (!table) { flush(); table = true; out.push(`<table class="t"><tr>${cells.map(c => `<th>${inline(c)}</th>`).join('')}</tr>`); }
      else out.push(`<tr>${cells.map(c => `<td>${inline(c)}</td>`).join('')}</tr>`);
    }
    else if (li) { if (table || para.length) flush(); if (!list) { list = true; out.push('<ul>'); } out.push(`<li>${inline(li[1])}</li>`); }
    else if (!line.trim()) flush();
    else { if (list || table) flush(); para.push(line); }
  }
  flush();
  return out.join('\n');
}
if (typeof module !== 'undefined') module.exports = { md };
