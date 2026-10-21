#!/usr/bin/env python3
"""calibrate.py - golden-cable calibration of one SKU file.

The sessions of >= MIN_CABLES known-good cables (PASS, the SKU, not a dry run, label SN and CMIS serials not in --known-bad)
give one distribution per key number (count, min, median, p90, max) and three PROPOSED candidates:
  prbs.limits.tightened.raw.ber_max_per_lane   = median of the per-run worst-lane BER (both directions) x BER_FACTOR, never above
                                                 the record's raw.ber_max_per_lane (post_fec.errors_max_per_lane stays 0)
  calibration.high_power_temp_c_max_tightened  = the record's from_module rule plus `max` = max observed + TEMP_MARGIN_C, never
                                                 above the lowest module warning threshold + the SKU margin (the lower wins, live too)
  calibration.timing_mgmt_init_ms              = interpolated p90 x MGMT_INIT_FACTOR, never above MSA Table 9 (2000 ms)
The candidate file is the SKU file with the `tightened: null` slot filled and a `calibration` block appended; every candidate
line carries `# CALIBRATION <date> n=<cables> method=<...> PROPOSED`. The SKU file of record is not touched: the project lead diffs the
candidate and adopts it after a review. The engine never reads `tightened` or `calibration`. `--check` applies
them (apply_tightened) and re-judges every session from its own files (observed_from_session): every golden must still PASS,
every known-bad must not. The rule that caught a known-bad is named; none caught = the evidence that the method does not
separate that cable. The method stays PROPOSED until the project lead adopts it.

  aectest calibrate --sku SKU --sessions DIR... [--lot DIR] [--known-bad SN...] [--out FILE] [--min 10] [--force]
  aectest calibrate --sku SKU --sessions DIR... [--known-bad SN...] --check CANDIDATE
"""
import copy
import glob
import json
import os
import re
import statistics
from datetime import datetime, timezone

from . import criteria
from .records import OUT_DIR, open_new, sha256
from .tech import CRITERIA_DIR, _slope, criteria_path

MIN_CABLES = 10          # 4.1 rule 3: >= 10 known-good cables, PROPOSED
BER_FACTOR = 10          # 4.1 rule 3: median x 10, PROPOSED
TEMP_MARGIN_C = 10       # 4.1 rule 3: max observed + 10 C, PROPOSED
MGMT_INIT_FACTOR = 2     # p90 x 2, PROPOSED
MGMT_INIT_MSA_MS = 2000  # MSA Rev 7.1 Table 9 MgmtInitDuration: the candidate never exceeds the standard
KEYS = ('worst_ber_AB', 'worst_ber_BA', 'worst_errors_AB', 'worst_errors_BA', 'temp_max_A', 'temp_max_B', 'mgmt_init_ms_A',
        'mgmt_init_ms_B', 'module_ready_s_A', 'module_ready_s_B', 'temp_slope_c_per_min_A', 'temp_slope_c_per_min_B', 'max_power_w')
# candidate -> (the key the engine reads, as section.key)
APPLIES_TO = {'ber_max_per_lane': 'prbs.limits.raw.ber_max_per_lane', 'errors_max_per_lane': 'prbs.limits.post_fec.errors_max_per_lane',
              'temp_c_max': 'high_power.temp_c_max', 'mgmt_init_ms': 'timing.mgmt_init_ms'}


# --------------------------------------------------------------------------- the sessions
def find_sessions(dirs):
    """[(session_dir, manifest)] for every manifest.json under the given roots (a lot directory is one such root)."""
    paths = sorted({p for d in dirs for p in glob.glob(os.path.join(d, '**', 'manifest.json'), recursive=True)})
    return [(os.path.dirname(p), json.load(open(p))) for p in paths]


def _serials(m):
    """The label SN and the CMIS serials of both ends: a known-bad is matched on any of them (A-4)."""
    cable = m.get('cable') or {}
    cmis = cable.get('cmis') or {}
    return {cable.get('label_sn'), *((cmis.get(e) or {}).get('serial') for e in ('A', 'B'))} - {None}


