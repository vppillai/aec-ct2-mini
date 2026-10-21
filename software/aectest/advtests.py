"""Advanced single-module tests: `audit`, `timing`, `flags`, `i2c-stress`.

Every command writes one JSON record (record_header + the rows) and prints a table. The power steps of `timing` and
`flags` run only when the LPMode permission flags were given (port.allow_lp_low); without them they report 'skipped'.
Register citations are OIF-CMIS-05.2.
"""
import json
import statistics
import time

from .cmis import (DP_ACTIVATED, DP_STATES, DURATION_MAX_S, IDENTIFIERS, L_FLAGS9, L_FW_MAJOR, L_MEM, L_REV, L_STATE, MEDIA_TECH,
                   MSA_CLASS_W, P0_MAXP, P0_PCLASS, P1_CDB_ADV, P1_DIAG_BIT, P1_DURATION_FIELDS, P1_MONITORS, P1_PAGE03_BIT,
                   P1_PAGES, P1_VDM_BIT, P10_DPDEINIT, P2_THRESHOLDS, STATES, ST_LOWPWR, decode_duration, decode_thresholds)
from .records import record_header, write_record
from .tests import _quiet, build_dump, cmd_reset, dp_states, read_id, release_lpmode, select_application, wait_state
from .transport import MODSELL, Nack

L_FLAGS8, L_MASKS, L_MASK_COUNT = 8, 31, 4  # [CMIS] Table 8-9 module flags 8-11; Table 8-12 module-level masks 31-34 (35-36 reserved/custom)
MASK_STATE_CHANGED = 0x01  # 31.0 ModuleStateChangedMask <-> 8.0 ModuleStateChangedFlag (Rqd.)
P0_CHECKSUM, P0_MEDIA_TYPE, P0_APPS = 222, 85, 86  # Table 8-25 (8.3.10: sum of 128-221); lower page 85 (Table 8-18) and 86-117 (Table 8-20, apps 1-8)
MEDIA_TYPES = {0: 'undefined', 1: 'optical MMF', 2: 'optical SMF', 3: 'passive copper', 4: 'active cable', 5: 'BASE-T'}  # Table 8-17


# --------------------------------------------------------------------------- audit (pure on a dump dict)
def _ascii(b):
    return all(0x20 <= x < 0x7F for x in b)


