"""`aectest web2` -- the second front end of the tester.

Reuses web.py: the units, the jobs and their SSE stream, the unit lock, the heartbeat, the record roots and every command of
run_command. This module adds only what the new screens need: the static files under `web2/` (with `web/` as the fallback,
so conn.js, help.js, labels.js and md.js are shared, not copied), the step blurbs, the capability map, the decode of a module
or a saved dump, the advanced checks (audit, timing, flags, i2c-stress) and the firmware commands over fw.py.

The cut-over from web to web2 is one line in cli.py: `from .web import add_web` -> `from .web2 import add_web`.
"""
import json
import os
import sys
import threading
import time
import urllib.parse

from . import advtests, tech, web
from .cmis.decode import decode as decode_dump, read_pages  # the package exports a function named decode too
from .safety import BUDGET_W, PRBS_MIN_S
from .transport import Nack, Refused
from .web import JOBS, Job, _budget, _confirmed

STATIC2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web2')
STATIC_DIRS = (STATIC2, web.STATIC)
CTYPES = {'html': 'text/html', 'js': 'application/javascript', 'css': 'text/css', 'svg': 'image/svg+xml'}
MINE = ('decode', 'audit', 'timing', 'flags', 'i2c-stress', 'fw-info', 'fw-update', 'fw-abort')

# The step blurbs (design section 5): one source for the Test view and the Help page, from the engine's constants.
STEPS = [
    {'name': 'open', 'title': 'Pre-check', 'what': 'Reads the pins of each unit: MOD PWR, MODE and the module answer.',
     'judges': 'Every check must pass. A miss names the switch and waits for Retry.', 'duration_s': 2, 'waits': 'the operator'},
    {'name': 'insert', 'title': 'Insert', 'what': 'Waits for the cable end in each unit and times the first answer after insertion (MgmtInit).',
     'judges': 'The module answers within the MgmtInit limit of 2 s.', 'duration_s': tech.TIMEOUT_S, 'waits': 'the module'},
    {'name': 'identity', 'title': 'Identity', 'what': 'Reads vendor, part number, serial, date code, CMIS revision, power class and MaxPower.',
     'judges': 'The part number matches the criteria file. The two ends match each other. The label serial matches the EEPROM when scanned.',
     'duration_s': 1, 'waits': None},
    {'name': 'low_power', 'title': 'Low power', 'what': 'Reads the module state, temperature, Vcc and the flags, then every advertised page into the record.',
     'judges': 'The module reads ModuleLowPwr with no fault flag.', 'duration_s': 3, 'waits': None},
    {'name': 'high_power', 'title': 'High power',
     'what': f'Lets the module leave low power and waits for Ready, up to {tech.TIMEOUT_S:g} s. Then it holds high power for the soak '
             f'({tech.SOAK_S:g} s unless the criteria file says otherwise) and reads temperature and Vcc every second.',
     'judges': 'Ready within the limit and the data path states equal on every lane. Temperature and its slope under the limits. No fault flag.'
               'no Tx failure flag. The step stops the module at the warning temperature.',
     'duration_s': tech.TIMEOUT_S + tech.SOAK_S, 'waits': 'the module'},
    {'name': 'prbs', 'title': 'PRBS', 'what': f'With both ends in, one end generates the test pattern and the other checks it, then the other way. '
                                             f'Each direction runs a gate of {PRBS_MIN_S:g} s unless the criteria file says otherwise.',
     'judges': 'The bit error ratio per lane is under the limit in both directions.', 'duration_s': 2 * PRBS_MIN_S, 'waits': 'the gate'},
    {'name': 'vdm_flags', 'title': 'Flags', 'what': 'Reads the latched flags and the VDM pages when the module advertises them.',
     'judges': 'No alarm flag is set.', 'duration_s': 2, 'waits': None},
    {'name': 'low_power_again', 'title': 'Low power again', 'what': 'Puts the module back to low power and reads its state once more.',
     'judges': 'The module returns to ModuleLowPwr.', 'duration_s': 3, 'waits': 'the module'},
    {'name': 'verdict', 'title': 'Verdict', 'what': 'Judges every observed value against the criteria file and writes the report.',
     'judges': 'PASS when every judged value is within its limit. FAIL on any limit. INCONCLUSIVE when a value was not measured.',
     'duration_s': 1, 'waits': None},
]