def select(sessions, sku, known_bad):
    """(golden, bad): golden = PASS sessions of the SKU that are not a dry run and not known-bad; bad = the known-bad sessions, any
    verdict. A known-bad is matched on the label SN or a CMIS serial."""
    of_sku = [(d, m) for d, m in sessions if (m.get('cable') or {}).get('sku') == sku and not (m.get('hardware') or {}).get('dry_run')]
    bad = [(d, m) for d, m in of_sku if _serials(m) & set(known_bad)]
    golden = [(d, m) for d, m in of_sku if not _serials(m) & set(known_bad) and (m.get('verdict') or {}).get('status') == criteria.PASS]
    return golden, bad


def observed_from_session(d):
    """The observed record of one session, rebuilt from its files: identity, timing, low and high power from events.jsonl
    (temp_c_max over the soak health events, as live), prbs from prbs.json through criteria.prbs_from_record, the runner
    passthrough codes (`inconclusive`) from the manifest reasons. A key the session never wrote stays absent (NOT-MEASURED
    under evaluate), never guessed. The manifest identity block lacks `identifier`, so the identity events are the source.
    Known gaps (C-3): a NOT-ADVERTISED run has no prbs.json and rebuilds as PRBS-INCONCLUSIVE:no run; vcc_v is unrounded.
    The status is the same."""
    m = json.load(open(os.path.join(d, 'manifest.json')))
    reasons = (m.get('verdict') or {}).get('reasons') or []
    obs = {'label_sn': (m.get('cable') or {}).get('label_sn'), 'ends': {}, 'timing': {}, 'low_power': {}, 'high_power': {}, 'prbs': {}, 'vdm': {},
           'inconclusive': [criteria.code_of(r) for r in reasons if ' runner passthrough ' in r]}
    released, soak = set(), {}
    hp = lambda end: obs['high_power'].setdefault(end, {})  # noqa: E731
    for line in open(os.path.join(d, 'events.jsonl')):
        e = json.loads(line)
        t, end, x = e['type'], e['end'], e['data']
        if t == 'identity':
            obs['ends'][end] = x
        elif t == 'insert':
            obs['timing'][end] = {'mgmt_init_ms': x.get('mgmt_init_ms'), 'edge_seen': x.get('edge')}
        elif t == 'health' and end not in released:
            obs['low_power'][end] = {'module_state': x.get('state_name'), 'flags_byte9': x.get('flags9'), 'temp_c': x.get('temp_c'), 'vcc_v': x.get('vcc_v')}
        elif t == 'health':
            soak.setdefault(end, []).append(x)
            hp(end).update(module_state=x.get('state_name'), vcc_v=x.get('vcc_v'))
        elif t == 'release':
            released.add(end)
            obs['timing'].setdefault(end, {})['module_ready_s'] = x.get('module_ready_observed_s')
            hp(end)['thresholds'] = x.get('thresholds_02h')
        elif t == 'dp_activate':
            hp(end)['dp_states'] = x.get('dp_states')
        elif t == 'flags':
            hp(end)['failure_flag_tx_11h_135'] = int(x['11h:135 FailureFlagTx'], 16)
        elif t == 'vdm':
            obs['vdm'][end] = x
    for end, samples in soak.items():
        hp(end).update(temp_slope_c_per_min=_slope(samples), temp_c_max=max(x['temp_c'] for x in samples))
    prbs = os.path.join(d, 'prbs.json')
    if os.path.exists(prbs):
        obs['prbs'] = criteria.prbs_from_record(json.load(open(prbs)))
    return obs


def numbers(manifest, obs):
    """One row of KEYS: the manifest key numbers plus module_ready_s and the slope per end from the rebuilt record."""
    k = dict((manifest.get('verdict') or {}).get('key_numbers') or {})
    for end in ('A', 'B'):
        k[f'module_ready_s_{end}'] = (obs['timing'].get(end) or {}).get('module_ready_s')
        k[f'temp_slope_c_per_min_{end}'] = (obs['high_power'].get(end) or {}).get('temp_slope_c_per_min')
    return {key: k.get(key) for key in KEYS}


# --------------------------------------------------------------------------- the statistics and the candidates
def _p90(xs):
    return statistics.quantiles(xs, n=10, method='inclusive')[8] if len(xs) >= 2 else xs[0]


