"""Feature matrix: every capability a cable can advertise, exercised when present. One row per capability the module can advertise; four states: `not advertised`,
`advertised` (not exercised yet), `OK` / `readable` (exercised, answers as the spec says), `hollow` (advertised, answers with
a placeholder: EXP5 / EXP6 of 2026-10-20), plus `FAIL`, `inconclusive`, `needs host` (a host-side generator has no host on
this tester) and `needs pair` (the exercise runs on two units joined by the cable).

`aectest bringup features --serial X [--exercise all|<name>,...] [--pair A:B ...]`: the matrix from the decode (read-only),
then the exercises asked for. Low-power exercises are write / read-back / restore on one byte or a read. High-power ones
(prbs, loopback, pattern) need the pair, the two permission flags and the budget; the pair goes back to low power after
each run. Record: features_<serial>_<UTC>.json (write_record), the pair runs beside it.
"""
import json
import os
from datetime import datetime, timezone

from .cdb import cdb_command
from .cmis import PATTERNS
from .cmis.decode import read_pages
from .records import record_header, write_record
from .tests import cmd_prbs, cmd_trend

PATTERN_NAMES = {v: k for k, v in PATTERNS.items()}
# (key, group, label, gate: an adv key or a callable(rec) -> bool, exercise, power)
ROWS = [
    ('page03', 'pages', 'User EEPROM page 03h', 'page03', 'page03_write', 'low'),
    ('vdm', 'pages', 'VDM pages 20h-2Fh', 'vdm', 'vdm_read', 'low'),
    ('diag', 'pages', 'Diagnostic pages 13h/14h', 'diag', None, None),
    ('cdb', 'pages', 'CDB (9Fh)', 'cdb', 'cdb_query', 'low'),
    ('banks', 'pages', 'Banked pages (more than 8 lanes)', lambda r: r['advertised'].get('banks', 1) > 1, None, None),
    ('tunable', 'pages', 'Tunable laser pages 04h/12h', 'tunable', None, None),
    ('mon.temp', 'monitors', 'Module temperature monitor', 'mon.temp', 'module_monitors', 'low'),
    ('mon.vcc', 'monitors', 'Module Vcc monitor', 'mon.vcc', 'module_monitors', 'low'),
    ('mon.aux1', 'monitors', 'Aux1 monitor', 'mon.aux1', None, None),
    ('mon.aux2', 'monitors', 'Aux2 monitor', 'mon.aux2', None, None),
    ('mon.aux3', 'monitors', 'Aux3 monitor', 'mon.aux3', None, None),
    ('mon.custom', 'monitors', 'Custom monitor', 'mon.custom', None, None),
    ('mon.tx_bias', 'monitors', 'Tx bias per lane', 'mon.tx_bias', 'lane_monitors', 'low'),
    ('mon.tx_power', 'monitors', 'Tx optical power per lane', 'mon.tx_power', 'lane_monitors', 'low'),
    ('mon.rx_power', 'monitors', 'Rx optical power per lane', 'mon.rx_power', 'lane_monitors', 'low'),
    ('ctl.polarity_tx', 'controls', 'Tx input polarity flip (10h:129)', 'ctl.polarity_tx', 'lane_ctrl', 'low'),
    ('ctl.disable_tx', 'controls', 'Tx output disable (10h:130)', 'ctl.disable_tx', 'lane_ctrl', 'low'),
    ('ctl.autosquelch_tx', 'controls', 'Tx auto squelch disable (10h:131)', 'ctl.autosquelch_tx', 'lane_ctrl', 'low'),
    ('ctl.force_squelch_tx', 'controls', 'Tx squelch force (10h:132)', 'ctl.force_squelch_tx', 'lane_ctrl', 'low'),
    ('si.eq_freeze', 'controls', 'Adaptive input EQ freeze (10h:134)', 'si.eq_freeze', 'lane_ctrl', 'low'),
    ('ctl.polarity_rx', 'controls', 'Rx output polarity flip (10h:137)', 'ctl.polarity_rx', 'lane_ctrl', 'low'),
    ('ctl.disable_rx', 'controls', 'Rx output disable (10h:138)', 'ctl.disable_rx', 'lane_ctrl', 'low'),
    ('ctl.autosquelch_rx', 'controls', 'Rx auto squelch disable (10h:139)', 'ctl.autosquelch_rx', 'lane_ctrl', 'low'),
    ('si.cdr_tx', 'signal integrity', 'Tx CDR control', 'si.cdr_tx', 'si_readback', 'low'),
    ('si.cdr_rx', 'signal integrity', 'Rx CDR control', 'si.cdr_rx', 'si_readback', 'low'),
    ('si.fixed_eq', 'signal integrity', 'Tx fixed input EQ', 'si.fixed_eq', 'si_readback', 'low'),
    ('si.adaptive_eq', 'signal integrity', 'Tx adaptive input EQ', 'si.adaptive_eq', 'si_readback', 'low'),
    ('si.eq_recall', 'signal integrity', 'Tx EQ recall', 'si.eq_recall', 'si_readback', 'low'),
    ('si.rx_amp', 'signal integrity', 'Rx output amplitude', 'si.rx_amp', 'si_readback', 'low'),
    ('si.rx_eq', 'signal integrity', 'Rx output EQ', 'si.rx_eq', 'si_readback', 'low'),
    ('si.scs1', 'signal integrity', 'Staged control set 1', 'si.scs1', 'si_readback', 'low'),
    ('si.unidir', 'signal integrity', 'Unidirectional data paths', 'si.unidir', None, None),
    ('flag.failure_tx', 'flags', 'Tx failure flag', 'flag.failure_tx', 'flags', 'low'),
    ('flag.los_tx', 'flags', 'Tx LOS flag', 'flag.los_tx', 'flags', 'low'),
    ('flag.lol_tx', 'flags', 'Tx CDR LOL flag', 'flag.lol_tx', 'flags', 'low'),
    ('flag.eq_fail_tx', 'flags', 'Tx adaptive EQ fail flag', 'flag.eq_fail_tx', 'flags', 'low'),
    ('flag.los_rx', 'flags', 'Rx LOS flag', 'flag.los_rx', 'flags', 'low'),
    ('flag.lol_rx', 'flags', 'Rx CDR LOL flag', 'flag.lol_rx', 'flags', 'low'),
    ('diag.gen_media', 'diagnostics', 'Media-side pattern generator', 'diag.gen_media', 'prbs', 'high'),
    ('diag.chk_media', 'diagnostics', 'Media-side pattern checker', 'diag.chk_media', 'prbs', 'high'),
    ('diag.gen_host', 'diagnostics', 'Host-side pattern generator', 'diag.gen_host', 'host', None),
    ('diag.chk_host', 'diagnostics', 'Host-side pattern checker', 'diag.chk_host', 'host', None),
    ('diag.patterns', 'diagnostics', 'Every advertised media pattern', lambda r: bool(_media_patterns(r)), 'pattern', 'high'),
    ('loop.media_in', 'diagnostics', 'Media-side input loopback (13h:181)', 'loop.media_in', 'loopback', 'high'),
    ('loop.media_out', 'diagnostics', 'Media-side output loopback (13h:180)', 'loop.media_out', 'loop_ctrl', 'low'),
    ('loop.host_out', 'diagnostics', 'Host-side output loopback (13h:182)', 'loop.host_out', 'loop_ctrl', 'low'),
    ('loop.host_in', 'diagnostics', 'Host-side input loopback (13h:183)', 'loop.host_in', 'loop_ctrl', 'low'),
    ('diag.gating', 'diagnostics', 'Gated measurements (13h:129)', lambda r: bool(_p13(r) and _p13(r)[1] & 0xC0), None, None),
    ('diag.snr', 'diagnostics', 'SNR readout (13h:130)', lambda r: bool(_p13(r) and _p13(r)[2] & 0x30), None, None),
    ('cdb.firmware', 'cdb', 'Firmware download (0041h)', lambda r: _cdb_has(r, 0x0041), 'fw_features', 'low'),
    ('cdb.pm', 'cdb', 'Performance monitoring (0042h)', lambda r: _cdb_has(r, 0x0042), 'pm_features', 'low'),
    ('cdb.bert', 'cdb', 'BERT and diagnostics over CDB (0043h)', lambda r: _cdb_has(r, 0x0043), 'bert_features', 'low'),
]
LANE_CTRL_BYTE = {'ctl.polarity_tx': 129, 'ctl.disable_tx': 130, 'ctl.autosquelch_tx': 131, 'ctl.force_squelch_tx': 132,
                  'si.eq_freeze': 134, 'ctl.polarity_rx': 137, 'ctl.disable_rx': 138, 'ctl.autosquelch_rx': 139}
