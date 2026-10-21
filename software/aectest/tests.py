"""tests.py - the test primitives.

Lines l.543-582, 610-720 and 731-1238 of the bring-up tool, one mechanical change: every `print(...)` became `say(...)` and
every primitive takes `say=None`. With no `say` a primitive prints nothing; it returns its dict (or the number / stop
reason it returned before) and raises Refused / Nack / PrbsAbort as before. `cli.say` prints to stdout, `say(text,
err=True)` to stderr, so `aectest bringup` prints what the tool printed. The 2.4 names (`identity`, `health`, `hold`,
`prbs_pair`, ...) are aliases of the `cmd_*` bodies; `on_sample` and the three pair primitives came with the technician flow.
"""
import csv
import json
import time
from datetime import datetime, timezone

from .cmis import (CAP_PERIODIC, LOOP_MEDIA_IN, P13_MLOOP_IN, REP_SNR_MEDIA, SEL_SNR, CMIS_MIN_REV, CTRL_LOWPWR_ALLOW_HW, CTRL_LOWPWR_SW, CTRL_RESET_ERR, DP_ACTIVATED, DP_STATES,
                   L_CTRL, L_FLAGS9, L_MEM, L_REV, L_STATE, L_TEMP, L_VCC, LOC_CHK_POST, LOC_CHK_PRE, LOC_GEN_POST, LOC_GEN_PRE,
                   LOC_MEDIA_CHK, LOC_MEDIA_GEN, MON_TEMP, MON_VCC, P1_DIAG_BIT, P1_DURATIONS, P1_PAGE03_BIT, P1_PAGES, P10_DPDEINIT,
                   P11_DPSTATE, P13_CAP_MEAS, P13_CAP_REP, P13_CTRL, P13_LOC, P13_MCHK_EN, P13_MCHK_PAT, P13_MCHK_POSTFEC,
                   P13_MCHK_SEL, P13_MGEN_EN, P13_MGEN_PAT, P13_MGEN_PREFEC, P13_MGEN_SEL, P14_CHK_LOL_MEDIA, P14_DATA, P14_MEDIA_BER,
                   P14_SEL, PATTERNS, REP_BER, REP_COUNTERS, SEL_BER, SEL_MEDIA_CNT, ST_FAULT, ST_LOWPWR, ST_READY, STATES,
                   TEMP_ALARM_FLAG, TEMP_HIGH_FLAGS, TEMP_SAMPLE_RANGE_C, TEMP_THR_RANGE_C, TEMP_WARN_FLAG, VCC_ALARM_FLAGS,
                   VCC_WARN_FLAGS, decode_duration, decode_flags, decode_id, decode_thresholds, f16, read_id, read_release_advert)
from .fru import summary as _fru_summary
from .records import CSV_HAND_COLS, open_new, record_header, write_record
from .safety import FORCED_AIR_MSG, JIG_TEMP_LIMIT_C, MAN_HINT, POSTURE_WARN, S10_CHECK, THROTTLE_MARGIN_C, budget_figure, posture_limit
from .transport import LPMODE, T_DDCS_S, T_MGMT_INIT_S, T_MODSEL_S, T_RESET_S, FakeBackend, Nack, Refused


def _quiet(text, err=False):
    """The default renderer: a primitive prints nothing (2.4)."""


# --------------------------------------------------------------------------- commands
def show(d, as_json, say, width=22):
    if as_json:
        say(json.dumps(d, indent=1))
    else:
        for k, v in d.items():
            say(f'{k:{width}} {v}')


PRESENCE_HINT = ('module_present comes from ModPrsL alone: with SW_PWR OFF the bled pull-ups read "present" too '
                 '(no rail sense) -- PWR LED lit / SW_PWR ON?')  # review R6-W06


HP_UNPOLLED = ('module in {} while LPMode(drv) = 1 -- it ignores the pin (00h:26.6 = 0) or was released elsewhere, so it runs '
               'UNPOLLED (no thermal stop). Recovery: `release-lpmode --i-am-on-usbc-power` polls it (or writes 26.4 = 1 '
               'on the 26.6 refusal path), else `tristate` + SW_PWR OFF/ON (volatile bits reset)')


def warn_hp_unpolled(port, state_name, say=None):
    """A module in ModulePwrUp / ModuleReady while the host drives LPMode high is in high power with nothing watching it."""
    say = say or _quiet
    if state_name in ('ModulePwrUp', 'ModuleReady') and port.val & LPMODE:
        say('WARN: ' + HP_UNPOLLED.format(state_name), err=True)
        return True
    return False


def fru_summary(port):
    """'rev 0, board ed9431d7, order ...' from the EEPROM board area read at open, 'none' without one."""
    return _fru_summary(getattr(port.be, 'fru', None))


def cmd_status(port, as_json=False, say=None):
    say = say or _quiet
    s = port.sense()
    show(s, as_json, say, 16)
    if not as_json:
        say(f'{"fru":16} {fru_summary(port)}')
    if s['module_present']:
        say(PRESENCE_HINT, err=True)
    return s


def cmd_id(port, as_json=False, say=None):
    d = read_id(port)
    show(d, as_json, say or _quiet)
    warn_hp_unpolled(port, d['module_state'], say)
    return d


def build_dump(port, serial):
    with port.selected():
        lower = port.lower()
        flat = bool(lower[L_MEM] & 0x80)
        pages = {'00h': port.read_page(0, flat=flat)}
        if not flat:  # paged memory: 01h, 10h, 11h mandatory ([CMIS] Table 8-5, §8.10 note), 02h "mandatory for paged memory
            for pg in (0x01, 0x02, 0x10, 0x11):
                pages[f'{pg:02X}h'] = port.read_page(pg, bank=0)
            if pages['01h'][P1_PAGES - 128] & P1_PAGE03_BIT:
                pages['03h'] = port.read_page(0x03, bank=0)
    decoded = decode_id(lower, pages['00h'])
    if '02h' in pages:
        decoded['thresholds_02h'] = decode_thresholds(pages['02h'])
        decoded['module_pwrup_advertised_01h_167'] = decode_duration(pages['01h'][P1_DURATIONS - 128] & 0xF)[0]
    d = {**record_header([serial]), 'ftdi_serial': serial, 'i2c_hz': port.be.hz,
         'register_source': 'OIF-CMIS-05.2', 'fru': fru_summary(port), 'gpio': port.sense(),
         'decoded': decoded, 'lower': lower.hex(), 'pages': {k: v.hex() for k, v in pages.items()},
         'flags': {f'00h:{L_FLAGS9} module flags (Vcc/Temp alarms+warnings)': {
             'value': f'0x{lower[L_FLAGS9]:02x}', 'status': 'ok' if not lower[L_FLAGS9] else 'CHECK'}}}
    if '11h' in pages:
        d['flags'].update(decode_flags(pages['11h']))
    return d


def cmd_dump(port, serial, out, say=None):
    """Default file name dump_<serial>_<UTC µs>.json (test plan T6), never overwritten; `--out -` prints to stdout."""
    d = build_dump(port, serial)
    write_record(d, out, f'dump_{serial}_{{}}.json', say)
    warn_hp_unpolled(port, d['decoded']['module_state'], say)
    return d


def cmd_watch(port, timeout, say=None):
    """Wait for ModPrsL to fall, then poll byte 3 until the first ACK; print ms."""
    say = say or _quiet
    if port.sense()['module_present']:
        say(f'module already present -- remove it; waiting up to {timeout:g} s for ModPrsL to go HIGH first\n  ({PRESENCE_HINT})')
        t0 = time.monotonic()
        while port.sense()['module_present']:
            if time.monotonic() - t0 > timeout:
                say('ABORT: module was not removed (ModPrsL stayed low)')
                return None
            time.sleep(0.005)
    say('waiting for insertion (ModPrsL falling edge, Ctrl-C to abort)')
    try:
        while not port.sense()['module_present']:
            time.sleep(0.001)
    except KeyboardInterrupt:
        say('')
        return None
    t0 = time.monotonic()
    with port.selected():
        while True:
            try:
                st = port.read(L_STATE, 1)[0]
                break
            except Nack:
                if time.monotonic() - t0 > T_MGMT_INIT_S:
                    say(f'FAIL: no ACK within MgmtInitDuration {T_MGMT_INIT_S:.1f} s after ModPrsL fell')
                    return None
                time.sleep(0.005)
    dt = time.monotonic() - t0
    say(f'ModPrsL fell -> first ACK after {dt * 1e3:.0f} ms (limit {T_MGMT_INIT_S * 1e3:.0f} ms; includes '
          f'{T_MODSEL_S * 1e3:.0f} ms ModSelL setup + USB polling latency -- the LA capture is the reference); '
          f'byte 3 = 0x{st:02x} {STATES.get((st >> 1) & 7, "reserved")}')
    warn_hp_unpolled(port, STATES.get((st >> 1) & 7), say)
    return dt


