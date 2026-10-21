"""tech.py - the technician test of one cable, steps 0-9, on the test primitives.

open_units(jig_profile, say, ask, dry_run=False, image=None) -> (ports, prechecks)
    steps 0-1 once: the hint line, the units opened from the profile (open_ports), the S2 line, then the pre-check per unit
    the shipped switch state (MODE AUTO, MOD PWR ON) is read from the pins and the module answer, never asked. A
    failed pre-check asks the operator for the one fix ("... Then press Retry.") and reads again. ports = {'A': Port, 'B': Port}.
    prechecks = every pre-check read with its UTC time (run_cable records them as `precheck` events). Raises Stopped.
run_cable(session, jig_profile, criteria, backends, label_sn, say, ask, checklist=(), soak_s=None, gate_s=None, last=True,
          on_insert=None, timeout=TIMEOUT_S, on_event=None) -> criteria.Verdict
    steps 2-9 on open units: both ends watched in one wait (an end already present is accepted, C-1), identity, low-power
    health + thresholds + pages_A/B.json, the preconditions, release + DP activate + the soak (health.csv), prbs_pair
    (prbs.json), VDM snapshot + 11h:135, both ends low power (+ tristate when `last`), the verdict, report.md,
    Session.close(). Every step is an event. An identity or low-power FAIL, or a low-power THERMAL, stops the flow before
    any high-power step. `on_insert(end)` is called at step 2 for every present end (the lot
    loop counts the insertion there). `on_event(ev)` is called once per session event (the TUI hook).

say(text, err=False) renders a line; ask(text) -> bool returns True when the technician acknowledged (Enter) and False to stop.
The primitives get `screen_say(say)`: only progress, WARN, THROTTLE and STOP lines reach the technician; `Port` gets no
`say` (no close summary, F-2). The soak and the gate come from the SKU file (`soak_s` top level, `prbs.seconds_per_direction`),
else the PROPOSED defaults; the --soak-s / --gate-s overrides are `override` events. The S4 budget is safety.BUDGET_W or the
signed profile's `power.budget_w` (an `override` event when it differs); no flag changes a limit.
"""
import glob
import json
import os
import sys
import time

from . import HW_SUPPORT, __version__, criteria, report, safety, tests  # noqa: F401 -- __version__ re-exported for the selftest
from .cmis import DP_STATES, L_STATE, P1_PAGES, P1_VDM_BIT, P11_FLAGS, P_VDM_ADV, ST_LOWPWR
from .criteria import INCONCLUSIVE, Reason, Verdict
from .fru import rev_of, summary as fru_summary
from .jig import STOCK_PID, hardware_manifest, load_profile, register_pid
from .records import OUT_DIR, Session, sha256, tool_block, utc
from .safety import BUDGET_W, FORCED_AIR_MSG, PRBS_MIN_S, TRISTATE_LAST
from .transport import CLOCKS, T_MGMT_INIT_S, FakeBackend, FtdiBackend, Nack, Port, Refused, ramp_clock

SOAK_S = 120.0
SLOPE_WINDOW_S = 30  # 4.2 temp_slope_c_per_min: plateau check over the last 30 s of the soak (real seconds, F-6)
SLOPE_FROM_S = 60
TIMEOUT_S = 15.0
P11_FAILURE_TX = 135  # [CMIS] Table 8-80: 11h:135 FailureFlagTx, media lanes (cmis.P11_FLAGS names it)
assert P11_FAILURE_TX in P11_FLAGS
FAKE_INSERT_S = {'A': 0.3, 'B': 0.6}  # dry-run: the fake cable ends are inserted this long after open, so step 2 sees the edge
_HERE = os.path.dirname(os.path.abspath(__file__))
CRITERIA_DIR = os.environ.get('AECTEST_CRITERIA') or next((d for d in (os.path.normpath(os.path.join(_HERE, '..', '..', 'criteria')), os.path.join(_HERE, 'criteria'))
                                                            if os.path.isdir(d)), os.path.join(_HERE, 'criteria'))  # the repo folder, else the package data
SCREEN_PREFIXES = ('WARN', 'THROTTLE', 'STOP', 'REFUSED', '  +', 'measuring', 'LPMode', 'ModuleReady', 'PRBS stopped', 'cleanup')