def audit_checks(dump):
    """Scored CMIS memory-map audit of one dump (build_dump output). Returns rows [(id, result, detail)]; result in PASS / FAIL / NA."""
    lo, pages = bytes.fromhex(dump['lower']), {k: bytes.fromhex(v) for k, v in dump['pages'].items()}
    p0 = pages['00h']
    up = lambda pg, off: pages[pg][off - 128]  # noqa: E731
    rows = []

    def row(cid, ok, detail):
        rows.append((cid, 'NA' if ok is None else 'PASS' if ok else 'FAIL', detail))

    flat = bool(lo[L_MEM] & 0x80)
    row('A01 identifier', lo[0] in IDENTIFIERS, f'00h:0 = 0x{lo[0]:02x} {IDENTIFIERS.get(lo[0], "unknown")}')
    row('A02 cmis revision', lo[L_REV] >= 0x30, f'00h:1 = {lo[L_REV] >> 4}.{lo[L_REV] & 15} (3.0 or later)')
    st = (lo[L_STATE] >> 1) & 7
    row('A03 module state code', 1 <= st <= 5, f'00h:3 = 0x{lo[L_STATE]:02x} {STATES.get(st, "reserved")}')
    fw = (lo[L_FW_MAJOR], lo[L_FW_MAJOR + 1])
    row('A04 active firmware field', fw != (255, 255), f'00h:39-40 = {fw[0]}.{fw[1]}' + (' (0.0 = no firmware, allowed)' if fw == (0, 0) else ''))
    for name, (a, b) in (('vendor', (129, 145)), ('part number', (148, 164)), ('serial', (166, 182))):
        field = p0[a - 128:b - 128]
        row(f'A05 {name} ascii', _ascii(field) and field.strip() != b'', f'00h:{a}-{b - 1} = {field.decode("ascii", "replace").strip()!r}')
    date = p0[182 - 128:188 - 128]
    row('A06 date code YYMMDD', date.isdigit() and 1 <= int(date[2:4]) <= 12 and 1 <= int(date[4:6]) <= 31, f'00h:182-187 = {date.decode("ascii", "replace")!r}')
    row('A07 page 00h checksum', sum(p0[0:94]) & 0xFF == p0[P0_CHECKSUM - 128], f'00h:222 = 0x{p0[P0_CHECKSUM - 128]:02x}, sum(128-221) = 0x{sum(p0[0:94]) & 0xFF:02x}')
    pclass, maxp = (p0[P0_PCLASS - 128] >> 5) + 1, p0[P0_MAXP - 128] * 0.25
    cmax = MSA_CLASS_W[pclass]
    row('A08 power class vs MaxPower', None if cmax is None else maxp <= cmax, f'class {pclass} (max {cmax} W), MaxPower {maxp:.2f} W')
    mtype, mtech = lo[P0_MEDIA_TYPE], p0[212 - 128]  # media type and the application table live in the LOWER page (Tables 8-18, 8-20)
    row('A09 media type code', mtype in MEDIA_TYPES, f'00h:85 = {mtype} {MEDIA_TYPES.get(mtype, "reserved")}')
    tech_copper, tech_vcsel = 0x0A <= mtech <= 0x0F, mtech <= 0x02
    consistent = {3: tech_copper, 4: tech_copper or mtech == 0x08, 1: tech_vcsel or mtech == 0x08, 2: not tech_copper and not tech_vcsel}.get(mtype)
    row('A10 media technology vs media type', consistent, f'00h:212 = 0x{mtech:02x} {MEDIA_TECH.get(mtech, "reserved")} for media type {mtype}')
    apps, ok_apps = [], True
    for i in range(8):
        d = lo[P0_APPS + 4 * i:P0_APPS + 4 * i + 4]
        if d[0] in (0x00, 0xFF):
            break
        hl, ml = d[2] >> 4, d[2] & 15
        apps.append((i + 1, d[0], d[1], hl, ml, d[3]))
        ok_apps &= 1 <= hl <= 8 and 1 <= ml <= 8 and d[3] != 0
    row('A11 application table', bool(apps) and ok_apps, f'{len(apps)} application(s): ' + ', '.join(f'{n}: host 0x{h:02x} media 0x{m:02x} {hl}/{ml} lanes assign 0x{a:02x}' for n, h, m, hl, ml, a in apps))
    if flat:
        row('A12 flat memory', True, '00h:2.7 = 1: lower page and page 00h only, the paged checks are NA')
        for cid in ('A13 page 01h checksum', 'A14 page 02h checksum', 'A15 thresholds ordered', 'A16 duration codes', 'A17 advertised pages present', 'A18 monitors vs live values'):
            row(cid, None, 'flat memory')
        return rows
    p1, p2 = pages.get('01h'), pages.get('02h')
    row('A13 page 01h checksum', p1 is not None and sum(p1[2:127]) & 0xFF == p1[127], 'none' if p1 is None else f'01h:255 = 0x{p1[127]:02x}, sum(130-254) = 0x{sum(p1[2:127]) & 0xFF:02x}')
    row('A14 page 02h checksum', p2 is not None and sum(p2[0:127]) & 0xFF == p2[127], 'none' if p2 is None else f'02h:255 = 0x{p2[127]:02x}, sum(128-254) = 0x{sum(p2[0:127]) & 0xFF:02x}')
    if p2 is not None:
        t = decode_thresholds(p2)
        ordered = (t['temp_high_alarm_c'] >= t['temp_high_warning_c'] > t['temp_low_warning_c'] >= t['temp_low_alarm_c']
                   and t['vcc_high_alarm_v'] >= t['vcc_high_warning_v'] > t['vcc_low_warning_v'] >= t['vcc_low_alarm_v'])
        row('A15 thresholds ordered', ordered, f'temp {t["temp_high_alarm_c"]}/{t["temp_high_warning_c"]}/{t["temp_low_warning_c"]}/{t["temp_low_alarm_c"]} C, '
                                               f'Vcc {t["vcc_high_alarm_v"]}/{t["vcc_high_warning_v"]}/{t["vcc_low_warning_v"]}/{t["vcc_low_alarm_v"]} V')
    else:
        row('A15 thresholds ordered', None, 'no page 02h in the dump')
    if p1 is not None:
        codes = {k: (p1[off - 128] >> lo_bit) & 0xF for k, (off, lo_bit) in P1_DURATION_FIELDS.items()}
        row('A16 duration codes', all(c <= 13 for c in codes.values()), ', '.join(f'{k} {decode_duration(c)[0].split(":")[0]}' for k, c in codes.items()))
        adv = p1[P1_PAGES - 128]
        want = [('03h', bool(adv & P1_PAGE03_BIT)), ('13h', bool(adv & P1_DIAG_BIT)), ('2Fh', bool(adv & P1_VDM_BIT))]
        missing = ['03h'] if adv & P1_PAGE03_BIT and '03h' not in pages else []  # the dump reads 03h when advertised; 13h / 2Fh are read by their own commands
        row('A17 advertised pages present', not missing, f'01h:142 = 0x{adv:02x}; advertised {[pg for pg, w in want if w]}; missing from the dump: {missing or "none"}')
        mon = p1[P1_MONITORS - 128]
        temp = int.from_bytes(lo[14:16], 'big', signed=True) / 256.0
        vcc = int.from_bytes(lo[16:18], 'big') * 1e-4
        ok_mon = (not mon & 1 or -10.0 <= temp <= 120.0) and (not mon & 2 or 2.5 <= vcc <= 4.0)
        row('A18 monitors vs live values', ok_mon, f'01h:159 = 0x{mon:02x}; TempMon {temp:.1f} C, VccMon {vcc:.3f} V')
        cdb = p1[P1_CDB_ADV - 128]
        row('A19 CDB advertisement', True, f'01h:163 = 0x{cdb:02x}: {cdb >> 6} instance(s)')
    else:
        for cid in ('A16 duration codes', 'A17 advertised pages present', 'A18 monitors vs live values', 'A19 CDB advertisement'):
            row(cid, None, 'no page 01h in the dump')
    return rows


