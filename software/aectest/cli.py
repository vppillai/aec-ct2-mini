"""`aectest` command line: `bringup` (the bench commands, every flag and help text kept) and `selftest`.

add_bringup() builds the parser and run_bringup() runs the body under one subparser. The primitives (tests.py) return and
never print; `say` renders their lines here.
"""
import argparse
import os
import signal
import sys

from .cmis import PATTERNS
from .jig import STOCK_PID, register_pid  # noqa: F401 -- the selftest imports register_pid from here
from .records import open_new, pyftdi_version, tool_commit  # noqa: F401
from .safety import BUDGET_W, GUARD, JIG_TEMP_LIMIT_C, MAN_HINT, PRBS_MIN_S, RULES, THROTTLE_MARGIN_C, TRISTATE_LAST
from .calibrate import add_calibrate
from .lot import add_inspect
from .tech import add_tech
from .advtests import cmd_audit, cmd_flags, cmd_i2c_stress, cmd_timing
from .cdb import cmd_cdb
from .derive import cmd_derive
from .tests import cmd_dump, cmd_id, cmd_idle, cmd_media_link, cmd_prbs, cmd_trend, cmd_reset, cmd_status, cmd_watch, hold_high_power, log_meta, release_lpmode
from . import transport
from .transport import PVS1, PVS4, PYFTDI_PINNED, UART_WINDOW_NOTE, FakeBackend, FtdiBackend, Nack, Port, Refused


def say(text, err=False):
    """The renderer the CLI hands to every primitive (2.4): stdout, or stderr for the WARN / hint lines."""
    print(text, file=sys.stderr if err else sys.stdout)


BRINGUP_DESCRIPTION = 'AEC-CT2-MINI (single-port QSFP-DD stick) bring-up over the on-board FT2232H'

BLANK_HINT = ('a stock 0403:6010 WITHOUT a serial string is attached = blank 93LC56B (first article): no udev rule matches it -- '
              'the unit has no identity yet; contact the supplier of the unit')


# --------------------------------------------------------------------------- glue
def parse_speed(s):
    """The clock at open. 'auto' and '1M' open at 100 kHz and ramp (transport.ramp_clock); 100k / 400k are fixed."""
    s = s.lower().rstrip('hz')
    if s in ('auto', '1m'):
        return 100_000
    return int(float(s[:-1]) * 1e3) if s.endswith('k') else int(float(s))


NO_DEVICE = ('no FTDI device found: libusb installed? udev rule installed (`aectest setup-linux`)? ftdi_sio/AppleUSBFTDI bound '
             'to the unit? cable in the PC port (not PWR ONLY)? USB LED lit?')
UNIT_HINT = 'serial = the unit identity: the same as the UNIT field on the case label'


def check_pyftdi(any_version, version=None):
    """Every pyftdi behaviour note in this file (UART window, freeze, tristate, GPIO direction) was read from
    PYFTDI_PINNED; another version is refused unless --any-pyftdi says the operator accepts the risk."""
    v = version or pyftdi_version()
    if v == PYFTDI_PINNED:
        return
    msg = (f'pyftdi {v} != pinned {PYFTDI_PINNED}: the UART-window / freeze / tristate behaviour this tool '
           f'relies on was read from {PYFTDI_PINNED} -- ')
    if not any_version:
        raise SystemExit(msg + f'refusing; `pip install pyftdi=={PYFTDI_PINNED}` or pass --any-pyftdi')
    print(msg + 'continuing on --any-pyftdi', file=sys.stderr)


def blank_units(ftdi_cls=None):
    """Stock-PID FT2232Hs with no serial string = unwritten 93LC56B; drives the BLANK_HINT."""
    try:
        if ftdi_cls is None:
            from pyftdi.ftdi import Ftdi as ftdi_cls
        return [d for d, _ in ftdi_cls.list_devices('ftdi:///?') if d.pid == 0x6010 and not d.sn]
    except Exception:  # noqa: BLE001 -- enumeration itself failed (no libusb access): nothing to add
        return []


def arm_prompt(names):
    """S2 for two sticks: both FT2232Hs are open in MPSSE with SAFE outputs -- only now SW_MODE -> AUTO."""
    print(f'MPSSE configured, directions set on {names[0]} and {names[1]} (both opened in MAN, S2)')
    if not sys.stdin.isatty():
        print('stdin is not a terminal: no arm prompt -- SW_MODE must already be AUTO on both sticks')
        return
    try:
        input('move SW_MODE to AUTO on BOTH sticks, then press Enter (Ctrl-C aborts before any CMIS write): ')
    except (KeyboardInterrupt, EOFError):
        raise SystemExit('\naborted before any CMIS write; both sticks stay in low power') from None