def cmd_reset(port, say=None):
    say = say or _quiet
    say(f'note: valid in SW_MODE = AUTO only -- in MAN the FTDI ResetL never reaches the pad, the first read ACKs at '
          f'once and the number below is meaningless ({MAN_HINT})')  # review R6-W24
    port.reset_pulse()
    t0 = time.monotonic()
    with port.selected():
        while True:
            try:
                st = port.read(L_STATE, 1)[0]
                break
            except Nack:
                if time.monotonic() - t0 > T_MGMT_INIT_S:
                    say(f'FAIL: no ACK within MgmtInitDuration {T_MGMT_INIT_S:.1f} s after ResetL rising edge')
                    return None
                time.sleep(0.02)
    dt = time.monotonic() - t0
    say(f'ResetL pulse {T_RESET_S * 1e3:.0f} ms -> first ACK after {dt * 1e3:.0f} ms '
          f'(limit {T_MGMT_INIT_S * 1e3:.0f} ms, includes {T_MODSEL_S * 1e3:.0f} ms ModSelL setup + USB latency); '
          f'byte 3 = 0x{st:02x} {STATES.get((st >> 1) & 7, "reserved")}'
          + (' -- suspiciously fast: SW_MODE = MAN? the override stage isolates ResetL -- no module reset happened (or the module '
             'ignored the pulse)' if dt < 0.01 else ''))
    return dt


def wait_state(port, want, timeout, say=None):
    say = say or _quiet
    t0 = time.monotonic()
    last = None
    while True:
        with port.selected():
            try:
                st = (port.read(L_STATE, 1)[0] >> 1) & 7
            except Nack:
                st = None
        if st != last:
            say(f'  +{time.monotonic() - t0:6.2f} s  module state {STATES.get(st, st)}')
            last = st
        if st == want:
            return True
        if st == ST_FAULT or time.monotonic() - t0 > timeout:  # ModuleFault never recovers by waiting (R6-W08)
            return False
        time.sleep(0.1)


def release_lpmode(port, budget_w, timeout, assume_w=None, posture=None, ambient_c=25, temp_limit_c=JIG_TEMP_LIMIT_C,
                   temp_warn_c=None, temp_throttle_c=None, external_temp_ack=False, say=None):
    """Pre-flight, S4 budget gate, S1 posture gate (opt-in, informational), S3 thresholds/durations -> the WARN /
    THROTTLE levels, then LPMode low and wait for ModuleReady within max(--timeout, advertised MaxDurationModulePwrUp).
    Returns the id dict + `gate`, `thresholds_02h`, `temp_warn_c`, `temp_stop_c` (= THROTTLE), `module_pwrup_advertised_01h_167`,
    `module_ready_observed_s`, `module_ready_within_advertised`."""
    say = say or _quiet
    info = read_id(port)
    say(f'module: {info["vendor"]} {info["part_number"]} class {info["power_class"]} '
          f'(MSA max {info["power_class_msa_max_w"]} W), MaxPower {info["max_power_w"]:.2f} W, state {info["module_state"]}')
    ctrl, flags9 = info['module_ctrl_byte26'], info['module_flags_byte9']
    if not ctrl & CTRL_LOWPWR_ALLOW_HW:
        with port.selected():
            port.write(L_CTRL, bytes([ctrl | CTRL_LOWPWR_SW]))
        low = wait_state(port, ST_LOWPWR, T_MGMT_INIT_S, say)
        raise Refused('00h:26.6 LowPwrAllowRequestHW = 0: this module ignores the LPMode pin (26.4 controls it) -- not driving it; '
                      f'wrote 00h:26.4 LowPwrRequestSW = 1 -> {"ModuleLowPwr confirmed" if low else "module did NOT reach ModuleLowPwr (SW_PWR OFF/ON resets the volatile bits)"}')
    if ctrl & CTRL_LOWPWR_SW:
        raise Refused('00h:26.4 LowPwrRequestSW = 1: the module would stay in ModuleLowPwr with LPMode low -- clear it first')
    if flags9 & (TEMP_HIGH_FLAGS | VCC_ALARM_FLAGS):
        raise Refused(f'pre-flight 00h:9 = 0x{flags9:02x}: a TEMP/VCC alarm was latched before release (this read cleared '
                      f'it -- run `id` again; if it returns it is real, do not release)')
    watts, src = budget_figure(info, assume_w)
    if watts is None:
        raise Refused('power class 8 with MaxPower (00h:201) = 0: no power figure to gate on (S4) -- --assume-w W to state one')
    say(f'S4 budget gate: {watts:.2f} W from {src} vs --budget-w {budget_w:.2f} W')
    if watts > budget_w:
        raise Refused(f'{src} {watts:.2f} W > budget {budget_w:.2f} W '
                      f'(MINI_BRIEF §1: a 3 A USB-C port gives ~6 W at 3.3 V; override with --budget-w)')
    limit = posture_limit(posture, ambient_c)
    if limit is None:
        say(POSTURE_WARN)
    else:
        say(f'S1 posture gate: {watts:.2f} W vs {posture} at {ambient_c} C = {limit:.2f} W (the posture table, informational)')
        if watts > limit:
            raise Refused(f'POSTURE-OVER-CLASS: {src} {watts:.2f} W > {posture} limit {limit:.2f} W at {ambient_c} C -- '
                          f'change the posture (hood off / fan / forced air), not the flag')
    info['gate'] = {'figure_w': watts, 'source': src, 'budget_w': budget_w, 'posture': posture, 'posture_limit_w': limit,
                    'ambient_c': ambient_c}
    thr, dur, mon = read_release_advert(port, info['memory_model'] == 'flat')  # S3: once, before the release
    thr = thr or {}
    if mon is not None:
        if not mon & MON_TEMP and not external_temp_ack:
            raise Refused(f'01h:159 TempMonSupported = 0 (0x{mon:02x}): no temperature monitor -> the WARN / THROTTLE path can never fire; '
                          '--external-temp-ack with a thermocouple on the sink (hand columns of the log) states that a human watches it')
        if not mon & MON_VCC:
            say(f'WARN: 01h:159 VccMonSupported = 0 (0x{mon:02x}): the S5 VccMon alarm stop cannot fire on this module', err=True)
        if mon & MON_TEMP:
            bad = [f'{k} {v}' for k in ('temp_high_alarm_c', 'temp_high_warning_c') for v in [thr.get(k)]
                   if v is None or not TEMP_THR_RANGE_C[0] <= v <= TEMP_THR_RANGE_C[1]]
            if not TEMP_SAMPLE_RANGE_C[0] <= info['temp_c'] <= TEMP_SAMPLE_RANGE_C[1]:
                bad.append(f'TempMonValue {info["temp_c"]:.2f}')
            if bad:
                raise Refused(f'temperature figures implausible ({", ".join(bad)} C): 02h high thresholds must lie in {TEMP_THR_RANGE_C} C and the '
                              f'live 00h:14-15 sample in {TEMP_SAMPLE_RANGE_C} C -- a monitor that reads nonsense cannot throttle')
        info['monitors_01h_159'] = f'0x{mon:02x}'
    alarm = thr.get('temp_high_alarm_c')
    port.temp_warn_c = temp_warn_c if temp_warn_c is not None else thr.get('temp_high_warning_c')
    throttle = temp_throttle_c if temp_throttle_c is not None else (alarm - THROTTLE_MARGIN_C if alarm is not None else None)
    stops = [t for t in (throttle, temp_limit_c) if t is not None]
    port.temp_stop_c = min(stops) if stops else None
    warn_src = '--temp-warn-c' if temp_warn_c is not None else '02h:132-133 TempMonHighWarningThreshold'
    thr_src = '--temp-throttle-c' if temp_throttle_c is not None else f'02h:128-129 TempMonHighAlarmThreshold {alarm} - {THROTTLE_MARGIN_C:g}'
    text, adv_max = decode_duration(dur & 0xF) if dur is not None else ('n/a (flat memory, no Page 01h)', None)
    wait_s = max(timeout, adv_max or 0.0)
    info.update({'thresholds_02h': thr or None, 'temp_warn_c': port.temp_warn_c, 'temp_stop_c': port.temp_stop_c,
                 'module_pwrup_advertised_01h_167': text})
    say(f'S3 thermal levels: WARN at {port.temp_warn_c} C ({warn_src}; run continues), THROTTLE at {port.temp_stop_c} C = '
          f'min({thr_src}, --temp-limit-c {temp_limit_c}) = LPMode high, test stopped, INCONCLUSIVE: THERMAL; byte-9 bit 2 = WARN, '
          f'bit 0 = THROTTLE; ModulePwrUp advertised 01h:167 {text} -> waiting up to {wait_s:g} s for ModuleReady')
    say(S10_CHECK)
    port.set_lpmode(0)  # LowPwrRequestHW deasserted; LowPwrRequestSW (26.4) left at module default
    say('LPMode driven LOW -> waiting for ModuleReady')
    t0 = time.monotonic()
    if not wait_state(port, ST_READY, wait_s, say):
        port.set_lpmode(1)
        raise Refused(f'module did not reach ModuleReady within {wait_s:g} s (advertised {text}); LPMode returned HIGH ({MAN_HINT}; '
                      f'or ModuleFault / LowPwrRequestSW 00h:26.4 set)')
    dt = time.monotonic() - t0
    info['module_ready_observed_s'] = round(dt, 3)
    info['module_ready_within_advertised'] = None if adv_max is None else dt <= adv_max
    say(f'ModuleReady after {dt:.2f} s; advertised {text}'
          + ('' if adv_max is None else (' -- within' if dt <= adv_max else ' -- SLOWER than advertised (record)')))
    return info