LOOP_CTRL_BYTE = {'loop.media_out': 180, 'loop.host_out': 182, 'loop.host_in': 183}
HIGH = ('prbs', 'pattern', 'loopback')


def _p13(rec):
    h = (rec.get('raw_dump') or {}).get('pages', {}).get('13h')
    return bytes.fromhex(h) if h else None


def _media_patterns(rec):
    p = _p13(rec)
    if not p or not rec['advertised'].get('diag'):
        return []
    gen = p[132 - 128] | (p[133 - 128] << 8)
    chk = p[138 - 128] | (p[139 - 128] << 8)
    return [PATTERN_NAMES[i] for i in range(13) if gen >> i & 1 and chk >> i & 1]


def _cdb_has(rec, cid):
    """0040h lists the supported commands; a module that answers 0040h 'unknown' (the Accelink, EXP2) still runs 0041h, so with no
    list the row stays advertised and the exercise decides."""
    q = (rec.get('cdb_query') or {}).get('features') or {}
    ids = q.get('supported_cmds')
    if ids is None:
        return bool(rec['advertised'].get('cdb'))
    return f'0x{cid:04x}' in ids


def build_matrix(rec):
    """The rows from a decode record (cmis.decode.read_pages / decode): state `not advertised` or `advertised`."""
    adv = rec['advertised']
    rows = []
    for key, group, label, gate, ex, power in ROWS:
        on = bool(gate(rec)) if callable(gate) else bool(adv.get(gate))
        rows.append({'key': key, 'group': group, 'feature': label, 'advertised': on, 'exercise': ex if on else None,
                     'power': power if on else None, 'state': 'advertised' if on else 'not advertised', 'note': ''})
    return rows