def list_devices(dry, pid=None):
    if dry:
        print('FAKE-A           0403:6010 (AEC-CT2-MINI fake)  ports 2  <- FT2232H\n'
              'FAKE-B           0403:6010 (AEC-CT2-MINI fake)  ports 2  <- FT2232H')
        print(UNIT_HINT)
        return
    from pyftdi.ftdi import Ftdi
    ours = {Ftdi.PRODUCT_IDS[Ftdi.FTDI_VENDOR]['2232h'], pid}
    devs = Ftdi.list_devices('ftdi:///?')  # every PID pyftdi knows, the registered custom one included
    if not devs:
        print(NO_DEVICE)
    for desc, ports in devs:
        print(f'{desc.sn or "?":16} {desc.vid:04x}:{desc.pid:04x} ({desc.description})  ports {ports}'
              + ('  <- FT2232H' if desc.pid in ours else ''))
    if devs:
        print(UNIT_HINT)
    if any(desc.pid == 0x6010 and not desc.sn for desc, _ in devs):
        print(BLANK_HINT)


def open_port(serial, args, allow):
    if args.dry_run:
        from . import fake_modules
        image = fake_modules.load(args.fake_sku) if getattr(args, 'fake_sku', None) else None
        port = Port(FakeBackend(serial or 'FAKE-A', parse_speed(args.speed), image), allow, say=say)
    else:
        dev = f'0x0403:0x{args.pid:04x}' if args.pid else 'ftdi:2232h'
        url = f'ftdi://{dev}{":" + serial if serial else ""}/1'  # channel A = interface 1
        try:
            port = Port(FtdiBackend(url, parse_speed(args.speed)), allow, say=say)
        except Exception as e:  # noqa: BLE001 -- pyftdi raises UsbToolsError/USBError/FtdiError; all mean "not reachable"
            hint = NO_DEVICE + ('\n  ' + BLANK_HINT if blank_units() else '')
            raise SystemExit(f'cannot open {url} ({e}).\n  {hint}') from None
    print(f'pyftdi {pyftdi_version()}, I2C {port.be.hz} Hz actual ({args.speed} requested); {UART_WINDOW_NOTE}',
          file=sys.stderr)
    if args.speed in ('auto', '1M'):
        port.ramp_target = args.speed
        port.say = _say_clock(port.say)
    if getattr(args, 'ramp_rounds', None):
        transport.RAMP_ROUNDS = int(args.ramp_rounds)
    return port


def _say_clock(say):
    def both(text, err=False):
        if text.startswith(('ramped ', 'fell back ', 'kept ', '1 MHz not advertised', 'fixed ')):
            print(f'I2C clock: {text}', file=sys.stderr)
        else:
            say(text, err)
    return both


def cmd_decode(port, serial, out, as_json, say, dump=None):
    """`decode` on the module in `port`, or on a saved record (`--dump FILE`: a `dump` / `decode` JSON, no port).
    JSON with --json, else the page-by-page table; --out writes the JSON record (never overwritten)."""
    import json
    from . import cmis
    from .records import record_header, write_record
    if dump is not None:
        with open(dump, encoding='utf-8') as fh:
            d = json.load(fh)
        rec = cmis.decode(d.get('raw_dump') or d)
        rec['source'] = dump
    else:
        rec = cmis.read_pages(port, say=say)
        rec = {**record_header([serial]), **rec}
    if as_json:
        say(json.dumps(rec, indent=1, default=str))
    else:
        say(cmis.render(rec))
    if out:
        write_record(rec, out, f'decode_{serial}_{{}}.json', say)
    return rec


def run_cmd(port, cmd, a):
    """One command against an open port -- shared by main() and the `shell` REPL."""
    if cmd == 'status':
        cmd_status(port, a.json, say)
    elif cmd == 'id':
        cmd_id(port, a.json, say)
    elif cmd == 'dump':
        cmd_dump(port, a.serial or getattr(port.be, 'serial', '?'), a.out, say)
    elif cmd == 'decode':
        cmd_decode(port, a.serial or getattr(port.be, 'serial', '?'), a.out, a.json, say)
    elif cmd == 'reset':
        cmd_reset(port, say)
    elif cmd == 'watch':
        cmd_watch(port, a.timeout, say)
    elif cmd == 'release-lpmode':
        if not port.allow_lp_low:
            raise Refused(GUARD)
        f = open_new(a.log) if a.log else None
        try:
            info = release_lpmode(port, a.budget_w, a.timeout, getattr(a, 'assume_w', None), getattr(a, 'posture', None),
                                  getattr(a, 'ambient_c', 25), getattr(a, 'temp_limit_c', JIG_TEMP_LIMIT_C),
                                  getattr(a, 'temp_warn_c', None), getattr(a, 'temp_throttle_c', None), getattr(a, 'external_temp_ack', False),
                                  say)
        except BaseException:
            if f:  # a refused release leaves no empty record behind
                f.close()
                os.remove(a.log)
            raise
        hold_high_power(port, a.hold, a.log, log_meta(info, a, a.serial or getattr(port.be, 'serial', '?')), logf=f, say=say)
    elif cmd == 'idle':
        cmd_idle(port, a.hold, say)
    elif cmd == 'derive-criteria':
        cmd_derive(port, a, say)
    elif cmd == 'cdb':
        cmd_cdb(port, getattr(a, 'arg', None) or [], a.json, a.out, getattr(a, 'raw', False), a.serial or getattr(port.be, 'serial', '?'), say)
    elif cmd in ('fw-info', 'fw-update', 'fw-abort'):
        from .fw import cmd_fw
        cmd_fw(port, cmd, a, say, a.serial or getattr(port.be, 'serial', '?'))
    elif cmd == 'features':
        from .features import cmd_features, table
        rec = cmd_features(port, a.serial or getattr(port.be, 'serial', '?'), getattr(a, 'exercise', None), a.out, a.json, say,
                           folder=getattr(a, 'folder', None) or '.', budget_w=a.budget_w, timeout=a.timeout, apsel=getattr(a, 'apsel', None))
        if not a.json:
            say(table(rec))
    elif cmd == 'cdb-password':
        from .campaign import cmd_cdb_password
        pw = int(a.password, 0) if getattr(a, 'password', None) else None
        cmd_cdb_password(port, a.serial or getattr(port.be, 'serial', '?'), pw, a.out, say)
    elif cmd == 'audit':
        cmd_audit(port, a.serial or getattr(port.be, 'serial', '?'), a.out, a.json, say)
    elif cmd == 'timing':
        cmd_timing(port, a.serial or getattr(port.be, 'serial', '?'), a.out, a.budget_w, a.timeout, getattr(a, 'apsel', None), a.json, say)
    elif cmd == 'flags':
        cmd_flags(port, a.serial or getattr(port.be, 'serial', '?'), a.out, a.budget_w, a.timeout, a.json, say)
    elif cmd == 'i2c-stress':
        cmd_i2c_stress(port, a.serial or getattr(port.be, 'serial', '?'), getattr(a, 'count', 20) or 20, a.out, a.json, say)
    elif cmd == 'tristate':
        port.tristate()
        print(TRISTATE_LAST)
    else:
        print(f'unknown command {cmd!r}; one of {", ".join(SHELL_CMDS)}, quit')