HINT_LINE = 'Charger on PWR ONLY, host cable on PC. The switches stay as shipped: MODE AUTO, slides at O, MOD PWR ON.'
S2_LINE = 'MPSSE configured, directions set on {} (S2)'
FORCED_AIR_LINE = 'advanced tests (class ≥ 3 module power) require forced air over the sink at the table'
PRECHECK_RETRY = ' Then press Retry.'
STEPS = {2: 'insert', 3: 'identity', 4: 'low_power', 5: 'high_power', 6: 'prbs', 7: 'vdm_flags', 8: 'low_power_again', 9: 'verdict'}


class Stopped(SystemExit):
    """The technician did not acknowledge a line, or a unit did not open: no session step ran."""

    def __init__(self, code, text):
        super().__init__(f'{code}: {text}')
        self.code, self.text = code, text


class _Stop(Exception):
    """Leave steps 2-7 early; steps 8 and 9 still run."""


def ask_tty(text, read=None):
    """The default `ask`: Enter acknowledges; n / Ctrl-C / EOF stop. `read` is the screen's own input (tui.py)."""
    read = read or input
    try:
        return read(f'{text} [Enter = done, n = stop] ').strip().lower() not in ('n', 'no', 'stop')
    except (EOFError, KeyboardInterrupt):
        if read is input:  # the screen's own input restarts its Live page: a bare newline would land inside it
            print()
        return False


def identity_line(d):
    """The identity of one end as the screen and the page say it (shared by tech and tui)."""
    return f'{d["vendor"]} {d["part_number"]} SN {d["serial"]}'


def verdict_line(status, codes):
    """The verdict line: the status and the reason codes, or `(no reason codes)`."""
    return f'{status} {" ".join(codes) if codes else "(no reason codes)"}'


_CONSOLES = {}


def line_style(text):
    """The screen colour of one line (2.8): WARN yellow, THROTTLE / FAIL / STOP red, PASS green, else None."""
    return ('bold yellow' if text.startswith('WARN') else 'bold red' if text.startswith(('THROTTLE', 'FAIL', 'STOP')) else
            'bold green' if text.startswith('PASS') else None)


def say_screen(text, err=False):
    """The technician screen line: rich colours when the venv has rich, plain print otherwise (2.8). WARN yellow, THROTTLE /
    FAIL / STOP red, PASS green. soft_wrap: a log line is never broken at 80 columns when stdout is a pipe."""
    try:
        from rich.console import Console
        from rich.markup import escape
    except ImportError:
        print(text, file=sys.stderr if err else sys.stdout)
        return
    if err not in _CONSOLES:
        _CONSOLES[err] = Console(stderr=err, highlight=False, soft_wrap=True)
    _CONSOLES[err].print(escape(text), style=line_style(text))


def screen_say(say):
    """The filtering `say` for the primitives: progress, WARN, THROTTLE and STOP lines pass; engineer text does not."""
    return lambda text, err=False: say(text, err) if text.startswith(SCREEN_PREFIXES) else None


def quiet(text, err=False):
    """A `say` that drops the line."""


# --------------------------------------------------------------------------- steps 0-1
def _open(serial, pid, dry, image, allow, insert_s):
    if dry:
        be = FakeBackend(serial, 100_000, image)
        be.present_at = time.monotonic() + insert_s
        return Port(be, allow)  # no `say`: the Port close summary is engineer text (F-2)
    from .cli import NO_DEVICE, check_pyftdi  # here, not at the top: cli imports this module
    check_pyftdi(False)
    if pid != STOCK_PID:
        register_pid(pid)
    dev = f'0x0403:0x{pid:04x}' if pid != STOCK_PID else 'ftdi:2232h'
    try:
        return Port(FtdiBackend(f'ftdi://{dev}:{serial}/1', 100_000), allow)
    except Exception as e:  # noqa: BLE001 -- pyftdi raises UsbToolsError / USBError / FtdiError; all mean "not reachable"
        raise Stopped('JIG-UNIT-MISSING', f'cannot open unit {serial} ({e}). {NO_DEVICE}') from None


def open_ports(jig_profile, dry_run=False, image=None):
    """Both units from the profile, in MAN, SAFE outputs. The signed profile with a power acknowledgement allows
    LPMode low. A failure closes the unit already open and re-raises."""
    power = jig_profile.get('power') or {}
    allow = bool(jig_profile.get('signed')) and bool(power.get('acknowledged_by'))
    units = jig_profile['units']
    ports = {}
    try:
        for end in units:
            ports[end] = _open(units[end], jig_profile.get('pid') or STOCK_PID, dry_run, image, allow, FAKE_INSERT_S.get(end, 0.3))
        for end, p in ports.items():  # 7.5: an unlisted product string is INCONCLUSIVE HW-REV-UNSUPPORTED before any step
            product, fru = getattr(p.be, 'product', None), getattr(p.be, 'fru', None)
            if product not in HW_SUPPORT or (fru and rev_of(fru) not in HW_SUPPORT[product]):
                raise Stopped('HW-REV-UNSUPPORTED', f'unit {units[end]} reports product {product!r}, FRU {fru_summary(fru)}; aectest '
                              f'{__version__} supports {HW_SUPPORT}' + ('' if fru else ' (no FRU area: the product string alone is judged)'))
    except BaseException:
        for p in ports.values():
            p.close()
        raise
    return ports


