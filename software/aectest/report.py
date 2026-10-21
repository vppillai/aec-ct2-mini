"""report.py - the technician page and the lot reporters.

technician_page(manifest, events, session_dir=None) -> str   the one-page report.md: verdict first, reason codes with their
    meaning, the next step, the key numbers, the identity of both ends, the checklist as acknowledged, the thermal and PRBS
    summary, the session path and the file hashes. tech.run_cable writes it BEFORE Session.close(), so the manifest hashes it.
lot_rows(session_dirs) -> list[list]   one records.lot_csv_row per manifest.json, oldest first (no header row)
lot_markdown(rows) -> str              yield, reason-code histogram, worst BER / errors per direction, temperature spread, identity anomalies
lot_csv(rows, path)                    LOT_COLUMNS header + rows, plain csv module (no charts)

Pure functions on the records. No hardware, no printing. Stdlib only.
"""
import csv
import json
import os
from collections import Counter

from .criteria import CODES, code_of
from .records import LOT_COLUMNS, lot_csv_row

NEXT = {'PASS': 'Label the cable. Put it in the pass bin.',
        'FAIL': 'Put the cable in the fail bin. Hand the session folder to engineering.'}
NEXT_INCONCLUSIVE = (  # first matching code prefix wins; the order follows 4.4 (most frequent first)
    (('JIG-UNIT-MISSING', 'NO-CRITERIA', 'POWER-UNACKED', 'STRAPS-CUT'), 'Call the engineer. The jig is not ready.'),
    (('NOT-PRESENT-', 'REMOVED-MID-TEST'), 'Seat both ends again. Run the test again.'),
    (('NO-ACK-',), 'Swap the ends. Run the test again.'),
    (('THERMAL',), 'Let the cable cool. Put forced air over the sink. Run the test again.'),
    (('OVER-JIG-BUDGET',), 'This cable needs the main board. Hand it to engineering.'),
    (('CRITERIA-PROPOSED',), 'The criteria for this cable are proposed, not confirmed. Hand the record to engineering. Do not bin the cable.'),
)
KEY_ROWS = (('MaxPower W', 'max_power_w'), ('Temp max A C', 'temp_max_A'), ('Temp max B C', 'temp_max_B'),
            ('Worst errors A>B', 'worst_errors_AB'), ('Worst errors B>A', 'worst_errors_BA'),
            ('Worst BER A>B', 'worst_ber_AB'), ('Worst BER B>A', 'worst_ber_BA'),
            ('MgmtInit ms A', 'mgmt_init_ms_A'), ('MgmtInit ms B', 'mgmt_init_ms_B'))
ID_ROWS = ('vendor', 'part_number', 'revision', 'serial', 'date_code', 'cmis_revision', 'power_class', 'max_power_w')


def _fmt(v):
    if v is None:
        return '-'
    if isinstance(v, float):
        return f'{v:.3e}' if 0 < abs(v) < 1e-3 else f'{v:g}'
    return str(v)


def meaning(reason):
    """The CODES line for one verdict reason string ('BER-LANE-3 prbs.AB.lane3 ...' -> the BER-LANE-n text)."""
    code = code_of(reason).split(':')[0]
    if code.startswith(('BER-LANE-', 'ERRORS-LANE-')):
        code = code.rsplit('-', 1)[0] + '-n'
    return CODES.get(code, 'see the engineer')


def next_step(verdict):
    status, codes = verdict.get('status'), [code_of(r) for r in verdict.get('reasons') or []]
    if status in NEXT:
        return NEXT[status]
    for prefixes, text in NEXT_INCONCLUSIVE:
        if any(c.startswith(prefixes) for c in codes):
            return text
    return 'Run the test once more. Then hand the session folder to engineering.'