SHELL_CMDS = ('status', 'id', 'dump', 'decode', 'reset', 'watch', 'release-lpmode', 'idle', 'tristate', 'cdb', 'cdb-password', 'audit', 'timing', 'flags', 'i2c-stress',
              'fw-info', 'fw-update', 'fw-abort', 'features')


def cmd_shell(port, a, lines=None):
    """REPL: one open()/close() for a whole T5-T7 session, so the UART-mode window happens once.
    Each line is a command name, optionally followed by --hold/--log/--out/--json overrides; `quit` exits.
    LPMode guard per line, same double-guard as main(): `release-lpmode --i-am-on-usbc-power` (the command name is
    flag 1), or launch the shell with both flags to pre-arm every line."""
    print(f'MPSSE configured, directions set: {port.sense()}')  # same line `idle` prints: S2 is satisfied by either
    print('shell: commands ' + ' '.join(SHELL_CMDS) + ' quit  (options per line, e.g. `release-lpmode '
          '--i-am-on-usbc-power --hold 30 --log t7.csv`)')
    sub = argparse.ArgumentParser(prog='', add_help=False)
    sub.add_argument('cmd')
    sub.add_argument('arg', nargs='*')  # cdb: the query name, or raw 0xNNNN [hexbytes]
    sub.add_argument('--raw', action='store_true')
    sub.add_argument('--count', type=int)
    sub.add_argument('--hold', type=float)
    sub.add_argument('--log')
    sub.add_argument('--out')
    sub.add_argument('--json', action='store_true')
    for k in ('--budget-w', '--timeout', '--assume-w', '--temp-limit-c', '--temp-warn-c', '--temp-throttle-c'):
        #                                                                                              must not force a second open in AUTO
        sub.add_argument(k, type=float)
    sub.add_argument('--posture', choices=POSTURES)
    sub.add_argument('--ambient-c', type=int, choices=(25, 35))
    sub.add_argument('--release-lpmode', action='store_true')
    sub.add_argument('--i-am-on-usbc-power', action='store_true')
    sub.add_argument('--external-temp-ack', action='store_true')
    allow0 = port.allow_lp_low
    lines = lines if lines is not None else iter(lambda: input('mini> '), 'quit')
    try:
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line == 'quit':
                break
            try:
                o = sub.parse_args(line.split())
            except SystemExit:
                print('usage: <cmd> [--hold S] [--log F] [--out F|-] [--json] [--budget-w W] [--timeout S] [--assume-w W] '
                      '[--posture P] [--ambient-c 25|35] [--temp-limit-c C] [--temp-warn-c C] [--temp-throttle-c C] '
                      '[--release-lpmode] [--i-am-on-usbc-power]')
                continue
            ns = argparse.Namespace(**vars(a))
            for k in ('hold', 'log', 'out', 'budget_w', 'timeout', 'assume_w', 'posture', 'ambient_c', 'temp_limit_c', 'temp_warn_c',
                      'temp_throttle_c', 'count'):
                if getattr(o, k) is not None:
                    setattr(ns, k, getattr(o, k))
            ns.json = a.json or o.json
            ns.arg, ns.raw = o.arg, getattr(a, 'raw', False) or o.raw
            ns.external_temp_ack = getattr(a, 'external_temp_ack', False) or o.external_temp_ack
            flag1 = a.release_lpmode or o.release_lpmode or o.cmd == 'release-lpmode'
            port.allow_lp_low = allow0 or (flag1 and (a.i_am_on_usbc_power or o.i_am_on_usbc_power))
            try:
                run_cmd(port, o.cmd, ns)
            except Nack as e:
                print(NACK_MSG.format(e))
            except Refused as e:
                print(e)
            except SystemExit as e:
                print(e)
            if o.cmd == 'tristate':
                print('tristate ends the shell -- SW_PWR may be switched off now (S11); re-open only after SW_MODE = MAN')
                break
    except (EOFError, KeyboardInterrupt):
        print()


