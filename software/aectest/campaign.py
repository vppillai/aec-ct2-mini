"""campaign.py -- the extended test commands for cables with rich diagnostics (first run on an Accelink pair).

Three pair commands on top of the PRBS engine of tests.py, one single-unit probe:
  lane-integrity  Tx output disable per lane at the generator end: the checker end must lose lock on that lane only
                  (14h:139), its LOS / CDR LOL Rx flags (11h:147 / 148) must name it, and a restore must relock.
  si-sweep        the explicit control set of Page 10h (CDR enable, Rx output amplitude / pre- / post-cursor codes):
                  write, ApplyDPInit, ConfigStatus, the Active Control Set read-back (11h:214-234), a BER window per
                  setting, restore. On a media-side loop the host-side SI settings change no number: the sweep tells
                  which codes the module accepts and whether the apply path works (Table 8-84 codes).
  flag-matrix     every flag the tool can provoke, one at a time, with every other flag masked: IntL must go low while
                  the flag is pending and unmasked, the read must clear it, IntL must go high again (Tables 8-9/8-12,
                  8-77/8-79). Thresholds on Page 02h are written only when they read back (restored after).
  cdb-password    00h:122-125 written, then CDB 0000h Query Status: does the module report 'host password accepted'?
Every command writes one JSON record (write_record), leaves both ends in low power and restores every byte it wrote.
"""
import time

from . import cdb as cdbmod
from .cmis import (CTRL_RESET_ERR, DP_ACTIVATED, DP_STATES, P13_CTRL, P14_CHK_LOL_MEDIA, P2_THRESHOLDS, PATTERNS, ST_LOWPWR, ST_READY,
                   module_flag_names, read_id)
from .records import record_header, write_record
from .tests import (PrbsAbort, _quiet, dp_states, ensure_dp_active, fec_placement, media_lane_mask, poll_health, prbs_arm, prbs_caps,
                    prbs_disable, read_media_flags, read_media_stats, release_lpmode, select_application, stop_reason, wait_state)
from .transport import Nack, Refused

ID_KEYS = ('vendor', 'part_number', 'serial', 'power_class', 'max_power_w', 'cmis_revision', 'media_interface_technology_00h_212')
P10_TX_DISABLE, P10_RX_DISABLE, P10_DPDEINIT, P10_APPLY_DPINIT = 130, 138, 128, 143  # Tables 8-61, 8-63, 8-67
P10_SCS0 = {'cdr_tx': 160, 'cdr_rx': 161, 'rx_pre': 162, 'rx_post': 166, 'rx_amp': 170}  # Table 8-64 (SCS0 143-177)
P11_ACS = {'cdr_tx': 221, 'cdr_rx': 222, 'rx_pre': 223, 'rx_post': 227, 'rx_amp': 231}   # Table 8-88 (ACS 214-234)
P10_MASKS = {'dp_state': 213, 'failure_tx': 214, 'los_rx': 226, 'lol_rx': 227}  # Table 8-77 lane flag masks
P11_LANE_FLAGS = {'dp_state': 134, 'failure_tx': 135, 'los_rx': 147, 'lol_rx': 148}  # Table 8-79 (latched, clear on read)
L_FLAGS, L_MASKS = 8, 31  # Tables 8-9 / 8-12: masks 31-34 mirror flags 8-11 bit for bit
PASSWORD_ENTRY = 122  # Table 8-10


def _flag_bit(name):
    """(flag byte 8-11, bit) of a module flag by its Table 8-9 name, from the decoder's field table."""
    from .cmis.fields import LOWER
    for row in LOWER:
        if 8 <= row.byte <= 11 and row.kind == 'bits' and isinstance(row.arg, dict):
            for bit, n in row.arg.items():
                if n == name:
                    return row.byte, bit
    raise KeyError(name)


def _rd(port, page, off, n=1):
    with port.selected():
        if page:
            port.page(page)
        return port.read(off, n)