def fake_image(crit, sku, dry_run):
    """The fake_modules image of the SKU for --dry-run, None when there is none (or no dry run)."""
    if not dry_run:
        return None
    from . import fake_modules
    name = crit['sku'] if crit else os.path.basename(str(sku)).replace('.yaml', '')
    return fake_modules.load(name) if os.path.exists(os.path.join(fake_modules.HERE, f'{name}.yaml')) else None


def session_for(out, mode, name, profile, crit, label_sn, operator=None, lot=None, dry_run=False):
    """One records.Session with the 5.2 manifest blocks (F-1). `hardware.dry_run` marks a rehearsal: calibrate skips it (A-2)."""
    return Session(out, mode, name, tool=tool_block(), hardware={**hardware_manifest(profile), 'power': profile.get('power'), 'dry_run': bool(dry_run),
                                 'i2c': {u: CLOCKS.get(u) for u in profile['units'].values()}},
                   cable={'sku': crit['sku'] if crit else None, 'label_sn': (label_sn or '').strip() or None, 'cmis': {}}, criteria=crit, operator=operator, lot=lot)


def open_units(jig_profile, say, ask, dry_run=False, image=None):
    units = jig_profile['units']
    say(HINT_LINE)
    ports = open_ports(jig_profile, dry_run, image)
    say(S2_LINE.format(' and '.join(units[e] for e in ports)))
    prechecks = []
    try:
        for end, p in ports.items():
            while True:
                pc = precheck(p, units[end])
                prechecks.append(pc)
                say(precheck_line(pc))
                if pc['ok']:
                    break
                fix = next(c for c in pc['checks'] if c['ok'] is False)
                if not ask(fix['fix'] + PRECHECK_RETRY):
                    raise Stopped('ABORTED:checklist', f"pre-check not met on {units[end]}: {fix['name']} {fix['got']}")  # the code the pages know
    except BaseException:
        for p in ports.values():
            p.close()
        raise
    return ports, prechecks


def precheck(port, unit):
    """The shipped switch state of one unit read from the pins, never asked. MOD PWR: ModPrsL and IntL both low with no
    module answer is the rail-off signature. MODE: a present module that answers is AUTO (the FTDI drives ModSelL low);
    one that does not answer within tMgmtInit sits in MAN with the MSEL slide at O or H. No cable: nothing to judge yet."""
    s = port.sense()
    low = s['ModPrsL'] == 0 and s['IntL'] == 0
    answers = s['module_present'] and _first_ack_ms(port) is not None
    checks = [{'name': 'MOD PWR', 'want': 'ON', 'got': 'OFF (ModPrsL and IntL both low)' if low and not answers else 'ON',
               'ok': answers or not low, 'fix': f'Set MOD PWR to ON on {unit}.',
               'hint': 'ModPrsL and IntL both low with no module answer means the module rail is off.'},
              {'name': 'MODE', 'want': 'AUTO', 'ok': True, 'fix': f'Set MODE to AUTO on {unit}.',
               'hint': 'In MAN the MSEL slide at O or H deselects the module. Set MODE to AUTO.'},
              {'name': 'module answers', 'want': 'yes', 'ok': True}]
    if answers:
        checks[1]['got'], checks[2]['got'] = 'AUTO', 'yes'
    elif not s['module_present'] or low:
        checks[1]['got'] = checks[2]['got'] = 'no cable' if not s['module_present'] else 'unknown (rail off)'
    else:
        checks[1].update(got='MAN or MSEL not at L', ok=False)
        checks[2].update(got='NACK', ok=False, fix=checks[1]['fix'], hint=checks[1]['hint'])
    return {'unit': unit, 't': utc(), 'checks': checks, 'ok': all(c['ok'] for c in checks), 'pins': pins_confirm({unit: port})}


def precheck_line(pc):
    """One screen line per pre-check read: `precheck CT2M-01: MOD PWR ON, MODE AUTO, module answers yes`, a miss marked."""
    return f"precheck {pc['unit']}: " + ', '.join(f"{c['name']} {c['got']}" + ('' if c['ok'] else ' (fix)') for c in pc['checks'])