NACK_MSG = ('NO ACK from module ({}): SW_PWR ON / PWR LED lit? module absent, not selected (' + MAN_HINT + ', or SW_MODSEL '
            'not at L), in reset/MgmtInit, or the SCL/SDA straps JP401/JP402 cut (dongle session) - restore the '
            'solder blobs')


EXAMPLES = ('%(prog)s --list',
            '%(prog)s --serial CT2M-01 id --json',
            '%(prog)s --serial CT2M-01 dump',
            '%(prog)s --serial CT2M-01 watch',
            '%(prog)s --serial CT2M-01 release-lpmode --i-am-on-usbc-power --hold 60 --log t7_trend.csv',
            '%(prog)s --serial CT2M-01 shell',
            '%(prog)s prbs --pair CT2M-01:CT2M-02 --release-lpmode --i-am-on-usbc-power',
            '%(prog)s media-link --pair CT2M-01:CT2M-02 --release-lpmode --i-am-on-usbc-power --time 30',
            '%(prog)s trend --pair CT2M-01:CT2M-02 --release-lpmode --i-am-on-usbc-power --time 60 --apsel 1 --budget-w 8',
            '%(prog)s loopback --pair CT2M-01:CT2M-02 --release-lpmode --i-am-on-usbc-power --time 30 --apsel 1 --budget-w 8',
            '%(prog)s --dry-run id',
            '%(prog)s --selftest')


POSTURES = ('hood-on', 'hood-off', 'fan', 'forced-air')
EPILOG_NOTES = ('standalone commands re-open the FT2232H (UART-mode window at the pads) -- power up in MAN, use the `shell` in AUTO.\n'
                'thermal control: the module\'s own TempMonValue is polled at 1 s in every high-power path -- WARN at its\n'
                'high-warning threshold (run continues), THROTTLE at high-alarm - 5 C capped by --temp-limit-c 70 (LPMode high, stop,\n'
                'INCONCLUSIVE: THERMAL, forced-air instruction). --posture is informational (off by default, no refusal);\n'
                '--posture P opts the S1 POSTURE-OVER-CLASS refusal in for a bench that wants it.')