# --------------------------------------------------------------------------- low-power exercises: (port, rec, say) -> (state, note)
def _rw1(port, page, off, value):
    """Write one byte on `page`, read it back, return the read-back (the caller restores)."""
    with port.selected():
        port.page(page)
        port.write(off, bytes([value]))
        return port.read(off, 1)[0]


def _r1(port, page, off):
    with port.selected():
        port.page(page)
        return port.read(off, 1)[0]


def ex_page03_write(port, rec, say):
    orig = _r1(port, 0x03, 128)
    back = _rw1(port, 0x03, 128, orig ^ 0xFF)
    _rw1(port, 0x03, 128, orig)
    return ('OK', f'03h:128 {orig:02x} -> wrote {orig ^ 0xFF:02x}, read {back:02x}, restored') if back == orig ^ 0xFF else \
        ('hollow', f'03h:128 ignores writes: wrote {orig ^ 0xFF:02x}, read {back:02x} (EXP5)')


def ex_vdm_read(port, rec, say):
    v = rec.get('vdm') or {}
    if v.get('descriptor_page_all_zero'):
        return 'hollow', 'VDM descriptor page 20h all zero: no instance defined (EXP6)'
    n = len(v.get('instances') or [])
    return ('OK', f'{n} VDM instance(s) in {", ".join(v.get("groups_read") or [])}') if n else ('hollow', 'no VDM instance decoded')


def ex_cdb_query(port, rec, say):
    r = cdb_command(port, 0x0000, lpl=b'\x00\x00', say=say)
    return ('OK', f"0000h Query Status: {r['status']}") if r.get('ok') else ('FAIL', f"0000h: {r['status']}")


def ex_module_monitors(port, rec, say):
    with port.selected():
        lo = port.lower()
    temp = int.from_bytes(lo[14:16], 'big', signed=True) / 256
    vcc = int.from_bytes(lo[16:18], 'big') / 10000
    if temp == 0 and vcc == 0:
        return 'hollow', 'temperature and Vcc read 0'
    return 'OK', f'temperature {temp:.2f} C, Vcc {vcc:.3f} V'


def ex_lane_monitors(port, rec, say, key=None):
    with port.selected():
        p11 = port.read_page(0x11)
    base = {'mon.tx_bias': 170, 'mon.tx_power': 154, 'mon.rx_power': 186}[key]
    vals = [int.from_bytes(p11[base - 128 + 2 * i:base - 126 + 2 * i], 'big') for i in range(8)]
    if not any(vals):
        return 'hollow', 'every lane reads 0'
    if len(set(vals)) == 1:
        return 'hollow', f'every lane reads the same value {vals[0]} (EXP5: a placeholder)'
    return 'OK', f'lanes {vals}'


def ex_lane_ctrl(port, rec, say, key=None):
    off = LANE_CTRL_BYTE[key]
    orig = _r1(port, 0x10, off)
    back = _rw1(port, 0x10, off, orig ^ 0x01)  # lane 1 toggled
    _rw1(port, 0x10, off, orig)
    return ('OK', f'10h:{off} {orig:02x} -> {orig ^ 1:02x} read back, restored') if back == orig ^ 0x01 else \
        ('FAIL', f'10h:{off} wrote {orig ^ 1:02x}, read {back:02x}')