def pins_confirm(ports, state=False):
    """One `confirm:` line with the pins of every opened unit. ModPrsL and IntL both low with no module answering is the
    rail-off signature (the bled pull-ups). With `state`, the module state of a present module (a NACK reads as none)."""
    parts = []
    for end, p in ports.items():
        s = p.sense()
        low = s['ModPrsL'] == 0 and s['IntL'] == 0
        txt = f"{end} ModPrsL {s['ModPrsL']}, IntL {s['IntL']}"
        if state and s['module_present']:
            try:
                txt += f", module state {tests.poll_health(p)['state_name']}"
            except Nack:
                txt += ', module state: no ACK yet'
        elif low:
            txt += ' (both low: consistent with the rail off)'
        elif not s['module_present']:
            txt += ' (no module)'
        parts.append(txt)
    return 'confirm: ' + '; '.join(parts)


# --------------------------------------------------------------------------- steps 2-9
def _first_ack_ms(port):
    """ms from now (the ModPrsL edge) to the first ACK on byte 3, as cmd_watch times it; None past tMgmtInit (Table 10-2)."""
    t0 = time.monotonic()
    with port.selected():
        while True:
            try:
                port.read(L_STATE, 1)
                return round((time.monotonic() - t0) * 1e3)
            except Nack:
                if time.monotonic() - t0 > T_MGMT_INIT_S:
                    return None
                time.sleep(0.005)


def watch_both(ports, timeout, say):
    """Step 2 on both ends in one wait of `timeout` s (C-1). An end present at entry is accepted without a MgmtInit time
    (no edge seen). An edge is timed to the first ACK. Returns {end: {'present', 'edge', 'mgmt_init_ms'}}."""
    seen = {}
    for end, port in ports.items():
        if port.sense()['module_present']:
            seen[end] = {'present': True, 'edge': False, 'mgmt_init_ms': None}
            say(f'End {end} is already in its unit. No MgmtInit time.')
    t0 = time.monotonic()
    while len(seen) < len(ports) and time.monotonic() - t0 < timeout:
        for end, port in ports.items():
            if end not in seen and port.sense()['module_present']:
                seen[end] = {'present': True, 'edge': True, 'mgmt_init_ms': _first_ack_ms(port)}
        time.sleep(0.001)
    return {end: seen.get(end, {'present': False, 'edge': False, 'mgmt_init_ms': None}) for end in ports}


def _slope(samples):
    """C/min over the last SLOPE_WINDOW_S real seconds of the soak, the first SLOPE_FROM_S seconds left out (the step
    response) when the soak lasts 2 x SLOPE_FROM_S or more; a shorter soak keeps the plain window. None below two samples or a zero span."""
    if not samples:
        return None
    last = samples[-1]['t_s']
    start = SLOPE_FROM_S if last >= 2 * SLOPE_FROM_S else 0.0  # the first minute is left out when the soak has two or more; a short soak keeps the old window
    w = [h for h in samples if h['t_s'] >= max(start, last - SLOPE_WINDOW_S)]
    span = w[-1]['t_s'] - w[0]['t_s'] if len(w) >= 2 else 0.0
    return round((w[-1]['temp_c'] - w[0]['temp_c']) / (span / 60.0), 3) if span > 0 else None


def _sku_min_version(crit):
    v = str(((crit.get('identity') or {}).get('cmis_revision') or {}).get('min') or '5.0')
    return tuple(int(x) for x in v.split('.'))


def write_report(session, verdict):
    """report.md from the manifest and the events so far, written before close() so the manifest hashes it (5.1)."""
    session.manifest['verdict'] = verdict.as_dict() if hasattr(verdict, 'as_dict') else verdict
    session.manifest['files'] = {n: sha256(session.path(n)) for n in sorted(os.listdir(session.dir))
                                 if n not in ('manifest.json', 'events.jsonl', 'report.md')}  # events.jsonl grows until close
    with open(session.path('events.jsonl')) as f:
        events = [json.loads(line) for line in f]
    page = report.technician_page(session.manifest, events, session.dir)
    with open(session.path('report.md'), 'x') as f:
        f.write(page)
    return page


