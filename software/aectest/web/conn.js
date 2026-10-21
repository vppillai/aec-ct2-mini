// selftest part i mirrors the same contract in Python (the instance id changes across a server restart).
//   state: {status: 'connecting'|'live'|'down', instance: string|null, restarted: bool, attempts: int, lastBeat: number|null}
//   events: {type:'hello', instance, t} | {type:'beat', instance, t} | {type:'drop'} | {type:'retry'}
'use strict';
function connInit() { return { status: 'connecting', instance: null, restarted: false, attempts: 0, lastBeat: null }; }
function connReduce(s, e) {
  if (e.type === 'hello' || e.type === 'beat') {
    const restarted = s.instance !== null && e.instance !== s.instance;
    return { status: 'live', instance: e.instance, restarted, attempts: 0, lastBeat: e.t };
  }
  if (e.type === 'drop') return { ...s, status: 'down', restarted: false };
  if (e.type === 'retry') return { ...s, status: 'down', restarted: false, attempts: s.attempts + 1 };
  return s;
}
function connBackoffMs(attempts) { return Math.min(8000, 1000 * 2 ** Math.min(attempts, 3)); }
// A value read at `readAt` is stale when the connection is down or when it is older than its refresh interval.
function connStale(status, readAt, intervalMs, now) { return status !== 'live' || readAt === null || now - readAt > intervalMs; }
if (typeof module !== 'undefined') module.exports = { connInit, connReduce, connBackoffMs, connStale };