def _pool(rows, *keys):
    return sorted(r[k] for r in rows for k in keys if r.get(k) is not None)


def stats(rows):
    """{key: {n, min, median, p90, max}} over the rows' values; a key with no value is absent."""
    out = {}
    for key in KEYS:
        xs = _pool(rows, key)
        if xs:
            out[key] = {'n': len(xs), 'min': xs[0], 'median': statistics.median(xs), 'p90': _p90(xs), 'max': xs[-1]}
    return out


def candidates(rows, warn_c=None, margin=0, ber_cap=None):
    """The PROPOSED candidates (4.1 rule 3), each with its method string and sample size. warn_c = the lowest module warning
    threshold seen; margin = the SKU `from_module` margin; the temperature candidate never exceeds warn_c + margin. ber_cap =
    the record's raw.ber_max_per_lane; the BER candidate never exceeds it (A-1)."""
    c = {}
    ber = _pool(rows, 'worst_ber_AB', 'worst_ber_BA')
    if ber:
        med = statistics.median(ber)
        want = float(f'{med * BER_FACTOR:.1e}')
        method = f'median of the per-run worst-lane BER (both directions) {med:.1e} x {BER_FACTOR} = {want:.1e}'
        if ber_cap is not None:
            method += f'; record {ber_cap:.1e}; the lower wins'
            want = min(want, ber_cap)
        c['ber_max_per_lane'] = {'value': want, 'samples': len(ber), 'method': method}
    errs = _pool(rows, 'worst_errors_AB', 'worst_errors_BA')
    if errs:
        c['errors_max_per_lane'] = {'value': 0, 'samples': len(errs), 'method': f'stays 0 (T11 plan criterion; max observed {errs[-1]})'}
    temp = _pool(rows, 'temp_max_A', 'temp_max_B')
    if temp:
        want = temp[-1] + TEMP_MARGIN_C
        method = f'max observed {temp[-1]:.1f} C + {TEMP_MARGIN_C} C = {want:.1f} C'
        if warn_c is not None:
            cap = warn_c + margin
            method += f'; module warning {warn_c:.1f} C {margin:+} C = {cap:.1f} C; the lower wins'
            want = min(want, cap)
        c['temp_c_max'] = {'value': round(want, 1), 'samples': len(temp), 'method': method}
    mi = _pool(rows, 'mgmt_init_ms_A', 'mgmt_init_ms_B')
    if mi:
        p90 = _p90(mi)
        c['mgmt_init_ms'] = {'value': min(int(round(p90 * MGMT_INIT_FACTOR)), MGMT_INIT_MSA_MS), 'samples': len(mi),
                             'method': f'interpolated p90 {p90:g} ms x {MGMT_INIT_FACTOR} = {p90 * MGMT_INIT_FACTOR:g} ms; MSA Table 9 {MGMT_INIT_MSA_MS} ms; the lower wins'}
    return c


def apply_tightened(crit):
    """A copy of a candidate with the tightened values in the keys the engine reads (--check judges with it)."""
    c = copy.deepcopy(crit)
    for placement, limit in (((c.get('prbs') or {}).get('limits') or {}).get('tightened') or {}).items():
        c['prbs']['limits'].setdefault(placement, {}).update(limit)
    cal = c.get('calibration') or {}
    if 'high_power_temp_c_max_tightened' in cal:
        c['high_power']['temp_c_max'] = cal['high_power_temp_c_max_tightened']
    if 'timing_mgmt_init_ms' in cal:
        c['timing']['mgmt_init_ms'] = cal['timing_mgmt_init_ms']
    return c


# --------------------------------------------------------------------------- the files
def _fmt(v):
    return '' if v is None else f'{v:.4g}' if isinstance(v, float) else str(v)


