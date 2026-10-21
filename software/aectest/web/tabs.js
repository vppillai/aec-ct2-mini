// Per-tab results (batch 7, item 11): every tab owns its result panel. One container on the page; this store keeps the
// HTML of the tab that leaves and hands back the HTML of the tab that opens. Pure: no DOM; selftest parts i and n run it in node.
'use strict';
function tabSwap(store, from, to, html, empty) { // store: {tab: html}; returns the HTML the container shows for `to`
  if (from) store[from] = html;
  return Object.prototype.hasOwnProperty.call(store, to) ? store[to] : empty;
}
if (typeof module !== 'undefined') module.exports = { tabSwap };