def _wr(port, page, off, data):
    with port.selected():
        if page:
            port.page(page)
        port.write(off, bytes(data))
        return port.read(off, len(data))


def pair_up(gen, chk, names, rec, pattern, budget_w, timeout, apsel, say):
    """Identity and 13h capabilities of both ends into rec, both released, the application selected, the Data Paths
    active; returns the lane mask (DPActivated on both ends and in the media lanes of the application)."""
    an, bn = names
    for p, n in ((gen, an), (chk, bn)):
        info = read_id(p)
        rec['ends'][n].update({k: info[k] for k in ID_KEYS})
        for role in ('generator', 'checker'):
            prbs_caps(p, role, pattern, False, rec['ends'][n], say)
    for p, n in ((gen, an), (chk, bn)):
        info = release_lpmode(p, budget_w, timeout, say=say)
        rec['ends'][n]['release'] = {k: info[k] for k in ('gate', 'module_ready_observed_s', 'module_ready_within_advertised')}
        if apsel is not None:
            rec['ends'][n]['config_status_11h_202'] = select_application(p, n, apsel, timeout, say)
    masks = []
    for p, n in ((gen, an), (chk, bn)):
        codes = ensure_dp_active(p, n, timeout, say)
        rec['ends'][n]['dp_states_11h_128'] = [DP_STATES.get(c, c) for c in codes]
        masks.append(sum(1 << i for i, c in enumerate(codes) if c == DP_ACTIVATED))
    mask = masks[0] & masks[1] & media_lane_mask(gen) & media_lane_mask(chk)
    rec['lane_mask'] = f'0x{mask:02x}'
    if not mask:
        raise Refused('no lane DPActivated on both ends in the media lanes of the application (11h:128-131)')
    return mask


def pair_down(gen, chk, names, rec, timeout, say):
    for p, n in zip((gen, chk), names):
        p.set_lpmode(1)
        rec['ends'][n]['lowpwr_after'] = wait_state(p, ST_LOWPWR, timeout, say)
    say('both ends back in low power' if all(rec['ends'][n].get('lowpwr_after') for n in names) else
        'WARNING: an end did not report ModuleLowPwr after LPMode went high')


def arm(gen, chk, names, rec, pattern, mask):
    """Generator -> checker armed on `mask` with the placement both ends advertise (prbs_arm: counters reset, LOL cleared)."""
    an, bn = names
    pat = PATTERNS[pattern]
    sel = bytes([pat | pat << 4]) * 4
    g155, c171, place = fec_placement(rec['ends'][an]['p13_131'], rec['ends'][bn]['p13_131'])
    res = {'readback': {}, 'cleanup': {}, 'health': {an: [], bn: []}, 'placement': place}
    prbs_arm(gen, chk, an, bn, sel, g155, c171, mask, res)
    return res


def window(gen, chk, names, rec, lanes, seconds, say):
    """One host-timed measurement window at the checker: reset (13h:177.5 1 -> 0), wait, freeze and read BER, counters
    and the latched LOL (read_media_stats). GatingSupport 13h:129.7-6 = 0 on the Accelink: the host times the window and
    TotalBits is the accurate length (Table 8-95 note)."""
    an, bn = names
    with chk.selected():
        chk.page(0x13)
        ctrl = chk.read(P13_CTRL, 1)[0]
        chk.write(P13_CTRL, bytes([ctrl | CTRL_RESET_ERR]))
        chk.write(P13_CTRL, bytes([ctrl & ~CTRL_RESET_ERR]))
    t0 = time.monotonic()
    while True:
        for p, n in ((gen, an), (chk, bn)):
            h = poll_health(p)
            rec.setdefault('health', {}).setdefault(n, []).append({'t_s': round(time.monotonic() - t0, 1), **h})
            reason = stop_reason(h, p, set(), n, say)
            if reason:
                if reason.startswith('THERMAL'):
                    for q in (gen, chk):
                        q.set_lpmode(1)
                raise PrbsAbort(f'{n}: {reason}')
        left = seconds - (time.monotonic() - t0)
        if left <= 0:
            break
        time.sleep(min(1.0, left))
    res, problems = {}, []
    lol = read_media_stats(gen, chk, an, bn, rec['ends'][bn], lanes, res, problems, _quiet)
    out = {'seconds': seconds, 'media_ber': res.get('media_ber'), 'media_errors': res.get('media_errors'),
           'media_bits': res.get('media_bits'), 'media_lol_14h_139': f'0x{lol:02x}', 'problems': problems}
    say('  ' + ' '.join(f'L{i + 1}:{b:.1e}' for i, b in enumerate(out['media_ber'] or []) if i in lanes)
        + (f'  LOL 0x{lol:02x}' if lol else ''))
    return out