def run_cable(session, jig_profile, criteria_, backends, label_sn, say, ask, checklist=(), soak_s=None, gate_s=None, last=True,
              on_insert=None, timeout=TIMEOUT_S, on_event=None):
    if on_event:
        session.on_event = on_event  # every event of this session, the close ones included, reaches the screen (tui.py)
    ports = backends
    crit = criteria_
    prim = screen_say(say)
    units = jig_profile['units']
    label_sn = (label_sn or '').strip() or None
    session.manifest['cable']['label_sn'] = label_sn
    obs = {'label_sn': label_sn, 'ends': {}, 'timing': {}, 'low_power': {}, 'high_power': {}, 'prbs': {}, 'vdm': {}, 'inconclusive': [],
           'host_present': bool(jig_profile.get('host_present'))}
    codes = obs['inconclusive']
    present, vdm_adv, released = {}, {}, set()
    obs['mode'] = 'both ends' if 'B' in ports else 'one end'
    session.manifest['cable']['mode'] = obs['mode']
    say(f"mode: {obs['mode']}")

    cur = [1]

    def step(n):
        cur[0] = n
        session.event('step', data={'n': n, 'name': STEPS[n]})
        say(f'step {n}: {STEPS[n]}')

    def attempt(end, fn):
        """One CMIS access per end: a NACK is NO-ACK-<end>, or REMOVED-MID-TEST when ModPrsL says the cable is gone."""
        try:
            return fn()
        except Nack as e:
            gone = not ports[end].sense()['module_present']
            codes.append('REMOVED-MID-TEST' if gone else f'NO-ACK-{end}')
            session.event('stop', end, {'text': f'NACK: {e}', 'present': not gone})
            if gone:
                say(f'STOP {end}: cable removed ({e})')
            else:
                say(f'STOP {end}: no ACK ({e}). Check MODE AUTO and MOD PWR ON on unit {units[end]}.')  # G-2
            raise _Stop from None

    def step6():  # PRBS A->B then B->A, health polled inside prbs_pair; the ends end in low power
        step(6)
        p = crit.get('prbs') or {}
        force_cmis = _sku_min_version(crit) < (5, 0)  # the SKU accepts a CMIS < 5.0 module: the 13h/14h layouts are assumed (R6-W14)
        if force_cmis:
            session.event('override', data={'flag': 'force_cmis', 'value': True, 'source': 'sku'})
        try:
            rec = tests.prbs_pair(ports['A'], ports['B'], p.get('pattern', 'prbs31q'), gate, budget_w, timeout, force_cmis,
                                  session.path('prbs.json'), ('A', 'B'), None, say=prim)
        except (Refused, Nack) as e:
            session.event('refused', data={'text': str(e)})
            if 'not advertised' in str(e):
                obs['prbs'] = {d: {'verdict': 'NOT-ADVERTISED', 'text': str(e)} for d in ('AB', 'BA')}
                return
            codes.append(f'ABORTED:prbs {e}')
            raise _Stop from None
        obs['prbs'] = criteria.prbs_from_record(rec)
        directions = {d: r['verdict'] for d, r in rec['directions'].items()}
        session.event('prbs', data={'file': 'prbs.json', 'verdict': rec['verdict'], 'directions': directions,
                                    'lowpwr_after': {e: rec['ends'][e].get('lowpwr_after') for e in ('A', 'B')}})
        if any('THERMAL' in v for v in directions.values()):
            codes.append('THERMAL')
            raise _Stop

    for item in checklist:
        session.event('precheck' if 'checks' in item else 'checklist', data=item)
    soak = soak_s if soak_s is not None else float((crit or {}).get('soak_s', SOAK_S))
    gate = gate_s if gate_s is not None else float(((crit or {}).get('prbs') or {}).get('seconds_per_direction', PRBS_MIN_S))
    budget_w = float((jig_profile.get('power') or {}).get('budget_w') or BUDGET_W)
    for flag, value, default in (('--soak-s', soak_s, (crit or {}).get('soak_s', SOAK_S)),
                                 ('--gate-s', gate_s, ((crit or {}).get('prbs') or {}).get('seconds_per_direction', PRBS_MIN_S)),
                                 ('power.budget_w', budget_w if budget_w != BUDGET_W else None, BUDGET_W)):
        if value is not None:
            session.event('override', data={'flag': flag, 'value': value, 'default': default, 'source': 'jig profile' if flag[0] != '-' else 'flag'})
            say(f'override {flag} {value:g} (default {default:g})')
    if gate < PRBS_MIN_S:
        say(f'WARN: gate {gate:g} s is below the {PRBS_MIN_S:g} s minimum: a rehearsal, not a record', err=True)
    try:
        if crit is None:
            codes.append('NO-CRITERIA')
            raise _Stop
        step(2)  # both ends in one wait; an end already present is accepted (C-1); MgmtInit ms when an edge was seen
        say(' and '.join(f'Insert end {e} into unit {units[e]}' for e in ports) + '.')
        seen = watch_both(ports, timeout, say)
        for end, w in seen.items():
            present[end] = w['present']
            obs['timing'][end] = {'mgmt_init_ms': w['mgmt_init_ms'], 'edge_seen': w['edge']}
            session.event('insert', end, w)
            if w['present'] and on_insert:
                on_insert(end)  # C-3: the insertion is counted here, not after the test
        if not all(present.values()):
            codes.extend(f'NOT-PRESENT-{e}' for e in ports if not present.get(e))
            raise _Stop
        step(3)  # identity both ends; the SKU comparison is the criteria engine's at step 9 (and the early stop below)
        for end, port in ports.items():
            d = attempt(end, lambda: tests.identity(port))
            obs['ends'][end] = d
            session.manifest['cable'].setdefault('cmis', {})[end] = {k: d.get(k) for k in report.ID_ROWS}
            session.event('identity', end, d)
            say(f'{end}: {identity_line(d)} CMIS {d["cmis_revision"]} class {d["power_class"]} {d["max_power_w"]:.2f} W, state {d["module_state"]}')
            clk = ramp_clock(port, say)
            session.manifest['hardware'].setdefault('i2c', {})[units[end]] = clk
            session.event('i2c_clock', end, clk)
        step(4)  # low-power health, thresholds, raw pages
        for end, port in ports.items():
            h = attempt(end, lambda: tests.health(port))
            obs['low_power'][end] = {'module_state': h['state_name'], 'flags_byte9': h['flags9'], 'temp_c': h['temp_c'],
                                     'vcc_v': round(h['vcc_v'], 4)}
            session.write_health_row(0, end, h)
            session.event('health', end, {'t_s': 0, **h})
            say(f'  {end}: {tests.health_line(h)}')
            dump = attempt(end, lambda: tests.build_dump(port, units[end]))
            session.write_json(f'pages_{end}.json', dump)
            obs['high_power'][end] = {'thresholds': dump['decoded'].get('thresholds_02h')}
            p01 = dump['pages'].get('01h')
            vdm_adv[end] = bool(bytes.fromhex(p01)[P1_PAGES - 128] & P1_VDM_BIT) if p01 else False
            session.event('pages', end, {'file': f'pages_{end}.json', 'pages': sorted(dump['pages']), 'vdm_advertised': vdm_adv[end]})
        early = criteria.evaluate(crit, obs)
        if early.status == criteria.FAIL or 'THERMAL' in early.codes:  # PROPOSED: not the SKU, wrong in low power, or too warm: no high power
            fails = sorted({r.code for r in early.reasons if r.code != 'NOT-MEASURED'})
            session.event('stop', data={'text': 'identity or low-power criteria missed: no high-power step', 'codes': fails})
            say(f'STOP: {" ".join(fails)} -- no high-power step')
            raise _Stop
        step(5)  # preconditions, release both ends, DP activate, soak
        pre = safety.preconditions({'units': {e: e in ports for e in ('A', 'B')}, 'ends': tuple(ports), 'present': present,
                                    'max_power_w': max(d['max_power_w'] for d in obs['ends'].values())}, jig_profile, budget_w)
        if pre:
            codes.extend(pre)
            session.event('refused', data={'codes': pre})
            say(f'STOP: {" ".join(pre)} -- no high-power step')
            raise _Stop
        if any((obs['ends'].get(e) or {}).get('power_class', 0) >= 3 for e in ports):
            say(FORCED_AIR_LINE)
        for end, port in ports.items():
            try:
                info = attempt(end, lambda: tests.release(port, budget_w, timeout, say=prim))
            except Refused as e:
                codes.append(f'ABORTED:{end} release refused: {e}')
                session.event('refused', end, {'text': str(e)})
                raise _Stop from None
            released.add(end)
            obs['timing'][end]['module_ready_s'] = info['module_ready_observed_s']
            session.event('release', end, {k: info[k] for k in ('gate', 'thresholds_02h', 'temp_warn_c', 'temp_stop_c',
                                                                 'module_pwrup_advertised_01h_167', 'module_ready_observed_s',
                                                                 'module_ready_within_advertised')})
        for end, port in ports.items():
            states = attempt(end, lambda: tests.dp_activate(port, end, timeout, prim))
            obs['high_power'][end]['dp_states'] = [DP_STATES.get(c, c) for c in states]
            session.event('dp_activate', end, {'dp_states': obs['high_power'][end]['dp_states']})
        say(f'soak {soak:g} s: health at 1 s on both ends (WARN continues, THROTTLE stops)')
        samples, warned, reason, where = {e: [] for e in ports}, set(), None, None
        t0 = time.monotonic()
        for _ in range(int(round(soak))):
            for end, port in ports.items():
                h = attempt(end, lambda: tests.health(port))
                h['t_s'] = time.monotonic() - t0  # F-6: real seconds, not the loop index
                reason = tests.stop_reason(h, port, warned, end, prim)
                if reason:
                    for q in ports.values():
                        q.set_lpmode(1)
                samples[end].append(h)
                session.write_health_row(round(h['t_s'], 2), end, h)
                session.event('health', end, {**h, 't_s': round(h['t_s'], 2)})
                say(f'  +{h["t_s"]:6.1f} s  {end}: {tests.health_line(h)}')
                if reason:
                    where = end
                    break
            if reason:
                break
            time.sleep(1.0)
        for end in ports:
            s = samples[end]
            if s:
                obs['high_power'][end].update(module_state=s[-1]['state_name'], temp_c_max=max(h['temp_c'] for h in s),
                                              temp_slope_c_per_min=_slope(s), vcc_v=round(s[-1]['vcc_v'], 4))
        if reason:
            if reason.startswith('THERMAL'):
                codes.append('THERMAL')
                say(f'THROTTLE {where}: {reason} -> both ends in low power, test stopped, INCONCLUSIVE: THERMAL. {FORCED_AIR_MSG}')
            elif not samples[where][-1]['present']:
                codes.append('REMOVED-MID-TEST')
                say(f'STOP {where}: cable removed during the soak')
            else:
                codes.append(f'ABORTED:{where} {reason}')
                say(f'STOP {where}: {reason} -> both ends in low power')
            session.event('stop', where, {'text': reason})
            raise _Stop
        if 'B' in ports:
            step6()
        else:
            session.event('skip', data={'step': 6, 'name': STEPS[6], 'reason': 'one end tested'})
            say('step 6: prbs skipped (one end tested)')
        step(7)  # VDM snapshot if advertised (record only), FailureFlagTx 11h:135
        for end, port in ports.items():
            def read_vdm_flags():
                with port.selected():
                    page_2f = port.read_page(P_VDM_ADV).hex() if vdm_adv.get(end) else None
                    flag = port.read_page(0x11)[P11_FAILURE_TX - 128]
                return page_2f, flag
            page_2f, flag = attempt(end, read_vdm_flags)
            obs['vdm'][end] = {'advertised': vdm_adv.get(end, False), 'page_2fh': page_2f}
            obs['high_power'][end]['failure_flag_tx_11h_135'] = flag
            session.event('vdm', end, obs['vdm'][end])
            session.event('flags', end, {'11h:135 FailureFlagTx': f'0x{flag:02x}'})
    except _Stop:
        obs['stopped'] = {'step': cur[0], 'name': STEPS.get(cur[0], 'open')}
    except KeyboardInterrupt:
        codes.append('ABORTED:Ctrl-C')
        obs['stopped'] = {'step': cur[0], 'name': STEPS.get(cur[0], 'open')}
        session.event('abort', data={'error': 'KeyboardInterrupt'})
        say('')
    step(8)
    try:
        for end, port in ports.items():
            low = None
            try:
                port.set_lpmode(1)
                if end in released:
                    low = tests.wait_state(port, ST_LOWPWR, timeout, prim)
            except Nack:
                pass
            session.event('lowpwr', end, {'lowpwr_after': low})
    except KeyboardInterrupt:  # F-8: a Ctrl-C in the wait still ends with LPMode high, the report and the close
        codes.append('ABORTED:Ctrl-C')
        session.event('abort', data={'error': 'KeyboardInterrupt in step 8'})
        for port in ports.values():
            port.set_lpmode(1)
    if last:
        for port in ports.values():
            port.tristate()
        say(TRISTATE_LAST)
    remove = 'Remove the cable from ' + ' and '.join(units[e] for e in ports) + '.'
    say(remove)
    step(9)
    verdict = criteria.evaluate(crit, obs)
    page = write_report(session, verdict)
    session.close(verdict)
    say(page)
    say(verdict_line(verdict.status, verdict.codes))
    say(f'session: {session.dir}')
    ask(remove)
    return verdict