def cmd_idle(port, hold_s, say=None):
    """Enter MPSSE, set directions with the SAFE values, then just hold the port."""
    say = say or _quiet
    say(f'MPSSE configured, directions set: {port.sense()}')
    say('idle -- SW_MODE may go to AUTO now (Ctrl-C to exit; the chip stays in MPSSE with SAFE outputs)')
    t0 = time.monotonic()
    try:
        while hold_s is None or time.monotonic() - t0 < hold_s:
            time.sleep(0.2)
    except KeyboardInterrupt:
        say('')


def poll_health(port):
    """One S5 sample: lower bytes 3 (state), 9 (flags), 14-15 (TempMonValue), 16-17 (VccMonVoltage) + IntL + present (ModPrsL, 2.6)."""
    with port.selected():
        lo = port.read(0, 18)
    s = port.sense()
    return {'state': (lo[L_STATE] >> 1) & 7, 'state_name': STATES.get((lo[L_STATE] >> 1) & 7, 'reserved'),
            'temp_c': int.from_bytes(lo[L_TEMP:L_TEMP + 2], 'big', signed=True) / 256.0,
            'vcc_v': int.from_bytes(lo[L_VCC:L_VCC + 2], 'big') * 100e-6,  # [CMIS] Table 8-10: U16, 100 µV units
            'flags9': lo[L_FLAGS9], 'intl': s['IntL'], 'present': s['module_present']}


def thermal_level(h, warn_c=None, throttle_c=None):
    """Classify one health sample. ('THROTTLE', why) when 00h:9 bit 0 TempMonHighAlarmFlag is set or
    TempMonValue (00h:14-15, Table 8-10) >= throttle_c; ('WARN', why) when 00h:9 bit 2 TempMonHighWarningFlag is set or
    TempMonValue >= warn_c; None otherwise. Levels from release_lpmode() (Page 02h Table 8-55 or the --temp-*-c overrides)."""
    t = h['temp_c']
    if h['flags9'] & TEMP_ALARM_FLAG:
        return 'THROTTLE', 'TempMonHighAlarmFlag set (00h:9 bit 0)'
    if throttle_c is not None and t >= throttle_c:
        return 'THROTTLE', f'TempMonValue {t:.2f} C >= throttle {throttle_c:g} C'
    if h['flags9'] & TEMP_WARN_FLAG:
        return 'WARN', 'TempMonHighWarningFlag set (00h:9 bit 2)'
    if warn_c is not None and t >= warn_c:
        return 'WARN', f'TempMonValue {t:.2f} C >= warn {warn_c:g} C'
    return None


def stop_reason(h, port, warned=None, name='module', say=None):
    """S5 stop conditions while LPMode is low; None = keep going. Thermal: WARN
    is printed once per end (`warned` set) and the run continues; THROTTLE returns 'THERMAL: ...' and the caller asserts
    LPMode and records INCONCLUSIVE: THERMAL. A Vcc alarm or a state other than ModuleReady stop as before."""
    say = say or _quiet
    lvl = thermal_level(h, port.temp_warn_c, port.temp_stop_c)
    if lvl:
        h['thermal'] = lvl[0]
        if lvl[0] == 'THROTTLE':
            return f'THERMAL: {lvl[1]}'
        if warned is not None and name not in warned:
            warned.add(name)
            say(f'WARN {name}: {lvl[1]} -- run continues; THROTTLE (LPMode high, INCONCLUSIVE: THERMAL) at '
                  f'{port.temp_stop_c} C or the alarm flag. {FORCED_AIR_MSG}')
    if h['flags9'] & VCC_ALARM_FLAGS:
        return 'VccMon alarm set (00h:9 bits 5/4)'
    if h['state'] != ST_READY:
        return f'module state {h["state_name"]} (left ModuleReady)'
    return None


def health_line(h):
    return (f'state {h["state_name"]}  temp {h["temp_c"]:6.2f} C  vcc {h["vcc_v"]:.3f} V  flags9 0x{h["flags9"]:02x}  '
            f'IntL {h["intl"]}' + ('  (Vcc warning)' if h['flags9'] & VCC_WARN_FLAGS else '')
            + (f'  {h["thermal"]}' if h.get('thermal') else ''))


def log_meta(info, a, serial):
    """Provenance header of the per-run CSV: who, what, which gate figures, which acknowledgement."""
    g = info['gate']
    return {**record_header([serial]), 'module': f'{info["vendor"]} {info["part_number"]} SN {info["serial"]}',
            'max_power_w': info['max_power_w'], 'gate_figure': f'{g["figure_w"]} W from {g["source"]}', 'budget_w': g['budget_w'],
            'posture': g['posture'] or 'none (posture table not enforced -- warning only)', 'posture_limit_w': g['posture_limit_w'],
            'ambient_c': g['ambient_c'], 'temp_warn_c': info['temp_warn_c'], 'temp_stop_c (THROTTLE)': info['temp_stop_c'],
            'thresholds_02h': info['thresholds_02h'],
            'module_pwrup_advertised_01h_167': info['module_pwrup_advertised_01h_167'],
            'module_ready_observed_s': info['module_ready_observed_s'],
            'acknowledgement': '--release-lpmode --i-am-on-usbc-power given: operator states a 3 A charger is on PWR ONLY and the host on PC (S10)',
            'hand_columns': 'I (J_SH), V_pad (Kelvin), U_M / D101 / D102 / sink case temperatures (E14), TP107 V, MAN LED -- by hand every 30 s'}


def hold_high_power(port, hold_s, log=None, meta=None, logf=None, say=None):
    """Keep LPMode low while polling temperature/Vcc/state at 1 s: WARN once at the module's high-warning level, THROTTLE
    (LPMode high, stop, INCONCLUSIVE: THERMAL, forced-air line) at the THROTTLE level, stop on a Vcc alarm or the state leaving
    ModuleReady (S5). --log FILE writes a PER-RUN CSV (never appended): `# key: value`
    provenance lines, then t_s,state,temp_c,vcc_v,flags9,intl + the reserved hand columns at 1 s, and a
    `# stop:` / `# verdict:` trailer when a stop condition ended the hold. Returns the stop reason (None = hold elapsed)."""
    say = say or _quiet
    t0 = time.monotonic()
    warned, stop = set(), None
    say('holding high power (Ctrl-C to return to low power)')
    f = logf if logf is not None else (open_new(log) if log else None)
    if f:
        for k, v in (meta or {}).items():
            f.write(f'# {k}: {v}\n')
        f.write('t_s,state,temp_c,vcc_v,flags9,intl,' + CSV_HAND_COLS + '\n')
    try:
        while hold_s is None or time.monotonic() - t0 < hold_s:
            h = poll_health(port)
            say(f'  +{time.monotonic() - t0:6.1f} s  {health_line(h)}')
            if f:
                f.write(f'{time.monotonic() - t0:.1f},{h["state_name"]},{h["temp_c"]:.2f},{h["vcc_v"]:.4f},'
                        f'0x{h["flags9"]:02x},{h["intl"]}' + ',' * CSV_HAND_COLS.count(',') + ',\n')
                f.flush()
            stop = stop_reason(h, port, warned, say=say)
            if stop:
                if stop.startswith('THERMAL'):
                    say(f'THROTTLE: {stop} -> LPMode HIGH (low power), test stopped, INCONCLUSIVE: THERMAL. {FORCED_AIR_MSG}')
                else:
                    say(f'STOP: {stop} -> back to low power')
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        say('')
    except Nack as e:
        stop = f'NACK ({e})'
        say(f'STOP: {stop} -> back to low power')
    finally:
        port.set_lpmode(1)
        if f:
            if stop:
                verdict = 'INCONCLUSIVE: THERMAL' if stop.startswith('THERMAL') else 'STOPPED'
                f.write(f'# stop: {stop}\n# verdict: {verdict}\n')
            f.close()
        say('LPMode returned HIGH (low power)')
    return stop


