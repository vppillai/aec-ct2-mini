"""lot.py - the incoming inspection of a lot on tech.open_units / tech.run_cable, one process per lot.

aectest inspect start --lot ID --sku SKU --jig PROFILE --operator NAME [--out out/test] [--dry-run] [--soak-s S] [--gate-s S]
    the lot manifest out/test/lot_<ID>/lot.json (schema aectest.lot/2), the pre-checks and the S2 step once (tech.open_units),
    both units kept open (2.2: one open per session), then the loop: scan the label -> 3.3 steps 2-9 (tech.run_cable,
    last=False) -> scan, until the technician types `done` (2.8) or a stop rule fires. The loop end tristates both units,
    writes `ended`, lot.csv and lot.md. A lot that exists and is not stopped continues (`reopened` in lot.json).
    The state file out/test/.inspect_lot ("<lot dir> <pid>") is written after the open and refuses a second loop while
    that pid lives.
aectest inspect resume  --lot ID --reason TEXT   records an override and lifts the stop; `start` then continues the lot.
aectest inspect summary --lot ID                 lot.csv + lot.md from every session directory under the lot (C-3).
aectest inspect export  --lot ID                 summary, then one zip of the lot directory (records.export_zip).

Per cable (C-3): the insertion of every present end is counted at step 2 (`on_insert`, UnitState 5.4); the lot row is
written in a `finally` from the session manifest, so a crash or a Ctrl-C mid-test still leaves the row and the counts.
Stop rules (3.4, PROPOSED): a unit at WEAR_WARN_CYCLES of WEAR_RATING_CYCLES insertions prints a wear warning at every
insertion; STOP_AFTER_INCONCLUSIVE consecutive INCONCLUSIVE verdicts that name the same unit (a `-A` / `-B` code; a code
without an end names both, C-7) stop the lot with "check the jig" and end the loop.
The end swap: a NO-ACK-A|B verdict prints "swap ends and re-run" and remembers the unit per label SN. The same label scanned
again asks "swap the ends" (recorded as an `ask` event); NO-ACK on the other unit after a swap is the same cable end on
both units: the lot row reads FAIL NO-ACK-END. The session keeps its own INCONCLUSIVE record (5.4); lot.json carries the
lot verdict and summary applies it.
"""
import glob
import json
import os

from . import criteria, report, tech
from .criteria import CODES
from .jig import load_profile
from .records import LOT_COLUMNS, OUT_DIR, UnitState, export_zip, pid_alive, utc
from .safety import TRISTATE_LAST

LOT_SCHEMA = 'aectest.lot/2'
STATE_FILE = '.inspect_lot'
WEAR_WARN_CYCLES, WEAR_RATING_CYCLES = 80, 100  # 3.4 PROPOSED: MSA Table 21 connector rating, test plan T12
STOP_AFTER_INCONCLUSIVE = 3  # 3.4 PROPOSED: consecutive INCONCLUSIVE on one unit
SWAP_HINT = 'swap ends and re-run'
SCAN_PROMPT = 'Scan the label of the next cable, or type done: '
SWAP_LINE = 'Swap the ends of cable {}.'
RESULT, REASONS, SESSION_ID = LOT_COLUMNS.index('result'), LOT_COLUMNS.index('reasons'), LOT_COLUMNS.index('session_id')


def scan_tty(prompt, read=None):
    """The default scan: one line from the terminal; Ctrl-C / EOF end the lot like `done`. `read` is the screen's own input (tui.py)."""
    read = read or input
    try:
        return read(prompt)
    except (EOFError, KeyboardInterrupt):
        if read is input:  # the screen's own input restarts its Live page: a bare newline would land inside it
            print()
        return 'done'


def _state_path(out):
    return os.path.join(out, STATE_FILE)


def _lot(out, lot_id):
    """(lot_dir, lot dict) of lot_<ID> under out, or SystemExit when there is none."""
    lot_dir = os.path.join(out, f'lot_{lot_id}')
    try:
        with open(os.path.join(lot_dir, 'lot.json')) as f:
            return lot_dir, json.load(f)
    except FileNotFoundError:
        raise SystemExit(f'no lot {lot_id} under {out} (no {lot_dir}/lot.json)') from None