def write_candidate(sku_file, out, cands, meta, temp_rule=None):
    """The SKU file text with the `tightened: null` slot filled (the indent of the slot line is reused) and a `calibration`
    block appended; the comments of record stay. temp_rule = the record's temp_c_max rule; the candidate adds `max` to it (A-3)."""
    tag = f'# CALIBRATION {meta["date"]} n={meta["cables"]}'
    line = lambda key, text: f'{text:<46}  {tag} method={cands[key]["method"]} ({cands[key]["samples"]} samples) PROPOSED'  # noqa: E731

    def block(match):
        ind = match.group(1)
        L = [f'{ind + "tightened:":<46}  {tag} PROPOSED: filled by aectest calibrate; the engine reads it only through --check']
        if 'ber_max_per_lane' in cands:
            L.append(line('ber_max_per_lane', f'{ind}  raw:      {{ber_max_per_lane: {cands["ber_max_per_lane"]["value"]:.1e}}}'))
        if 'errors_max_per_lane' in cands:
            L.append(line('errors_max_per_lane', f'{ind}  post_fec: {{errors_max_per_lane: 0}}'))
        return '\n'.join(L)
    text = open(sku_file).read()
    new, hits = re.subn(r'^( *)tightened: null.*$', block, text, count=1, flags=re.M)
    if hits != 1:
        raise SystemExit(f'{sku_file}: no `tightened: null` slot to fill (4.2)')
    cal = [f'{"calibration:":<46}  {tag} PROPOSED: provenance and the candidates outside prbs; the engine ignores this block',
           f'  date: "{meta["date"]}"', '  method: "ARCHITECTURE 4.1 rule 3, PROPOSED"', f'  cables: {meta["cables"]}',
           f'  sessions: {meta["sessions"]}', f'  known_bad_excluded: {json.dumps(meta["known_bad"])}',
           f'  forced_below_min: {str(meta["forced"]).lower()}', f'  source_sha256: {meta["source_sha256"]}', f'  record: {meta["record"]}']
    if 'temp_c_max' in cands:
        rule = {**(temp_rule if 'from_module' in (temp_rule or {}) else {}), 'max': cands['temp_c_max']['value']}
        cal.append(line('temp_c_max', f'  high_power_temp_c_max_tightened: {json.dumps(rule)}'))
    if 'mgmt_init_ms' in cands:
        cal.append(line('mgmt_init_ms', f'  timing_mgmt_init_ms: {{max: {cands["mgmt_init_ms"]["value"]}}}'))
    header = (f'# CANDIDATE written by aectest calibrate on {meta["date"]}: NOT the file of record. Diff it against '
              f'{os.path.basename(sku_file)} and adopt it after a review. The limits stay PROPOSED until then.\n')
    with open_new(out) as f:
        f.write(header + new.rstrip('\n') + '\n' + '\n'.join(cal) + '\n')
    return out


def write_record(path, sku, st, cands, meta, golden, bad, rows_by_bad):
    L = [f'# Calibration of {sku}, {meta["date"]}', '',
         f'Method: ARCHITECTURE 4.1 rule 3, PROPOSED. Golden cables: {meta["cables"]} ({meta["sessions"]} PASS sessions, minimum {meta["min"]}). '
         f'Forced below the minimum: {"yes" if meta["forced"] else "no"}.',
         f'SKU file of record: `{meta["source"]}` sha256 `{meta["source_sha256"]}`. Candidate: `{meta["candidate"]}`.',
         'A reviewer adopts the candidate. The engine does not read it.', '',
         '## Statistics', '', '| Key | n | min | median | p90 | max |', '|---|---|---|---|---|---|']
    L += [f'| {k} | {s["n"]} | {_fmt(s["min"])} | {_fmt(s["median"])} | {_fmt(s["p90"])} | {_fmt(s["max"])} |' for k, s in st.items()]
    L += ['', '## Candidates', '', '| Candidate | Applies to | Value | Method | Samples |', '|---|---|---|---|---|']
    L += [f'| {k} | {APPLIES_TO[k]} | {_fmt(c["value"])} | {c["method"]} | {c["samples"]} |' for k, c in cands.items()]
    L += ['', '## Known-bad cables excluded', '', '| Label SN | Session | Verdict | worst BER A>B | worst BER B>A |', '|---|---|---|---|---|']
    L += [f'| {m["cable"].get("label_sn")} | {m["session_id"]} | {(m.get("verdict") or {}).get("status")} | '
          f'{_fmt(rows_by_bad[d].get("worst_ber_AB"))} | {_fmt(rows_by_bad[d].get("worst_ber_BA"))} |' for d, m in bad] or ['| none | | | | |']
    L += ['', '## Sessions used', '', '| Session | Label SN | Verdict | manifest sha256 | prbs.json sha256 (from the manifest) |', '|---|---|---|---|---|']
    L += [f'| {m["session_id"]} | {m["cable"].get("label_sn")} | {m["verdict"]["status"]} | {sha256(os.path.join(d, "manifest.json"))} | '
          f'{(m.get("files") or {}).get("prbs.json", "")} |' for d, m in golden]
    with open_new(path) as f:
        f.write('\n'.join(L) + '\n')
    return path