# --------------------------------------------------------------------------- the subcommand
def criteria_path(sku, root=CRITERIA_DIR):
    """`--sku` is a file path, or a name under `root` (--criteria DIR; default <repo>/criteria or $AECTEST_CRITERIA, F-3)."""
    return sku if os.path.exists(sku) else os.path.join(root, f'{sku}.yaml')


def load_criteria(sku, root=CRITERIA_DIR):
    """The SKU file of record for a test: a calibration candidate (a `calibration` block) is refused."""
    crit = criteria.load(criteria_path(sku, root))
    if crit and 'calibration' in crit:
        raise SystemExit(f'{crit["file"]} is a calibration candidate, not the file of record: adopt it after a review, then test with the SKU file')
    return crit


def add_tech(sub):
    ap = sub.add_parser('tech', help='technician test of one cable (ARCHITECTURE 3.3)')
    verbs = ap.add_subparsers(dest='tech_verb', required=True)
    run = verbs.add_parser('run', help='steps 0-9 on one cable: checklist, open, insert, identity, soak, PRBS, verdict, report.md')
    run.add_argument('--jig', required=True, metavar='PROFILE', help='signed jig profile (jig.py sign)')
    run.add_argument('--sku', required=True, help='SKU name under --criteria, a criteria file path, or auto (read the cable, match the database)')
    run.add_argument('--criteria', default=CRITERIA_DIR, metavar='DIR', help=f'criteria directory; default {CRITERIA_DIR}')
    run.add_argument('--sn', '--label-sn', dest='label_sn', metavar='SN', help='the scanned label serial (compared with CMIS 00h:166-181)')
    run.add_argument('--operator', metavar='NAME')
    run.add_argument('--out', default=OUT_DIR, help=f'session directory root (5.1); default {OUT_DIR}')
    run.add_argument('--soak-s', type=float, metavar='S', help='override the soak (recorded as an override event)')
    run.add_argument('--gate-s', type=float, metavar='S', help='override the PRBS gate per direction (recorded as an override event)')
    run.add_argument('--dry-run', action='store_true', help='fake FT2232H + the fake image of --sku (no hardware)')
    run.add_argument('--tui', action='store_true', help='the one-page technician screen (rich Live); plain lines when stdout is not a terminal')
    run.set_defaults(func=run_tech)
    last = verbs.add_parser('last', help='print the newest report.md under --out (2.8)')
    last.add_argument('--out', default=OUT_DIR, help=f'session directory root; default {OUT_DIR}')
    last.set_defaults(func=run_last)
    return ap


