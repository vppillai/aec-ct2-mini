#!/usr/bin/env python3
"""criteria.py - load one SKU criteria file and judge an observed record.

Pure data in, Verdict out. No hardware, no printing. Rule kinds: equals, matches, min, max, range, flag_clear,
all_lanes_equal, from_module (+ margin), ber_max_per_lane, errors_max_per_lane, require_measured, the boolean identity
rules and `unknown` (record only). Every reason carries a code from CODES (section 4.4 plus the derived codes marked here).

Observed record shape (plain dict, every part optional; a judged key with no value gives NOT-MEASURED):
  {"label_sn": "...",
   "ends": {"A": decode_id() dict, "B": ...},                      identity per end
   "timing": {"A": {"mgmt_init_ms": 420, "module_ready_s": 3.1}, "B": ...},
   "low_power": {"A": {"module_state", "flags_byte9", "temp_c", "vcc_v"}, "B": ...},
   "high_power": {"A": {"module_state", "dp_states": [8 names], "temp_c_max", "temp_slope_c_per_min", "vcc_v",
                        "failure_flag_tx_11h_135", "thresholds": decode_thresholds() dict}, "B": ...},
   "prbs": {"AB": {"verdict": "MEASURED...|NOT-ADVERTISED|INCONCLUSIVE: ...|ABORTED: ...", "text": "...",
                   "placement": "raw|post_fec or the runner's Table 8-97 label (PLACEMENT maps it)",
                   "lanes": {1: {"ber": 1e-6, "errors": 0}, ...}}, "BA": ...},   prbs_from_record() builds it from cmd_prbs
   "vdm": {...},                                                     recorded, never judged while vdm.judge is false
   "inconclusive": ["THERMAL", "REMOVED-MID-TEST", ...]}             codes the runner already knows (section 4.4)

  python -m aectest.criteria --selftest
"""
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field

import yaml