# --------------------------------------------------------------------------- the two verbs
def calibrate(crit, golden, bad, out, known_bad=(), forced=False, minimum=MIN_CABLES):
    """Statistics over the golden sessions, the candidates, the candidate file `out` and the record beside it. `out` on the
    file of record is refused; both files are per run (records.open_new)."""
    if os.path.abspath(out) == os.path.abspath(crit['file']):
        raise SystemExit(f'calibrate: --out {out} is the file of record; the candidate is a new file')
    obs = {d: observed_from_session(d) for d, _ in golden + bad}
    rows = [numbers(m, obs[d]) for d, m in golden]
    warn = [((obs[d]['high_power'].get(e) or {}).get('thresholds') or {}).get('temp_high_warning_c') for d, _ in golden for e in ('A', 'B')]
    warn = [w for w in warn if w is not None]
    rule = (crit.get('high_power') or {}).get('temp_c_max') or {}
    cands = candidates(rows, min(warn) if warn else None, rule.get('margin', 0) if 'from_module' in rule else 0,
                       ((crit.get('prbs') or {}).get('limits') or {}).get('raw', {}).get('ber_max_per_lane'))
    date = datetime.now(timezone.utc).date().isoformat()
    record = os.path.join(os.path.dirname(out) or '.', f'calibration_{crit["sku"]}_{date}.md')
    meta = {'date': date, 'cables': len({m['cable'].get('label_sn') for _, m in golden}), 'sessions': len(golden), 'min': minimum,
            'known_bad': sorted(known_bad), 'forced': forced, 'source': os.path.abspath(crit['file']), 'source_sha256': crit['sha256'],
            'candidate': os.path.abspath(out), 'record': os.path.basename(record)}
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    write_candidate(crit['file'], out, cands, meta, rule)
    st = stats(rows)
    write_record(record, crit['sku'], st, cands, meta, golden, bad, {d: numbers(m, obs[d]) for d, m in bad})
    return {'stats': st, 'candidates': cands, 'meta': meta, 'candidate': out, 'record': record}


def check(cand, golden, bad):
    """Every golden session re-judged under the applied candidate must PASS; every known-bad must FAIL (lines, ok). A known-bad
    counts as caught only on a FAIL, and only the FAIL rules are named (C-1): a passthrough INCONCLUSIVE is not evidence."""
    crit = apply_tightened(cand)
    rule = lambda r: ' '.join(f'{k} {_fmt(v)}' for k, v in r.items()) if isinstance(r, dict) else _fmt(r)  # noqa: E731
    applied = ', '.join(f'{k}: {rule(v)}' for k, v in (('prbs.limits.raw', crit['prbs']['limits'].get('raw')), ('high_power.temp_c_max', crit['high_power'].get('temp_c_max')),
                                                        ('timing.mgmt_init_ms', crit['timing'].get('mgmt_init_ms'))))
    lines, ok = [f'candidate {cand["file"]} applied: {applied}'], True
    missed = []
    for d, m in golden:
        v = criteria.evaluate(crit, observed_from_session(d))
        if v.status != criteria.PASS:
            missed.append(f'golden {m["cable"].get("label_sn")} ({m["session_id"]}): {v.status} {" ".join(str(r) for r in v.reasons)}')
    lines.append(f'golden: {len(golden) - len(missed)} of {len(golden)} PASS under the candidate')
    lines += missed
    ok = ok and not missed
    for d, m in bad:
        v = criteria.evaluate(crit, observed_from_session(d))
        sn, sid = m['cable'].get('label_sn'), m['session_id']
        if v.status == criteria.FAIL:
            lines.append(f'known-bad {sn} ({sid}): FAIL -- caught by ' + '; '.join(f'{r.code} {r.where} {r.rule}' for r in v.reasons if criteria.is_fail(r.code)))
        else:
            ok = False
            codes = ' (' + ' '.join(dict.fromkeys(c for c in v.codes if not criteria.is_fail(c))) + ')' if v.codes else ''  # a FAIL after the interrupt did not decide
            lines.append(f'known-bad {sn} ({sid}): {v.status}{codes} -- no rule caught it (the method does not separate this cable)')
    return lines, ok