# The capability map (design section 4): what each command needs, for the Explore command list and the Help page.
CAPS = [
    {'cmd': 'status', 'group': 'Read', 'title': 'Pins', 'what': 'The control pins the FT2232H reads. No bus traffic.', 'needs': ['unit']},
    {'cmd': 'id', 'group': 'Read', 'title': 'Identity', 'what': 'Vendor, part, serial, revision, state, class and MaxPower.', 'needs': ['module']},
    {'cmd': 'cable', 'group': 'Read', 'title': 'Plate', 'what': 'The passport: identity, applications, test features, thresholds, firmware.', 'needs': ['module']},
    {'cmd': 'decode', 'group': 'Read', 'title': 'Decode all pages', 'what': 'Every advertised CMIS page as named fields, bank-aware, with the raw bytes behind a toggle.', 'needs': ['module']},
    {'cmd': 'dump', 'group': 'Read', 'title': 'Dump pages', 'what': 'The raw pages into a record file for later decode.', 'needs': ['module']},
    {'cmd': 'passport', 'group': 'Read', 'title': 'Save passport', 'what': 'The plate as JSON and HTML under the unit folder.', 'needs': ['module']},
    {'cmd': 'reg-read', 'group': 'Control', 'title': 'Register read', 'what': 'Bytes from a page and offset.', 'needs': ['module']},
    {'cmd': 'reg-write', 'group': 'Control', 'title': 'Register write', 'what': 'Bytes to a page and offset, read back after. A write changes the module.', 'needs': ['module', 'care']},
    {'cmd': 'reset', 'group': 'Control', 'title': 'Reset timing', 'what': 'Pulses ResetL and times the first answer. Needs MODE in AUTO.', 'needs': ['unit']},
    {'cmd': 'tristate', 'group': 'Control', 'title': 'Tristate', 'what': 'Every FTDI pin back to input, before MOD PWR goes off with a module in.', 'needs': ['unit']},
    {'cmd': 'release', 'group': 'Run', 'title': 'Release and hold', 'what': 'Lets the module leave low power and holds it with temperature and Vcc every second.', 'needs': ['module', 'budget', 'confirm']},
    {'cmd': 'timing', 'group': 'Run', 'title': 'State timing', 'what': 'MgmtInit, power up, data path init and deinit, power down, against the advertised maxima.', 'needs': ['module', 'budget', 'confirm']},
    {'cmd': 'flags', 'group': 'Run', 'title': 'Flags and masks', 'what': 'Latched flags clear on read, masks read back, IntL follows the mask.', 'needs': ['module', 'budget', 'confirm']},
    {'cmd': 'prbs', 'group': 'Run', 'title': 'PRBS both ways', 'what': 'A test pattern from each end to the other, bit error ratio per lane.', 'needs': ['two units', 'budget', 'confirm']},
    {'cmd': 'media-link', 'group': 'Run', 'title': 'Media link check', 'what': 'Both ends up, the media lanes linked, the flags watched.', 'needs': ['two units', 'budget', 'confirm']},
    {'cmd': 'trend', 'group': 'Run', 'title': 'Trend', 'what': 'One direction sampled every second: BER and SNR per lane, loss of lock, temperature.', 'needs': ['two units', 'budget', 'confirm']},
    {'cmd': 'loopback', 'group': 'Run', 'title': 'Loopback', 'what': 'The far end loops the media input back; the near end sends and checks.', 'needs': ['two units', 'budget', 'confirm']},
    {'cmd': 'features', 'group': 'Features', 'title': 'Feature matrix', 'what': 'Every advertised capability in four states: works, hollow, not exercised, not advertised. Exercise a row or a group.', 'needs': ['module']},
    {'cmd': 'audit', 'group': 'Query', 'title': 'Audit', 'what': 'Twenty checks of the advertisement against the pages the module serves.', 'needs': ['module']},
    {'cmd': 'i2c-stress', 'group': 'Query', 'title': 'Bus stress', 'what': 'Repeated page reads: NACKs, page read-back mismatches, time per read, the deselect test.', 'needs': ['module']},
    {'cmd': 'cdb', 'group': 'Query', 'title': 'CDB query', 'what': 'Read-only queries over the command block: status, features, firmware info.', 'needs': ['module', 'CDB']},
    {'cmd': 'fw-info', 'group': 'Firmware', 'title': 'Firmware info', 'what': 'The firmware management features and the two image banks.', 'needs': ['module', 'CDB']},
    {'cmd': 'fw-update', 'group': 'Firmware', 'title': 'Firmware update', 'what': 'The CDB download: plan first, then write, complete, verify; run and commit behind their own flags.', 'needs': ['module', 'CDB', 'image file', 'gate FW-1']},
    {'cmd': 'fw-abort', 'group': 'Firmware', 'title': 'Abort download', 'what': 'Ends a download the module still holds open.', 'needs': ['module', 'CDB']},
    {'cmd': 'derive', 'group': 'Database', 'title': 'Derive criteria', 'what': 'A criteria file from the plugged cable, PROPOSED, for review and commit.', 'needs': ['module']},
    {'cmd': 'match-sku', 'group': 'Database', 'title': 'Match the database', 'what': 'The identity against the criteria files.', 'needs': ['module']},
]