def ex_loop_ctrl(port, rec, say, key=None):
    off = LOOP_CTRL_BYTE[key]
    orig = _r1(port, 0x13, off)
    back = _rw1(port, 0x13, off, orig ^ 0x01)
    _rw1(port, 0x13, off, orig)
    return ('OK', f'13h:{off} {orig:02x} -> {orig ^ 1:02x} read back, restored (no traffic measured)') if back == orig ^ 0x01 else \
        ('FAIL', f'13h:{off} wrote {orig ^ 1:02x}, read {back:02x}')


def ex_si_readback(port, rec, say, key=None):
    blk = (rec.get('pages') or {}).get('10h') or {}
    rows = [f for f in blk.get('fields') or [] if f.get('adv') == key or key.split('.')[1] in str(f.get('name', '')).lower()]
    return 'readable', '; '.join(f"{f.get('name')}: {f.get('value')}" for f in rows[:4]) or 'staged control bytes read'


def ex_flags(port, rec, say, key=None):
    with port.selected():
        p11 = port.read_page(0x11)
    off = {'flag.failure_tx': 135, 'flag.los_tx': 136, 'flag.lol_tx': 137, 'flag.eq_fail_tx': 138, 'flag.los_rx': 147, 'flag.lol_rx': 148}[key]
    return 'readable', f'11h:{off} = 0x{p11[off - 128]:02x} (latched, clear on read)'


def _cdb_state(r):
    """A module that answers 'CMDID unknown' does not have the feature: that is 'not advertised', not a failure."""
    if r.get('ok'):
        return 'OK'
    return 'not advertised' if 'unknown' in str(r.get('status', '')).lower() else 'FAIL'


def ex_fw_features(port, rec, say):
    r = cdb_command(port, 0x0041, say=say)
    d = r.get('decoded') or {}
    st = _cdb_state(r)
    return st, (f"download {d.get('write_mechanism')}, header {d.get('start_cmd_payload_size')} bytes" if st == 'OK' else f"0041h: {r['status']}")


def ex_pm_features(port, rec, say):
    r = cdb_command(port, 0x0042, say=say)
    return _cdb_state(r), f"0042h: {r['status']}"


def ex_bert_features(port, rec, say):
    r = cdb_command(port, 0x0043, say=say)
    return _cdb_state(r), f"0043h: {r['status']}"


LOW = {'page03_write': ex_page03_write, 'vdm_read': ex_vdm_read, 'cdb_query': ex_cdb_query, 'module_monitors': ex_module_monitors,
       'lane_monitors': ex_lane_monitors, 'lane_ctrl': ex_lane_ctrl, 'loop_ctrl': ex_loop_ctrl, 'si_readback': ex_si_readback,
       'flags': ex_flags, 'fw_features': ex_fw_features, 'pm_features': ex_pm_features, 'bert_features': ex_bert_features}
KEYED = {'lane_monitors', 'lane_ctrl', 'loop_ctrl', 'si_readback', 'flags'}


# --------------------------------------------------------------------------- high-power exercises on the pair
def _verdict_state(v):
    v = str(v or '')
    return 'OK' if v.startswith(('MEASURED', 'PASS')) else 'inconclusive' if v.startswith('INCONCLUSIVE') else 'FAIL'


def _stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def run_pair(kind, pa, pb, names, folder, budget_w, timeout, apsel, seconds, say, pattern='prbs31q'):
    """One pair run saved in `folder`; returns (state, note, file)."""
    a, b = names
    if kind == 'loopback':
        f = os.path.join(folder, f'T11_loop_{a}_{b}_{_stamp()}.json')
        rec = cmd_trend(pa, pb, pattern, seconds, budget_w, timeout, False, f, names, None, say=say, apsel=apsel, loop=True)
    else:
        f = os.path.join(folder, f'T11_ber_{a}_{b}_{_stamp()}.json')
        rec = cmd_prbs(pa, pb, pattern, seconds, budget_w, timeout, False, f, names, None, say=say, apsel=apsel)
    v = rec.get('verdict')
    dirs = rec.get('directions') or {}
    if dirs:  # a two-direction record: its own verdict reads 'see directions' when they differ (cmd_prbs)
        states = [_verdict_state(r.get('verdict')) for r in dirs.values()]
        st = 'OK' if all(s == 'OK' for s in states) else 'inconclusive' if 'inconclusive' in states else 'FAIL'
        return st, f'{pattern}: ' + '; '.join(f"{k} {str(r.get('verdict'))[:60]}" for k, r in dirs.items()), f
    return _verdict_state(v), f'{pattern}: {v}', f