def _save(lot_dir, lot):
    tmp = os.path.join(lot_dir, 'lot.json.tmp')
    with open(tmp, 'w') as f:
        json.dump(lot, f, indent=1, default=str)
    os.replace(tmp, os.path.join(lot_dir, 'lot.json'))


# --------------------------------------------------------------------------- one cable
def _touch(out, unit, session_id, sn, say):
    """The insertion count of one unit at step 2 (5.4) and the wear warning (3.4)."""
    n = UnitState(out, unit).touch(session_id, 'insert', cable_serial=sn, test_ref='T11 (inspect)')['insertions']
    if n >= WEAR_WARN_CYCLES:
        say(f'WARN wear: unit {unit} is at {n} of {WEAR_RATING_CYCLES} insertions (MSA Table 21, T12)', err=True)


def _bookkeep(lot, sn, session_id, status, codes, swapped, say):
    """The end swap per label, the lot row, the jig stop rule. Returns the lot status of this cable."""
    noack = [c for c in codes if c in ('NO-ACK-A', 'NO-ACK-B')]
    pending = lot['pending_swap']
    if noack and swapped and sn in pending and pending[sn]['unit_end'] != noack[0][-1]:  # the same cable end on both units
        status, codes = criteria.FAIL, codes + ['NO-ACK-END']
        del pending[sn]
        say(f'FAIL NO-ACK-END: {CODES["NO-ACK-END"]}')
    elif noack:
        pending[sn] = {'unit_end': noack[0][-1], 'session_id': session_id}
        say(f'{status} {noack[0]}: {SWAP_HINT}')
    else:
        pending.pop(sn, None)
    lot['cables'].append({'sn': sn, 'session_id': session_id, 'status': status, 'codes': codes, 'swapped': swapped, 'ended': utc()})
    run = lot['inconclusive_run']
    if status == criteria.INCONCLUSIVE:
        ends = {c[-1] for c in codes if c[-2:] in ('-A', '-B')} or {'A', 'B'}
        for end in run:
            run[end] = run[end] + 1 if end in ends else 0
        hit = [end for end in run if run[end] >= STOP_AFTER_INCONCLUSIVE]
        if hit:
            reason = f'{STOP_AFTER_INCONCLUSIVE} consecutive INCONCLUSIVE on unit {lot["units"][hit[0]]}: check the jig'
            lot['stopped'] = {'t': utc(), 'reason': reason, 'session_id': session_id}
            say(f'STOP: {reason}. Run `inspect resume --lot {lot["lot"]} --reason <text>`. Then run `inspect start` again.')
    else:
        run.update(A=0, B=0)
    return status


def _run_one(lot_dir, lot, profile, crit, ports, sn, say, ask, out, soak_s, gate_s, on_event=None):
    """One cable on the open units: the swap ask (C-4), the session, run_cable, the lot row in a `finally` (C-3)."""
    swapped = False
    if sn in lot['pending_swap']:
        swapped = ask(SWAP_LINE.format(sn))
    s = tech.session_for(lot_dir, 'inspect', sn, profile, crit, sn, lot['operator'], lot['lot'], dry_run=lot['dry_run'])
    if sn in lot['pending_swap']:
        s.event('ask', data={'line': SWAP_LINE.format(sn), 'answer': swapped})
    try:
        with s:
            tech.run_cable(s, profile, crit, ports, sn, say, ask, lot.get('prechecks') or lot.get('checklist') or (), soak_s, gate_s, last=False,
                           on_insert=lambda end: _touch(out, lot['units'][end], s.session_id, sn, say), on_event=on_event)
    finally:
        v = s.manifest.get('verdict') or {'status': criteria.INCONCLUSIVE, 'reasons': ['ABORTED:no verdict']}
        status = _bookkeep(lot, sn, s.session_id, v['status'], [criteria.code_of(r) for r in v.get('reasons') or []], swapped, say)
        _save(lot_dir, lot)
    return status


