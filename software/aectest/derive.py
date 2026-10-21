#!/usr/bin/env python3
"""derive.py -- the cable database (criteria/*.yaml) as a lookup, and a criteria file derived from a cable.

The UI reads the cable type from the cable and maps it on the database of cables. A new cable is derived from a read
in engineering mode or with this script; the file is then reviewed and committed to the database.

  list_db(root)                 every criteria file: sku, vendor and part rules, the `owner` key, PROPOSED flag, sha256
  match(identity, root)         vendor + part number of a read identity against the database -> sku or candidates
  figures(port, serial, ...)    the identity, applications, advertisements, thresholds, 01h:167 bound (and the measured
                                ModuleReady time when the two high-power flags are given) from a module in a unit
  figures_from_record(path)     the same from a saved dump or passport JSON (scripts seed the database from records)
  render(fig, owner, note)      the YAML text: every derived limit marked PROPOSED with its source register
  write(root, fig, ...)         criteria/<vendor>-<pn>.yaml, or <name>.proposed.yaml when the file exists
  python -m aectest.derive --from dump.json [--from ...] [--out DIR]   the script path
The CLI (`aectest bringup --serial S derive-criteria`), the Advanced tab and the Test-a-cable fallback all call this module.
"""
import argparse
import glob
import hashlib
import json
import math
import os
import re
import sys
import time

import yaml

from .cmis import ST_LOWPWR, decode_durations, decode_thresholds
from .safety import BUDGET_W
from .transport import Refused

_HERE = os.path.dirname(os.path.abspath(__file__))
CRITERIA_DIR = os.environ.get('AECTEST_CRITERIA') or next((d for d in (os.path.normpath(os.path.join(_HERE, '..', '..', 'criteria')), os.path.join(_HERE, 'criteria'))
                                                            if os.path.isdir(d)), os.path.join(_HERE, 'criteria'))  # the repo folder, else the package data
SCHEMA = 'aectest.criteria/1'
PROPOSED = "# PROPOSED: from the cable's own advertisement ({}), to be confirmed by the database maintainer"


def _rule_text(rule):
    if isinstance(rule, dict):
        return str(next(iter(rule.values()), ''))
    return '' if rule is None else str(rule)


def _rule_hit(rule, value):
    if not isinstance(rule, dict) or value is None:
        return False
    kind, want = next(iter(rule.items()))
    if kind == 'matches':
        return re.search(str(want), str(value)) is not None
    if kind == 'equals':
        return str(want) == str(value)
    return False


def list_db(root=CRITERIA_DIR):
    """The database: one row per criteria file. A `.proposed.yaml` or a calibration candidate is listed, flagged, not matched."""
    out = []
    for path in sorted(glob.glob(os.path.join(root, '*.yaml'))):
        raw = open(path, 'rb').read()
        try:
            d = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        ident = d.get('identity') or {}
        out.append({'sku': d.get('sku') or os.path.basename(path)[:-5], 'file': path, 'name': os.path.basename(path),
                    'firmware': str((d.get('match') or {}).get('firmware') or '') or None,
                    'vendor': _rule_text(ident.get('vendor')).lstrip('^').replace('\\', ''), 'part_number': _rule_text(ident.get('part_number')).lstrip('^').replace('\\', ''),
                    'owner': d.get('owner'), 'proposed': b'PROPOSED' in raw, 'schema_ok': d.get('schema') == SCHEMA,
                    'candidate': path.endswith('.proposed.yaml') or 'calibration' in d, 'sha256': hashlib.sha256(raw).hexdigest(),
                    'identity': ident})
    return out


def match(identity, root=CRITERIA_DIR):
    """The criteria file whose identity rules accept this vendor and part number. One hit = matched; a vendor-only or
    part-only hit is a candidate the operator sees."""
    v, pn, fw = (identity or {}).get('vendor'), (identity or {}).get('part_number'), (identity or {}).get('firmware')
    hits, cands = [], []
    for e in list_db(root):
        if e['candidate'] or not e['schema_ok']:
            continue
        if e['firmware'] and e['firmware'] != fw:
            continue
        vendor_ok, pn_ok = _rule_hit(e['identity'].get('vendor'), v), _rule_hit(e['identity'].get('part_number'), pn)
        (hits if vendor_ok and pn_ok else cands if vendor_ok or pn_ok else []).append(e)
    hits.sort(key=lambda e: 0 if e['firmware'] else 1)  # the per-firmware entry wins over the generic one
    best = hits[0] if hits else None
    return {'matched': best is not None, 'sku': best['sku'] if best else None, 'file': best['file'] if best else None,
            'proposed': best['proposed'] if best else None, 'identity': identity, 'firmware': best['firmware'] if best else None,
            'candidates': [h['sku'] for h in hits[1:]] + [c['sku'] for c in cands]}