class PrbsAbort(Exception):
    """Measurement gate stopped early (S5 stop condition, NACK, Ctrl-C)."""


def prbs_caps(port, role, pattern, force_cmis, into, say=None):
    """Capability + CMIS-revision gate for one end. Everything read lands in `into` BEFORE a refusal, so the JSON record
    carries the revision and the advertisement even when the run is refused."""
    say = say or _quiet
    pat = PATTERNS[pattern]
    with port.selected():
        rev = port.read(L_REV, 1)[0]
        into['cmis_revision'] = f'{rev >> 4}.{rev & 15}'
        if rev < CMIS_MIN_REV and not force_cmis:
            raise Refused(f'{role}: CMIS {rev >> 4}.{rev & 15} < 5.0 -- the Page 13h/14h writes below use OIF-CMIS-05.2 '
                          f'layouts; --force-cmis to proceed anyway (R6-W14)')
        into['p01_142'] = port.read_page(0x01)[P1_PAGES - 128]
        if not into['p01_142'] & P1_DIAG_BIT:
            raise Refused(f'{role}: diagnostic pages 13h/14h not advertised (01h:142.5)')
        p13 = port.read_page(0x13)
    cap = P13_MGEN_PAT if role == 'generator' else P13_MCHK_PAT  # Tables 8-99 / 8-100
    into.update({'p13_129': p13[P13_CAP_MEAS - 128], 'p13_130': p13[P13_CAP_REP - 128], 'p13_131': p13[P13_LOC - 128],
                 f'p13_{cap}_{cap + 1}': p13[cap - 128] | p13[cap - 127] << 8})
    if rev < CMIS_MIN_REV:
        into['cmis_gate'] = 'WARNING: CMIS < 5.0 with 13h/14h advertised -- OIF-CMIS-05.2 layouts assumed (--force-cmis, R6-W14)'
        say(f'{role}: {into["cmis_gate"]}')
    need = LOC_MEDIA_GEN if role == 'generator' else LOC_MEDIA_CHK
    if not p13[P13_LOC - 128] & need:
        raise Refused(f'{role}: media-side {role} not advertised (13h:131 = 0x{p13[P13_LOC - 128]:02x})')
    if not into[f'p13_{cap}_{cap + 1}'] >> pat & 1:
        raise Refused(f'{role}: pattern {pattern} (ID {pat}) not in 13h:{cap}-{cap + 1}')
    if role == 'checker' and not p13[P13_CAP_REP - 128] & (REP_BER | REP_COUNTERS):
        raise Refused(f'{role}: neither BER (13h:130.0) nor error counters (13h:130.1) advertised -- nothing to read')
    return into


def fec_placement(gen_loc, chk_loc):
    """Pick one advertised generator/checker placement pair (Table 8-97): the raw line pattern (generator after the FEC
    encoder, checker before the decoder) when both ends offer it, else the FEC-encoded path. Returns (155, 171, label)."""
    if gen_loc & LOC_GEN_POST and chk_loc & LOC_CHK_PRE:
        return 0x00, 0x00, 'raw pattern on the line (gen post-FEC-encoder 131.6, chk pre-FEC-decoder 131.5)'
    if gen_loc & LOC_GEN_PRE and chk_loc & LOC_CHK_POST:
        return 0xFF, 0xFF, 'FEC-encoded pattern (gen pre-FEC-encoder 131.7, chk post-FEC-decoder 131.4)'
    raise Refused(f'no matching generator/checker placement: 13h:131 gen 0x{gen_loc:02x} vs chk 0x{chk_loc:02x}')