SCHEMA = 'aectest.criteria/1'
SECTIONS = ('identity', 'timing', 'low_power', 'high_power', 'prbs', 'vdm')
PASS, FAIL, INCONCLUSIVE = 'PASS', 'FAIL', 'INCONCLUSIVE'
# section 4.4 codes; "derived" = not named in 4.4, one per rule family the draft file judges without a code (README lists them)
CODES = {
    'ID-VENDOR': 'vendor string does not match the SKU',
    'ID-PN': 'part number does not match the SKU',
    'ID-SN-LABEL': 'scanned label serial differs from CMIS 00h:166-181',
    'ID-ENDS-DIFFER': 'the two ends report different part numbers',
    'ID-CMIS-REV': 'CMIS revision below the SKU minimum (derived)',
    'ID-IDENTIFIER': 'SFF-8024 identifier is not the SKU form factor (derived)',
    'STATE-LOWPWR': 'module not in ModuleLowPwr after insertion',
    'STATE-READY': 'module not in ModuleReady after release',
    'TEMP-ALARM': 'reserved: a temperature alarm bit in 00h:9 is reported as INCONCLUSIVE THERMAL',
    'TEMP-OVER-LIMIT': 'temperature outside the limit (window, or module warning threshold minus margin)',
    'TEMP-SLOPE': 'temperature still rising at the end of the soak (derived)',
    'VCC-OUT-OF-RANGE': 'VccMon outside MSA Table 13',
    'FLAGS-BYTE9': 'a Vcc alarm or warning bit set in 00h:9',
    'TX-FAILURE-FLAG': 'FailureFlagTx set in 11h:135',
    'BER-LANE-n': 'raw BER above the per-lane ceiling on a MEASURED run',
    'ERRORS-LANE-n': 'error count above the per-lane limit on a MEASURED run',
    'MGMT-INIT-SLOW': 'MgmtInitDuration above MSA Table 9',
    'NO-ACK-END': 'the same cable end does not ACK on both units',
    'MODULE-READY-SLOW': 'ModuleReady later than the SKU limit (derived)',
    # INCONCLUSIVE codes the engine emits
    'OVER-JIG-BUDGET': 'power class or MaxPower above the MINI jig limit (S4); not a cable finding',
    'DP-NOT-ACTIVATED': 'not every lane reached DPActivated',
    'PRBS-NOT-ADVERTISED': 'module does not advertise media-side PRBS; not applicable, never FAIL',
    'PRBS-INCONCLUSIVE': 'PRBS run not MEASURED; the reason follows the colon',
    'NOT-MEASURED': 'a judged key has no observed value (derived)',
    'CRITERIA-PROPOSED': 'the matched criteria file is PROPOSED, not yet confirmed: a miss is INCONCLUSIVE, never FAIL',
    'NOT-RUN': 'the steps after an early stop did not run; one line for all their keys (derived)',
    'NO-CRITERIA': 'no criteria file for the SKU',
    # INCONCLUSIVE codes the runner passes through in observed["inconclusive"]
    'THERMAL': 'the jig thermal condition: a THROTTLE, or a low-power temperature outside the window',
    'REMOVED-MID-TEST': 'ModPrsL went high during a test',
    'JIG-UNIT-MISSING': 'one of the two units is not attached',
    'POWER-UNACKED': 'no signed jig profile and no power acknowledgement',
    'NOT-PRESENT-A': 'no module sensed on unit A', 'NOT-PRESENT-B': 'no module sensed on unit B',
    'NO-ACK-A': 'the module on unit A does not ACK; swap ends and re-run', 'NO-ACK-B': 'the module on unit B does not ACK; swap ends and re-run',
    'STRAPS-CUT': 'jig profile straps_intact false: R&D dongle session, the FTDI is off the pads; tech mode refuses',
    'ABORTED': 'the runner aborted; the text follows the colon',
}
INTERRUPTS = ('NO-ACK-', 'REMOVED-MID-TEST', 'THERMAL', 'ABORTED')  # passthrough codes after which high_power / prbs FAILs do not decide
HIGH_POWER_PHASE = ('timing.module_ready_s', 'high_power', 'prbs', 'vdm')
KINDS = ('equals', 'matches', 'min', 'max', 'range', 'flag_clear', 'all_lanes_equal', 'from_module')
PLACEMENT = {'raw pattern on the line': 'raw', 'FEC-encoded pattern': 'post_fec'}  # tests.fec_placement label prefix -> limits key
# criteria key -> (code, status when missed)
CODE_FOR = {
    'identity.vendor': ('ID-VENDOR', FAIL), 'identity.part_number': ('ID-PN', FAIL),
    'identity.cmis_revision': ('ID-CMIS-REV', FAIL), 'identity.identifier': ('ID-IDENTIFIER', FAIL),
    'identity.power_class': ('OVER-JIG-BUDGET', INCONCLUSIVE), 'identity.max_power_w': ('OVER-JIG-BUDGET', INCONCLUSIVE),
    'identity.ends_same_pn': ('ID-ENDS-DIFFER', FAIL), 'identity.label_sn_matches_cmis': ('ID-SN-LABEL', FAIL),
    'timing.mgmt_init_ms': ('MGMT-INIT-SLOW', FAIL), 'timing.module_ready_s': ('MODULE-READY-SLOW', FAIL),
    'low_power.module_state': ('STATE-LOWPWR', FAIL), 'low_power.flags_byte9': ('FLAGS-BYTE9', FAIL),
    'low_power.temp_c': ('THERMAL', INCONCLUSIVE), 'low_power.vcc_v': ('VCC-OUT-OF-RANGE', FAIL),
    'high_power.module_state': ('STATE-READY', FAIL), 'high_power.dp_states': ('DP-NOT-ACTIVATED', INCONCLUSIVE),
    'high_power.temp_c_max': ('TEMP-OVER-LIMIT', FAIL), 'high_power.temp_slope_c_per_min': ('TEMP-SLOPE', FAIL),
    'high_power.vcc_v': ('VCC-OUT-OF-RANGE', FAIL), 'high_power.failure_flag_tx_11h_135': ('TX-FAILURE-FLAG', FAIL),
}
TEMP_FLAGS9 = 0x05
THRESHOLD_KEY = {'TempMonHighWarningThreshold': 'temp_high_warning_c', 'TempMonHighAlarmThreshold': 'temp_high_alarm_c',
                 'VccMonHighWarningThreshold': 'vcc_high_warning_v', 'VccMonHighAlarmThreshold': 'vcc_high_alarm_v'}
PER_END = ('identity', 'timing', 'low_power', 'high_power')
OBSERVED_SECTION = {'identity': 'ends'}  # criteria section -> observed key when the names differ


FAIL_CODES = {c for c, s in CODE_FOR.values() if s == FAIL} | {'ID-ENDS-DIFFER', 'ID-SN-LABEL'}  # plus the BER-LANE-n / ERRORS-LANE-n codes


def code_of(reason):
    """The 4.4 code of one verdict reason string ('BER-LANE-3 prbs.AB.lane3 ...' -> 'BER-LANE-3')."""
    return str(reason).split(' ')[0]