def audit_score(rows):
    done = [r for r in rows if r[1] != 'NA']
    passed = sum(1 for r in done if r[1] == 'PASS')
    return passed, len(done)


def audit_markdown(dump, rows):
    passed, total = audit_score(rows)
    d = dump['decoded']
    lines = [f"# CMIS memory-map audit: {d['vendor']} {d['part_number']} S/N {d['serial']} on {dump['ftdi_serial']}", '',
             f'Score {passed} of {total} applicable checks pass. Source OIF-CMIS-05.2. Record {dump["timestamp"]}.', '',
             '| check | result | detail |', '|---|---|---|']
    lines += [f'| {cid} | {res} | {detail} |' for cid, res, detail in rows]
    return '\n'.join(lines) + '\n'


def cmd_audit(port, serial, out, as_json, say=None):
    say = say or _quiet
    dump = build_dump(port, serial)
    rows = audit_checks(dump)
    passed, total = audit_score(rows)
    rec = {**record_header([serial]), 'kind': 'audit', 'module': {k: dump['decoded'][k] for k in ('vendor', 'part_number', 'serial', 'cmis_revision')},
           'score': {'pass': passed, 'applicable': total}, 'rows': [{'id': c, 'result': r, 'detail': t} for c, r, t in rows]}
    if as_json:
        say(json.dumps(rec, indent=1))
    else:
        for cid, res, detail in rows:
            say(f'  {res:<4} {cid:<34} {detail}')
        say(f'audit: {passed} of {total} applicable checks pass')
    if out == '-':
        write_record(rec, out, '', say)
    else:  # JSON and markdown side by side, one stamp (write_record refuses an existing file)
        base = out.rsplit('.', 1)[0] if out else f'audit_{serial}_' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        write_record(rec, base + '.json', '', say)
        with open(base + '.md', 'x') as f:
            f.write(audit_markdown(dump, rows))
        say(f'wrote {base}.md')
    return rec


# --------------------------------------------------------------------------- timing
def _bound(rec, key):
    text, hi = decode_duration(rec[key]) if rec.get(key) is not None else ('n/a', None)
    return text, hi