def run_last(a):
    pages = glob.glob(os.path.join(a.out, '**', 'report.md'), recursive=True)
    if not pages:
        raise SystemExit(f'no report.md under {a.out}')
    newest = max(pages, key=os.path.getmtime)
    say_screen(open(newest).read())
    say_screen(f'report: {newest}')
    return 0


def run_tech(a):
    from .cli import install_signal_stop  # here, not at the top: cli imports this module
    install_signal_stop()
    profile = load_profile(a.jig)
    if a.sku == 'auto':
        from . import derive
        p = _open(profile['units']['A'], profile.get('pid') or STOCK_PID, a.dry_run, None, False, 0)
        try:
            m = derive.match(tests.identity(p), a.criteria)
        finally:
            p.close()
        a.sku = m['sku'] or 'none'
        print(f"auto: {m['identity'].get('vendor')} {m['identity'].get('part_number')} -> {'criteria ' + a.sku if m['matched'] else 'no criteria (NO-CRITERIA)'}")
    crit = load_criteria(a.sku, a.criteria)
    image = fake_image(crit, a.sku, a.dry_run)
    name = a.label_sn or (crit['sku'] if crit else 'cable')
    from .tui import Screen  # here, not at the top: tui imports this module
    with Screen(profile['units'], a.sku, a.operator) if a.tui else Screen.plain() as scr, \
            session_for(a.out, 'tech', name, profile, crit, a.label_sn, a.operator, dry_run=a.dry_run) as s:
        say, ask, s.on_event = scr.say, scr.ask, scr.on_event
        if not crit:
            s.manifest['cable']['sku'] = a.sku
        try:
            ports, checklist = open_units(profile, say, ask, a.dry_run, image)
        except Stopped as e:
            s.event('refused', data={'text': e.text, 'code': e.code})
            v = Verdict(INCONCLUSIVE, [Reason(e.code, 'open_units', e.text)])
            write_report(s, v)
            s.close(v)
            say(f'{v.status} {e.code}: {e.text}')
            say(f'session: {s.dir}')
            return 1
        try:
            v = run_cable(s, profile, crit, ports, a.label_sn, say, ask, checklist, a.soak_s, a.gate_s)
        finally:
            for p in ports.values():
                p.close()
    return 0 if v.status == 'PASS' else 1