def _fig_from_module(m):
    return {'vendor': m.get('vendor'), 'part_number': m.get('part_number'), 'serial': m.get('serial'), 'revision': m.get('revision'),
            'cmis_revision': m.get('cmis_revision'), 'memory_model': m.get('memory_model'), 'identifier': m.get('identifier'),
            'power_class': m.get('power_class'), 'max_power_w': m.get('max_power_w'), 'media': m.get('media_interface_technology_00h_212'),
            'firmware': m.get('firmware')}


def figures(port, serial, say=None, timing=False, budget_w=BUDGET_W):
    """What `render` needs, read from the module in `port`. `timing` releases LPMode (the caller holds the two flags) and
    records the measured ModuleReady time, then returns the module to low power."""
    from . import passport
    from .tests import release_lpmode, wait_state
    say = say or (lambda t, err=False: None)
    pp = passport.read(port, serial, budget_w, say)
    if not pp.get('module'):
        raise Refused('no module present: nothing to derive')
    fig = {**_fig_from_module(pp['module']), 'unit': serial, 'read_at': pp['read_at'], 'applications': pp.get('applications') or [],
           'diagnostics': pp.get('diagnostics'), 'thresholds': pp.get('thresholds'), 'advertised': pp.get('advertised') or {}, 'pwrup_max_s': None}
    if fig['memory_model'] != 'flat':
        with port.selected():
            fig['pwrup_max_s'] = decode_durations(port.read_page(0x01))['module_pwrup']['max_s']
    if timing:
        info = release_lpmode(port, budget_w, 20, say=say)
        fig['module_ready_observed_s'] = info['module_ready_observed_s']
        port.set_lpmode(1)
        wait_state(port, ST_LOWPWR, 20, say)
    return fig


def figures_from_record(path):
    """The same figures from a saved dump JSON (decoded + pages) or a passport JSON."""
    from . import passport
    d = json.load(open(path, encoding='utf-8'))
    if d.get('unit') and 'module' in d:  # a passport
        if not d['module']:
            raise Refused(f'{path}: the passport has no module')
        fig = {**_fig_from_module(d['module']), 'unit': d['unit'], 'applications': d.get('applications') or [], 'diagnostics': d.get('diagnostics'),
               'thresholds': d.get('thresholds'), 'advertised': d.get('advertised') or {}, 'pwrup_max_s': None}
    elif 'decoded' in d and 'lower' in d:  # a dump
        pages = d.get('pages') or {}
        lower = bytes.fromhex(d['lower'])
        fig = {**_fig_from_module(d['decoded']), 'unit': d.get('ftdi_serial'), 'applications': passport._apps(lower), 'diagnostics': None,
               'thresholds': None, 'advertised': {}, 'pwrup_max_s': None}
        fig['firmware'] = fig.get('firmware') or f'{lower[39]}.{lower[40]}'
        if '01h' in pages:
            p1 = bytes.fromhex(pages['01h'])
            adv = p1[142 - 128]
            fig['advertised'] = {'diagnostic_pages': bool(adv & 0x20), 'vdm': bool(adv & 0x40), 'cdb_instances': p1[163 - 128] >> 6}
            fig['pwrup_max_s'] = decode_durations(p1)['module_pwrup']['max_s']
        if '02h' in pages:
            fig['thresholds'] = decode_thresholds(bytes.fromhex(pages['02h']))
        if '13h' in pages:
            fig['diagnostics'] = passport._caps(bytes.fromhex(pages['13h']))
    else:
        raise Refused(f'{path}: not a dump or a passport')
    fig['source'] = path
    return fig


def sku_name(fig, per_firmware=False):
    base = re.sub(r'[^a-z0-9]+', '-', f"{fig['vendor']}-{fig['part_number']}".lower()).strip('-')
    return f"{base}-fw{str(fig.get('firmware')).replace('.', '-')}" if per_firmware and fig.get('firmware') else base


ACTIVE_MEDIA = ('0x0c', '0x0d', '0x0e', '0x0f')  # Table 8-36: copper with active equalizers (an AEC); 0x0a / 0x0b are passive
TAU_S, C_PER_W = 310.0, 5.0
SLOPE_MARGIN = 1.25


def slope_limit(max_power_w, from_s=60.0):
    """C/min a module may still rise after `from_s` of the soak, from the first-order fit of record: the initial slope is
    P * C_PER_W / TAU_S, decayed by exp(-from_s / TAU_S), with SLOPE_MARGIN on top. 4.5 W -> 4.5 C/min."""
    return round(float(max_power_w) * C_PER_W / (TAU_S / 60.0) * math.exp(-from_s / TAU_S) * SLOPE_MARGIN, 1)


def _rx(text):
    """An anchored literal for the identity rule: regex specials escaped, the hyphen left readable."""
    return re.escape(str(text)).replace('\\-', '-')


