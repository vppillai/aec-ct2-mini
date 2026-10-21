"""rd.py - the R&D session: `aectest rd <verb>` on the primitives.

One process per verb. `open` creates a records.Session under --out (default records.OUT_DIR) and, once the port is open,
writes its path to <out>/.rd_session (one line); every later verb attaches to that session, runs one primitive, appends
its event (schema aectest.event/1) and exits; `close` writes the manifest and removes the state file. The connection
(--serial, --dry-run, --fake-sku, --speed, --pid) given to `open` is kept in the manifest `hardware.connection`, so the
later verbs need no flags; a flag on a later verb wins. A pyftdi other than the pinned one refuses before any real open
(cli.check_pyftdi, --any-pyftdi).

Guards stay the bring-up ones: `release`, `hold`, `prbs` and `lpmode` drive LPMode low only with --i-am-on-usbc-power (the
verb name is permission flag 1, S10); the flag is recorded as an `acknowledgement` event. The recorded overrides (3.2)
are --force-cmis, --budget-w, --assume-w, --allow-short, --lane-mask and the temperature flags: each one is an
`override` event before the verb runs.

Every verb re-opens the FT2232H (the UART-mode window of transport.py): `script FILE` runs its lines on one open, like the
bring-up shell; the script opens unarmed, an LPMode line with --i-am-on-usbc-power arms the lines after it. `dump` is
`pages 00 01 02 10 11` (2.8). `log start` forks `rd log _run`, a 1 s health poll into events.jsonl and health.csv until
`log stop`; `start` waits for the child's first row, `stop` kills only a pid whose command line is that child.
The event types `thresholds`, `cdb`, `reset`, `watch`, `lpmode`, `script` and `log` are additions to the record schema.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

from . import cmis
from .cli import POSTURES, check_pyftdi, install_signal_stop, open_port, say
from .cmis import L_CDB_STATUS, L_MEM, P1_BIAS_SCALE, P1_PAGES, P1_VDM_BIT, P_VDM_ADV, P_VDM_DESC, P_VDM_SAMPLE, PATTERNS, ST_LOWPWR, ST_READY, STATES
from .jig import hardware_manifest, load_profile
from .records import OUT_DIR, Session, open_new, tool_block
from .safety import BUDGET_W, GUARD, JIG_TEMP_LIMIT_C, PRBS_MIN_S
from .tests import cmd_id, cmd_prbs, cmd_reset, cmd_watch, hold_high_power, log_meta, poll_health, release_lpmode, show, wait_state
from .transport import Nack, Refused

STATE = os.path.join(OUT_DIR, '.rd_session')  # the default; `state_path(a.out)` for another --out
DUMP_PAGES = [0x00, 0x01, 0x02, 0x10, 0x11]  # `rd dump` (2.8) = `pages 00 01 02 10 11`
ACK_TEXT = 'a 3 A charger is on PWR ONLY and the host on PC (S10)'
CONN = ('serial', 'dry_run', 'fake_sku', 'speed', 'pid')
OVERRIDES = ('force_cmis', 'budget_w', 'assume_w', 'allow_short', 'lane_mask', 'temp_limit_c', 'temp_warn_c', 'temp_throttle_c',
             'external_temp_ack')
LP_VERBS = ('release', 'hold', 'prbs', 'lpmode')  # the verb name is permission flag 1; --i-am-on-usbc-power is flag 2
SCRIPT_VERBS = ('id', 'pages', 'thresholds', 'vdm', 'cdb', 'release', 'hold', 'reset', 'watch', 'lpmode')
NOT_JUDGED = {'status': 'NOT-JUDGED', 'reasons': ['RD-SESSION'], 'key_numbers': {}}


# --------------------------------------------------------------------------- parser
def add_rd(sub):
    """The `rd` subparser: common flags on every verb (parents=), one positional set per verb. Registers run_rd as `run`."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--serial', help='FT2232H serial (kept from `open` when omitted)')
    common.add_argument('--pid', type=lambda s: int(s, 0), help='custom USB PID')
    common.add_argument('--speed', choices=('100k', '400k'), help='I2C clock (default 100k)')
    common.add_argument('--dry-run', action='store_true', help='fake FT2232H + fake CMIS module (no hardware)')
    common.add_argument('--fake-sku', metavar='SKU', help='--dry-run: answer with fake_modules/<SKU>.yaml')
    common.add_argument('--i-am-on-usbc-power', action='store_true', help='permission flag 2 of 2 (S10) for release / hold / prbs / lpmode')
    common.add_argument('--any-pyftdi', action='store_true', help='run on a pyftdi other than the pinned one')
    common.add_argument('--json', action='store_true', help='id: JSON instead of the table')
    common.add_argument('--timeout', type=float, default=15.0, help='seconds to wait for ModuleReady / an insertion')
    common.add_argument('--out', default=OUT_DIR, help=f'session directory root and state file location; default {OUT_DIR}')
    common.add_argument('--budget-w', type=float, help=f'release / hold / prbs: power budget (default {BUDGET_W:g} W; recorded as an override)')
    common.add_argument('--assume-w', type=float, metavar='W', help='class-8 module with MaxPower 0 (override)')
    common.add_argument('--posture', choices=POSTURES)
    common.add_argument('--ambient-c', type=int, choices=(25, 35), default=25)
    common.add_argument('--temp-limit-c', type=float, metavar='C', help=f'jig cap on THROTTLE (default {JIG_TEMP_LIMIT_C:g}; override)')
    common.add_argument('--temp-warn-c', type=float, metavar='C', help='WARN level (override)')
    common.add_argument('--temp-throttle-c', type=float, metavar='C', help='THROTTLE level (override)')
    common.add_argument('--external-temp-ack', action='store_true', help='module without a temperature monitor: a human watches (override)')
    common.add_argument('--force-cmis', action='store_true', help='prbs: Page 13h/14h writes on CMIS < 5.0 (override)')
    common.add_argument('--allow-short', action='store_true', help=f'prbs: --time below {PRBS_MIN_S:g} s (override)')
    common.add_argument('--lane-mask', type=lambda x: int(x, 0), metavar='0xNN', help='prbs: lanes to enable (override)')
    ap = sub.add_parser('rd', help='R&D session: open, id, pages, thresholds, vdm, cdb fw-info, release, hold, prbs, reset, watch, script, log, close',
                        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.set_defaults(run=run_rd, rd_parser=ap)
    v = ap.add_subparsers(dest='rd_verb', required=True)
    v.add_parser('open', parents=[common], help='open a session (S2 line; then SW_MODE -> AUTO)').add_argument('--jig', metavar='PROFILE', help='signed jig profile (2.9)')
    v.add_parser('id', parents=[common], help='identity + 01h durations + firmware version')
    pg = v.add_parser('pages', parents=[common], help='raw pages, hex, into pages_NNN.json')
    pg.add_argument('pages', nargs='+', type=lambda x: int(x.rstrip('hH'), 16), metavar='PAGE', help='e.g. 00 01 02 10 11 13 14')
    pg.add_argument('--bank', type=int, default=0)
    v.add_parser('dump', parents=[common], help='pages 00 01 02 10 11 (2.8)')
    v.add_parser('thresholds', parents=[common], help='Page 02h module and lane thresholds (Tables 8-55 / 8-56)')
    v.add_parser('vdm', parents=[common], help='VDM advertisement, descriptors and samples (read-only)')
    v.add_parser('cdb', parents=[common], help='CDB status + advertisement, read-only').add_argument('cdb_cmd', choices=('fw-info',))
    v.add_parser('release', parents=[common], help='release LPMode, time ModuleReady, back to low power')
    h = v.add_parser('hold', parents=[common], help='release and hold high power S seconds')
    h.add_argument('seconds', type=float)
    h.add_argument('--log', action='store_true', help='per-run T7 CSV hold_NNN.csv in the session')
    pr = v.add_parser('prbs', parents=[common], help='media-side PRBS both ways between two sticks')
    pr.add_argument('--pair', metavar='A:B', help='FT2232H serials (default FAKE-A:FAKE-B in --dry-run)')
    pr.add_argument('--pattern', default='prbs31q', choices=sorted(PATTERNS, key=PATTERNS.get))
    pr.add_argument('--time', type=float, default=PRBS_MIN_S, help='seconds per direction')
    v.add_parser('reset', parents=[common], help='ResetL pulse, time to the first ACK')
    v.add_parser('watch', parents=[common], help='wait for an insertion, time to the first ACK')
    lp = v.add_parser('lpmode', parents=[common], help='drive LPMode (script timing capture)')
    lp.add_argument('level', type=int, choices=(0, 1))
    v.add_parser('script', parents=[common], help='one verb per line on one open').add_argument('file')
    v.add_parser('log', parents=[common], help='1 s health into events.jsonl + health.csv').add_argument('action', choices=('start', 'stop', '_run'))
    v.add_parser('close', parents=[common], help='stop a running log, write the manifest, remove the state file')
    return ap


# --------------------------------------------------------------------------- session glue
def state_path(out):
    return os.path.join(out, '.rd_session')


def attach(out):
    st = state_path(out)
    if not os.path.exists(st):
        raise SystemExit(f'no open rd session ({st} missing): run `aectest rd open` first')
    s_dir = open(st).read().strip()
    if not os.path.isfile(os.path.join(s_dir, 'manifest.json')):
        raise SystemExit(f'the rd session directory {s_dir} is gone: remove {st}, then `aectest rd open` again')
    return Session.attach(s_dir)


def open_rd_port(a, ns, allow):
    """cli.open_port behind the pinned-pyftdi gate: a real open on another pyftdi refuses unless --any-pyftdi."""
    if not ns.dry_run:
        check_pyftdi(a.any_pyftdi)
    return open_port(ns.serial, ns, allow)


def connection(a, s):
    """The port flags: the verb's own flag, else the one `open` recorded in the manifest."""
    conn = s.manifest.get('hardware', {}).get('connection', {}) if s else {}
    ns = argparse.Namespace(**{k: getattr(a, k, None) or conn.get(k) for k in CONN})
    ns.speed = ns.speed or '100k'
    return ns


def record_overrides(s, a):
    if getattr(a, 'i_am_on_usbc_power', False):
        s.event('acknowledgement', data={'flag': '--i-am-on-usbc-power', 'text': ACK_TEXT})
    for k in OVERRIDES:
        val = getattr(a, k, None)
        if val not in (None, False):
            s.event('override', data={'flag': '--' + k.replace('_', '-'), 'value': val})


def needs_lp(a):
    """The verbs that drive LPMode low; `lpmode 1` returns to low power and needs no flag."""
    return a.rd_verb in LP_VERBS and not (a.rd_verb == 'lpmode' and a.level == 1)


def port_for(a, ns):
    allow = needs_lp(a) and a.i_am_on_usbc_power
    if needs_lp(a) and not allow:
        raise Refused(GUARD)
    return open_rd_port(a, ns, allow)


def serial_of(p, ns):
    return ns.serial or getattr(p.be, 'serial', '?')


# --------------------------------------------------------------------------- verbs on one open port
def v_id(p, s, a, ns):
    d = cmd_id(p, a.json, say)
    if d['memory_model'] == 'paged':
        with p.selected():
            p1 = p.read_page(0x01)
            lower = p.lower()
        d['durations_01h'] = cmis.decode_durations(p1)
        d['firmware'] = cmis.decode_firmware(lower, p1)
        show({k: v['code'] for k, v in d['durations_01h'].items() if isinstance(v, dict)} | {'modsel_wait_us': d['durations_01h']['modsel_wait_us']},
             a.json, say)
        show(d['firmware'], a.json, say)
    s.event('identity', data=d)
    return d


def v_pages(p, s, a, ns):
    with p.selected():
        lower = p.lower()
        flat = bool(lower[L_MEM] & 0x80)
        pages = {}
        for pg in a.pages:
            try:
                pages[f'{pg:02X}h'] = p.read_page(pg, bank=a.bank, flat=flat).hex()
            except Nack as e:
                pages[f'{pg:02X}h'] = f'NACK ({e})'
    name = f'pages_{s.seq + 1:03d}.json'
    s.write_json(name, {'bank': a.bank, 'lower': lower.hex(), 'pages': pages})
    for k, v in pages.items():
        say(f'{k} bank {a.bank}: {v}')
    say(f'wrote {s.path(name)}')
    s.event('pages', data={'file': name, 'bank': a.bank, 'pages': list(pages)})
    return pages


def v_thresholds(p, s, a, ns):
    with p.selected():
        p1, p2 = p.read_page(0x01), p.read_page(0x02)
    d = cmis.decode_thresholds(p2, lanes=True, bias_scale_bits=(p1[P1_BIAS_SCALE - 128] >> 3) & 3)
    show(d, a.json, say, 28)
    s.event('thresholds', data=d)
    return d


def v_vdm(p, s, a, ns):
    with p.selected():
        p1 = p.read_page(0x01)
        adv = cmis.decode_vdm_advert(p1, p.read_page(P_VDM_ADV) if p1[P1_PAGES - 128] & P1_VDM_BIT else None)
        pages = {}
        for g in range(adv['groups'] or 0):
            pages[P_VDM_DESC + g], pages[P_VDM_SAMPLE + g] = p.read_page(P_VDM_DESC + g), p.read_page(P_VDM_SAMPLE + g)
    rows = cmis.decode_vdm_descriptors(pages)
    d = {**adv, 'instances': rows, 'raw': {f'{k:02X}h': v.hex() for k, v in pages.items()}}
    say(f'VDM advertised (01h:142.6): {adv["advertised_01h_142_6"]}; groups {adv["groups"]}; fine interval {adv["fine_interval_ms"]} ms; '
        f'{len(rows)} instances in use')
    for r in rows:
        say(f'  VDM {r["instance"]:3d} {r["page"]}:{r["byte"]} type {r["type"]:3d} {r["name"]:58} {r["resource"]:8} set {r["threshold_set"]:2d}'
            f'  sample {r.get("sample")} {r["unit"]}' + ('  [AEC observable]' if r['aec_observable'] else ''))
    s.event('vdm', data=d)
    return d


def v_cdb(p, s, a, ns):
    with p.selected():
        lower, p1 = p.lower(), p.read_page(0x01)
        adv = cmis.decode_cdb_advert(p1)
        n = adv['instances'] if isinstance(adv['instances'], int) else 2
        d = {'advert_01h_163': adv, 'status': {f'00h:{off}': cmis.decode_cdb_status(lower[off]) for off in L_CDB_STATUS[:max(1, n)]},
             'firmware': cmis.decode_firmware(lower, p1), 'reply_header_9fh': None, 'note': 'read-only: no CDB command sent'}
        if n:
            try:
                d['reply_header_9fh'] = cmis.decode_cdb_reply_header(p.read_page(0x9F))
            except Nack as e:
                d['reply_header_9fh'] = f'NACK ({e})'
    say(json.dumps(d, indent=1))
    s.event('cdb', data={'verb': a.cdb_cmd, **d})
    return d


def _release(p, s, a):
    info = release_lpmode(p, a.budget_w if a.budget_w is not None else BUDGET_W, a.timeout, a.assume_w, a.posture, a.ambient_c,
                          a.temp_limit_c if a.temp_limit_c is not None else JIG_TEMP_LIMIT_C, a.temp_warn_c, a.temp_throttle_c,
                          a.external_temp_ack, say)
    s.event('release', data=info)
    return info


def v_release(p, s, a, ns):
    info = _release(p, s, a)
    p.set_lpmode(1)
    say('LPMode returned HIGH (one process per verb; `hold S` keeps high power)')
    return info


def v_hold(p, s, a, ns):
    info = _release(p, s, a)
    name = f'hold_{s.seq:03d}.csv' if a.log else None
    f = open_new(s.path(name)) if a.log else None
    stop = hold_high_power(p, a.seconds, logf=f, meta=log_meta(info, a, serial_of(p, ns)) if f else None, say=say)
    s.event('hold', data={'seconds': a.seconds, 'stop': stop, 'file': name})
    return stop


def v_reset(p, s, a, ns):
    dt = cmd_reset(p, say)
    s.event('reset', data={'first_ack_s': dt, 'limit_s': cmis.T_MGMT_INIT_MAX_S})
    return dt


def v_watch(p, s, a, ns):
    dt = cmd_watch(p, a.timeout, say)
    s.event('watch', data={'first_ack_s': dt, 'limit_s': cmis.T_MGMT_INIT_MAX_S})
    return dt


def v_lpmode(p, s, a, ns):
    """Drive LPMode and time the module state that follows (script timing capture, 3.2)."""
    t0 = time.monotonic()
    p.set_lpmode(a.level)
    want = ST_LOWPWR if a.level else ST_READY
    ok = wait_state(p, want, a.timeout, say)
    d = {'level': a.level, 'want': STATES[want], 'reached': ok, 't_s': round(time.monotonic() - t0, 3)}
    say(f'LPMode -> {a.level}: {STATES[want]} {"reached" if ok else "NOT reached"} after {d["t_s"]:.3f} s')
    s.event('lpmode', data=d)
    return d


VERBS = {'id': v_id, 'pages': v_pages, 'thresholds': v_thresholds, 'vdm': v_vdm, 'cdb': v_cdb, 'release': v_release, 'hold': v_hold,
         'reset': v_reset, 'watch': v_watch, 'lpmode': v_lpmode}


def run_verb(p, s, a, ns):
    record_overrides(s, a)
    try:
        return VERBS[a.rd_verb](p, s, a, ns)
    except Refused as e:
        s.event('refused', data={'verb': a.rd_verb, 'text': str(e)})
        raise


def v_script(p, s, a, ns, rd_parser):
    lines = [l.strip() for l in open(a.file) if l.strip() and not l.strip().startswith('#')]
    s.event('script', data={'file': a.file, 'lines': lines})
    armed = p.allow_lp_low  # the script opens unarmed (`script` is not an LP verb); an LPMode line with --i-am-on-usbc-power arms the lines after it
    for line in lines:
        say(f'rd> {line}')
        try:
            o = rd_parser.parse_args(line.split())
        except SystemExit:
            say(f'  bad line, skipped: {line}', err=True)
            continue
        if o.rd_verb not in SCRIPT_VERBS:
            say(f'  {o.rd_verb}: not a script verb (one of {", ".join(SCRIPT_VERBS)})', err=True)
            continue
        armed = armed or (needs_lp(o) and o.i_am_on_usbc_power)
        p.allow_lp_low = armed
        if needs_lp(o) and not armed:
            s.event('refused', data={'verb': o.rd_verb, 'text': GUARD})
            say(f'REFUSED: {GUARD}')
            continue
        try:
            run_verb(p, s, o, ns)
        except Refused as e:
            say(str(e))
        except Nack as e:
            say(f'NACK: {e}')


# --------------------------------------------------------------------------- log (a child process)
def log_run(s, a):
    """`rd log _run`: poll_health at 1 s into events.jsonl (`health`) and health.csv until SIGTERM / Ctrl-C."""
    ns = connection(a, s)
    p = open_rd_port(a, ns, False)
    prev = install_signal_stop()
    n, t0 = 0, time.monotonic()
    try:
        while True:
            h = poll_health(p)
            s.event('health', data=h)
            s.write_health_row(round(time.monotonic() - t0, 1), None, h)
            n += 1
            time.sleep(1.0)
    except (KeyboardInterrupt, Nack) as e:
        s.event('log', data={'action': 'stopped', 'rows': n, 'reason': f'{type(e).__name__}: {e}'})
    finally:
        for sg, h in prev.items():
            signal.signal(sg, h)
        p.close()


def _log_child(pid):
    """True when `pid` runs `aectest rd log _run` (ps command line): a stale log.pid never kills another process."""
    r = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True)
    return all(w in r.stdout for w in ('aectest', 'log', '_run'))