# --------------------------------------------------------------------------- verbs
def start(a, say, ask, read=scan_tty, on_event=None, profile=None):
    st = _state_path(a.out)
    if os.path.exists(st):
        lot_dir, _, pid = open(st).read().strip().rpartition(' ')
        if pid_alive(pid):
            raise SystemExit(f'a lot is open in process {pid} ({lot_dir}): type `done` there first')
    profile = profile or load_profile(a.jig)
    crit = tech.load_criteria(a.sku, a.criteria)
    if crit is None:
        raise SystemExit(f'no criteria file for {a.sku} (NO-CRITERIA)')
    lot_dir = os.path.join(a.out, f'lot_{a.lot}')
    os.makedirs(lot_dir, exist_ok=True)
    if os.path.exists(os.path.join(lot_dir, 'lot.json')):
        lot = _lot(a.out, a.lot)[1]
        if lot['stopped']:
            raise SystemExit(f'lot {a.lot} is stopped: {lot["stopped"]["reason"]}. Run `inspect resume --lot {a.lot} --reason <text>` first')
        lot['reopened'].append(utc())
        lot['ended'] = None
    else:
        lot = {'schema': LOT_SCHEMA, 'lot': a.lot, 'sku': crit['sku'], 'criteria_file': crit['file'], 'criteria_sha256': crit['sha256'],
               'jig': profile['jig'], 'jig_profile': os.path.abspath(a.jig), 'jig_profile_sha256': profile['sha256'],
               'units': dict(profile['units']), 'operator': a.operator, 'started': utc(), 'ended': None, 'reopened': [],
               'dry_run': bool(a.dry_run), 'prechecks': [], 'cables': [], 'inconclusive_run': {'A': 0, 'B': 0}, 'pending_swap': {},
               'stopped': None, 'overrides': []}
    ports, lot['prechecks'] = tech.open_units(profile, say, ask, a.dry_run, tech.fake_image(crit, a.sku, a.dry_run))
    _save(lot_dir, lot)
    with open(st, 'w') as f:
        f.write(f'{lot_dir} {os.getpid()}\n')
    say(f'lot {a.lot} open: {lot_dir}')
    try:
        while True:
            sn = read(SCAN_PROMPT).strip()
            if sn.lower() in ('done', ''):
                break
            _run_one(lot_dir, lot, profile, crit, ports, sn, say, ask, a.out, a.soak_s, a.gate_s, on_event)
            say(f'lot {a.lot}: {len(lot["cables"])} cables.')
            if lot['stopped']:
                break
    finally:
        try:
            for p in ports.values():
                p.tristate()
        finally:
            for p in ports.values():
                p.close()
        say(TRISTATE_LAST)
        say('Remove the cable. The switches stay as shipped.')
        lot['ended'] = utc()
        _save(lot_dir, lot)
        _summary(a.out, lot_dir, lot, say)
        os.remove(st)
        say(f'lot {a.lot} closed: {len(lot["cables"])} cables')
    return 1 if lot['stopped'] else 0


def resume(a, say, ask):
    lot_dir, lot = _lot(a.out, a.lot)
    if not lot['stopped']:
        say(f'lot {lot["lot"]} is not stopped')
        return 1
    lot['overrides'].append({'t': utc(), 'reason': a.reason, 'stopped': lot['stopped'], 'operator': lot['operator']})
    lot['stopped'] = None
    lot['inconclusive_run'] = {'A': 0, 'B': 0}
    _save(lot_dir, lot)
    say(f'override recorded: {a.reason}. Run `inspect start --lot {lot["lot"]}` to go on.')
    return 0