# --------------------------------------------------------------------------- lane-integrity
def cmd_lane_integrity(gen, chk, pattern, seconds, budget_w, timeout, out, names, say=None, apsel=None):
    say = say or _quiet
    an, bn = names
    rec = {**record_header(names), 'kind': 'lane_integrity', 'pattern': pattern, 'window_s': seconds,
           'ends': {an: {}, bn: {}}, 'lane_mask': None, 'baseline': None, 'lanes': [], 'verdict': None,
           'method': f'PRBS {pattern} {an} -> {bn}; per lane: 10h:{P10_TX_DISABLE} bit at {an} (media Tx off), a window at {bn}, '
                     f'restore, a window; then 10h:{P10_RX_DISABLE} at {bn} (host-side Rx output off: no effect on the media '
                     f'checker is the expectation without a host)'}
    try:
        mask = pair_up(gen, chk, names, rec, pattern, budget_w, timeout, apsel, say)
        lanes = [i for i in range(8) if mask >> i & 1]
        res = arm(gen, chk, names, rec, pattern, mask)
        rec['placement'] = res['placement']
        try:
            say('baseline window')
            rec['baseline'] = window(gen, chk, names, rec, lanes, seconds, say)
            for lane in lanes:
                row = {'lane': lane + 1}
                say(f'lane {lane + 1}: Tx output disable at {an}')
                row['tx_disable_readback'] = _wr(gen, 0x10, P10_TX_DISABLE, [1 << lane]).hex()
                time.sleep(1.0)
                w = window(gen, chk, names, rec, lanes, seconds, say)
                w['rx_flags_11h'] = {str(k): f'0x{v:02x}' for k, v in read_media_flags(chk).items()}
                row['tx_disabled'] = w
                row['restore_readback'] = _wr(gen, 0x10, P10_TX_DISABLE, [0]).hex()
                time.sleep(2.0)
                _rd(chk, 0x14, P14_CHK_LOL_MEDIA)  # the relock transient, cleared before the restore window
                read_media_flags(chk)
                row['restored'] = window(gen, chk, names, rec, lanes, seconds, say)
                say(f'lane {lane + 1}: Rx output disable at {bn}')
                row['rx_disable_readback'] = _wr(chk, 0x10, P10_RX_DISABLE, [1 << lane]).hex()
                time.sleep(1.0)
                row['rx_disabled_at_checker'] = window(gen, chk, names, rec, lanes, seconds, say)
                _wr(chk, 0x10, P10_RX_DISABLE, [0])
                time.sleep(1.0)
                # the judge is the checker's TotalBits (Table 8-121): a dark lane stops counting (1 bit, odd = sync loss);
                # the latched LOL byte and the LOS / LOL Rx flags flicker on a marginal fibre and only support the call
                bits_off = w.get('media_bits') or [0] * 8
                bits_back = row['restored'].get('media_bits') or [0] * 8
                dark_lanes = [i + 1 for i in lanes if bits_off[i] <= 1]
                dead_after = [i + 1 for i in lanes if bits_back[i] <= 1]
                row['dark_lanes_while_off'] = dark_lanes
                row['dead_lanes_after_restore'] = dead_after
                lol = int(w['media_lol_14h_139'], 16)
                if dark_lanes == [lane + 1] and not dead_after:
                    row['verdict'] = 'PASS'
                elif (lane + 1) not in dark_lanes:
                    row['verdict'] = 'FAIL: the lane kept counting with its Tx off'
                elif len(dark_lanes) > 1:
                    row['verdict'] = f'FAIL: collateral, lanes {[x for x in dark_lanes if x != lane + 1]} went dark too'
                else:
                    row['verdict'] = f'FAIL: lanes {dead_after} stayed dark after the restore'
                rec['lanes'].append(row)
                say(f'lane {lane + 1}: {row["verdict"]} (dark while off {dark_lanes}, dead after restore {dead_after}, LOL 0x{lol:02x})')
        finally:
            prbs_disable(gen, chk, an, bn, res, say)
            _wr(gen, 0x10, P10_TX_DISABLE, [0])
            _wr(chk, 0x10, P10_RX_DISABLE, [0])
        bad = [r['lane'] for r in rec['lanes'] if r['verdict'] != 'PASS']
        rec['verdict'] = 'PASS' if rec['lanes'] and not bad else f'FAIL: lanes {bad}' if bad else 'INCONCLUSIVE: no lane'
    except (Refused, Nack, PrbsAbort) as e:
        rec['verdict'] = f'ABORTED: {e}'
        raise
    finally:
        try:
            pair_down(gen, chk, names, rec, timeout, say)
        finally:
            write_record(rec, out, f'LANE_{an}_{bn}_{{}}.json', say)
    say(f'lane-integrity: {rec["verdict"]}')
    return rec