def log_stop(s, quiet=False):
    pidf = s.path('log.pid')
    if not os.path.exists(pidf):
        if not quiet:
            say('no log running')
        return 0
    pid = int(open(pidf).read())
    if not _log_child(pid):
        os.remove(pidf)
        say(f'log.pid is stale: pid {pid} is not an `rd log _run` child; nothing killed, file removed', err=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            os.kill(pid, 0)
            time.sleep(0.05)
    except ProcessLookupError:
        pass
    os.remove(pidf)
    s2 = Session.attach(s.dir)  # the child wrote its stop event: continue after it
    rows = sum(1 for l in open(s2.path('events.jsonl')) if json.loads(l)['type'] == 'health')
    s2.event('log', data={'action': 'stop', 'pid': pid, 'health_rows': rows})
    say(f'log stopped (pid {pid}); {rows} health rows in events.jsonl')
    return rows


def log_start(s, a):
    pidf = s.path('log.pid')
    if os.path.exists(pidf):
        raise SystemExit(f'a log is running (pid {open(pidf).read()}): `rd log stop` first')
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(x for x in (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                                      os.environ.get('PYTHONPATH', '')) if x)}
    s.event('log', data={'action': 'start'})
    child = subprocess.Popen([sys.executable, '-m', 'aectest', 'rd', 'log', '_run'], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    open(pidf, 'w').write(str(child.pid))
    n0 = s.seq
    for _ in range(100):
        if child.poll() is not None:
            os.remove(pidf)
            raise SystemExit(f'the log child exited with {child.returncode} before its first row (see events.jsonl)')
        if Session.attach(s.dir).seq > n0:
            break
        time.sleep(0.05)
    else:
        child.terminate()
        os.remove(pidf)
        raise SystemExit('the log child wrote no health row within 5 s; stopped')
    say(f'log started (pid {child.pid}): health at 1 s into {s.path("events.jsonl")}; `rd log stop` ends it')
    return child.pid


# --------------------------------------------------------------------------- entry
def run_rd(a):
    verb = a.rd_verb
    st = state_path(a.out)
    if verb == 'dump':
        verb = a.rd_verb = 'pages'
        a.pages, a.bank = list(DUMP_PAGES), 0
    if verb == 'open':
        if os.path.exists(st):
            raise SystemExit(f'an rd session is open ({open(st).read().strip()}): `aectest rd close` first')
        hw = {'connection': {k: getattr(a, k) for k in CONN}}
        if a.jig:
            prof = load_profile(a.jig, strict=False)
            hw.update(hardware_manifest(prof))
            hw['power'] = prof['power']
        os.makedirs(a.out, exist_ok=True)
        s = Session(a.out, 'rd', a.serial or ('FAKE-A' if a.dry_run else 'rd'), tool=tool_block(), hardware=hw)
        ns = connection(a, s)
        try:
            p = open_rd_port(a, ns, False)  # the S2 line: MPSSE configured, SAFE outputs -- only now SW_MODE -> AUTO
        except BaseException as e:
            s.event('abort', data={'error': f'{type(e).__name__}: {e}'})
            s.close(NOT_JUDGED)
            raise
        try:
            say(f'MPSSE configured, directions set: {p.sense()}')
        finally:
            p.close()
        open(st, 'w').write(s.dir + '\n')
        say(f'rd session {s.session_id} open: {s.dir} (SW_MODE may go to AUTO now; `rd close` ends it)')
        return 0
    s = attach(a.out)
    if verb == 'close':
        log_stop(s, quiet=True)
        s = Session.attach(s.dir)
        m = s.close(NOT_JUDGED)
        os.remove(st)
        say(f'rd session {s.session_id} closed: {len(m["files"])} files hashed in {s.path("manifest.json")}')
        return 0
    if verb == 'log':
        {'start': lambda: log_start(s, a), 'stop': lambda: log_stop(s), '_run': lambda: log_run(s, a)}[a.action]()
        return 0
    if verb == 'prbs':
        allow = a.i_am_on_usbc_power
        if not allow:
            s.event('refused', data={'verb': 'prbs', 'text': GUARD})
            raise Refused(GUARD)
        ns = connection(a, s)
        pair = a.pair or ('FAKE-A:FAKE-B' if ns.dry_run else '')
        sa, sb = pair.split(':', 1) if ':' in pair else ('', '')
        if not sa or not sb or (sa == sb and not ns.dry_run):
            raise SystemExit('prbs needs --pair A:B (two DISTINCT FT2232H serials)')
        if a.time < PRBS_MIN_S and not a.allow_short:
            raise SystemExit(f'--time {a.time:g} < {PRBS_MIN_S:g} s minimum; --allow-short for a rehearsal')
        record_overrides(s, a)
        prev = install_signal_stop()
        gen = open_rd_port(a, argparse.Namespace(**{**vars(ns), 'serial': sa}), allow)
        try:
            chk = open_rd_port(a, argparse.Namespace(**{**vars(ns), 'serial': sb}), allow)
            try:
                name = f'prbs_{s.seq + 1:03d}.json'
                rec = cmd_prbs(gen, chk, a.pattern, a.time, a.budget_w if a.budget_w is not None else BUDGET_W, a.timeout, a.force_cmis,
                               s.path(name), (sa, sb), a.lane_mask, a.assume_w, a.posture, a.ambient_c,
                               a.temp_limit_c if a.temp_limit_c is not None else JIG_TEMP_LIMIT_C, a.temp_warn_c, a.temp_throttle_c,
                               a.external_temp_ack, say)
            finally:
                chk.close()
        except Refused as e:
            s.event('refused', data={'verb': 'prbs', 'text': str(e)})
            raise
        finally:
            gen.close()
            for sg, h in prev.items():
                signal.signal(sg, h)
        s.event('prbs', data={'file': name, **rec})
        return 0
    ns = connection(a, s)
    try:
        p = port_for(a, ns)
    except Refused as e:
        s.event('refused', data={'verb': verb, 'text': str(e)})
        raise
    prev = install_signal_stop() if verb == 'hold' else {}
    try:
        if verb == 'script':
            v_script(p, s, a, ns, a.rd_parser)
        else:
            run_verb(p, s, a, ns)
    finally:
        for sg, h in prev.items():
            signal.signal(sg, h)
        p.close()
    return 0