def cmd_timing(port, serial, out, budget_w, timeout, apsel=None, as_json=False, say=None):
    """State-machine timings against the advertised maxima: MgmtInit (ResetL, limit 2 s), ModulePwrUp / PwrDn (01h:167),
    DPInit / DPDeinit (01h:144), ApSel switch (no advertised bound). The power steps need the LPMode permission."""
    say = say or _quiet
    info = read_id(port)
    steps = []

    def step(name, measured, limit, note=''):
        within = None if measured is None or limit is None else measured <= limit
        steps.append({'step': name, 'measured_s': None if measured is None else round(measured, 3), 'limit_s': limit, 'within': within, 'note': note})
        say(f'  {name:<22} {"skipped" if measured is None else f"{measured * 1e3:8.1f} ms":>12}  limit {"n/a" if limit is None else f"{limit * 1e3:.0f} ms":>10}  '
            f'{"" if within is None else "within" if within else "OVER"}  {note}')

    flat = info['memory_model'] == 'flat'
    codes = {}
    if not flat:
        with port.selected():
            p1 = port.read_page(0x01)
        codes = {k: (p1[off - 128] >> lo) & 0xF for k, (off, lo) in P1_DURATION_FIELDS.items()}
    lim = {k: DURATION_MAX_S[c] if c <= 12 else None for k, c in codes.items()}
    dt = cmd_reset(port, say)  # 1. MgmtInit: ResetL pulse -> first ACK (valid in AUTO only; MAN gives a meaningless fast number)
    step('mgmt_init', dt, 2.0, 'ResetL low 1 ms -> first ACK; [MSA] MgmtInitDuration 2 s; AUTO only')
    if not port.allow_lp_low:
        for k in ('module_pwrup', 'dp_init', 'dp_deinit', 'apsel_switch', 'module_pwrdn'):
            step(k, None, lim.get(k), 'needs --release-lpmode --i-am-on-usbc-power')
    elif flat:
        for k in ('module_pwrup', 'dp_init', 'dp_deinit', 'apsel_switch', 'module_pwrdn'):
            step(k, None, None, 'flat memory: no state machine to time')
    else:
        rel = release_lpmode(port, budget_w, timeout, say=say)  # 2. ModulePwrUp (LPMode low -> ModuleReady)
        step('module_pwrup', rel['module_ready_observed_s'], lim.get('module_pwrup'), f'advertised {rel["module_pwrup_advertised_01h_167"]}')
        try:
            if apsel is not None:  # 3. application switch: staged ApSel -> ConfigSuccess on every lane
                t0 = time.monotonic()
                conf = select_application(port, serial, apsel, timeout, say)
                step('apsel_switch', time.monotonic() - t0, None, f'ApSel {apsel}: ConfigStatus {conf}')
            with port.selected():  # 4. DPInit: deinit all, then init all, time to DPActivated on every lane
                port.page(0x10)
                port.write(P10_DPDEINIT, b'\xff')
            t0 = time.monotonic()
            while time.monotonic() - t0 < max(timeout, lim.get('dp_deinit') or 0):
                if all(c != DP_ACTIVATED for c in dp_states(port)):
                    break
                time.sleep(0.005)
            dt_deinit_pre = time.monotonic() - t0
            with port.selected():
                port.page(0x10)
                port.write(P10_DPDEINIT, b'\x00')
            t0 = time.monotonic()
            while True:
                codes_dp = dp_states(port)
                if all(c == DP_ACTIVATED for c in codes_dp) or time.monotonic() - t0 > max(timeout, lim.get('dp_init') or 0):
                    break
                time.sleep(0.005)
            dt_init = time.monotonic() - t0
            step('dp_init', dt_init if all(c == DP_ACTIVATED for c in codes_dp) else None, lim.get('dp_init'),
                 f'states {[DP_STATES.get(c, c) for c in codes_dp]}' + ('' if all(c == DP_ACTIVATED for c in codes_dp) else ' -- not every lane reached DPActivated'))
            with port.selected():  # 5. DPDeinit: init -> deinit, time until no lane is DPActivated
                port.page(0x10)
                port.write(P10_DPDEINIT, b'\xff')
            t0 = time.monotonic()
            while time.monotonic() - t0 < max(timeout, lim.get('dp_deinit') or 0):
                if all(c != DP_ACTIVATED for c in dp_states(port)):
                    break
                time.sleep(0.005)
            dt_deinit = time.monotonic() - t0
            step('dp_deinit', dt_deinit, lim.get('dp_deinit'), f'first deinit before the init took {dt_deinit_pre * 1e3:.0f} ms')
            with port.selected():
                port.page(0x10)
                port.write(P10_DPDEINIT, b'\x00')  # leave the Data Path armed as found (10h:128 default 0 on the modules seen)
        finally:
            port.set_lpmode(1)  # 6. ModulePwrDn: LPMode high -> ModuleLowPwr
            t0 = time.monotonic()
            low = wait_state(port, ST_LOWPWR, max(timeout, lim.get('module_pwrdn') or 0), say)
            step('module_pwrdn', (time.monotonic() - t0) if low else None, lim.get('module_pwrdn'), '' if low else 'module did not report ModuleLowPwr')
    rec = {**record_header([serial]), 'kind': 'timing', 'module': {k: info[k] for k in ('vendor', 'part_number', 'serial', 'cmis_revision')},
           'advertised_codes_01h': codes, 'steps': steps,
           'verdict': 'PASS' if all(s['within'] in (True, None) for s in steps) and any(s['within'] is True for s in steps) else
                      'FAIL' if any(s['within'] is False for s in steps) else 'INCONCLUSIVE (no step measured against a bound)'}
    say(f'timing: {rec["verdict"]}')
    if as_json:
        say(json.dumps(rec, indent=1))
    write_record(rec, out, f'timing_{serial}_{{}}.json', say)
    return rec