def render(fig, owner='<name>', note='', per_firmware=False, host_present=False):
    """The criteria YAML. Every derived limit carries PROPOSED and its source register. The text loads with criteria.load.
    `per_firmware` writes the `match.firmware` key; `host_present` keeps DPActivated as the expectation on an AEC."""
    adv, diag = fig.get('advertised') or {}, fig.get('diagnostics') or {}
    active = str(fig.get('media') or '').startswith(ACTIVE_MEDIA)
    dp_rule = ('  dp_states: {all_lanes_equal: [DPInit, DPActivated]}   # 11h:128-131; PROPOSED: an active cable with no host signal waits in DPInit, one state on every lane'
               if active and not host_present else '  dp_states: {all_lanes_equal: DPActivated}     # 11h:128-131')
    slope = slope_limit(fig['max_power_w'])
    pats = diag.get('generator_media') or []
    pattern = 'prbs31q' if not pats or 'prbs31q' in pats else pats[0]
    widest = max((a for a in fig.get('applications') or [] if a.get('media_lanes')), key=lambda a: int(a.get('media_lanes') or 0), default=None)
    media_lanes = int(widest['media_lanes']) if widest else 8
    media_prbs = bool(pats) and bool(diag.get('checker_media'))  # a pair test needs a generator at one end and a checker at the other
    ready = fig.get('pwrup_max_s')
    ready_max = max(float(ready), 1.0) if ready else 15
    over = float(fig['max_power_w']) > BUDGET_W
    measured = fig.get('module_ready_observed_s')
    head = [f"# Pass / fail criteria for one SKU, derived from the cable's own EEPROM by aectest derive on {time.strftime('%Y-%m-%d')}.",
            f"# Source: unit {fig.get('unit') or '-'}, module {fig['vendor']} {fig['part_number']} serial {fig.get('serial') or '-'}"
            + (f", record {os.path.basename(fig['source'])}" if fig.get('source') else '') + '.',
            "# Every limit marked PROPOSED comes from an advertisement or an MSA figure, not from golden-cable statistics. The database maintainer confirms each one.",
            "# Written by aectest derive. Commit it after review: the tester reads criteria/<sku>.yaml by vendor and part number."]
    if note:
        head.append(f'# {note}')
    body = [
        f'schema: {SCHEMA}', f'sku: {sku_name(fig, per_firmware)}', f'owner: {json.dumps(owner)}                  # who may change this file',
        *([f"match: {{firmware: {json.dumps(str(fig['firmware']))}}}   # 00h:39-40; this entry matches this firmware only"]
          if per_firmware and fig.get('firmware') else []),
        'soak_s: 120                                     # PROPOSED: the slope window needs 60 s of step response first',
        'identity:',
        f"  vendor:   {{matches: {json.dumps('^' + _rx(fig['vendor']))}}}   # 00h:129-144",
        f"  part_number: {{matches: {json.dumps('^' + _rx(fig['part_number']))}}}   # 00h:148-163",
        f"  cmis_revision: {{min: {json.dumps(str(fig['cmis_revision']))}}}   # 00h:1 as read",
        f"  identifier: {{equals: {fig['identifier']}}}   # 00h:0",
        f"  power_class: {{max: {fig['power_class']}}}   {PROPOSED.format('00h:200')}"
        + ('  # class 4 or above sits above the MINI rail: the tester refuses the release (S4)' if int(fig['power_class']) >= 4 else ''),
        f"  max_power_w: {{max: {float(fig['max_power_w']):g}}}   {PROPOSED.format('00h:201')}"
        + (f'  # above the {BUDGET_W:g} W budget: INCONCLUSIVE OVER-JIG-BUDGET on the MINI' if over else ''),
        '  ends_same_pn: true', '  ends_same_sn: unknown                         # record only', '  label_sn_matches_cmis: true',
        'timing:', '  mgmt_init_ms: {max: 2000}                     # MSA Table 9 MgmtInitDuration',
        f"  module_ready_s: {{max: {ready_max:g}}}   "
        + (PROPOSED.format('01h:167 ModulePwrUp code') if ready else '# PROPOSED: the tool timeout; a flat module advertises no 01h:167')
        + (f'  # measured {measured:.2f} s on derive' if measured is not None else ''),
        'low_power:', '  module_state: {equals: ModuleLowPwr}', '  flags_byte9: {flag_clear: 0xFF}',
        '  temp_c: {range: [5, 60]}                      # PROPOSED sanity window, hood off, room ambient',
        '  vcc_v:  {range: [3.135, 3.465]}               # MSA Table 13',
        'high_power:', '  module_state: {equals: ModuleReady}', dp_rule,
        ('  temp_c_max: {from_module: TempMonHighWarningThreshold, margin: -5}   ' + PROPOSED.format('02h:132-133')) if fig.get('thresholds')
        else "  temp_c_max: {max: 70}                          # PROPOSED: no thresholds page on this module; the tool's 70 C cap",
        f'  temp_slope_c_per_min: {{max: {slope:g}}}              # PROPOSED: thermal_fit.py tau {TAU_S:g} s, {C_PER_W:g} C/W, after 60 s, margin {SLOPE_MARGIN:g}',
        '  vcc_v: {range: [3.135, 3.465]}',
        '  failure_flag_tx_11h_135: {flag_clear: 0xFF}',
        'prbs:',
        f'  pattern: {pattern}   ' + (PROPOSED.format('13h:134-135 media generator patterns') if pats
                                       else '# PROPOSED: no media-side generator on 13h:134-135; a pattern needs a host' if diag
                                       else '# PROPOSED: page 13h not read; CMIS Table 8-98 ID 0'),
        '  seconds_per_direction: 60                     # the PRBS minimum gate',
        f"  lane_mask: 0x{(1 << int(media_lanes)) - 1:02X}   {PROPOSED.format(f'application {widest.get(chr(110)) if widest else 1} media lanes, the widest')}",
        '  placement_preference: raw', '  limits:',
        '    raw:      {ber_max_per_lane: 2.4e-4}        # IEEE 802.3 KP4 RS(544,514) FEC input ceiling',
        '    post_fec: {errors_max_per_lane: 0}', '    tightened: null',
        f"  require_measured: {'true' if media_prbs else 'false'}"
        + ('' if media_prbs else '   # no media-side generator and checker pair on 13h: a pair PRBS cannot be MEASURED here, never a FAIL'
           if adv.get('diagnostic_pages') else '   # no diagnostic pages advertised (01h:142.5 = 0): no PRBS over CMIS, never a FAIL'),
        'vdm:', f"  judge: false   # {'VDM advertised: record only until its observables are known' if adv.get('vdm') else 'no VDM pages advertised'}"]
    return '\n'.join(head + body) + '\n'