# --------------------------------------------------------------------------- si-sweep
SI_SETTINGS = [  # (name, SCS0 key, bytes to write on all lanes, note)
    ('cdr_rx_off', 'cdr_rx', [0x00], 'Rx CDR bypass on every lane (10h:161 = 0)'),
    ('cdr_rx_on', 'cdr_rx', [0xFF], 'Rx CDR enabled on every lane'),
    ('cdr_tx_off', 'cdr_tx', [0x00], 'Tx CDR bypass on every lane (10h:160 = 0)'),
    ('cdr_tx_on', 'cdr_tx', [0xFF], 'Tx CDR enabled on every lane'),
    ('rx_amp_0', 'rx_amp', [0x00] * 4, 'OutputAmplitudeTargetRx code 0 (100-400 mVpp, Table 8-74)'),
    ('rx_amp_1', 'rx_amp', [0x11] * 4, 'code 1 (300-600 mVpp)'),
    ('rx_amp_3', 'rx_amp', [0x33] * 4, 'code 3 (600-1200 mVpp)'),
    ('rx_pre_0', 'rx_pre', [0x00] * 4, 'OutputEqPreCursorTargetRx 0 dB'),
    ('rx_pre_3', 'rx_pre', [0x33] * 4, 'pre-cursor 3 dB'),
    ('rx_post_0', 'rx_post', [0x00] * 4, 'OutputEqPostCursorTargetRx 0 dB'),
    ('rx_post_4', 'rx_post', [0x44] * 4, 'post-cursor 4 dB'),
]