# --------------------------------------------------------------------------- flags
def cmd_flags(port, serial, out, budget_w, timeout, as_json=False, say=None):
    """Latched module flags clear on read (00h:8-11), the masks (00h:31-34) read back, IntL idles high with every flag masked,
    and with the LPMode permission: the ModuleStateChangedFlag of the ModuleReady entry pulls IntL low while unmasked and the
    read clears it. The masks are restored at the end."""
    say = say or _quiet
    info = read_id(port)
    checks = []

    def chk(name, ok, detail):
        checks.append({'check': name, 'result': 'NA' if ok is None else 'PASS' if ok else 'FAIL', 'detail': detail})
        say(f'  {"NA  " if ok is None else "PASS" if ok else "FAIL"} {name:<30} {detail}')

    if info['memory_model'] == 'flat':
        chk('flat memory', None, 'no flags or masks on a flat-memory module')
        rec = {**record_header([serial]), 'kind': 'flags', 'checks': checks, 'verdict': 'NA'}
        write_record(rec, out, f'flags_{serial}_{{}}.json', say)
        return rec
    with port.selected():
        masks0 = port.read(L_MASKS, L_MASK_COUNT)
        f1 = port.read(L_FLAGS8, 4)
        f2 = port.read(L_FLAGS8, 4)
    chk('masks read', True, f'00h:31-34 = {masks0.hex()} (restored at the end)')
    chk('flags clear on read', not any(f2), f'first read 00h:8-11 = {f1.hex()}, second = {f2.hex()}')
    try:
        with port.selected():
            port.write(L_MASKS, b'\xff' * L_MASK_COUNT)
            back = port.read(L_MASKS, L_MASK_COUNT)
        chk('masks writable', back == b'\xff' * L_MASK_COUNT, f'wrote ff ff ff ff, read {back.hex()}')
        chk('IntL high when all masked', not port.sense()['interrupt'], f'sense {port.sense()}')
        if not port.allow_lp_low:
            chk('state-change flag drives IntL', None, 'needs --release-lpmode --i-am-on-usbc-power')
        else:
            with port.selected():
                port.write(L_MASKS, bytes([0xFF & ~MASK_STATE_CHANGED, 0xFF, 0xFF, 0xFF]))  # unmask ModuleStateChanged only
            release_lpmode(port, budget_w, timeout, say=say)  # ModuleReady entry is module-initiated: the flag sets (Table 6-9)
            try:
                time.sleep(0.2)
                intl_low = port.sense()['interrupt']
                with port.selected():
                    f3 = port.read(L_FLAGS8, 1)[0]
                time.sleep(0.05)
                intl_after = port.sense()['interrupt']
                chk('state-change flag set', bool(f3 & MASK_STATE_CHANGED), f'00h:8 = 0x{f3:02x} after ModuleReady')
                chk('IntL low while unmasked', intl_low, 'IntL read low with 31.0 unmasked and the flag pending')
                chk('IntL high after the read', not intl_after, 'the read of 00h:8 cleared the flag and released IntL')
            finally:
                port.set_lpmode(1)
                wait_state(port, ST_LOWPWR, timeout, say)
    finally:
        with port.selected():
            port.write(L_MASKS, masks0)
            back = port.read(L_MASKS, L_MASK_COUNT)
        chk('masks restored', back == masks0, f'00h:31-34 = {back.hex()}')
    verdict = 'FAIL' if any(c['result'] == 'FAIL' for c in checks) else 'PASS'
    say(f'flags: {verdict}')
    rec = {**record_header([serial]), 'kind': 'flags', 'module': {k: info[k] for k in ('vendor', 'part_number', 'serial', 'cmis_revision')},
           'checks': checks, 'verdict': verdict}
    if as_json:
        say(json.dumps(rec, indent=1))
    write_record(rec, out, f'flags_{serial}_{{}}.json', say)
    return rec