def technician_page(manifest, events, session_dir=None):
    m = manifest
    v = m.get('verdict') or {}
    status = v.get('status') or 'NO VERDICT'
    k = v.get('key_numbers') or {}
    cable = m.get('cable') or {}
    cmis = cable.get('cmis') or {}
    hw = m.get('hardware') or {}
    units = hw.get('units') or {}
    health = [e for e in events if e.get('type') == 'health']
    L = [f'# Cable test: {status}', '', f'Verdict: **{status}**', '',
         f'SKU: {cable.get("sku") or "-"}. Label SN: {cable.get("label_sn") or "-"}. Operator: {m.get("operator") or "-"}.',
         f'Jig: {hw.get("jig") or "-"}. Unit A: {units.get("A") or "-"}. Unit B: {units.get("B") or "-"}. Ends tested: {cable.get("mode") or "both ends"}.', '',
         '## Reason codes', '']
    L += [f'- `{r}`: {meaning(r)}' for r in v.get('reasons') or []] or ['- none']
    L += ['', '## Next step', '', next_step(v), '', '## Key numbers', '', '| Number | Value |', '|---|---|']
    L += [f'| {name} | {_fmt(k.get(key))} |' for name, key in KEY_ROWS]
    vcc = [e['data'].get('vcc_v') for e in health if e.get('data', {}).get('vcc_v') is not None]
    L += [f'| Vcc min-max V | {_fmt(min(vcc)) + " - " + _fmt(max(vcc)) if vcc else "-"} |', '',
          '## Identity', '', '| Field | End A | End B |', '|---|---|---|']
    L += [f'| {f} | {_fmt((cmis.get("A") or {}).get(f))} | {_fmt((cmis.get("B") or {}).get(f))} |' for f in ID_ROWS]
    L += ['', '## Pre-checks', '']
    L += [f'- {e["data"].get("t") or e.get("t")} {e["data"].get("unit")}: ' + ', '.join(f'{c["name"]} {c["got"]}' + ('' if c['ok'] else ' (not met)') for c in e['data'].get('checks') or [])
          for e in events if e.get('type') == 'precheck']
    L += [f'- {e["data"].get("t") or e.get("t")} {e["data"].get("line")}' for e in events if e.get('type') == 'checklist']
    if L[-1] == '':
        L.append('- none recorded')
    L += ['', '## Thermal', '']
    for end in ('A', 'B'):
        temps = [e['data']['temp_c'] for e in health if e.get('end') == end and e['data'].get('temp_c') is not None]
        warns = sum(1 for e in health if e.get('end') == end and e['data'].get('thermal') == 'WARN')
        L.append(f'- End {end}: {len(temps)} samples, max {_fmt(max(temps)) if temps else "-"} C, WARN samples {warns}.')
    if any(r.startswith('THERMAL') for r in v.get('reasons') or []):
        L.append('- THROTTLE stopped the test. Both ends went to low power.')
    prbs = [e for e in events if e.get('type') == 'prbs']
    L += ['', '## PRBS', '']
    L += [f'- {d}: {verdict}' for e in prbs for d, verdict in (e['data'].get('directions') or {}).items()] or ['- no PRBS run']
    L += ['', '## Session', '', f'- Id: {m.get("session_id")}', f'- Path: {session_dir or "-"}',
          f'- Started: {m.get("started")}. Criteria sha256: {(m.get("criteria") or {}).get("sha256") or "-"}.',
          '- manifest.json, events.jsonl and report.md are hashed at close in manifest.json.', '', '| File | sha256 |', '|---|---|']
    L += [f'| {n} | {h} |' for n, h in sorted((m.get('files') or {}).items())]
    return '\n'.join(L) + '\n'


def lot_rows(session_dirs):
    rows = []
    for d in session_dirs:
        with open(os.path.join(d, 'manifest.json')) as f:
            rows.append(lot_csv_row(json.load(f)))
    return sorted(rows, key=lambda r: str(r[0]))


def lot_csv(rows, path):
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(LOT_COLUMNS)
        w.writerows(rows)
    return path


def lot_markdown(rows):
    recs = [dict(zip(LOT_COLUMNS, r)) for r in rows]
    n = len(recs)
    count = Counter(r['result'] for r in recs)
    codes = Counter(code_of(x).split(':')[0] for r in recs for x in (r['reasons'] or '').split(' | ') if x)
    L = ['# Lot summary', '', f'Cables: {n}. PASS: {count["PASS"]}. FAIL: {count["FAIL"]}. INCONCLUSIVE: {count["INCONCLUSIVE"]}.',
         f'Yield: {100.0 * count["PASS"] / n if n else 0.0:.1f} %.', '', '## Reason codes', '', '| Code | Count | Meaning |', '|---|---|---|']
    L += [f'| {c} | {k} | {meaning(c)} |' for c, k in codes.most_common()] or ['| none | 0 | |']
    L += ['', '## Worst PRBS per direction', '', '| Direction | Worst BER | Worst errors |', '|---|---|---|']
    for d in ('AB', 'BA'):
        ber = [r[f'worst_ber_{d}'] for r in recs if r[f'worst_ber_{d}'] is not None]
        err = [r[f'worst_errors_{d}'] for r in recs if r[f'worst_errors_{d}'] is not None]
        L.append(f'| {d[0]}>{d[1]} | {_fmt(max(ber)) if ber else "-"} | {_fmt(max(err)) if err else "-"} |')
    L += ['', 'The per-lane values are in each session prbs.json.', '', '## Temperature', '']
    for end in ('A', 'B'):
        t = [r[f'temp_max_{end}'] for r in recs if r[f'temp_max_{end}'] is not None]
        L.append(f'- End {end} max temperature: {_fmt(min(t)) + " to " + _fmt(max(t)) + " C" if t else "-"} over {len(t)} cables.')
    ident = sum(k for c, k in codes.items() if c.startswith('ID-'))
    L += ['', f'Identity anomalies: {ident}.', '', '## Cables', '', '| Timestamp | Label SN | SN A | SN B | Result | Key number | Reasons |',
          '|---|---|---|---|---|---|---|']
    L += [f'| {r["timestamp"]} | {_fmt(r["label_sn"])} | {_fmt(r["SN_A"])} | {_fmt(r["SN_B"])} | {r["result"]} | {_fmt(r["key_number"])} | '
          f'{r["reasons"] or "-"} |' for r in recs]
    return '\n'.join(L) + '\n'