def _apply_dpinit(port, mask, timeout):
    """ApplyDPInitLane on `mask`, then ConfigStatusLane1-8 until no lane is 'in progress' (Table 8-84: 0 undefined,
    1 success, 2 rejected, 3 invalid apsel, 4 invalid DP, 5 invalid SI, 6 in use, 7 incomplete, 0xC in progress)."""
    _wr(port, 0x10, P10_APPLY_DPINIT, [mask])
    t0 = time.monotonic()
    while True:
        raw = _rd(port, 0x11, 202, 4)
        codes = [(raw[i // 2] >> (4 * (i % 2))) & 0xF for i in range(8)]
        if all(c != 0xC for c in codes) or time.monotonic() - t0 > timeout:
            return codes, round(time.monotonic() - t0, 3)
        time.sleep(0.1)


CONFIG_STATUS = {0: 'ConfigUndefined', 1: 'ConfigSuccess', 2: 'ConfigRejected', 3: 'ConfigRejectedInvalidAppSel',
                 4: 'ConfigRejectedInvalidDataPath', 5: 'ConfigRejectedInvalidSI', 6: 'ConfigRejectedLaneInUse',
                 7: 'ConfigRejectedPartialDataPath', 0xC: 'ConfigInProgress'}


def cmd_si_sweep(gen, chk, pattern, seconds, budget_w, timeout, out, names, say=None, apsel=None, end='checker'):
    """The settings of SI_SETTINGS applied at one end (the checker by default) with a BER window each."""
    say = say or _quiet
    an, bn = names
    tgt, tname = (chk, bn) if end == 'checker' else (gen, an)
    rec = {**record_header(names), 'kind': 'si_sweep', 'pattern': pattern, 'window_s': seconds, 'applied_at': tname,
           'ends': {an: {}, bn: {}}, 'lane_mask': None, 'baseline': None, 'settings': [], 'verdict': None,
           'method': 'per setting: SCS0 bytes written (10h), ApplyDPInit (10h:143), ConfigStatus (11h:202-205), the ACS mirror '
                     'read (11h:214-234), one BER window at the checker; the original SCS0 bytes restored and re-applied at the end'}
    try:
        mask = pair_up(gen, chk, names, rec, pattern, budget_w, timeout, apsel, say)
        lanes = [i for i in range(8) if mask >> i & 1]
        adv = _rd(tgt, 0x01, 161, 2)
        rec['advert_01h_161_162'] = adv.hex()
        orig = {k: _rd(tgt, 0x10, off, 4 if k.startswith('rx_') else 1) for k, off in P10_SCS0.items()}
        rec['scs0_before'] = {k: v.hex() for k, v in orig.items()}
        rec['acs_before'] = {k: _rd(tgt, 0x11, off, 4 if k.startswith('rx_') else 1).hex() for k, off in P11_ACS.items()}
        res = arm(gen, chk, names, rec, pattern, mask)
        try:
            say('baseline window')
            rec['baseline'] = window(gen, chk, names, rec, lanes, seconds, say)
            for name, key, data, note in SI_SETTINGS:
                row = {'setting': name, 'scs0_byte': P10_SCS0[key], 'wrote': bytes(data).hex(), 'note': note}
                row['scs0_readback'] = _wr(tgt, 0x10, P10_SCS0[key], data).hex()
                codes, dt = _apply_dpinit(tgt, mask, timeout)
                row['config_status'] = [CONFIG_STATUS.get(c, c) for c in codes]
                row['apply_s'] = dt
                row['acs_readback'] = _rd(tgt, 0x11, P11_ACS[key], len(data)).hex()
                row['dp_states'] = [DP_STATES.get(c, c) for c in dp_states(tgt)]
                before = rec['acs_before'][key]
                row['taken'] = row['acs_readback'] == bytes(data).hex() and row['acs_readback'] != before  # the ACS moved to the value
                row['effect'] = ('changed' if row['taken'] else 'unchanged (the ACS already held the value)' if row['acs_readback'] == bytes(data).hex()
                                 else 'ignored (ConfigSuccess, the ACS kept its value)')
                time.sleep(1.0)
                _rd(chk, 0x14, P14_CHK_LOL_MEDIA)  # a re-init transient, cleared before the window
                row['window'] = window(gen, chk, names, rec, lanes, seconds, say)
                say(f'{name}: ConfigStatus {sorted(set(row["config_status"]))}, ACS {row["acs_readback"]} ({row["effect"]})')
                rec['settings'].append(row)
                # the setting's own restore before the next one keeps every row a single-variable test
                _wr(tgt, 0x10, P10_SCS0[key], orig[key])
                _apply_dpinit(tgt, mask, timeout)
                time.sleep(0.5)
        finally:
            prbs_disable(gen, chk, an, bn, res, say)
            for k, v in orig.items():
                _wr(tgt, 0x10, P10_SCS0[k], v)
            codes, _ = _apply_dpinit(tgt, mask, timeout)
            rec['scs0_restored'] = {k: _rd(tgt, 0x10, off, 4 if k.startswith('rx_') else 1).hex() for k, off in P10_SCS0.items()}
            rec['restore_config_status'] = [CONFIG_STATUS.get(c, c) for c in codes]
        changed = [r['setting'] for r in rec['settings'] if r['taken']]
        ignored = [r['setting'] for r in rec['settings'] if r['effect'].startswith('ignored')]
        rec['verdict'] = (f'MEASURED: {len(changed)} settings changed the ACS {changed}, {len(ignored)} ignored {ignored}'
                          + ('; the SI controls are hollow on this module' if not changed and ignored else ''))
    except (Refused, Nack, PrbsAbort) as e:
        rec['verdict'] = f'ABORTED: {e}'
        raise
    finally:
        try:
            pair_down(gen, chk, names, rec, timeout, say)
        finally:
            write_record(rec, out, f'SI_{an}_{bn}_{{}}.json', say)
    say(f'si-sweep: {rec["verdict"]}')
    return rec


# --------------------------------------------------------------------------- flag-matrix
def _s16_c(c):
    v = int(round(c * 256)) & 0xFFFF
    return [v >> 8, v & 0xFF]


def _mask_all(port, on):
    """Every module and lane flag masked (on=True) or the saved bytes written back (on = the saved dict)."""
    if on is True:
        saved = {'00h:31': _rd(port, 0, L_MASKS, 4).hex(), '10h:213': _rd(port, 0x10, 213, 5).hex(),
                 '10h:218': _rd(port, 0x10, 218, 14).hex()}
        _wr(port, 0, L_MASKS, [0xFF] * 4)
        _wr(port, 0x10, 213, [0xFF] * 5)
        _wr(port, 0x10, 218, [0xFF] * 14)
        return saved
    _wr(port, 0, L_MASKS, bytes.fromhex(on['00h:31']))
    _wr(port, 0x10, 213, bytes.fromhex(on['10h:213']))
    _wr(port, 0x10, 218, bytes.fromhex(on['10h:218']))
    return None


def _clear_flags(port):
    """Read every latched flag byte once (module 00h:8-11, lane 11h:134-152) so nothing pends before a row."""
    _rd(port, 0, L_FLAGS, 4)
    _rd(port, 0x11, 134, 19)


def cmd_flag_matrix(gen, chk, pattern, budget_w, timeout, out, names, say=None, apsel=None):
    """Rows provoked at the checker end `bn` (the receiving end); the generator end only feeds the light."""
    say = say or _quiet
    an, bn = names
    rec = {**record_header(names), 'kind': 'flag_matrix', 'pattern': pattern, 'ends': {an: {}, bn: {}}, 'lane_mask': None,
           'rows': [], 'verdict': None, 'observed_at': bn,
           'method': 'every mask set, the flags cleared, IntL read; one flag unmasked at a time, provoked, IntL read while it pends, '
                     'the flag byte read (clear on read), IntL read again; the provocation undone; masks and thresholds restored'}
    saved = None
    thr = {}
    try:
        mask = pair_up(gen, chk, names, rec, pattern, budget_w, timeout, apsel, say)
        res = arm(gen, chk, names, rec, pattern, mask)
        try:
            saved = _mask_all(chk, True)
            _clear_flags(chk)
            time.sleep(0.5)
            rec['intl_all_masked'] = chk.sense()['interrupt']

            def row(name, unmask, provoke, undo, flag_read):
                r = {'flag': name}
                unmask()
                time.sleep(0.2)
                r['intl_before'] = chk.sense()['interrupt']
                r['provoked'] = provoke()
                time.sleep(0.8)
                r['intl_pending'] = chk.sense()['interrupt']
                r['flags_read'] = flag_read()
                time.sleep(0.2)
                r['intl_after_read_condition_held'] = chk.sense()['interrupt']  # a held condition re-latches (CMIS latched flags)
                undo()
                time.sleep(1.0)
                flag_read()  # the re-latched flag of the condition's end
                time.sleep(0.2)
                r['intl_after_undo'] = chk.sense()['interrupt']
                _clear_flags(chk)
                _mask_all(chk, True)
                r['pass'] = bool(r['intl_pending']) and not r['intl_after_undo'] and bool(r['flags_read'])
                rec['rows'].append(r)
                say(f'  {name:<26} provoked {str(r["provoked"])[:40]:<40} IntL pending {r["intl_pending"]} after undo {r["intl_after_undo"]} '
                    f'flags {r["flags_read"]} -> {"PASS" if r["pass"] else "FAIL"}')

            def module_flag(name):
                byte, bit = _flag_bit(name)
                return (lambda: _wr(chk, 0, L_MASKS + byte - 8, [0xFF & ~(1 << bit)]),
                        lambda: module_flag_names(_rd(chk, 0, L_FLAGS, 4)))

            def lane_flag(key, lane_bit):
                return (lambda: _wr(chk, 0x10, P10_MASKS[key], [0xFF & ~lane_bit]),
                        lambda: (lambda v: [f'{key} lanes 0x{v:02x}'] if v else [])(_rd(chk, 0x11, P11_LANE_FLAGS[key])[0]))

            # 1. ModuleStateChangedFlag by an LPMode cycle at the checker end
            u, f = module_flag('ModuleStateChangedFlag')

            def lp_cycle():
                chk.set_lpmode(1)
                wait_state(chk, ST_LOWPWR, timeout, _quiet)
                chk.set_lpmode(0)
                return 'LPMode high then low'
            row('ModuleStateChangedFlag', u, lp_cycle, lambda: wait_state(chk, ST_READY, timeout, _quiet), f)
            ensure_dp_active(chk, bn, timeout, _quiet)
            # 2/3. temperature and Vcc high warnings by thresholds on Page 02h, only when the module takes the write
            for fname, tkey, cur in (('TempMonHighWarning', 'temp_high_warning_c', 'temp'), ('VccMonHighWarning', 'vcc_high_warning_v', 'vcc')):
                off, signed = P2_THRESHOLDS[tkey]
                before = _rd(chk, 0x02, off, 2)
                h = _rd(chk, 0, 14, 4)
                temp_c = int.from_bytes(h[0:2], 'big', signed=True) / 256
                vcc_v = int.from_bytes(h[2:4], 'big') / 10000
                low = _s16_c(temp_c - 2.0) if cur == 'temp' else list(int((vcc_v - 0.05) * 10000).to_bytes(2, 'big'))
                back = _wr(chk, 0x02, off, low)
                thr[tkey] = (off, before)
                if back != bytes(low):
                    rec['rows'].append({'flag': fname, 'provoked': f'02h:{off} not writable (wrote {bytes(low).hex()}, read {back.hex()})', 'pass': None})
                    say(f'  {fname:<26} 02h:{off} read-only on this module ({back.hex()}): not provoked')
                    _wr(chk, 0x02, off, before)
                    continue
                u, f = module_flag(fname)
                row(fname, u, lambda: f'02h:{off} = {bytes(low).hex()} ({cur} {temp_c:.1f} C / {vcc_v:.3f} V)',
                    lambda: _wr(chk, 0x02, off, before), f)
            # 4. DPStateChangedFlag by a DPDeinit of the highest lane in the mask, then back
            top = max(i for i in range(8) if mask >> i & 1)
            u, f = lane_flag('dp_state', 1 << top)
            row('DPStateChangedFlag', u, lambda: _wr(chk, 0x10, P10_DPDEINIT, [1 << top]).hex() and f'DPDeinit lane {top + 1}',
                lambda: (_wr(chk, 0x10, P10_DPDEINIT, [0]), ensure_dp_active(chk, bn, timeout, _quiet)), f)
            # 5/6. LOS and CDR LOL Rx on the lowest lane by a Tx output disable at the generator end
            low_lane = min(i for i in range(8) if mask >> i & 1)
            for key, fname in (('los_rx', 'LOSFlagRx'), ('lol_rx', 'CDRLOLFlagRx')):
                u, f = lane_flag(key, 1 << low_lane)
                row(fname, u, lambda: _wr(gen, 0x10, P10_TX_DISABLE, [1 << low_lane]).hex() and f'Tx off lane {low_lane + 1} at {an}',
                    lambda: _wr(gen, 0x10, P10_TX_DISABLE, [0]), f)
                time.sleep(1.5)
                _clear_flags(chk)
        finally:
            prbs_disable(gen, chk, an, bn, res, say)
            _wr(gen, 0x10, P10_TX_DISABLE, [0])
            for off, before in thr.values():
                _wr(chk, 0x02, off, before)
            if saved:
                _mask_all(chk, saved)
                rec['masks_restored'] = {'00h:31': _rd(chk, 0, L_MASKS, 4).hex(), '10h:213': _rd(chk, 0x10, 213, 5).hex()}
            _clear_flags(chk)
        judged = [r for r in rec['rows'] if r.get('pass') is not None]
        bad = [r['flag'] for r in judged if not r['pass']]
        rec['verdict'] = ('PASS' if judged and not bad else f'FAIL: {bad}' if bad else 'INCONCLUSIVE: nothing provoked') + \
                         f' ({len(judged)} flags judged, {len(rec["rows"]) - len(judged)} not provokable)'
    except (Refused, Nack, PrbsAbort) as e:
        rec['verdict'] = f'ABORTED: {e}'
        raise
    finally:
        try:
            pair_down(gen, chk, names, rec, timeout, say)
        finally:
            write_record(rec, out, f'FLAGS_{an}_{bn}_{{}}.json', say)
    say(f'flag-matrix: {rec["verdict"]}')
    return rec


# --------------------------------------------------------------------------- cdb-password
def cmd_cdb_password(port, serial, password, out, say=None):
    """00h:122-125 <- password (MSB first), then CDB 0000h Query Status (Table 9-3: status 1 = host password accepted)
    and 0041h. Read-only CDB; the entry area clears at reset. Low power is enough."""
    say = say or _quiet
    rec = {**record_header([serial]), 'kind': 'cdb_password', 'password_given': password is not None, 'before': None, 'after': None}
    with port.selected():
        q = cdbmod.cdb_command(port, 0x0000, b'\x00\x00', selected=True)
        rec['before'] = {'ok': q['ok'], 'status': q['status'], 'decoded': q.get('decoded')}
        if password is not None:
            port.write(PASSWORD_ENTRY, int(password).to_bytes(4, 'big'))
            time.sleep(0.2)
            q = cdbmod.cdb_command(port, 0x0000, b'\x00\x00', selected=True)
            rec['after'] = {'ok': q['ok'], 'status': q['status'], 'decoded': q.get('decoded')}
            fw = cdbmod.cdb_command(port, 0x0041, selected=True)
            rec['fw_features_after'] = {'ok': fw['ok'], 'status': fw['status']}
    for k in ('before', 'after'):
        d = rec[k]
        if d:
            say(f'{k}: query {d["status"].get("coarse")} {(d.get("decoded") or {}).get("status_text")}')
    write_record(rec, out, f'cdbpw_{serial}_{{}}.json', say)
    return rec