def dp_states(port):
    with port.selected():
        b = port.read_page(0x11)[P11_DPSTATE - 128:P11_DPSTATE - 128 + 4]
    return [(b[i // 2] >> (4 * (i % 2))) & 0xF for i in range(8)]


def ensure_dp_active(port, name, timeout, say=None):
    """After ModuleReady: 11h:128-131 must show DPActivated; if not, clear DPDeinitLane (10h:128 = 0, Table 8-61) and
    wait. Returns the final per-lane codes (review R6-W09); the caller marks INCONCLUSIVE when any lane is not active."""
    say = say or _quiet
    codes = dp_states(port)
    if all(c == DP_ACTIVATED for c in codes):
        return codes
    say(f'{name}: DP states {[DP_STATES.get(c, c) for c in codes]} -> writing 10h:128 DPDeinitLane1-8 = 0')
    with port.selected():
        port.page(0x10)
        port.write(P10_DPDEINIT, b'\x00')
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        codes = dp_states(port)
        if all(c == DP_ACTIVATED for c in codes):
            break
        time.sleep(0.2)
    say(f'{name}: DP states {[DP_STATES.get(c, c) for c in codes]}')
    return codes


def read_selector(port, sel):
    """Write 14h:128, wait tDDCS, read it back (Table 8-118: reverts to 0 when unsupported) and the 64-byte data area."""
    port.page(0x14)
    port.write(P14_SEL, bytes([sel]))
    time.sleep(T_DDCS_S)
    back = port.read(P14_SEL, 1)[0]
    return back, port.read(P14_DATA, 64)


def prbs_setup(p, name, place_off, place_val, sel_off, sel, en_off, mask, res):
    """Write placement / pattern select / enable for one end, read the five+one bytes back (155-159 and 152, or
    171-175 and 168 -- contiguous) and abort BEFORE timing when the module did not take them."""
    pat = sel[0] & 0xF
    with p.selected():
        p.page(0x13)
        p.write(place_off, bytes([place_val]))
        p.write(sel_off, sel)
        p.write(en_off, bytes([mask]))
        back = p.read(place_off, 5)
        en = p.read(en_off, 1)[0]
    pats = [(back[1 + i // 2] >> (4 * (i % 2))) & 0xF for i in range(8)]
    res['readback'][name] = {f'13h:{place_off}': f'0x{back[0]:02x}', f'13h:{sel_off}-{sel_off + 3}_pattern_ids': pats,
                             f'13h:{en_off}': f'0x{en:02x}'}
    if en != mask:
        raise PrbsAbort(f'{name}: 13h:{en_off} read back 0x{en:02x}, wanted lane mask 0x{mask:02x} -- enable rejected')
    bad = [i + 1 for i in range(8) if mask >> i & 1 and pats[i] != pat]
    if bad:
        raise PrbsAbort(f'{name}: 13h:{sel_off}-{sel_off + 3} pattern select read back {pats} != ID {pat} on lanes {bad}')
    if (back[0] ^ place_val) & mask:
        raise PrbsAbort(f'{name}: 13h:{place_off} placement read back 0x{back[0]:02x}, wrote 0x{place_val:02x}')
    return en


PRBS_SETTLE_S = 2.0


def prbs_direction(gen, chk, gname, cname, pattern, seconds, caps_gen, caps_chk, mask=0xFF, say=None):
    """One media-side PRBS run generator -> checker with the S5 poll gate and the validity checks of R6-W03/W04/W15 +
    Validity: read-backs before timing, lane mask, >= 1 health sample per end, non-zero TotalBits, cleanup read-back.
    Returns the result dict (verdict MEASURED / INCONCLUSIVE: ... / ABORTED: ...); both ends' gen/chk are disabled
    and 13h:177.5 returned to 0 in every exit path (LPMode is the caller's)."""
    say = say or _quiet
    pat = PATTERNS[pattern]
    sel = bytes([pat | pat << 4]) * 4  # two 4-bit pattern IDs per byte, lanes 1-8 (Tables 8-105/8-109)
    g155, c171, place = fec_placement(caps_gen['p13_131'], caps_chk['p13_131'])
    periodic = bool(caps_chk['p13_129'] & CAP_PERIODIC)
    gating = (caps_chk['p13_129'] >> 6) & 3  # Table 8-95 GatingSupport: 0 = host-defined measurement time
    lanes = [i for i in range(8) if mask >> i & 1]
    res = {'generator': gname, 'checker': cname, 'pattern': pattern, 'seconds': seconds, 'placement': place,
           'lane_mask': f'0x{mask:02x}', 'checker_periodic_updates_13h_129_4': periodic, 'readback': {}, 'cleanup': {},
           'measurement_mode': ('host-timed window, TotalBits is the interval (GatingSupport 13h:129.7-6 = 0)' if gating == 0 else
                                f'host-timed window; the module offers gating with accuracy code {gating} (not used)'),
           'thermal_levels_c': {n: {'warn': p.temp_warn_c, 'throttle': p.temp_stop_c} for p, n in ((gen, gname), (chk, cname))},
           'health': {gname: [], cname: []}, 'verdict': None}
    warned = set()
    say(f'{pattern} media-side {gname} -> {cname}: {place}; lanes 0x{mask:02x}; checker real-time updates during '
          f'measurement: {periodic}')
    try:
        prbs_arm(gen, chk, gname, cname, sel, g155, c171, mask, res)
        t0 = time.monotonic()
        say(f'measuring {seconds:.0f} s, polling both ends every 1 s (S5; Ctrl-C aborts)')
        try:
            while time.monotonic() - t0 < seconds:
                for p, name in ((gen, gname), (chk, cname)):
                    h = poll_health(p)
                    reason = stop_reason(h, p, warned, name, say)  # tags h['thermal'] before the sample is recorded
                    res['health'][name].append({'t_s': round(time.monotonic() - t0, 1), **h})
                    say(f'  +{time.monotonic() - t0:6.1f} s  {name}: {health_line(h)}')
                    if reason:
                        if reason.startswith('THERMAL'):
                            for q in (gen, chk):
                                q.set_lpmode(1)
                        raise PrbsAbort(f'{name}: {reason}')
                time.sleep(max(0.0, min(1.0, seconds - (time.monotonic() - t0))))
        except KeyboardInterrupt:
            say('')
            raise PrbsAbort('Ctrl-C') from None
        except Nack as e:
            raise PrbsAbort(f'NACK during the gate ({e})') from None
        problems = [f'no health sample from {n} (gate {seconds:g} s)' for n in (gname, cname) if not res['health'][n]]
        read_media_stats(gen, chk, gname, cname, caps_chk, lanes, res, problems, say)
        assumed = ' (CMIS<5.0 layouts assumed)' if 'cmis_gate' in caps_gen or 'cmis_gate' in caps_chk else ''
        res['verdict'] = 'MEASURED' + assumed if not problems else 'INCONCLUSIVE: ' + '; '.join(problems)
    except PrbsAbort as e:
        if 'THERMAL:' in str(e):
            res['verdict'] = f'INCONCLUSIVE: THERMAL ({e})'
            say(f'THROTTLE: {e} -> LPMode HIGH, PRBS stopped, INCONCLUSIVE: THERMAL. {FORCED_AIR_MSG}')
        else:
            res['verdict'] = f'ABORTED: {e}'
    except Nack as e:
        res['verdict'] = f'ABORTED: NACK ({e})'
    finally:
        prbs_disable(gen, chk, gname, cname, res, say)
        say(f'{gname} -> {cname}: {res["verdict"]}')
    return res


def prbs_arm(gen, chk, gname, cname, sel, g155, c171, mask, res):
    """Generator and checker set up and read back, the checker counters reset, the lock settle, the latched LOL cleared
    (recorded in res) and the counters reset again. Shared by prbs_direction and cmd_trend."""
    en = prbs_setup(gen, gname, P13_MGEN_PREFEC, g155, P13_MGEN_SEL, sel, P13_MGEN_EN, mask, res)
    res['gen_enable_readback'] = f'0x{en:02x}'
    en = prbs_setup(chk, cname, P13_MCHK_POSTFEC, c171, P13_MCHK_SEL, sel, P13_MCHK_EN, mask, res)
    res['chk_enable_readback'] = f'0x{en:02x}'
    with chk.selected():
        chk.page(0x13)
        ctrl = chk.read(P13_CTRL, 1)[0]
        chk.write(P13_CTRL, bytes([ctrl | CTRL_RESET_ERR]))   # 0->1 freeze ...
        chk.write(P13_CTRL, bytes([ctrl & ~CTRL_RESET_ERR]))  # ... 1->0 reset error counters (Table 8-110)
    time.sleep(0.0 if isinstance(chk.be, FakeBackend) else PRBS_SETTLE_S)
    with chk.selected():
        chk.page(0x14)
        lol0 = chk.read(P14_CHK_LOL_MEDIA, 1)[0]  # latched since checker enable (Table 8-120): clear it, record it
        chk.page(0x13)
        chk.write(P13_CTRL, bytes([ctrl | CTRL_RESET_ERR]))   # drop the errors counted while locking
        chk.write(P13_CTRL, bytes([ctrl & ~CTRL_RESET_ERR]))
    res['media_lol_before_gate_14h_139'] = f'0x{lol0:02x}'


def read_media_snr(chk):
    """Selector 06h (Table 8-119): media-side input SNR per lane in dB, with the selector read-back (0 = not supported).
    Call inside chk.selected()."""
    back, data = read_selector(chk, SEL_SNR)
    off = P14_MEDIA_BER - P14_DATA  # the media lanes sit at the same data offset as their BER values (+16..31)
    return back, [int.from_bytes(data[off + 2 * i:off + 2 * i + 2], 'little') / 256.0 for i in range(8)]


def read_media_stats(gen, chk, gname, cname, caps_chk, lanes, res, problems, say):
    """End of a gate: freeze, the media BER (01h) and counters (04h/05h) at the checker, the latched LOL, the raw pages of
    both ends, one printed line per lane. Shared by prbs_direction and cmd_trend. Returns the LOL byte."""
    with chk.selected():
        chk.page(0x13)
        ctrl = chk.read(P13_CTRL, 1)[0]
        chk.write(P13_CTRL, bytes([ctrl | CTRL_RESET_ERR]))  # 0->1 freeze: with 129.4 = 0 the data is not valid before
        time.sleep(T_DDCS_S)
        if caps_chk['p13_130'] & REP_BER:
            back, data = read_selector(chk, SEL_BER)
            res['selector_01h_readback'] = back
            if back == SEL_BER:
                ber = data[P14_MEDIA_BER - P14_DATA:P14_MEDIA_BER - P14_DATA + 16]
                res['media_ber'] = [f16(ber[2 * i], ber[2 * i + 1]) for i in range(8)]
            else:
                problems.append(f'selector 01h reverted to {back} (BER area is all zeroes, Table 8-118)')
        if caps_chk['p13_130'] & REP_COUNTERS:
            errs, bits = [], []
            for s in SEL_MEDIA_CNT:
                back, data = read_selector(chk, s)
                res[f'selector_{s:02x}h_readback'] = back
                if back != s:
                    problems.append(f'selector {s:02x}h reverted to {back}')
                    continue
                for i in range(4):
                    errs.append(int.from_bytes(data[16 * i:16 * i + 8], 'little'))
                    bits.append(int.from_bytes(data[16 * i + 8:16 * i + 16], 'little'))
            if len(errs) == 8:
                res['media_errors'], res['media_bits'] = errs, bits
                if any(bits[i] & 1 for i in lanes):
                    problems.append('odd TotalBits counter = pattern sync loss during the gate (Table 8-121 note)')
                if any(bits[i] == 0 for i in lanes):
                    problems.append(f'TotalBits = 0 on lanes {[i + 1 for i in lanes if bits[i] == 0]} (checker not counting)')
        chk.page(0x14)
        lol = chk.read(P14_CHK_LOL_MEDIA, 1)[0]  # Table 8-120 (clear-on-read)
        res['media_lol_14h_139'] = f'0x{lol:02x}'
        lol_in = lol & sum(1 << i for i in lanes)
        if lol_in:
            problems.append(f'checker LOL on media lanes 0x{lol_in:02x}' + (f' (0x{lol:02x} with the lanes outside the mask)' if lol != lol_in else ''))
        res['raw_pages'] = {f'{cname}_14h': chk.read(128, 128).hex()}
        chk.page(0x13)                                                  # layouts are 5.2's -- re-decode later if 4.x differs)
        res['raw_pages'][f'{cname}_13h'] = chk.read(128, 128).hex()
    with gen.selected():
        gen.page(0x13)
        res['raw_pages'][f'{gname}_13h'] = gen.read(128, 128).hex()
    for lane in range(8):
        line = f'  media lane {lane + 1}:'
        if 'media_ber' in res:
            line += f' BER {res["media_ber"][lane]:.3e}'
        if 'media_errors' in res:
            line += f'  errors {res["media_errors"][lane]} / bits {res["media_bits"][lane]}'
        say(line + ('  LOL' if lol >> lane & 1 else '') + ('' if lane in lanes else '  (not in lane mask)'))
    return lol


def prbs_disable(gen, chk, gname, cname, res, say):
    """Every exit path: generator and checker enables to 0 (read back), 13h:177.5 back to 0 on the checker."""
    for p, name, en in ((gen, gname, P13_MGEN_EN), (chk, cname, P13_MCHK_EN)):
        try:
            with p.selected():
                p.page(0x13)
                p.write(en, b'\x00')
                back = p.read(en, 1)[0]
                if en == P13_MCHK_EN:
                    ctrl = p.read(P13_CTRL, 1)[0]
                    p.write(P13_CTRL, bytes([ctrl & ~CTRL_RESET_ERR]))  # 1->0: reset, ready for the next run
                    ctrl = p.read(P13_CTRL, 1)[0]
            res['cleanup'][name] = f'13h:{en} = 0x{back:02x}' + (f'; 13h:177 = 0x{ctrl:02x}' if en == P13_MCHK_EN else '')
            if en == P13_MCHK_EN and ctrl & CTRL_RESET_ERR and (res['verdict'] or '').startswith('MEASURED'):
                res['verdict'] = f'INCONCLUSIVE: 13h:177 still 0x{ctrl:02x} after clearing bit 5 (statistics stay frozen)'
            if back:
                say(f'cleanup: {name} 13h:{en} still 0x{back:02x} after writing 0 -- the pattern may still be running')
        except Nack as e:
            res['cleanup'][name] = f'NACK ({e})'
            say(f'cleanup: NACK disabling 13h:{en} on {name} ({e})')


def select_application(port, name, apsel, timeout, say=None):
    """Stage application `apsel` on every host lane (10h:145-152 = code << 4, DataPathID 0, no explicit control), apply
    with ApplyDPInitLane (10h:143 = 0xFF, Table 8-63) and wait until ConfigStatusLane (11h:202-205, Table 8-84) reads
    ConfigSuccess (1) on every lane. Some 400G VR4 modules power up in their application 2 (4 host / 2 media
    lanes), so a 400G PRBS needs application 1 selected first. Returns the per-lane status codes."""
    say = say or _quiet
    with port.selected():
        lower = port.lower()
        hl = (lower[86 + 4 * (apsel - 1) + 2] >> 4) or 8  # host lanes of the application (Table 8-21)
        # DataPathID = the lowest host lane of each Data Path (Table 8-64, 0-based in the 3-bit field): an application
        # narrower than 8 lanes is staged as 8 // hl Data Paths; a single DPID 0 on all lanes is ConfigRejectedInvalidDataPath (4h),
        # seen on the Accelink VR4 for its 4-lane and 2-lane applications on 2026-10-20
        cfg = bytes([(apsel << 4) | (((i // hl) * hl) << 1) for i in range(8)])
        port.page(0x10)
        port.write(145, cfg)
        port.write(143, b'\xff')
    t0 = time.monotonic()
    while True:
        with port.selected():
            port.page(0x11)
            raw = port.read(202, 4)
        codes = [(raw[i // 2] >> (4 * (i % 2))) & 0xF for i in range(8)]
        if all(c == 1 for c in codes) or time.monotonic() - t0 > timeout:
            break
        time.sleep(0.2)
    say(f'{name}: application {apsel} applied, ConfigStatusLane1-8 = {codes} (1 = ConfigSuccess)')
    return codes


def media_lane_mask(port):
    """Lanes that exist on the media side of the selected application: the ApSel code of lane 1 (10h:145 bits 7-4)
    picks the application descriptor in the lower page (86 + 4 (n - 1), Table 8-21), whose byte 2 low nibble is the media
    lane count. A 4-lane optical module (400G VR4) reports PRBS counters only on media lanes 1-4; lanes 5-8 read
    TotalBits = 1 and tripped the sync-loss rule on 2026-10-19. Returns 0xFF when the descriptor is absent."""
    with port.selected():
        lower = port.lower()
        port.page(0x11)
        apsel = port.read(206, 1)[0] >> 4  # ActiveApSelCode lane 1 (Table 8-86), not the staged 10h:145
    if not 1 <= apsel <= 8:
        return 0xFF
    n = lower[86 + 4 * (apsel - 1) + 2] & 0x0F
    return (1 << n) - 1 if 1 <= n <= 8 else 0xFF


def cmd_prbs(gen, chk, pattern, seconds, budget_w, timeout, force_cmis=False, out=None, names=('A', 'B'), lane_mask=None,
             assume_w=None, posture=None, ambient_c=25, temp_limit_c=JIG_TEMP_LIMIT_C, temp_warn_c=None, temp_throttle_c=None,
             external_temp_ack=False, say=None, apsel=None, ber_limit=None):
    """Media-side PRBS both ways between two sticks ([CMIS] §8.12/8.13): capability + CMIS-rev gate, both ends to high
    power, Data Paths activated, A->B then B->A with S5 polling. The JSON record (--out F | -; default
    T11_ber_<A>_<B>_<UTC µs>.json) exists before the first register read and is written on EVERY exit -- a refusal or a
    NACK leaves `verdict: ABORTED: ...` with whatever was read; the identity (`read_id`) is read into the
    record before any gate. Lane mask: --lane-mask, else the lanes DPActivated on both ends -- none active =
    INCONCLUSIVE DP-NOT-ACTIVATED with NO Page 13h write. Clock source (13h:176/178) and gating (13h:177.3-1)
    stay at module defaults."""
    say = say or _quiet
    an, bn = names
    rec = {**record_header(names), 'pattern': pattern, 'seconds': seconds, 'lane_mask': None,
           'ends': {an: {}, bn: {}}, 'directions': {}, 'verdict': None}
    try:
        for p, name in ((gen, an), (chk, bn)):
            info = read_id(p)
            rec['ends'][name].update({k: info[k] for k in ('vendor', 'part_number', 'serial', 'power_class', 'max_power_w',
                                                             'cmis_revision', 'media_interface_technology_00h_212')})
        for p, name in ((gen, an), (chk, bn)):
            for role in ('generator', 'checker'):  # the reverse run needs both roles on both ends
                prbs_caps(p, role, pattern, force_cmis, rec['ends'][name], say)
        try:
            masks = []
            for p, name in ((gen, an), (chk, bn)):
                info = release_lpmode(p, budget_w, timeout, assume_w, posture, ambient_c, temp_limit_c, temp_warn_c, temp_throttle_c, external_temp_ack,
                                      say)
                rec['ends'][name]['release'] = {k: info[k] for k in ('gate', 'thresholds_02h', 'temp_warn_c', 'temp_stop_c',
                                                                     'module_pwrup_advertised_01h_167', 'module_ready_observed_s',
                                                                     'module_ready_within_advertised')}
            if apsel is not None:
                rec['apsel'] = apsel
                for p, name in ((gen, an), (chk, bn)):
                    rec['ends'][name]['config_status_11h_202'] = select_application(p, name, apsel, timeout, say)
            for p, name in ((gen, an), (chk, bn)):
                codes = ensure_dp_active(p, name, timeout, say)
                rec['ends'][name]['dp_states_11h_128'] = [DP_STATES.get(c, c) for c in codes]
                masks.append(sum(1 << i for i, c in enumerate(codes) if c == DP_ACTIVATED))
            active = masks[0] & masks[1]
            if lane_mask is None and not active:
                rec['lane_mask'] = '0x00 (no lane DPActivated on both ends -- Page 13h not written; --lane-mask 0xNN forces a run)'
                rec['verdict'] = 'INCONCLUSIVE: DP-NOT-ACTIVATED (no lane DPActivated on both ends, 11h:128-131)'
                say(rec['verdict'])
            else:
                mm = media_lane_mask(gen) & media_lane_mask(chk)
                rec['media_lane_mask'] = f'0x{mm:02x}'
                mask = lane_mask if lane_mask is not None else active & mm
                rec['lane_mask'] = f'0x{mask:02x}' + ('' if lane_mask is not None else ' (11h:128-131 DPActivated on both ends, masked to the media lanes of the selected application)')
                dp_ok = active & mask == mask
                for g, c, gn, cn in ((gen, chk, an, bn), (chk, gen, bn, an)):
                    r = prbs_direction(g, c, gn, cn, pattern, seconds, rec['ends'][gn], rec['ends'][cn], mask, say)
                    if not dp_ok and r['verdict'].startswith('MEASURED'):
                        r['verdict'] = 'INCONCLUSIVE: a Data Path in the lane mask is not DPActivated (11h:128-131)'
                    if ber_limit is not None and r.get('media_ber'):
                        r['ber_limit'] = ber_limit
                        r['lane_verdict'] = {f'lane{i + 1}': ('PASS' if b <= ber_limit else 'FAIL') if mask >> i & 1 else 'n/a'
                                             for i, b in enumerate(r['media_ber'])}
                        say(f'  per-lane against {ber_limit:.1e}: ' + ' '.join(f'{k} {v}' for k, v in r['lane_verdict'].items() if v != 'n/a'))
                    rec['directions'][f'{gn}_to_{cn}'] = r
                    if r['verdict'].startswith(('ABORTED', 'INCONCLUSIVE: THERMAL')):
                        break
        finally:
            for p, name in ((gen, an), (chk, bn)):
                p.set_lpmode(1)
                rec['ends'][name]['lowpwr_after'] = wait_state(p, ST_LOWPWR, timeout, say)
            say('PRBS stopped, both ends back in low power' if all(rec['ends'][n]['lowpwr_after'] for n in names) else
                  'PRBS stopped -- WARNING: an end did not report ModuleLowPwr after LPMode went high (JSON lowpwr_after)')
        verdicts = [r['verdict'] for r in rec['directions'].values()]
        if rec['verdict'] is None:
            rec['verdict'] = (max(verdicts, key=len) if len(verdicts) == 2 and all(v.startswith('MEASURED') for v in verdicts)
                              else 'see directions')  # the longer MEASURED string carries the CMIS<5.0 caveat when either end had it
    except (Refused, Nack) as e:
        rec['verdict'] = f'ABORTED: {e}'
        raise
    finally:
        write_record(rec, out, f'T11_ber_{an}_{bn}_{{}}.json', say)
    return rec



MEDIA_FLAG_BYTES = (135, 147, 148)  # Page 11h media-side lane flags: FailureFlagTx, LOSFlagRx, CDRLOLFlagRx (latched, clear on read)


def read_media_flags(port):
    with port.selected():
        pg = port.read_page(0x11)
    return {off: pg[off - 128] for off in MEDIA_FLAG_BYTES}


def cmd_media_link(a, b, seconds, budget_w, timeout, out=None, names=('A', 'B'), assume_w=None, posture=None, ambient_c=25,
                   temp_limit_c=JIG_TEMP_LIMIT_C, temp_warn_c=None, temp_throttle_c=None, external_temp_ack=False, say=None):
    """End-to-end media link check of one AEC between two sticks, no diagnostic pages: both ends to high power,
    Data Paths active, then the media-side lane flags (11h:135 / 147 / 148, latched, cleared once) sampled every second
    for `seconds` on both ends. A lane links when every sample reads 0 on both ends. No BER: a pass or fail per lane.
    Record T11_media_<A>_<B>_<UTC µs>.json, written on every exit like the PRBS record."""
    say = say or _quiet
    an, bn = names
    rec = {**record_header(names), 'seconds': seconds, 'ends': {an: {}, bn: {}}, 'lanes_linked': None, 'verdict': None}
    try:
        for p, name in ((a, an), (b, bn)):
            info = read_id(p)
            rec['ends'][name].update({k: info[k] for k in ('vendor', 'part_number', 'serial', 'power_class', 'max_power_w',
                                                             'cmis_revision', 'media_interface_technology_00h_212')})
        try:
            for p, name in ((a, an), (b, bn)):
                info = release_lpmode(p, budget_w, timeout, assume_w, posture, ambient_c, temp_limit_c, temp_warn_c, temp_throttle_c,
                                      external_temp_ack, say)
                rec['ends'][name]['release'] = {k: info[k] for k in ('gate', 'module_ready_observed_s', 'module_ready_within_advertised')}
            dp_ok = True
            for p, name in ((a, an), (b, bn)):
                codes = ensure_dp_active(p, name, timeout, say)
                rec['ends'][name]['dp_states_11h_128'] = [DP_STATES.get(c, c) for c in codes]
                dp_ok = dp_ok and all(c == DP_ACTIVATED for c in codes)
            for p, name in ((a, an), (b, bn)):
                rec['ends'][name]['media_flags_cleared'] = {f'11h:{k}': f'0x{v:02x}' for k, v in read_media_flags(p).items()}
            sticky = {an: dict.fromkeys(MEDIA_FLAG_BYTES, 0), bn: dict.fromkeys(MEDIA_FLAG_BYTES, 0)}
            t0 = time.monotonic()
            n = 0
            while time.monotonic() - t0 < seconds:
                time.sleep(1.0)
                n += 1
                line = []
                for p, name in ((a, an), (b, bn)):
                    f = read_media_flags(p)
                    for k, v in f.items():
                        sticky[name][k] |= v
                    line.append(f'{name} tx-fail 0x{f[135]:02x} rx-los 0x{f[147]:02x} rx-lol 0x{f[148]:02x}')
                say(f'  +{time.monotonic() - t0:6.1f} s  ' + '  |  '.join(line))
            rec['samples'] = n
            for name in names:
                rec['ends'][name]['media_flags_or'] = {f'11h:{k}': f'0x{v:02x}' for k, v in sticky[name].items()}
            bad = {name: sticky[name][135] | sticky[name][147] | sticky[name][148] for name in names}
            linked = [not ((bad[an] | bad[bn]) >> i & 1) for i in range(8)]
            rec['lanes_linked'] = linked
            cnt = sum(linked)
            if not dp_ok:
                rec['verdict'] = 'INCONCLUSIVE: a Data Path is not DPActivated on an end (11h:128-131)'
            elif n == 0:
                rec['verdict'] = 'INCONCLUSIVE: no sample'
            else:
                rec['verdict'] = (f'MEASURED: {cnt} of 8 media lanes linked both ways over {n} samples' if cnt == 8 else
                                  f'MEASURED: {cnt} of 8 media lanes linked; flagged lanes '
                                  f'{[i + 1 for i, ok in enumerate(linked) if not ok]} (OR of 11h:135/147/148 per end in the record)')
            say(rec['verdict'])
        finally:
            for p, name in ((a, an), (b, bn)):
                p.set_lpmode(1)
                rec['ends'][name]['lowpwr_after'] = wait_state(p, ST_LOWPWR, timeout, say)
            say('media link check stopped, both ends back in low power' if all(rec['ends'][x]['lowpwr_after'] for x in names) else
                  'media link check stopped -- WARNING: an end did not report ModuleLowPwr after LPMode went high (JSON lowpwr_after)')
    except (Refused, Nack) as e:
        rec['verdict'] = f'ABORTED: {e}'
        raise
    finally:
        write_record(rec, out, f'T11_media_{an}_{bn}_{{}}.json', say)
    return rec


def cmd_trend(gen, chk, pattern, seconds, budget_w, timeout, force_cmis=False, out=None, names=('A', 'B'), lane_mask=None,
              assume_w=None, posture=None, ambient_c=25, temp_limit_c=JIG_TEMP_LIMIT_C, temp_warn_c=None, temp_throttle_c=None,
              external_temp_ack=False, say=None, apsel=None, loop=False):
    """`trend`: one media-side PRBS direction A -> B sampled every second at the checker: BER per lane (selector
    01h), input SNR per lane (selector 06h, when 13h:130.5 advertises it), temperature / Vcc / flags on both ends. `loop`:
    the far end B loops its media input back (13h:181, advertised at 13h:128.1), A runs generator and checker, so one
    stick measures the whole fiber out and back. Same gates, setup and cleanup as prbs_direction; the gated counters and
    the LOL flag at the end. Record T11_trend|loop_<A>_<B>_<UTC µs>.json plus a CSV with one row per sample."""
    say = say or _quiet
    an, bn = names
    meas, mname, kind = (gen, an, 'loop') if loop else (chk, bn, 'trend')
    path = out or f'T11_{kind}_{an}_{bn}_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")}.json'
    res = {'generator': an, 'checker': mname, 'pattern': pattern, 'seconds': seconds, 'readback': {}, 'cleanup': {},
           'health': {an: [], bn: []}, 'verdict': None}
    rec = {**record_header(names), 'kind': kind, 'pattern': pattern, 'seconds': seconds, 'loopback_end': bn if loop else None,
           'ends': {an: {}, bn: {}}, 'lane_mask': None, 'samples': [], 'direction': res, 'verdict': None,
           'media_lol_semantics': '14h:139 is clear-on-read: media_lol per sample = lanes with LOL since the previous sample; '
                                  'media_lol_14h_139 at the end covers only the last second',
           'csv': None if path == '-' else (path[:-5] if path.endswith('.json') else path) + '.csv'}
    try:
        for p, name in ((gen, an), (chk, bn)):
            info = read_id(p)
            rec['ends'][name].update({k: info[k] for k in ('vendor', 'part_number', 'serial', 'power_class', 'max_power_w',
                                                             'cmis_revision', 'media_interface_technology_00h_212')})
        cg, cc = {}, {}
        prbs_caps(gen, 'generator', pattern, force_cmis, cg, say)
        prbs_caps(meas, 'checker', pattern, force_cmis, cc, say)
        rec['ends'][an]['caps_generator'], rec['ends'][mname]['caps_checker'] = cg, cc
        if loop:
            with chk.selected():
                adv = chk.read_page(0x13)[0]
            rec['ends'][bn]['p13_128'] = adv
            if not adv & LOOP_MEDIA_IN:
                raise Refused(f'{bn}: media-side input loopback not advertised (13h:128.1 = 0, Table 8-94)')
        try:
            for p, name in ((gen, an), (chk, bn)):
                info = release_lpmode(p, budget_w, timeout, assume_w, posture, ambient_c, temp_limit_c, temp_warn_c, temp_throttle_c,
                                      external_temp_ack, say)
                rec['ends'][name]['release'] = {k: info[k] for k in ('gate', 'module_ready_observed_s', 'module_ready_within_advertised')}
            if apsel is not None:
                rec['apsel'] = apsel
                for p, name in ((gen, an), (chk, bn)):
                    rec['ends'][name]['config_status_11h_202'] = select_application(p, name, apsel, timeout, say)
            masks = []
            for p, name in ((gen, an), (chk, bn)):
                codes = ensure_dp_active(p, name, timeout, say)
                rec['ends'][name]['dp_states_11h_128'] = [DP_STATES.get(c, c) for c in codes]
                masks.append(sum(1 << i for i, c in enumerate(codes) if c == DP_ACTIVATED))
            mm = media_lane_mask(gen) & media_lane_mask(chk)
            rec['media_lane_mask'] = f'0x{mm:02x}'
            mask = lane_mask if lane_mask is not None else masks[0] & masks[1] & mm
            rec['lane_mask'] = res['lane_mask'] = f'0x{mask:02x}'
            if not mask:
                rec['verdict'] = 'INCONCLUSIVE: DP-NOT-ACTIVATED (no media lane DPActivated on both ends, 11h:128-131)'
                say(rec['verdict'])
            else:
                lanes = [i for i in range(8) if mask >> i & 1]
                pat = PATTERNS[pattern]
                sel = bytes([pat | pat << 4]) * 4
                g155, c171, res['placement'] = fec_placement(cg['p13_131'], cc['p13_131'])
                snr_ok = bool(cc['p13_130'] & REP_SNR_MEDIA)
                rec['snr_advertised_13h_130_5'] = snr_ok
                res['checker_periodic_updates_13h_129_4'] = bool(cc['p13_129'] & CAP_PERIODIC)
                if not snr_ok:
                    say(f'{mname}: media-side input SNR not advertised (13h:130.5 = 0) -- SNR columns stay empty')
                try:
                    if loop:
                        with chk.selected():
                            chk.page(0x13)
                            chk.write(P13_MLOOP_IN, bytes([mask]))
                            back = chk.read(P13_MLOOP_IN, 1)[0]
                        rec['ends'][bn]['loopback_13h_181_readback'] = f'0x{back:02x}'
                        if back != mask:
                            raise PrbsAbort(f'{bn}: 13h:181 read back 0x{back:02x}, wanted 0x{mask:02x} -- loopback rejected')
                    prbs_arm(gen, meas, an, mname, sel, g155, c171, mask, res)
                    say(f'{kind}: {pattern} {an} -> {bn}' + (f' -> {an} (far-end media loopback)' if loop else '')
                        + f', lanes 0x{mask:02x}, {seconds:.0f} s, one sample per second (Ctrl-C aborts)')
                    t0, warned = time.monotonic(), set()
                    try:
                        while time.monotonic() - t0 < seconds:
                            sample = {'t_s': round(time.monotonic() - t0, 1), 'media_ber': None, 'media_snr_db': None}
                            with meas.selected():
                                if cc['p13_130'] & REP_BER:
                                    b1, d1 = read_selector(meas, SEL_BER)
                                    if b1 == SEL_BER:
                                        o = P14_MEDIA_BER - P14_DATA
                                        sample['media_ber'] = [f16(d1[o + 2 * i], d1[o + 2 * i + 1]) for i in range(8)]
                                if cc['p13_130'] & REP_COUNTERS:
                                    errs, bits = [], []
                                    for s_ in SEL_MEDIA_CNT:
                                        bs, ds = read_selector(meas, s_)
                                        if bs != s_:
                                            break
                                        for i in range(4):
                                            errs.append(int.from_bytes(ds[16 * i:16 * i + 8], 'little'))
                                            bits.append(int.from_bytes(ds[16 * i + 8:16 * i + 16], 'little'))
                                    if len(errs) == 8:
                                        sample['media_errors'], sample['media_bits'] = errs, bits
                                if snr_ok:
                                    b6, snr = read_media_snr(meas)
                                    if b6 == SEL_SNR:
                                        sample['media_snr_db'] = snr
                                meas.page(0x14)
                                sample['media_lol'] = f'0x{meas.read(P14_CHK_LOL_MEDIA, 1)[0]:02x}'  # lane since the previous sample
                            for p, name in ((gen, an), (chk, bn)):
                                h = poll_health(p)
                                reason = stop_reason(h, p, warned, name, say)
                                res['health'][name].append({'t_s': sample['t_s'], **h})
                                sample[name] = {k: h[k] for k in ('state_name', 'temp_c', 'vcc_v', 'flags9')}
                                if reason:
                                    if reason.startswith('THERMAL'):
                                        for q in (gen, chk):
                                            q.set_lpmode(1)
                                    raise PrbsAbort(f'{name}: {reason}')
                            rec['samples'].append(sample)
                            ber = sample['media_ber'] or []
                            snr = sample['media_snr_db'] or []
                            say(f'  +{sample["t_s"]:6.1f} s  BER ' + ' '.join(f'{ber[i]:.1e}' for i in lanes if ber)
                                + ('  SNR ' + ' '.join(f'{snr[i]:5.2f}' for i in lanes) + ' dB' if snr else '')
                                + (f'  LOL {sample["media_lol"]}' if sample['media_lol'] != '0x00' else '')
                                + f'  T {sample[an]["temp_c"]:.1f}/{sample[bn]["temp_c"]:.1f} C'
                                + f'  Vcc {sample[an]["vcc_v"]:.3f}/{sample[bn]["vcc_v"]:.3f} V')
                            time.sleep(max(0.0, min(1.0, seconds - (time.monotonic() - t0))))
                    except KeyboardInterrupt:
                        say('')
                        raise PrbsAbort('Ctrl-C') from None
                    except Nack as e:
                        raise PrbsAbort(f'NACK during the run ({e})') from None
                    problems = [] if rec['samples'] else [f'no sample in {seconds:g} s']
                    read_media_stats(gen, meas, an, mname, cc, lanes, res, problems, say)
                    res['verdict'] = 'MEASURED' if not problems else 'INCONCLUSIVE: ' + '; '.join(problems)
                except PrbsAbort as e:
                    if 'THERMAL:' in str(e):
                        res['verdict'] = f'INCONCLUSIVE: THERMAL ({e})'
                        say(f'THROTTLE: {e} -> LPMode HIGH, run stopped, INCONCLUSIVE: THERMAL. {FORCED_AIR_MSG}')
                    else:
                        res['verdict'] = f'ABORTED: {e}'
                except Nack as e:
                    res['verdict'] = f'ABORTED: NACK ({e})'
                finally:
                    if loop:
                        try:
                            with chk.selected():
                                chk.page(0x13)
                                chk.write(P13_MLOOP_IN, b'\x00')
                                rec['ends'][bn]['loopback_cleanup'] = f'13h:181 = 0x{chk.read(P13_MLOOP_IN, 1)[0]:02x}'
                        except Nack as e:
                            rec['ends'][bn]['loopback_cleanup'] = f'NACK ({e})'
                    prbs_disable(gen, meas, an, mname, res, say)
                rec['verdict'] = res['verdict']
                say(f'{kind} {an} -> {bn}: {rec["verdict"]}')
        finally:
            for p, name in ((gen, an), (chk, bn)):
                p.set_lpmode(1)
                rec['ends'][name]['lowpwr_after'] = wait_state(p, ST_LOWPWR, timeout, say)
            say(f'{kind} stopped, both ends back in low power' if all(rec['ends'][n]['lowpwr_after'] for n in names) else
                f'{kind} stopped -- WARNING: an end did not report ModuleLowPwr after LPMode went high (JSON lowpwr_after)')
    except (Refused, Nack) as e:
        rec['verdict'] = f'ABORTED: {e}'
        raise
    finally:
        write_record(rec, path, path, say)
        if rec['csv'] and rec['samples']:
            with open_new(rec['csv']) as f:
                w = csv.writer(f)
                w.writerow(['t_s'] + [f'lane{i + 1}_ber' for i in range(8)] + [f'lane{i + 1}_snr_db' for i in range(8)]
                           + ['gen_temp_c', 'gen_vcc_v', 'chk_temp_c', 'chk_vcc_v', 'flags', 'media_lol'])
                for x in rec['samples']:
                    w.writerow([x['t_s']] + [f'{b:.3e}' if b != '' else '' for b in (x['media_ber'] or [''] * 8)] + [f'{v:.2f}' if v != '' else '' for v in (x['media_snr_db'] or [''] * 8)]
                               + [f'{x[an]["temp_c"]:.2f}', f'{x[an]["vcc_v"]:.3f}', f'{x[bn]["temp_c"]:.2f}', f'{x[bn]["vcc_v"]:.3f}',
                                  f'0x{x[an]["flags9"]:02x}|0x{x[bn]["flags9"]:02x}', x.get('media_lol', '')])
            say(f'wrote {rec["csv"]}')
    return rec


identity, health, hold, prbs_pair, reset_time, watch_insert, release, dp_activate = (
    read_id, poll_health, hold_high_power, cmd_prbs, cmd_reset, cmd_watch, release_lpmode, ensure_dp_active)