def is_fail(code):
    """True when the engine tags this code FAIL (the lane codes carry the lane number)."""
    return code.startswith(('BER-LANE-', 'ERRORS-LANE-')) or code in FAIL_CODES


@dataclass
class Reason:
    code: str
    where: str          # "identity.vendor@A", "prbs.AB.lane3"
    rule: str           # "matches ^Credo"
    value: object = None

    def __str__(self):
        return f'{self.code} {self.where} {self.rule} got {self.value!r}'


@dataclass
class Verdict:
    status: str
    reasons: list = field(default_factory=list)
    key_numbers: dict = field(default_factory=dict)

    @property
    def codes(self):
        return [r.code for r in self.reasons]

    def as_dict(self):
        return {'status': self.status, 'reasons': [str(r) for r in self.reasons], 'key_numbers': self.key_numbers}


def load(path):
    """The SKU file as a dict plus `file` and `sha256`; None when the file is missing (NO-CRITERIA); SystemExit on a
    schema mismatch, a missing section, a key without a code, a rule kind not in KINDS or an unquoted cmis_revision."""
    if not os.path.exists(path):
        return None
    raw = open(path, 'rb').read()
    d = yaml.safe_load(raw)
    if not isinstance(d, dict) or d.get('schema') != SCHEMA:
        raise SystemExit(f'{path}: schema {d.get("schema") if isinstance(d, dict) else None!r} is not {SCHEMA}')
    missing = [k for k in ('sku', 'owner', *SECTIONS) if k not in d]
    if missing:
        raise SystemExit(f'{path}: missing {missing}')
    for section in PER_END:
        for key, rule in (d[section] or {}).items():
            if f'{section}.{key}' not in CODE_FOR and key != 'ends_same_sn':
                raise SystemExit(f'{path}: {section}.{key} has no code in CODE_FOR')
            if isinstance(rule, dict) and next(iter(rule)) not in KINDS:
                raise SystemExit(f'{path}: {section}.{key} rule kind {next(iter(rule))!r} is not one of {KINDS}')
            if key == 'cmis_revision' and any(isinstance(v, float) for v in rule.values()):
                raise SystemExit(f'{path}: identity.cmis_revision wants a quoted version string ("4.0"), got {rule}')
    d['file'], d['sha256'] = path, hashlib.sha256(raw).hexdigest()
    d['proposed'] = b'Written by aectest derive' in raw and str(d.get('owner') or '').strip() in ('', '<name>')
    return d


def _num(v):
    """Comparable form: '0x18' -> 24, '4.0' -> (4, 0) version tuple, numbers unchanged."""
    if isinstance(v, str):
        if v.lower().startswith('0x'):
            return int(v, 16)
        if re.fullmatch(r'\d+(\.\d+)+', v):
            return tuple(int(x) for x in v.split('.'))
        try:
            return float(v)
        except ValueError:
            return v
    return v


def _check(rule, value, end_data):
    """(ok, rule text) for one rule dict against one value; None = this rule kind has no value to judge here."""
    kind, want = next(iter(rule.items()))
    text = f'{kind} {want}'
    if kind == 'equals':
        return _num(value) == _num(want), text
    if kind == 'matches':
        return bool(re.match(want, str(value))), text
    if kind == 'min':
        return _num(value) >= _num(want), text
    if kind == 'max':
        return _num(value) <= _num(want), text
    if kind == 'range':
        return want[0] <= _num(value) <= want[1], text
    if kind == 'flag_clear':
        return not (int(_num(value)) & int(want)), f'flag_clear 0x{int(want):02x}'
    if kind == 'all_lanes_equal':
        if isinstance(want, (list, tuple)):
            return bool(value) and len(set(value)) == 1 and value[0] in want, f'all_lanes_equal one of {list(want)}'
        return bool(value) and all(v == want for v in value), text
    if kind == 'from_module':
        thr = (end_data.get('thresholds') or {}).get(THRESHOLD_KEY.get(want, want))
        bounds = [b for b in (thr + rule.get('margin', 0) if thr is not None else None, rule.get('max')) if b is not None]
        if not bounds:
            return None, text
        limit = min(bounds)
        return _num(value) <= limit, f'from_module {want} {thr} margin {rule.get("margin", 0)}' + (f' max {rule["max"]}' if 'max' in rule else '') + f' -> max {limit}'
    raise SystemExit(f'unknown rule kind {kind!r}')


def _ends(obs):
    """The ends the run observed: A alone when one cable end sat in one unit, else A and B."""
    return ('A',) if obs.get('mode') == 'one end' else ('A', 'B')