def exercise(port, rec, rows, which, say, serial, folder, pair=None, names=None, budget_w=8.0, timeout=20.0, apsel=None, seconds=20.0):
    """Run the exercises named in `which` ('all' or a set of exercise names / row keys) on the advertised rows."""
    done = {}
    for row in rows:
        ex = row['exercise']
        if not row['advertised'] or not ex:
            continue
        if which != 'all' and ex not in which and row['key'] not in which:
            continue
        try:
            if ex == 'host':
                row['state'], row['note'] = 'needs host', 'a host-side generator or checker needs a host on the cage; none on this tester'
            elif ex in HIGH:
                if pair is None:
                    row['state'], row['note'] = 'needs pair', f'{ex}: run with --pair A:B, both permission flags and the budget'
                    continue
                if ex == 'pattern':
                    pats = _media_patterns(rec)
                    res = []
                    for p in pats:
                        if p == 'prbs31q' and 'prbs' in done:  # the plain run covers it
                            res.append(f'{p}: {done["prbs"][1]}')
                            continue
                        st, note, f = run_pair('prbs', port, pair, names, folder, budget_w, timeout, apsel, max(seconds / 2, 10.0), say, pattern=p)
                        res.append(f'{p}: {st}')
                        row.setdefault('files', []).append(f)
                    row['state'] = 'OK' if all(r.endswith('OK') for r in res) else 'inconclusive' if any('inconclusive' in r for r in res) else 'FAIL'
                    row['note'] = '; '.join(res)
                else:
                    if ex in done:
                        row['state'], row['note'] = done[ex][0], done[ex][1]
                    else:
                        st, note, f = run_pair(ex, port, pair, names, folder, budget_w, timeout, apsel, seconds, say)
                        done[ex] = (st, note)
                        row['state'], row['note'], row['files'] = st, note, [f]
            else:
                fn = LOW[ex]
                row['state'], row['note'] = fn(port, rec, say, key=row['key']) if ex in KEYED else fn(port, rec, say)
        except Exception as e:  # noqa: BLE001 -- one failed exercise must not void the matrix
            row['state'], row['note'] = 'FAIL', f'{type(e).__name__}: {e}'
        say(f"  {row['state']:<14} {row['feature']:<42} {row['note']}")
    return rows


def cmd_features(port, serial, which, out, as_json, say=None, pair=None, pair_serial=None, folder=None, budget_w=8.0,
                 timeout=20.0, apsel=None, seconds=20.0):
    say = say or (lambda t, err=False: None)
    rec = read_pages(port, say, cdb=True)
    rows = build_matrix(rec)
    folder = folder or '.'
    names = (serial, pair_serial or 'B')
    which = 'all' if which in (None, 'all') else 'none' if which == 'none' else set(str(which).split(','))
    if which != 'none':
        if pair is not None:
            port.allow_lp_low = pair.allow_lp_low = True
        try:
            exercise(port, rec, rows, which, say, serial, folder, pair, names, budget_w, timeout, apsel, seconds)
        finally:
            if pair is not None:
                port.allow_lp_low = pair.allow_lp_low = False
    adv = sum(r['advertised'] for r in rows)
    summary = {s: sum(r['state'] == s for r in rows) for s in sorted({r['state'] for r in rows})}
    say(f'features: {adv} of {len(rows)} advertised; ' + ', '.join(f'{k} {v}' for k, v in summary.items()))
    out_rec = {**record_header([serial] + ([pair_serial] if pair_serial else [])), 'kind': 'features', 'unit': serial,
               'module': rec.get('identity'), 'revision': rec.get('revision'), 'rows': rows, 'summary': summary,
               'exercised': 'all' if which == 'all' else sorted(which) if which != 'none' else []}
    if as_json:
        say(json.dumps(out_rec, indent=1))
    write_record(out_rec, out, os.path.join(folder, f'features_{serial}_{{}}.json'), say)
    return out_rec


def table(rec):
    """The matrix as text rows for the CLI and the records."""
    L = [f"{'state':<14} {'group':<17} feature"]
    for r in rec['rows']:
        L.append(f"{r['state']:<14} {r['group']:<17} {r['feature']}" + (f'  -- {r["note"]}' if r['note'] else ''))
    return '\n'.join(L)