def target(root, fig, per_firmware=False):
    """criteria/<vendor>-<pn>[-fwX-Y].yaml, or <name>.proposed.yaml beside an existing file (the database maintainer compares and decides)."""
    base = os.path.join(root, sku_name(fig, per_firmware) + '.yaml')
    return (base, False) if not os.path.exists(base) else (base[:-5] + '.proposed.yaml', True)


def write(root, fig, owner='<name>', note='', out=None, per_firmware=False, host_present=False):
    """Write the derived file; returns (path, text, existed). `out` forces a path."""
    from . import criteria
    text = render(fig, owner, note, per_firmware, host_present)
    path, existed = (out, os.path.exists(out)) if out else target(root, fig, per_firmware)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    criteria.load(path)  # a derived file that does not load is a bug here, not a surprise at test time
    return path, text, existed


def cmd_derive(port, a, say=None):
    """`aectest bringup --serial S derive-criteria [--out FILE] [--release-lpmode --i-am-on-usbc-power]`."""
    say = say or print
    timing = bool(getattr(a, 'release_lpmode', False) and getattr(a, 'i_am_on_usbc_power', False))
    fig = figures(port, a.serial or getattr(port.be, 'serial', '?'), say, timing, float(getattr(a, 'budget_w', None) or BUDGET_W))
    path, text, existed = write(getattr(a, 'criteria', None) or CRITERIA_DIR, fig, out=a.out,
                                per_firmware=bool(getattr(a, 'per_firmware', False)), host_present=bool(getattr(a, 'host_present', False)))
    say(text)
    say(f"wrote {path}{' (the sku file exists: this is the proposal beside it)' if existed else ''}. Commit it after review.")
    return {'path': path, 'existed': existed, 'sku': sku_name(fig), 'figures': fig}


def main(argv=None):
    ap = argparse.ArgumentParser(description='derive a criteria file from saved dump or passport records')
    ap.add_argument('--from', dest='src', action='append', required=True, help='a dump or passport JSON (repeat)')
    ap.add_argument('--out', default=CRITERIA_DIR, help='the criteria directory')
    ap.add_argument('--owner', default='<name>')
    ap.add_argument('--note', default='', help='one comment line added to the file head')
    ap.add_argument('--per-firmware', action='store_true', help='name the file <sku>-fwX-Y and write match.firmware')
    ap.add_argument('--host-present', action='store_true', help='a host drives the lanes: expect DPActivated on an active cable')
    a = ap.parse_args(argv)
    for src in a.src:
        path, _, existed = write(a.out, figures_from_record(src), a.owner, a.note, per_firmware=a.per_firmware, host_present=a.host_present)
        print(f"{path}{' (proposal beside the existing file)' if existed else ''}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