def _judge_sections(crit, obs, reasons):
    for section in PER_END:
        rules = crit.get(section) or {}
        observed = obs.get(OBSERVED_SECTION.get(section, section)) or {}
        for key, rule in rules.items():
            if not isinstance(rule, dict):
                continue  # the boolean / unknown identity rules
            code, status = CODE_FOR[f'{section}.{key}']
            for end in _ends(obs):
                data = observed.get(end) or {}
                value = data.get(key)
                where = f'{section}.{key}@{end}'
                if value is None and key == 'mgmt_init_ms' and data.get('edge_seen') is False:
                    continue  # C-1: the module was in the cage before step 2; no edge, no MgmtInit time, nothing to judge
                if value is None:
                    reasons.append((INCONCLUSIVE, Reason('NOT-MEASURED', where, next(iter(rule)))))
                    continue
                ok, text = _check(rule, value, data)
                if key == 'dp_states' and not ok and observed_host(obs) and rule.get('all_lanes_equal') == 'DPInit':
                    ok = bool(value) and all(v in ('DPInit', 'DPActivated') for v in value)
                    text += ' or DPActivated (host present)'
                if ok is None:
                    reasons.append((INCONCLUSIVE, Reason('NOT-MEASURED', where, text + ' (no module threshold)')))
                elif not ok:
                    if code == 'FLAGS-BYTE9' and int(value) & TEMP_FLAGS9:
                        reasons.append((INCONCLUSIVE, Reason('THERMAL', where, text + ' (temperature bit)', value)))
                        if not int(value) & ~TEMP_FLAGS9:
                            continue
                    reasons.append((status, Reason(code, where, text, value)))


def observed_host(obs):
    """`host_present` from the jig profile (a switch port drives the host lanes); the MINI alone has none."""
    return bool(obs.get('host_present'))


def _judge_identity_pairs(crit, obs, reasons, key_numbers):
    ident = crit.get('identity') or {}
    ends = obs.get('ends') or {}
    a, b = ends.get('A') or {}, ends.get('B') or {}
    if ident.get('ends_same_pn') is True and a.get('part_number') and b.get('part_number') and a['part_number'] != b['part_number']:
        reasons.append((FAIL, Reason('ID-ENDS-DIFFER', 'identity.ends_same_pn', 'equal part_number',
                                     [a.get('part_number'), b.get('part_number')])))
    if ident.get('ends_same_sn') == 'unknown':
        key_numbers['ends_same_sn'] = a.get('serial') == b.get('serial') if a and b else None
    if ident.get('label_sn_matches_cmis') is True:
        label = obs.get('label_sn')
        judged = (('A', a),) if ident.get('ends_same_sn') == 'unknown' or obs.get('mode') == 'one end' else (('A', a), ('B', b))
        key_numbers['label_sn_judged_on'] = ''.join(e for e, _ in judged)
        for end, d in judged:
            if label is None or d.get('serial') is None:
                reasons.append((INCONCLUSIVE, Reason('NOT-MEASURED', f'identity.label_sn_matches_cmis@{end}', 'label_sn')))
            elif label != d['serial']:
                reasons.append((FAIL, Reason('ID-SN-LABEL', f'identity.label_sn_matches_cmis@{end}', f'equals {label}',
                                             d['serial'])))


def _runner_code(text):
    """4.4 code from a runner verdict string: 'ABORTED: x' -> 'ABORTED:x', 'INCONCLUSIVE: x' or 'x' -> 'PRBS-INCONCLUSIVE:x'."""
    head, _, tail = text.partition(':')
    if head in ('ABORTED', 'INCONCLUSIVE'):
        return ('ABORTED:' if head == 'ABORTED' else 'PRBS-INCONCLUSIVE:') + tail.strip()
    return 'PRBS-INCONCLUSIVE:' + text