def run_command(units, cmd, body):
    """The web2 commands as jobs; everything else goes to web.run_command."""
    if cmd not in MINE:
        return web.run_command(units, cmd, body)
    serial = body.get('serial')
    timeout = float(body.get('timeout') or tech.TIMEOUT_S)

    def fn(say, ask):
        p = units.port(serial)
        if cmd == 'decode':
            rec = read_pages(p, say=say, cdb=body.get('cdb', True) is not False)
            rec['unit'], rec['read_at'] = serial, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            return rec
        if cmd == 'audit':
            return advtests.cmd_audit(p, serial, body.get('out'), False, say)
        if cmd == 'i2c-stress':
            return advtests.cmd_i2c_stress(p, serial, int(body.get('count') or 15), body.get('out'), False, say)
        if cmd in ('timing', 'flags'):
            _confirmed(body)
            budget = _budget(body, say)
            p.allow_lp_low = True
            try:
                if cmd == 'timing':
                    return advtests.cmd_timing(p, serial, body.get('out'), budget, timeout, body.get('apsel'), False, say)
                return advtests.cmd_flags(p, serial, body.get('out'), budget, timeout, False, say)
            finally:
                p.set_lpmode(1)
                p.allow_lp_low = False
        from . import fw as fwmod
        if cmd == 'fw-info':
            return fwmod.fw_info(p, say)
        if cmd == 'fw-abort':
            return fwmod.fw_abort(p, say)
        image = body.get('image') or ''
        if not os.path.isfile(image):
            raise Refused(f'no image file at {image or "(empty)"} on this host')
        hb = body.get('header_bytes')
        return fwmod.fw_update(p, image, say, dry_run=bool(body.get('plan')), commit=bool(body.get('commit')), run=bool(body.get('run_image')),
                               force=bool(body.get('force')), header_bytes=int(hb) if hb not in (None, '') else None,
                               confirm=body.get('confirm') or None, image_version=body.get('image_version') or None,
                               password=int(str(body['password']), 0) if body.get('password') else None,
                               run_only=bool(body.get('run_only')))

    job = Job(cmd).run(fn)
    JOBS[job.id] = job
    return job


def make_handler(units, roots):
    Base = web.make_handler(units, roots)

    class H(Base):
        def _static(self, name):
            for d in STATIC_DIRS:
                path = os.path.join(d, name)
                if os.path.isfile(path):
                    data = open(path, 'rb').read()
                    self.send_response(200)
                    self.send_header('Content-Type', CTYPES[name.rsplit('.', 1)[-1]] + '; charset=utf-8')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return None
            return self._json({'error': 'not found'}, 404)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = dict(urllib.parse.parse_qsl(u.query))
            name = u.path.lstrip('/')
            try:
                if u.path == '/api/steps':
                    return self._json({'steps': STEPS, 'order': [tech.STEPS[k] for k in sorted(tech.STEPS)]})
                if u.path == '/api/caps':
                    return self._json({'caps': CAPS, 'budget_w': BUDGET_W})
                if u.path == '/api/decode':  # a saved dump record (a dump_*.json with lower + pages)
                    rec = web._record(qs['path'], roots)
                    if 'lower' not in rec:
                        raise Refused('not a page dump: no lower memory in this record')
                    out = decode_dump(rec)
                    out['unit'], out['source'] = (rec.get('units') or [None])[0] if isinstance(rec.get('units'), list) else rec.get('unit'), qs['path']
                    return self._json(out)
                if name and '/' not in name and name.rsplit('.', 1)[-1] in CTYPES and os.path.isfile(os.path.join(STATIC2, name)):
                    return self._static(name)
            except (Refused, Nack, KeyError, ValueError, OSError) as e:
                return self._json({'error': f'{type(e).__name__}: {e}'}, 400)
            return super().do_GET()

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if u.path.startswith('/api/') and u.path[5:] in MINE:
                n = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(n) or b'{}')
                try:
                    return self._json({'job': run_command(units, u.path[5:], body).id})
                except (Refused, Nack, KeyError, ValueError) as e:
                    return self._json({'error': f'{type(e).__name__}: {e}'}, 400)
            return super().do_POST()
    return H


def make_server(port=8765, dry_run=False, sku=None, roots=web.RECORD_ROOTS):
    """web.make_server with the web2 handler swapped in (the server reads RequestHandlerClass per request)."""
    srv = web.make_server(port, dry_run, sku, roots)
    srv.RequestHandlerClass = make_handler(srv.units, web.ROOTS)
    return srv


def serve(port=8765, dry_run=False, sku=None, roots=web.RECORD_ROOTS, open_browser=False):
    srv = make_server(port, dry_run, sku, roots)
    url = f'http://127.0.0.1:{srv.server_address[1]}/'
    print(f'aectest web2: {url}  ({"fake backend" if dry_run else "hardware"}; Ctrl-C stops; the CLI must not open a unit the browser holds)')
    if open_browser:
        import webbrowser

        def later():
            time.sleep(1.5)
            if not web.CLIENTS:
                webbrowser.open(url)
        threading.Thread(target=later, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\naectest web2: stopping (Ctrl-C); units left SAFE', file=sys.stderr)
    finally:
        srv.units.close_all()
        srv.server_close()
    return 0


def add_web(sub, name='web2'):
    ap = sub.add_parser(name, help='the second browser UI on localhost')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--dry-run', action='store_true', help='the fake pair FAKE-A / FAKE-B (simulation, demo, selftest)')
    ap.add_argument('--sku', help='--dry-run: the fake module image of this SKU (fake_modules)')
    ap.add_argument('--open', action='store_true', help='open the page in the default browser')
    ap.set_defaults(func=lambda a: serve(a.port, a.dry_run, a.sku, open_browser=a.open))
    return ap