def install_signal_stop():
    """SIGTERM / SIGHUP end a hold exactly like Ctrl-C -- as KeyboardInterrupt -- so every `finally` runs
    (LPMode high, CSV trailer, close). Returns the previous handlers (the selftest restores them). Run long holds under
    `caffeinate -i` (macOS) / `systemd-inhibit` (Linux); the hardware stop is SW_PWR OFF (or unplug PC -> MAN -> slide H)."""
    def stop(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')
    return {sg: signal.signal(sg, stop) for sg in (signal.SIGTERM, getattr(signal, 'SIGHUP', None)) if sg is not None}



def add_bringup(sub):
    """The `bringup` subparser: the bench commands, names and help texts unchanged."""
    ap = sub.add_parser('bringup', help='bring-up and cable test of the AEC-CT2-MINI (the bench commands)',
                        description=BRINGUP_DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter,
                        epilog='examples (each one is run by --selftest against the fake):\n  ' + '\n  '.join(EXAMPLES)
                        + '\n\n' + EPILOG_NOTES)
    ap.add_argument('cmd', nargs='?', choices=list(SHELL_CMDS) + ['prbs', 'media-link', 'trend', 'loopback', 'lane-integrity', 'si-sweep', 'flag-matrix',
                                                                   'shell', 'records', 'derive-criteria'])
    ap.add_argument('--by-cable', action='store_true', help='records: group the files by the cable they were read from, else a flat list')
    ap.add_argument('--root', action='append', help='records: a record folder to index (repeat); default = the run folders and out/test')
    ap.add_argument('--serial', help='FT2232H serial (see --list); required when more than one is attached')
    ap.add_argument('--pid', type=lambda s: int(s, 0), help='custom USB PID programmed in the 93LC56; default 0x6010')
    ap.add_argument('--ramp-rounds', type=int, default=None, metavar='N', help='stress rounds the clock ramp needs (the setting i2c_ramp_rounds, default 20)')
    ap.add_argument('--speed', default='auto', choices=('auto', '100k', '400k', '1M'),
                    help='I2C clock: auto (default: open at 100k, ramp to 400k when 20 stress rounds pass, else back to 100k), '
                         '100k or 400k fixed, 1M = ramp to 1 MHz only when the module advertises it (MciMaxSpeed 00h:2)')
    ap.add_argument('--list', action='store_true', help='enumerate FTDI devices and exit (add --pid 0xNNNN to list a custom-PID unit)')
    ap.add_argument('--dry-run', action='store_true', help='use a fake FT2232H + fake CMIS module (no hardware)')
    ap.add_argument('--fake-sku', metavar='SKU', help='--dry-run: the fake answers with the fake_modules/<SKU>.yaml image of the package instead of the built-in image')
    ap.add_argument('--selftest', action='store_true', help='assert the safety logic against the fake and exit')
    ap.add_argument('--sniff', action='store_true', help='explain why there is no bus sniffing on the stick')
    ap.add_argument('--release-lpmode', action='store_true', help='permission flag 1 of 2 (also selects that command)')
    ap.add_argument('--i-am-on-usbc-power', action='store_true',
                    help='permission flag 2 of 2 (S10): a 3 A USB-C charger is on PWR ONLY (J102) and the host on PC (J101)')
    ap.add_argument('--external-temp-ack', action='store_true',
                    help='release-lpmode / prbs on a module advertising 01h:159 TempMonSupported = 0: a thermocouple on the sink '
                         'and a human watch it -- otherwise refused, the THROTTLE path cannot fire without the module monitor')
    ap.add_argument('--budget-w', type=float, default=BUDGET_W,
                    help='refuse high power if MaxPower (00h:201; MSA class max when 201 = 0) exceeds this [W]')
    ap.add_argument('--assume-w', type=float, metavar='W',
                    help='power figure for a class-8 module reporting MaxPower (00h:201) = 0 (otherwise refused: no figure to gate on)')
    ap.add_argument('--posture', choices=POSTURES,
                    help='OPT-IN S1 posture gate, informational (default off, no refusal): with it refuse release-lpmode/prbs '
                         'when the gate figure exceeds the row; omitted = one warning, S4 only')
    ap.add_argument('--ambient-c', type=int, choices=(25, 35), default=25, help='posture table column (35 C = x0.78)')
    ap.add_argument('--temp-limit-c', type=float, default=JIG_TEMP_LIMIT_C, metavar='C',
                    help=f'jig cap on the THROTTLE level: THROTTLE = min(--temp-throttle-c | 02h:128-129 TempMonHighAlarmThreshold - '
                         f'{THROTTLE_MARGIN_C:g}, this); default {JIG_TEMP_LIMIT_C:g} = the 70 C module-case criterion of the thermal analysis')
    ap.add_argument('--temp-warn-c', type=float, metavar='C',
                    help='WARN level on TempMonValue 00h:14-15 (default: live 02h:132-133 TempMonHighWarningThreshold); '
                         'printed once, the run continues')
    ap.add_argument('--temp-throttle-c', type=float, metavar='C',
                    help='THROTTLE level (default: live 02h:128-129 TempMonHighAlarmThreshold - 5 C), capped by --temp-limit-c: '
                         'LPMode high, test stopped, INCONCLUSIVE: THERMAL, forced-air instruction printed')
    ap.add_argument('--any-pyftdi', action='store_true',
                    help=f'run on a pyftdi other than the pinned {PYFTDI_PINNED}; otherwise refused at start')
    ap.add_argument('--apsel', type=int, choices=range(1, 9), help='prbs: select this application on both ends before the Data Path check (some 400G VR4 modules power up in their 200G application 2)')
    ap.add_argument('arg', nargs='*', help='cdb: query | features | fw-features | pm-features | bert-features | fw-info | raw 0xNNNN [hexbytes]')
    ap.add_argument('--raw', action='store_true', help='cdb: allow `raw 0xNNNN`, a CMDID outside the read-only queries')
    ap.add_argument('--image', metavar='FILE', help='fw-update: the vendor firmware file, binary')
    ap.add_argument('--password', metavar='0xNNNNNNNN', help='fw-update: the vendor host password written to 00h:122-125 before Start (the Credo refuses 0101h without it, status 46h)')
    ap.add_argument('--run-only', action='store_true', help='fw-update: the bank already holds --image-version; skip the download and go to Run / Commit')
    ap.add_argument('--per-firmware', action='store_true', help='derive-criteria: name the file <sku>-fwX-Y with match.firmware')
    ap.add_argument('--host-present', action='store_true', help='derive-criteria: a host drives the lanes, expect DPActivated on an active cable')
    ap.add_argument('--plan', action='store_true', help='fw-update: every guard and the step list, no CDB write (--dry-run is the fake backend)')
    ap.add_argument('--run-image', action='store_true', help='fw-update: after Complete and verify, Run the new image (0109h: a module reset)')
    ap.add_argument('--commit', action='store_true', help="fw-update: after Run, Commit the running image (010Ah); needs --confirm 'COMMIT <serial>'")
    ap.add_argument('--confirm', metavar='TEXT', help="fw-update --commit: the string 'COMMIT <module serial>'")
    ap.add_argument('--force', action='store_true', help='fw-update: download an image whose version the module already runs')
    ap.add_argument('--header-bytes', type=int, metavar='N', help='fw-update: the vendor header size when the header does not name the part number (must equal 0041h:138)')
    ap.add_argument('--image-version', metavar='X.Y', help='fw-update: the version in the file, for the already-running guard (else the header text is searched)')
    ap.add_argument('--dump', metavar='FILE', help='decode: a saved `dump` or `decode` JSON instead of a module (no port is opened)')
    ap.add_argument('--exercise', metavar='all|none|NAME,...', help='features: run the exercises (all, none = the matrix only, or names: '
                    'page03_write, vdm_read, cdb_query, module_monitors, lane_monitors, lane_ctrl, loop_ctrl, si_readback, flags, '
                    'fw_features, pm_features, prbs, pattern, loopback; the last three need --pair)')
    ap.add_argument('--folder', help='features: the folder for the records (default .)')
    ap.add_argument('--count', type=int, default=20, help='i2c-stress: rounds of lower + page reads (default 20)')
    ap.add_argument('--force-cmis', action='store_true', help='prbs: write Page 13h/14h (CMIS 5.2 layouts) to a module reporting CMIS < 5.0')
    ap.add_argument('--hold', type=float, help='release-lpmode / idle: seconds to hold (default: until Ctrl-C)')
    ap.add_argument('--timeout', type=float, default=15.0, help='seconds to wait for ModuleReady')
    ap.add_argument('--pair', metavar='A:B', help='prbs: FT2232H serials of generator end A and checker end B')
    ap.add_argument('--ber-limit', type=float, metavar='BER', help='prbs: a per-lane verdict against this BER in every direction')
    ap.add_argument('--pattern', default='prbs31q', choices=sorted(PATTERNS, key=PATTERNS.get), help='CMIS Table 8-98 pattern')
    ap.add_argument('--time', type=float, default=PRBS_MIN_S,
                    help=f'prbs: measurement seconds per direction (minimum {PRBS_MIN_S:g} per the test plan; shorter only with --allow-short)')
    ap.add_argument('--allow-short', action='store_true', help='prbs: accept --time below the minimum (rehearsal, not a T11 record)')
    ap.add_argument('--lane-mask', type=lambda x: int(x, 0), metavar='0xNN',
                    help='prbs: lanes to enable (bit n = lane n+1); default = the lanes DPActivated on both ends (11h:128-131)')
    ap.add_argument('--out', help='dump / prbs: file name (default dump_<serial>_<UTC>.json / T11_ber_<A>_<B>_<UTC>.json); - = stdout')
    ap.add_argument('--log', metavar='FILE', help='release-lpmode: per-run CSV (never appended/overwritten) with a `# provenance` header, '
                                                't,state,temp_c,vcc_v,flags9,intl at 1 s + reserved hand-sample columns')
    ap.add_argument('--json', action='store_true', help='status / id: print JSON instead of the table')
    ap.add_argument('--pvs1', type=int, choices=(0, 1), help='drive FT2232H AC0 (P/VS1) -- R426 DNP: no effect at the pad')
    ap.add_argument('--pvs4', type=int, choices=(0, 1), help='drive FT2232H AC1 (P/VS4) -- R427 DNP: no effect at the pad')
    return ap


def run_bringup(a, ap):
    """The body of the bringup command after parse_args(); `ap` is the bringup subparser (for ap.error)."""
    if not a.selftest:
        install_signal_stop()
    if a.pid and not a.dry_run:
        register_pid(a.pid)  # once, before --list and before any open (two ports for prbs)

    if a.sniff:
        print('No bus sniffing on AEC-CT2-MINI: the FT2232H MPSSE engine is a master only and cannot passively\n'
              'capture SCL/SDA. Sniffing (MCU GPIO tap after the source switch) exists on the main board only.\n'
              'On the stick clip an external analyser to SCL/SDA at the J407 debug header pins 2/4 (SCL/SDA; pin 3 = ModSelL).\n'
              'TP211/TP212 are the DRIVER side of the straps JP401/JP402 (FT2232H pins): dead after a strap cut, so in a dongle session use the header pins.')
        return 0
    if a.selftest:
        try:
            from .selftest import selftest  # here, not at the top: selftest imports this module
        except ImportError:
            sys.exit('the selftest is not part of this release')
        selftest()
        return 0
    if not a.dry_run:
        check_pyftdi(a.any_pyftdi)
    if a.list:
        list_devices(a.dry_run, a.pid)
        return 0
    if a.release_lpmode and not a.cmd:
        a.cmd = 'release-lpmode'
    if a.cmd == 'release-lpmode':
        a.release_lpmode = True
    if not a.cmd:
        ap.error('a command is required (or --list / --selftest / --sniff)')
    if a.cmd == 'records':
        from . import web
        print(web.records_table(a.root or web.RECORD_ROOTS, a.by_cable))
        return 0
    if a.cmd == 'decode' and a.dump:
        cmd_decode(None, a.serial or os.path.basename(a.dump), a.out, a.json, say, dump=a.dump)
        return 0
    allow = a.release_lpmode and a.i_am_on_usbc_power
    if a.cmd in ('release-lpmode', 'prbs', 'media-link', 'trend', 'loopback', 'lane-integrity', 'si-sweep', 'flag-matrix') and not allow:
        raise Refused(GUARD)

    if a.cmd == 'media-link':
        if not a.pair or ':' not in a.pair:
            ap.error('media-link needs --pair A:B (two FT2232H serials)')
        sa, sb = a.pair.split(':', 1)
        if not a.dry_run and (not sa or not sb or sa == sb):
            ap.error('media-link needs two DISTINCT FT2232H serials A:B (see --list)')
        names = (sa or 'A', sb or 'B')
        ea = open_port(sa, a, allow)
        try:
            eb = open_port(sb or 'FAKE-B', a, allow)
            try:
                arm_prompt(names)
                cmd_media_link(ea, eb, a.time, a.budget_w, a.timeout, a.out, names, a.assume_w, a.posture, a.ambient_c,
                               a.temp_limit_c, a.temp_warn_c, a.temp_throttle_c, a.external_temp_ack, say)
            finally:
                eb.close()
        finally:
            ea.close()
        return 0

    if a.cmd in ('trend', 'loopback'):
        if not a.pair or ':' not in a.pair:
            ap.error(f'{a.cmd} needs --pair A:B (two FT2232H serials)')
        sa, sb = a.pair.split(':', 1)
        if not a.dry_run and (not sa or not sb or sa == sb):
            ap.error(f'{a.cmd} needs two DISTINCT FT2232H serials A:B (see --list)')
        names = (sa or 'A', sb or 'B')
        gen = open_port(sa, a, allow)
        try:
            chk = open_port(sb or 'FAKE-B', a, allow)
            try:
                arm_prompt(names)
                cmd_trend(gen, chk, a.pattern, a.time, a.budget_w, a.timeout, a.force_cmis, a.out, names, a.lane_mask, a.assume_w,
                          a.posture, a.ambient_c, a.temp_limit_c, a.temp_warn_c, a.temp_throttle_c, a.external_temp_ack, say, a.apsel,
                          a.cmd == 'loopback')
            finally:
                chk.close()
        finally:
            gen.close()
        return 0

    if a.cmd in ('lane-integrity', 'si-sweep', 'flag-matrix'):
        if not a.pair or ':' not in a.pair:
            ap.error(f'{a.cmd} needs --pair A:B (two FT2232H serials)')
        sa, sb = a.pair.split(':', 1)
        if not a.dry_run and (not sa or not sb or sa == sb):
            ap.error(f'{a.cmd} needs two DISTINCT FT2232H serials A:B (see --list)')
        names = (sa or 'A', sb or 'B')
        from .campaign import cmd_flag_matrix, cmd_lane_integrity, cmd_si_sweep
        gen = open_port(sa, a, allow)
        try:
            chk = open_port(sb or 'FAKE-B', a, allow)
            try:
                arm_prompt(names)
                if a.cmd == 'lane-integrity':
                    cmd_lane_integrity(gen, chk, a.pattern, a.time, a.budget_w, a.timeout, a.out, names, say, a.apsel)
                elif a.cmd == 'si-sweep':
                    cmd_si_sweep(gen, chk, a.pattern, a.time, a.budget_w, a.timeout, a.out, names, say, a.apsel)
                else:
                    cmd_flag_matrix(gen, chk, a.pattern, a.budget_w, a.timeout, a.out, names, say, a.apsel)
            finally:
                chk.close()
        finally:
            gen.close()
        return 0

    if a.cmd == 'features' and a.pair:
        if ':' not in a.pair:
            ap.error('features --pair needs A:B (two FT2232H serials)')
        if not allow:
            raise Refused(GUARD)
        sa, sb = a.pair.split(':', 1)
        names = (sa or 'A', sb or 'B')
        from .features import cmd_features, table
        pa = open_port(sa, a, allow)
        try:
            pb = open_port(sb or 'FAKE-B', a, allow)
            try:
                arm_prompt(names)
                rec = cmd_features(pa, names[0], a.exercise, a.out, a.json, say, pair=pb, pair_serial=names[1], folder=a.folder or '.',
                                   budget_w=a.budget_w, timeout=a.timeout, apsel=a.apsel, seconds=a.time)
                if not a.json:
                    say(table(rec))
            finally:
                pb.close()
        finally:
            pa.close()
        return 0

    if a.cmd == 'prbs':
        if not a.pair or ':' not in a.pair:
            ap.error('prbs needs --pair A:B (two FT2232H serials)')
        if a.time < PRBS_MIN_S and not a.allow_short:
            ap.error(f'--time {a.time:g} < {PRBS_MIN_S:g} s minimum per the test plan; --allow-short for a rehearsal')
        if a.lane_mask is not None and not 0 < a.lane_mask <= 0xFF:
            ap.error('--lane-mask must be 0x01..0xff')
        sa, sb = a.pair.split(':', 1)
        if not a.dry_run and (not sa or not sb or sa == sb):
            ap.error('prbs needs two DISTINCT FT2232H serials A:B (see --list)')
        names = (sa or 'A', sb or 'B')
        gen = open_port(sa, a, allow)
        try:
            chk = open_port(sb or 'FAKE-B', a, allow)
            try:
                arm_prompt(names)
                cmd_prbs(gen, chk, a.pattern, a.time, a.budget_w, a.timeout, a.force_cmis, a.out, names, a.lane_mask, a.assume_w,
                         a.posture, a.ambient_c, a.temp_limit_c, a.temp_warn_c, a.temp_throttle_c, a.external_temp_ack, say, a.apsel,
                         ber_limit=a.ber_limit)
            finally:
                chk.close()
        finally:
            gen.close()
        return 0

    port = open_port(a.serial, a, allow)
    try:
        for lvl, mask in ((a.pvs1, PVS1), (a.pvs4, PVS4)):
            if lvl is not None:
                port.drive_pvs(mask, lvl)
        if a.cmd == 'shell':
            cmd_shell(port, a)
        else:
            run_cmd(port, a.cmd, a)
    finally:
        port.close()
    return 0


# --------------------------------------------------------------------------- setup-linux (7.2)
UDEV_TARGET = '/etc/udev/rules.d/99-aec-ct2-mini.rules'


def setup_linux_commands(pid, rules=RULES):
    """The three shell lines that install the udev rules of the package, PID substituted."""
    return [f'sed "s/@PID@/{pid:04x}/g" {rules} | sudo tee {UDEV_TARGET} >/dev/null',
            'sudo udevadm control --reload-rules', 'sudo udevadm trigger']


def add_setup_linux(sub):
    ap = sub.add_parser('setup-linux', help='install the udev rules of the wheel (PID substituted) and reload udev; Linux only')
    ap.add_argument('--pid', type=lambda x: int(x, 0), default=STOCK_PID, help='the USB PID in the 93LC56B (default 0x6010; or the custom PID)')
    ap.add_argument('--dry-run', action='store_true', help='print the commands, run nothing')
    ap.set_defaults(func=run_setup_linux)


def run_setup_linux(a):
    import shutil
    import subprocess
    cmds = setup_linux_commands(a.pid)
    if a.dry_run:
        print('\n'.join(cmds))
        return 0
    if sys.platform != 'linux' or not shutil.which('udevadm'):
        raise SystemExit('setup-linux runs on Linux with udevadm; on macOS `brew install libusb` is the whole setup')
    for c in cmds:
        print(c)
        subprocess.run(c, shell=True, check=True)
    print(f'{UDEV_TARGET} installed for PID {a.pid:04x}; be in plugdev; plug the PC port now')
    return 0


def add_unit(sub):
    """`aectest unit eeprom --unit CT2M-NN --file <read-back>`: the sha256 of the EEPROM read-back into the unit state (5.4; fix list P2-01)."""
    ap = sub.add_parser('unit', help='unit state (5.4): record the EEPROM read-back hash')
    us = ap.add_subparsers(dest='unit_verb', required=True)
    e = us.add_parser('eeprom', help='hash a read-back file (ftconf -x output or the raw image) into units/<unit>.json')
    e.add_argument('--unit', required=True, metavar='CT2M-NN')
    e.add_argument('--file', required=True, metavar='FILE', help='the EEPROM read-back as saved from ftconf')
    e.add_argument('--out', default=None, help='the run folder (default: AECTEST_OUT, else out/test)')
    e.set_defaults(func=run_unit)
    return ap


def run_unit(a):
    from .records import OUT_DIR, UnitState, sha256
    h = sha256(a.file)
    u = UnitState(a.out or OUT_DIR, a.unit)
    u.touch('setup', event='eeprom', test_ref='eeprom', eeprom_hash=h, notes=os.path.basename(a.file))
    print(f'{a.unit}: eeprom_sha256 {h[:16]}... written to {u.file}')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='aectest', description=f'aectest {__import__("aectest").__version__} -- AEC-CT2-MINI test software')
    ap.add_argument('--version', action='version', version=f'aectest {__import__("aectest").__version__}')
    sub = ap.add_subparsers(dest='verb', required=True)
    bringup = add_bringup(sub)
    st = sub.add_parser('selftest', help='run the package selftest against the fake FT2232H and exit')
    st.add_argument('--criteria', metavar='FILE', help='the SKU criteria file for parts b-e (default: <repo>/criteria/credo-cac41x321d1d.yaml; '
                                                     'an installed wheel has no repo: pass the file of the release zip)')
    add_setup_linux(sub)
    add_unit(sub)  # P2-01: the EEPROM read-back hash into the unit state
    add_tech(sub)
    from .rd import add_rd
    add_rd(sub)
    add_inspect(sub)
    add_calibrate(sub)
    from .web import add_web
    add_web(sub)
    from .web2 import add_web as add_web2
    add_web2(sub)
    a = ap.parse_args(argv)
    if a.verb == 'selftest':
        from .selftest import selftest
        selftest(**({'criteria_file': a.criteria} if a.criteria else {}))
        return 0
    if hasattr(a, 'func'):
        return a.func(a)
    if hasattr(a, 'run'):
        return a.run(a)
    return run_bringup(a, bringup)