def _judge_prbs(crit, obs, reasons, key_numbers):
    if obs.get('mode') == 'one end':
        key_numbers['mode'] = 'one end'
        return
    p = crit.get('prbs') or {}
    limits = p.get('limits') or {}
    for direction in ('AB', 'BA'):
        run = (obs.get('prbs') or {}).get(direction) or {}
        verdict = str(run.get('verdict') or '')
        if not verdict.startswith('MEASURED'):  # the runner writes 'MEASURED (CMIS<5.0 layouts assumed)' under --force-cmis
            if p.get('require_measured', True):
                if verdict == 'NOT-ADVERTISED':
                    reasons.append((INCONCLUSIVE, Reason('PRBS-NOT-ADVERTISED', f'prbs.{direction}', 'require_measured')))
                else:
                    reasons.append((INCONCLUSIVE, Reason(_runner_code(run.get('text') or verdict or 'no run'),
                                                         f'prbs.{direction}', 'require_measured', verdict or None)))
            continue
        if verdict != 'MEASURED':
            key_numbers[f'prbs_caveat_{direction}'] = verdict
        placement = str(run.get('placement'))
        placement = next((k for lbl, k in PLACEMENT.items() if placement.startswith(lbl)), placement)
        limit = limits.get(placement)
        if limit is None:  # an unmapped placement applies no limit: never PASS on it
            reasons.append((INCONCLUSIVE, Reason('NOT-MEASURED', f'prbs.{direction}', f'limits.{placement}', run.get('placement'))))
            continue
        lanes = run.get('lanes') or {}
        for field in ('errors', 'ber'):
            got = [l[field] for l in lanes.values() if l.get(field) is not None]
            key_numbers[f'worst_{field}_{direction}'] = max(got) if got else None
        for limit_key, field, code in (('ber_max_per_lane', 'ber', 'BER'), ('errors_max_per_lane', 'errors', 'ERRORS')):
            if limit_key not in limit:
                continue
            missing = [lane for lane, l in sorted(lanes.items()) if l.get(field) is None]
            if missing:
                reasons.append((INCONCLUSIVE, Reason('NOT-MEASURED', f'prbs.{direction}', f'{limit_key} {field}', f'lanes {missing}')))
            for lane, l in sorted(lanes.items()):
                if l.get(field) is not None and l[field] > limit[limit_key]:
                    reasons.append((FAIL, Reason(f'{code}-LANE-{lane}', f'prbs.{direction}.lane{lane}', f'{limit_key} {limit[limit_key]}', l[field])))


def prbs_from_record(rec, names=('A', 'B')):
    """The observed `prbs` block from one tests.cmd_prbs record (the one place that reads its shape): directions
    `<A>_to_<B>` -> AB, `media_ber` / `media_errors` lists -> lanes 1-8 inside the lane mask, verdict and placement verbatim."""
    out = {}
    for d, key in ((f'{names[0]}_to_{names[1]}', 'AB'), (f'{names[1]}_to_{names[0]}', 'BA')):
        r = (rec.get('directions') or {}).get(d)
        if r is None:
            continue
        mask = int(str(r.get('lane_mask') or '0xff').split()[0], 16)
        ber, errs = r.get('media_ber') or [], r.get('media_errors') or []
        lanes = {i + 1: {'ber': ber[i] if i < len(ber) else None, 'errors': errs[i] if i < len(errs) else None}
                 for i in range(8) if mask >> i & 1}
        out[key] = {'verdict': r.get('verdict'), 'placement': r.get('placement'), 'lanes': lanes}
    return out


def _key_numbers(obs, key_numbers):
    ends = obs.get('ends') or {}
    mp = [d.get('max_power_w') for d in ends.values() if d.get('max_power_w') is not None]
    key_numbers['max_power_w'] = max(mp) if mp else None
    for end in _ends(obs):
        hp, t = (obs.get('high_power') or {}).get(end) or {}, (obs.get('timing') or {}).get(end) or {}
        key_numbers[f'temp_max_{end}'] = hp.get('temp_c_max')
        key_numbers[f'mgmt_init_ms_{end}'] = t.get('mgmt_init_ms')


def evaluate(criteria, observed):
    """Verdict for one observed record. FAIL beats INCONCLUSIVE (4.4: a miss on evidence the cable produced is a FAIL)."""
    if not criteria:
        return Verdict(INCONCLUSIVE, [Reason('NO-CRITERIA', 'criteria', 'load')])
    tagged, key_numbers = [], {}
    _judge_sections(criteria, observed, tagged)
    _judge_identity_pairs(criteria, observed, tagged, key_numbers)
    _judge_prbs(criteria, observed, tagged, key_numbers)
    _key_numbers(observed, key_numbers)
    if observed.get('mode'):
        key_numbers['mode'] = observed['mode']
    passthrough = list(observed.get('inconclusive') or [])
    for code in passthrough:
        tagged.append((INCONCLUSIVE, Reason(code, 'runner', 'passthrough')))
    if (criteria.get('vdm') or {}).get('judge') is False and observed.get('vdm') is not None:
        key_numbers['vdm_recorded'] = True
    interrupted = any(c.startswith(INTERRUPTS) for c in passthrough)
    statuses = {s for s, r in tagged if not (interrupted and r.where.startswith(('high_power', 'prbs')))}
    status = FAIL if FAIL in statuses else INCONCLUSIVE if INCONCLUSIVE in statuses else PASS
    reasons = [r for _, r in tagged]
    if interrupted:
        gone = [r for r in reasons if r.code == 'NOT-MEASURED' or r.code == 'PRBS-INCONCLUSIVE:no run']
        if gone:
            reasons = [r for r in reasons if r not in gone] + [Reason('NOT-MEASURED', 'runner', f'{len(gone)} keys unmeasured after the interrupt')]
    elif observed.get('stopped'):
        st = observed['stopped']
        gone = [r for r in reasons if r.code == 'NOT-MEASURED' and r.where.startswith(HIGH_POWER_PHASE)]
        if gone:
            reasons = [r for r in reasons if r not in gone] + [Reason('NOT-RUN', 'runner', f"stopped at step {st.get('step')} ({st.get('name')})", f'{len(gone)} keys')]
    if status == FAIL and criteria.get('proposed'):
        status = INCONCLUSIVE
        reasons.append(Reason('CRITERIA-PROPOSED', 'criteria', 'criteria proposed, not confirmed', os.path.basename(str(criteria.get('file')))))
    return Verdict(status, reasons, key_numbers)