def _summary(out, lot_dir, lot, say):
    by = {c['session_id']: c for c in lot['cables']}
    rows = report.lot_rows(sorted(os.path.dirname(m) for m in glob.glob(os.path.join(lot_dir, '*', 'manifest.json'))))
    for r in rows:  # the lot verdict (NO-ACK-END) over the session verdict
        c = by.get(r[SESSION_ID])
        if c is None:
            continue  # a session with no lot row (the process died before its finally): the manifest verdict stands
        r[RESULT] = c['status']
        extra = [k for k in c['codes'] if k not in (r[REASONS] or '')]
        if extra:
            r[REASONS] = ' | '.join(filter(None, [r[REASONS], *extra]))
    report.lot_csv(rows, os.path.join(lot_dir, 'lot.csv'))
    L = [report.lot_markdown(rows), '## Lot', '', f'- Lot: {lot["lot"]}. SKU: {lot["sku"]}. Jig: {lot["jig"]}. Operator: {lot["operator"] or "-"}.',
         f'- Started: {lot["started"]}. Ended: {lot["ended"] or "open"}. Criteria sha256: {lot["criteria_sha256"]}.']
    if lot['stopped']:
        L.append(f'- Stopped: {lot["stopped"]["reason"]} ({lot["stopped"]["t"]}).')
    L += [f'- Override {o["t"]}: {o["reason"]}' for o in lot['overrides']]
    L += ['', '## Unit wear', '']
    for end, unit in lot['units'].items():
        n = UnitState(out, unit).state['insertions']
        L.append(f'- Unit {end} {unit}: {n} of {WEAR_RATING_CYCLES} insertions.' + (' Replace it soon.' if n >= WEAR_WARN_CYCLES else ''))
    with open(os.path.join(lot_dir, 'lot.md'), 'w') as f:
        f.write('\n'.join(L) + '\n')
    say(f'lot {lot["lot"]}: {len(rows)} cables -> {lot_dir}/lot.csv, lot.md')


def summary(a, say, ask):
    lot_dir, lot = _lot(a.out, a.lot)
    _summary(a.out, lot_dir, lot, say)
    return 0


def export(a, say, ask):
    summary(a, say, ask)
    lot_dir = _lot(a.out, a.lot)[0]
    try:
        z = export_zip(lot_dir)
    except FileExistsError:
        raise SystemExit(f'not overwriting {lot_dir}.zip: move it away first (records are per run)') from None
    say(f'wrote {z}')
    return 0


VERBS = {'start': start, 'resume': resume, 'summary': summary, 'export': export}


# --------------------------------------------------------------------------- the subcommand
def add_inspect(sub):
    ap = sub.add_parser('inspect', help='incoming inspection of a lot (ARCHITECTURE 3.4)')
    verbs = ap.add_subparsers(dest='inspect_verb', required=True)
    helps = {'start': 'open both units once, then scan -> test -> scan until `done`; an existing lot continues',
             'resume': 'record an override and lift the stop', 'summary': 'lot.csv and lot.md from the session directories',
             'export': 'summary, then one zip of the lot directory'}
    for name, text in helps.items():
        v = verbs.add_parser(name, help=text)
        v.add_argument('--lot', required=True, metavar='ID')
        v.add_argument('--out', default=OUT_DIR, help=f'session directory root (5.1); default {OUT_DIR}')
    s = verbs.choices['start']
    s.add_argument('--sku', required=True, help='SKU name under --criteria or a criteria file path')
    s.add_argument('--criteria', default=tech.CRITERIA_DIR, metavar='DIR', help=f'criteria directory; default {tech.CRITERIA_DIR}')
    s.add_argument('--jig', required=True, metavar='PROFILE', help='signed jig profile (jig.py sign)')
    s.add_argument('--operator', required=True, metavar='NAME')
    s.add_argument('--soak-s', type=float, metavar='S', help='override the soak (recorded as an override event per cable)')
    s.add_argument('--gate-s', type=float, metavar='S', help='override the PRBS gate per direction (recorded as an override event per cable)')
    s.add_argument('--dry-run', action='store_true', help='fake FT2232H + the fake image of --sku (no hardware)')
    s.add_argument('--tui', action='store_true', help='the one-page technician screen (rich Live); plain lines when stdout is not a terminal')
    verbs.choices['resume'].add_argument('--reason', required=True, help='why the lot goes on (recorded as an override)')
    ap.set_defaults(func=run_inspect)
    return ap


def run_inspect(a):
    from .cli import install_signal_stop  # here, not at the top: cli imports this module
    install_signal_stop()
    if a.inspect_verb != 'start' or not a.tui:
        return VERBS[a.inspect_verb](a, tech.say_screen, tech.ask_tty)
    from .tui import Screen  # here, not at the top: tui imports this module
    profile = load_profile(a.jig)
    with Screen(profile['units'], a.sku, a.operator, a.lot) as scr:
        return start(a, scr.say, scr.ask, scr.read, scr.on_event, profile)