def add_calibrate(sub):
    ap = sub.add_parser('calibrate', help='golden-cable calibration: statistics and a PROPOSED candidate SKU file (ARCHITECTURE 4.1 rule 3, 6.3)')
    ap.add_argument('--sku', required=True, help='SKU name under --criteria or a criteria file path')
    ap.add_argument('--criteria', default=CRITERIA_DIR, metavar='DIR', help=f'criteria directory; default {CRITERIA_DIR}')
    ap.add_argument('--sessions', nargs='+', default=[], metavar='DIR', help='session roots; every manifest.json below them is read')
    ap.add_argument('--lot', action='append', default=[], metavar='DIR', help='a lot directory (lot_<ID>/*/manifest.json); same as --sessions')
    ap.add_argument('--known-bad', nargs='*', default=[], metavar='SN', help='label or CMIS SNs of the known-bad cables: out of the statistics, judged by --check; '
                    '--check defaults to the candidate\'s known_bad_excluded')
    ap.add_argument('--out', metavar='FILE', help=f'the candidate file; default {OUT_DIR}/calibration/<sku>_<date>.yaml (the record .md sits beside it)')
    ap.add_argument('--min', type=int, default=MIN_CABLES, help=f'golden cables needed (4.1 rule 3, PROPOSED); default {MIN_CABLES}')
    ap.add_argument('--force', action='store_true', help='run under --min cables; recorded in the candidate and the record')
    ap.add_argument('--check', metavar='CANDIDATE', help='re-judge every golden and known-bad session under this candidate instead of writing one')
    ap.set_defaults(func=run_calibrate)
    return ap


def run_calibrate(a):
    crit = criteria.load(criteria_path(a.sku, a.criteria))
    if not crit:
        raise SystemExit(f'no criteria file for {a.sku} under {a.criteria}')
    if a.check:
        cand = criteria.load(a.check)
        if not cand:
            raise SystemExit(f'no candidate file {a.check}')
        known = a.known_bad or (cand.get('calibration') or {}).get('known_bad_excluded') or []
        golden, bad = select(find_sessions(a.sessions + a.lot), crit['sku'], set(known))
        lines, ok = check(cand, golden, bad)
        print('\n'.join(lines))
        return 0 if ok else 1
    golden, bad = select(find_sessions(a.sessions + a.lot), crit['sku'], set(a.known_bad))
    cables = {m['cable'].get('label_sn') for _, m in golden}
    if len(cables) < a.min and not a.force:
        raise SystemExit(f'calibrate: {len(cables)} golden cables of {crit["sku"]} ({len(golden)} PASS sessions) under the minimum {a.min} '
                         f'(4.1 rule 3, PROPOSED). Add sessions, or set --force to record a calibration below the minimum.')
    date = datetime.now(timezone.utc).date().isoformat()
    out = a.out or os.path.join(OUT_DIR, 'calibration', f'{crit["sku"]}_{date}.yaml')
    r = calibrate(crit, golden, bad, out, a.known_bad, a.force, a.min)
    for k, c in r['candidates'].items():
        print(f'{k}: {_fmt(c["value"])}  ({c["method"]}; {c["samples"]} samples) PROPOSED')
    print(f'wrote {r["candidate"]} and {r["record"]} from {r["meta"]["cables"]} golden cables, {r["meta"]["sessions"]} sessions'
          + (' (FORCED below the minimum)' if a.force else ''))
    print(f'next: aectest calibrate --sku {a.sku} --sessions ... --known-bad ... --check {r["candidate"]}')
    return 0


if __name__ == '__main__':
    print(__doc__)