# --------------------------------------------------------------------------- selftest
def _good_observed():
    end = {'vendor': 'Credo', 'part_number': 'CAC41X321D1D', 'serial': 'CRD000001', 'cmis_revision': '4.0',
           'identifier': '0x18', 'power_class': 3, 'max_power_w': 5.5}
    lp = {'module_state': 'ModuleLowPwr', 'flags_byte9': 0, 'temp_c': 31.5, 'vcc_v': 3.30}
    hp = {'module_state': 'ModuleReady', 'dp_states': ['DPActivated'] * 8, 'temp_c_max': 52.0, 'temp_slope_c_per_min': 0.4,
          'vcc_v': 3.28, 'failure_flag_tx_11h_135': 0, 'thresholds': {'temp_high_warning_c': 75.0}}
    lanes = {i: {'ber': 1.0e-6, 'errors': 0} for i in range(1, 9)}
    run = {'verdict': 'MEASURED', 'placement': 'raw', 'lanes': lanes}
    return {'label_sn': 'CRD000001', 'ends': {'A': dict(end), 'B': dict(end)},
            'timing': {'A': {'mgmt_init_ms': 410, 'module_ready_s': 2.5}, 'B': {'mgmt_init_ms': 395, 'module_ready_s': 2.4}},
            'low_power': {'A': dict(lp), 'B': dict(lp)}, 'high_power': {'A': dict(hp), 'B': dict(hp)},
            'prbs': {'AB': dict(run), 'BA': dict(run)}, 'vdm': {'advertised': False}}