# --------------------------------------------------------------------------- i2c stress
def cmd_i2c_stress(port, serial, count, out, as_json=False, say=None):
    """N rounds of lower + 00h (+ 01h / 10h / 11h when paged): NACKs, page-select read-back mismatches and the time per 128-byte
    read; then the ModSelL test: a read with ModSelL high must NACK (the module answers only while selected, [MSA] 4.5)."""
    say = say or _quiet
    info = read_id(port)
    flat = info['memory_model'] == 'flat'
    pages = [0x00] if flat else [0x00, 0x01, 0x10, 0x11]
    times, nacks, mismatches, reads = [], 0, 0, 0
    t_all = time.monotonic()
    for _ in range(count):
        try:
            with port.selected():
                t0 = time.monotonic()
                port.lower()
                times.append(time.monotonic() - t0)
                reads += 1
                for pg in pages:
                    t0 = time.monotonic()
                    try:
                        port.read_page(pg, flat=flat)
                    except Nack as e:
                        if 'read-back' in str(e):
                            mismatches += 1
                        raise
                    times.append(time.monotonic() - t0)
                    reads += 1
        except Nack:
            nacks += 1
    elapsed = time.monotonic() - t_all
    port.pin(MODSELL, 1)  # deselect test: no selected() context, ModSelL high
    try:
        port.read(0, 1)
        deselect_nack = False
    except Nack:
        deselect_nack = True
    ms = [t * 1e3 for t in times]
    rec = {**record_header([serial]), 'kind': 'i2c-stress', 'module': {k: info[k] for k in ('vendor', 'part_number', 'serial', 'cmis_revision')},
           'rounds': count, 'pages': [f'{p:02X}h' for p in pages], 'reads_128b': reads, 'nacks': nacks, 'page_readback_mismatches': mismatches,
           'read_ms': {'min': round(min(ms), 2), 'median': round(statistics.median(ms), 2), 'max': round(max(ms), 2)} if ms else None,
           'bytes_per_s': round(reads * 128 / elapsed) if elapsed else None, 'i2c_hz': port.be.hz,
           'deselected_read_nacks': deselect_nack,
           'verdict': 'PASS' if nacks == 0 and mismatches == 0 and deselect_nack else 'FAIL'}
    say(f'i2c-stress: {reads} reads of 128 bytes in {count} rounds, {nacks} NACK round(s), {mismatches} page read-back mismatch(es), '
        f'{rec["read_ms"]["median"] if ms else "n/a"} ms median per read, {rec["bytes_per_s"]} B/s; deselected read NACKs: {deselect_nack} -> {rec["verdict"]}')
    if as_json:
        say(json.dumps(rec, indent=1))
    write_record(rec, out, f'i2c_stress_{serial}_{{}}.json', say)
    return rec