def selftest(path):
    import copy
    crit = load(path)
    assert len(crit['sha256']) == 64 and crit['sku'] == 'credo-cac41x321d1d'
    good = _good_observed()
    v = evaluate(crit, good)
    assert v.status == PASS and not v.reasons, v.as_dict()
    assert v.key_numbers['ends_same_sn'] is True and v.key_numbers['max_power_w'] == 5.5 and v.key_numbers['vdm_recorded']
    assert v.key_numbers['worst_ber_AB'] == 1.0e-6 and v.key_numbers['temp_max_B'] == 52.0

    def one(mutate, want_status, want_code):
        o = copy.deepcopy(good)
        mutate(o)
        v = evaluate(crit, o)
        assert v.status == want_status and any(c.startswith(want_code) for c in v.codes), (want_code, v.as_dict())
        return v

    def setv(section, end, key, value):
        return lambda o: o[section][end].__setitem__(key, value)

    # one FAIL per rule family
    one(setv('ends', 'A', 'vendor', 'Acme'), FAIL, 'ID-VENDOR')                       # matches
    one(setv('ends', 'B', 'part_number', 'CAC41X321D1E'), FAIL, 'ID-PN')               # matches + ends differ
    one(setv('ends', 'B', 'part_number', 'CAC41X321D1E'), FAIL, 'ID-ENDS-DIFFER')
    one(setv('ends', 'A', 'cmis_revision', '3.0'), FAIL, 'ID-CMIS-REV')               # min on a version string
    one(setv('ends', 'A', 'identifier', '0x19'), FAIL, 'ID-IDENTIFIER')               # equals, hex string vs int
    v = one(lambda o: o.__setitem__('label_sn', 'OTHER'), FAIL, 'ID-SN-LABEL')
    assert v.codes.count('ID-SN-LABEL') == 1 and v.key_numbers['label_sn_judged_on'] == 'A', v.as_dict()
    one(setv('timing', 'A', 'mgmt_init_ms', 2500), FAIL, 'MGMT-INIT-SLOW')            # max
    one(setv('timing', 'B', 'module_ready_s', 16), FAIL, 'MODULE-READY-SLOW')
    one(setv('low_power', 'A', 'module_state', 'ModuleReady'), FAIL, 'STATE-LOWPWR')  # equals
    one(setv('low_power', 'A', 'flags_byte9', 0x04), INCONCLUSIVE, 'THERMAL')
    one(setv('low_power', 'A', 'flags_byte9', 0x40), FAIL, 'FLAGS-BYTE9')             # flag_clear, Vcc bit
    v = one(setv('low_power', 'A', 'flags_byte9', 0x44), FAIL, 'FLAGS-BYTE9')         # both: the Vcc bit still FAILs
    assert 'THERMAL' in v.codes and 'TEMP-ALARM' not in v.codes, v.codes
    one(setv('low_power', 'B', 'temp_c', 2.0), INCONCLUSIVE, 'THERMAL')
    one(setv('low_power', 'B', 'vcc_v', 3.5), FAIL, 'VCC-OUT-OF-RANGE')               # range
    one(setv('high_power', 'A', 'module_state', 'ModuleFault'), FAIL, 'STATE-READY')
    one(setv('high_power', 'A', 'temp_c_max', 71.0), FAIL, 'TEMP-OVER-LIMIT')         # from_module 75 - 5 = 70
    one(setv('high_power', 'A', 'temp_slope_c_per_min', 3.0), FAIL, 'TEMP-SLOPE')
    one(setv('high_power', 'B', 'failure_flag_tx_11h_135', 0x01), FAIL, 'TX-FAILURE-FLAG')
    one(lambda o: o['prbs']['AB']['lanes'][3].__setitem__('ber', 1e-3), FAIL, 'BER-LANE-3')   # ber_max_per_lane
    v = one(lambda o: (o['prbs']['BA'].__setitem__('placement', 'FEC-encoded pattern (gen pre-FEC-encoder 131.7, chk post-FEC-decoder 131.4)'),
                       o['prbs']['BA']['lanes'][7].__setitem__('errors', 2)), FAIL, 'ERRORS-LANE-7')
    assert v.key_numbers['worst_errors_BA'] == 2
    # INCONCLUSIVE paths
    one(setv('ends', 'A', 'power_class', 4), INCONCLUSIVE, 'OVER-JIG-BUDGET')
    one(setv('ends', 'B', 'max_power_w', 7.0), INCONCLUSIVE, 'OVER-JIG-BUDGET')
    one(setv('high_power', 'A', 'dp_states', ['DPActivated'] * 7 + ['DPInit']), INCONCLUSIVE, 'DP-NOT-ACTIVATED')
    one(lambda o: o['prbs']['BA'].update(verdict='INCONCLUSIVE', text='TotalBits 0'), INCONCLUSIVE, 'PRBS-INCONCLUSIVE:TotalBits 0')
    one(lambda o: o['prbs'].pop('AB'), INCONCLUSIVE, 'PRBS-INCONCLUSIVE:no run')
    one(lambda o: o['prbs']['AB'].update(verdict='NOT-ADVERTISED'), INCONCLUSIVE, 'PRBS-NOT-ADVERTISED')
    one(lambda o: o.__setitem__('inconclusive', ['THERMAL']), INCONCLUSIVE, 'THERMAL')
    # C-1 / C-2 / C-9: the runner's own strings -- MEASURED with the --force-cmis caveat, the Table 8-97 label, prefixed verdicts
    o = copy.deepcopy(good)
    o['prbs']['AB'].update(verdict='MEASURED (CMIS<5.0 layouts assumed)', placement='raw pattern on the line (gen post-FEC-encoder 131.6, chk pre-FEC-decoder 131.5)')
    v = evaluate(crit, o)
    assert v.status == PASS and v.key_numbers['prbs_caveat_AB'] == 'MEASURED (CMIS<5.0 layouts assumed)', v.as_dict()
    o['prbs']['AB']['lanes'][2]['ber'] = 1.0
    assert evaluate(crit, o).status == FAIL, 'the label maps to the raw limit'
    one(lambda o: o['prbs']['AB'].update(placement='something else'), INCONCLUSIVE, 'NOT-MEASURED')  # unmapped placement: no limit, no PASS
    one(lambda o: o['prbs']['AB'].update(verdict='INCONCLUSIVE: no health sample from A (gate 60 s)'), INCONCLUSIVE, 'PRBS-INCONCLUSIVE:no health')
    one(lambda o: o['prbs']['BA'].update(verdict='ABORTED: Ctrl-C'), INCONCLUSIVE, 'ABORTED:Ctrl-C')
    # C-3: one end silent = NOT-MEASURED, never ID-ENDS-DIFFER; a passthrough interrupt keeps high_power / prbs FAILs from deciding
    v = one(lambda o: o['ends'].__setitem__('B', {}), INCONCLUSIVE, 'NOT-MEASURED')
    assert 'ID-ENDS-DIFFER' not in v.codes, v.codes
    v = one(lambda o: (o.__setitem__('inconclusive', ['REMOVED-MID-TEST']), o['high_power']['A'].__setitem__('module_state', 'ModuleLowPwr')),
            INCONCLUSIVE, 'STATE-READY')
    assert 'REMOVED-MID-TEST' in v.codes
    one(lambda o: (o.__setitem__('inconclusive', ['NO-ACK-B']), o['ends']['A'].__setitem__('vendor', 'Acme')), FAIL, 'ID-VENDOR')  # identity stands
    one(lambda o: o['high_power']['A'].pop('thresholds'), INCONCLUSIVE, 'NOT-MEASURED')  # from_module without a threshold
    one(lambda o: o.pop('high_power'), INCONCLUSIVE, 'NOT-MEASURED')
    # FAIL beats INCONCLUSIVE: identity evidence stands even when the PRBS run did not happen
    one(lambda o: (o['ends']['A'].__setitem__('vendor', 'Acme'), o.pop('prbs')), FAIL, 'ID-VENDOR')
    # unknown = record only: differing serials change the key number, never the status (label rule off for this probe)
    c2, o = copy.deepcopy(crit), copy.deepcopy(good)
    c2['identity']['label_sn_matches_cmis'] = False
    o['ends']['B']['serial'] = 'CRD000002'
    v = evaluate(c2, o)
    assert v.status == PASS and v.key_numbers['ends_same_sn'] is False, v.as_dict()
    o['label_sn'] = None
    v = evaluate(crit, o)
    assert v.status == INCONCLUSIVE and v.codes.count('NOT-MEASURED') == 1, v.as_dict()
    v = one(lambda o: [l.__setitem__('ber', None) for l in o['prbs']['AB']['lanes'].values()], INCONCLUSIVE, 'NOT-MEASURED')
    assert v.key_numbers['worst_ber_AB'] is None and 'BER-LANE-1' not in v.codes, v.as_dict()
    one(lambda o: (o['prbs']['BA'].__setitem__('placement', 'post_fec'), o['prbs']['BA']['lanes'][5].__setitem__('errors', None)), INCONCLUSIVE, 'NOT-MEASURED')
    # C-1: a module already in the cage has no MgmtInit time and the rule is skipped, not NOT-MEASURED
    v = evaluate(crit, {**good, 'timing': {'A': {'mgmt_init_ms': None, 'edge_seen': False, 'module_ready_s': 2.5}, 'B': good['timing']['B']}})
    assert v.status == PASS, v.as_dict()
    v = evaluate(crit, {'label_sn': 'CRD000001', 'ends': good['ends'], 'inconclusive': ['NO-ACK-B']})
    assert v.codes.count('NOT-MEASURED') == 1 and 'NO-ACK-B' in v.codes and v.status == INCONCLUSIVE, v.as_dict()
    assert evaluate(None, good).codes == ['NO-CRITERIA']
    # loader: a missing file is None (NO-CRITERIA); a wrong schema, an unquoted version, an unknown key or rule kind is refused
    import tempfile
    assert load(path + '.missing') is None
    text = open(path).read()
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, 'bad.yaml')
        for body, word in (('schema: aectest.criteria/2\nsku: x\n', 'is not'),
                           (text.replace('{min: "4.0"}', '{min: 4.0}'), 'quoted version string'),
                           (text.replace('  vendor:   {matches', '  bogus: {max: 1}\n  vendor:   {matches'), 'no code'),
                           (text.replace('{matches: "^Credo"}', '{like: Credo}'), 'rule kind')):
            open(bad, 'w').write(body)
            try:
                load(bad)
                raise AssertionError(word)
            except SystemExit as e:
                assert word in str(e), (word, e)
    print(f'criteria selftest: OK ({len(CODES)} codes, sha256 {crit["sha256"][:12]})')


if __name__ == '__main__':
    import os
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'criteria', 'credo-cac41x321d1d.yaml')
    if sys.argv[1:] == ['--selftest']:
        selftest(os.path.normpath(default))
    else:
        print(__doc__)
