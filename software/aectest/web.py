"""`aectest web` -- the browser UI of the tester.

One process, stdlib `http.server`: static files from `web/`, a JSON API, server-sent events for the live lines of a
running command. Every command is the same function the CLI runs (tests.py, tech.py), so a run from the browser leaves
the same record files in the same folders. One Port per unit, opened on demand; the unit lock of transport.py keeps the
CLI and the web server from opening one stick at once. Localhost only, no auth.
"""
import base64
import collections
import hashlib
import sys
import csv
import datetime
import getpass
import glob
import json
import os
import queue
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import cdb as cdbmod, derive, passport, tech, tests
from . import fru as frumod
from .fru import summary as fru_summary
from .jig import EXAMPLE
from .records import OUT_DIR, write_record
from .safety import BUDGET_W
from . import transport
from .transport import CLOCKS, FakeBackend, FtdiBackend, Nack, Port, Refused, ramp_clock
from .cmis.decode import read_pages as cmis_read_pages

try:
    from . import fw as fwmod
    if os.path.dirname(os.path.abspath(fwmod.__file__)) != os.path.dirname(os.path.abspath(__file__)):
        fwmod = None  # an editable install can resolve the module from another checkout: only this tree's engine counts
except ImportError:
    fwmod = None
START_HINT = 'start it with: aectest web --open'

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
FW_IMAGES = os.path.abspath(os.path.join(STATIC, '..', '..', '..', 'build', 'fw_images'))
FW_IMAGE_MAX = 16 << 20
RECORD_ROOTS = (os.path.join('30-board', 'layout', 'test'), OUT_DIR)
OUT_ROOT = os.path.abspath(OUT_DIR)  # the technician sessions of browser runs; make_server sets it to the last root
CONFIRM = ('release-lpmode', 'usbc-power')  # the CLI's two permission flags, as two explicit clicks (design 6)
SPEED = 100_000
SPEED_MODE = 'auto'
CLIENTS = set()  # the heartbeat connections (/api/events) now open
TECH_STEPS = [tech.STEPS[k] for k in sorted(tech.STEPS)]
HEALTH_RE = re.compile(r'^\s*\+\s*([\d.]+) s\s+(?:(\S+): )?state (\S+)\s+temp\s+([\d.-]+) C\s+vcc ([\d.]+) V')  # the S5 line, app.js parses the same


class Units:
    """One Port per serial, opened on demand; the fake pair FAKE-A / FAKE-B in --dry-run."""

    def __init__(self, dry_run, image=None):
        self.dry_run, self.image, self.ports, self.lock = dry_run, image, {}, threading.Lock()
        self.extra = []  # dry-run: units that 'arrive' after launch (/api/fake-arrive), for the discovery check
        self.instance, self.started = secrets.token_hex(8), time.time()

    def list(self):
        if self.dry_run:
            return [{'serial': s, 'product': 'AEC-CT2-MINI fake'} for s in ['FAKE-A', 'FAKE-B', *self.extra]]
        from pyftdi.ftdi import Ftdi
        return sorted(({'serial': d.sn or '?', 'product': d.description} for d, _ in Ftdi.list_devices('ftdi:///?')), key=lambda u: u['serial'])

    def port(self, serial):
        with self.lock:
            if serial not in self.ports:
                be = FakeBackend(serial, SPEED, self.image) if self.dry_run else FtdiBackend(f'ftdi://ftdi:2232h:{serial}/1', SPEED)
                if self.dry_run and getattr(be, 'fru', None) is None:
                    be.fru = frumod.parse_board_area(frumod.build_board_area({'manufacturer': 'fake works', 'product': 'AEC-CT2-MINI fake', 'serial': serial, 'part': 'ed9431d7', 'file_id': 'fru-fake', 'rev': 'REV 0', 'order': 'FAKE-ORDER-01', 'case': ''}))
                self.ports[serial] = Port(be, False, say=lambda t, err=False: None)
            return self.ports[serial]

    def close_all(self):
        with self.lock:
            for serial, p in list(self.ports.items()):
                try:
                    p.close()  # a gone unit says so inside close (transport GONE_ERRORS); anything else must not stop the rest
                except Exception as e:  # noqa: BLE001
                    print(f'aectest web: {serial} close failed ({type(e).__name__}: {e})', file=sys.stderr)
            self.ports.clear()

    def scan(self):
        """Discovery: re-enumerate the descriptors (no open) and release the port of a unit that is gone."""
        names = [u['serial'] for u in self.list()]
        with self.lock:
            for s in [s for s in self.ports if s not in names]:
                try:
                    self.ports.pop(s).close()
                except Exception:  # noqa: BLE001 -- the device is gone; the lock file goes with the close
                    pass
        return names

    def fake_arrive(self):
        if not self.dry_run:
            raise Refused('fake-arrive is for the fake backend only')
        self.extra.append(f'FAKE-{chr(ord("C") + len(self.extra))}')
        return self.list()


class Job:
    """A command in a thread; its say() lines go to a queue the SSE stream drains; ask() blocks on the browser's answer."""

    seq = 0

    def __init__(self, name):
        Job.seq += 1
        self.id, self.name, self.q, self.result, self.error = str(Job.seq), name, queue.Queue(), None, None
        self.answer, self.reply = threading.Event(), None

    def say(self, text, err=False):
        self.q.put({'type': 'line', 'text': text, 'err': bool(err), 't': time.time()})

    def ask(self, text, read=None, skippable=False):
        """True = the operator pressed the button, False = Stop, 'skip' (truthy) = Skip with a reason (only when the
        engine says the step is skippable; the reason lands in the log line 'skipped by the operator: ...')."""
        self.answer.clear()
        self.skip_reason = None
        self.q.put({'type': 'ask', 'text': text, 'skippable': bool(skippable)})
        self.answer.wait()
        if self.reply == 'skip':
            self.say(f'skipped by the operator: {self.skip_reason or "no reason given"}')
        return self.reply

    def run(self, fn):
        def body():
            try:
                self.result = fn(self.say, self.ask)
                self.q.put({'type': 'done', 'result': self.result})
            except (Refused, Nack, SystemExit, Exception) as e:  # noqa: BLE001 -- every failure reaches the browser as a line
                self.error = f'{type(e).__name__}: {e}'
                self.q.put({'type': 'error', 'text': self.error})
        threading.Thread(target=body, daemon=True).start()
        return self


JOBS = {}


def _confirmed(body):
    if set(body.get('confirm') or []) < set(CONFIRM):
        raise Refused(f'high power needs both confirmations {CONFIRM} (design 6)')


def _budget(body, say):
    b = float(body.get('budget_w') or BUDGET_W)
    if b != BUDGET_W:
        if not body.get('reason'):
            raise Refused('a budget above the default needs a reason (Advanced only)')
        say(f'override budget_w {b:g} (default {BUDGET_W:g}): {body["reason"]}')
        write_record({'override': 'budget_w', 'value': b, 'default': BUDGET_W, 'reason': body['reason'], 'unit': body.get('serial') or body.get('pair')},
                     None, 'web_override_{}.json', say)
    return b


def run_command(units, cmd, body):
    """The command bodies, each the CLI's function on the shared Port; returns the job."""
    serial = body.get('serial')
    timeout = float(body.get('timeout') or 20)

    def one(say, ask):
        p = units.port(serial)
        if cmd == 'status':
            return {**p.sense(), **unit_fru(p)}
        if cmd == 'id':
            return tests.read_id(p)
        if cmd == 'i2c-ramp':
            return ramp_clock(p, say, target='1M' if SPEED_MODE == '1M' else None) if SPEED_MODE != '100k' else {'i2c_hz': p.be.hz, 'i2c_ramp': 'fixed 100000 Hz'}
        if cmd == 'dump':
            return tests.cmd_dump(p, serial, body.get('out'), say)
        if cmd == 'reset':
            return {'first_ack_s': tests.cmd_reset(p, say)}
        if cmd == 'tristate':
            p.tristate()
            return {'tristate': True}
        if cmd == 'reg-read':
            with p.selected():
                p.page(int(body.get('page', 0)), int(body.get('bank', 0)))
                data = p.read(int(body['off']), int(body.get('n', 1)))
            return {'page': body.get('page', 0), 'off': body['off'], 'hex': data.hex()}
        if cmd == 'reg-write':
            data = bytes.fromhex(body['hex'])
            with p.selected():
                p.page(int(body.get('page', 0)), int(body.get('bank', 0)))
                p.write(int(body['off']), data)
                back = p.read(int(body['off']), len(data))
            say(f'wrote page {body.get("page", 0)} off {body["off"]} {data.hex()} -> reads {back.hex()}')
            return {'written': data.hex(), 'readback': back.hex()}
        if cmd == 'cable':
            return passport.read(p, serial, BUDGET_W, say)
        if cmd == 'match-sku':
            return derive.match(tests.read_id(p), body.get('criteria') or derive.CRITERIA_DIR)
        if cmd == 'derive':
            timing = set(body.get('confirm') or []) >= set(CONFIRM)
            fig = derive.figures(p, serial, say, timing, float(body.get('budget_w') or BUDGET_W))
            path, text, existed = derive.write(body.get('criteria') or derive.CRITERIA_DIR, fig, body.get('owner') or '<name>', body.get('note') or '', body.get('out'))
            say(f'wrote {path}. Commit it after review.')
            return {'path': path, 'rel': os.path.relpath(path, os.path.dirname(derive.CRITERIA_DIR)), 'text': text, 'existed': existed, 'sku': derive.sku_name(fig), 'timing': timing}
        if cmd == 'decode':
            rec = cmis_read_pages(p, say)
            if body.get('save'):
                folder = _unit_folder(serial)
                os.makedirs(folder, exist_ok=True)
                rec['file'] = os.path.join(folder, f'decode_{serial}_{datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")}.json')
                write_record(rec, rec['file'], rec['file'], say)
            return rec
        if cmd == 'fw/info':
            if fwmod is None:
                return {'status': 'not available', 'reason': 'engine not merged'}
            return fwmod.fw_info(p)
        if cmd == 'fw/update':
            if fwmod is None:
                return {'status': 'not available', 'reason': 'engine not merged'}
            if not body.get('image'):
                raise Refused('the firmware update needs the image file path')
            pw = body.get('password') or None
            hb = body.get('header_bytes')
            return fwmod.fw_update(p, body['image'], say=say, dry_run=bool(body.get('dry_run', True)), commit=bool(body.get('commit')),
                                   run=bool(body.get('run_image')), run_only=bool(body.get('run_only')),
                                   header_bytes=int(hb) if hb not in (None, '') else None, image_version=body.get('image_version') or None,
                                   confirm=body.get('confirm_text') or None, password=int(str(pw), 0) if pw else None)
        if cmd == 'features':
            from . import advtests
            fn = next((getattr(m, 'cmd_features', None) for m in (advtests, tests) if getattr(m, 'cmd_features', None)), None)
            if fn is None:
                return {'status': 'not available', 'reason': 'engine not merged', 'kind': 'features', 'rows': []}
            if body.get('exercise'):  # an exercise may release the module: the same guards as every Run command
                _confirmed(body)
                p.allow_lp_low = True
            try:
                return fn(p, serial, exercise=body.get('exercise'), out=body.get('out'), budget_w=_budget(body, say) if body.get('exercise') else BUDGET_W, say=say)
            finally:
                if body.get('exercise'):
                    p.set_lpmode(1)
                    p.allow_lp_low = False
        if cmd == 'passport':
            pp = passport.read(p, serial, BUDGET_W, say)
            folder = _unit_folder(serial)
            j, h = passport.save(pp, folder, say)
            return {**pp, 'files': [j, h]}
        if cmd == 'cdb':
            args = body.get('args') or ['query']
            lines = []
            cdbmod.cmd_cdb(p, args, False, None, bool(body.get('raw')), serial, lambda t, err=False: (lines.append(t), say(t, err)))
            name = args[0]
            cmd_id = cdbmod.NAMES.get(name) if name != 'raw' else int(args[1], 0)
            res = cdbmod.cdb_command(p, cmd_id, b'\x00\x00' if name == 'query' else (bytes.fromhex(args[2]) if name == 'raw' and len(args) > 2 else b''),
                                     raw=name == 'raw') if cmd_id is not None else {}
            return {'query': name, **res}
        if cmd == 'release':
            _confirmed(body)
            budget = _budget(body, say)
            p.allow_lp_low = True
            try:
                info = tests.release_lpmode(p, budget, timeout, say=say)
                stop = tests.hold_high_power(p, float(body.get('hold', 30)), log=body.get('log'), say=say)
            finally:
                p.set_lpmode(1)
                p.allow_lp_low = False
            return {'release': {k: info.get(k) for k in ('gate', 'module_ready_observed_s', 'temp_warn_c', 'temp_stop_c')}, 'stop': stop}
        raise Refused(f'unknown command {cmd}')

    def pair(say, ask):
        a, b = body['pair'].split(':', 1)
        _confirmed(body)
        budget = _budget(body, say)
        pa, pb = units.port(a), units.port(b)
        pa.allow_lp_low = pb.allow_lp_low = True
        try:
            if cmd == 'prbs':
                return tests.cmd_prbs(pa, pb, body.get('pattern', 'prbs31q'), float(body.get('time', 20)), budget, timeout,
                                      bool(body.get('force_cmis')), body.get('out'), (a, b), body.get('lane_mask'), say=say,
                                      apsel=body.get('apsel'))
            if cmd in ('trend', 'loopback'):
                return tests.cmd_trend(pa, pb, body.get('pattern', 'prbs31q'), float(body.get('time', 20)), budget, timeout,
                                       bool(body.get('force_cmis')), body.get('out'), (a, b), body.get('lane_mask'), say=say,
                                       apsel=body.get('apsel'), loop=cmd == 'loopback')
            return tests.cmd_media_link(pa, pb, float(body.get('time', 20)), budget, timeout, body.get('out'), (a, b), say=say)
        finally:
            pa.allow_lp_low = pb.allow_lp_low = False

    def technician(say, ask):
        profile = dict(tech.load_profile(body.get('jig') or EXAMPLE))
        attached = [u['serial'] for u in units.list()]
        a_unit = body.get('a') or (attached[0] if attached else profile['units']['A'])
        mode = body.get('mode') or ('both' if len(attached) >= 2 else 'one')
        sel = {'A': a_unit}
        if mode == 'both':
            sel['B'] = body.get('b') or next((u for u in attached if u != a_unit), profile['units'].get('B'))
        profile['units'] = sel
        croot = body.get('criteria') or tech.CRITERIA_DIR
        sku = body.get('sku') or 'auto'
        if sku == 'auto':  # read the cable in unit A, match the cable database
            m = derive.match(tests.read_id(units.port(a_unit)), croot)
            sku = m['sku'] or 'none'
            say(f"cable {m['identity'].get('vendor')} {m['identity'].get('part_number')}: " + (f'criteria {sku}' if m['matched'] else 'no criteria: the verdict carries NO-CRITERIA'))
        units.close_all()  # the technician flow opens its own Ports (tech.open_units); the lock would refuse a second open
        crit = tech.load_criteria(sku, croot)
        image = tech.fake_image(crit, sku, units.dry_run)
        label_sn = (body.get('label_sn') or '').strip() or None
        operator = (body.get('operator') or '').strip() or getpass.getuser()
        name = label_sn or (crit['sku'] if crit else 'cable')
        with tech.session_for(body.get('out') or OUT_ROOT, 'tech', name, profile, crit, label_sn, operator,
                              dry_run=units.dry_run) as s:
            s.on_event = lambda e: say('event ' + json.dumps(e, default=str))
            try:
                ports, checklist = tech.open_units(profile, say, ask, units.dry_run, image)
            except tech.Stopped as e:
                text = f'stopped by the operator before step 1 of {len(TECH_STEPS)} ({e.text})' if e.code == 'ABORTED:checklist' else e.text
                v = tech.Verdict(tech.INCONCLUSIVE, [tech.Reason(e.code, 'open_units', text)])
                tech.write_report(s, v)
                s.close(v)
                return {'verdict': v.as_dict(), 'session': s.dir}
            try:
                v = tech.run_cable(s, profile, crit, ports, label_sn, say, ask, checklist,
                                   soak_s=body.get('soak_s'), gate_s=body.get('gate_s'))  # the selftest shortens them; the page does not send them
            finally:
                for p in ports.values():
                    p.close()
        return {'verdict': v.as_dict(), 'session': s.dir, 'report': os.path.join(s.dir, 'report.md')}

    fn = technician if cmd == 'tech' else pair if cmd in ('prbs', 'media-link', 'trend', 'loopback') else one
    job = Job(cmd).run(fn)
    JOBS[job.id] = job
    return job


def _unit_folder(serial):
    """The newest run folder of the unit under the first record root, else <out>/web (the browser's default folder)."""
    root = ROOTS[0] if ROOTS else OUT_ROOT
    cands = sorted(glob.glob(os.path.join(root, f'{serial}_*')), reverse=True)
    return cands[0] if cands and os.path.isdir(cands[0]) else os.path.join(OUT_ROOT, 'web')


ROOTS = ()


KINDS = (('T11_ber_', 'PRBS'), ('T11_media_', 'link'), ('T11_loop_', 'loopback'), ('T11_trend_', 'trend'), ('T7_trend', 'trend'), ('dump_', 'dump'),
         ('decode_', 'decode'), ('passport_', 'passport'), ('cdb_', 'cdb'), ('adv_', 'check'), ('report', 'report'), ('results', 'report'), ('EXP', 'probe'))


def _kind(name):
    """The file kind badge: by the record prefix, else the extension."""
    for prefix, kind in KINDS:
        if name.startswith(prefix):
            return kind
    return name.rsplit('.', 1)[-1]


def _records(roots):
    out = []
    for root in roots:
        for path in glob.glob(os.path.join(root, '**', '*'), recursive=True):
            if os.path.isfile(path) and path.endswith(('.json', '.csv', '.md')):
                out.append({'path': path, 'name': os.path.basename(path), 'folder': os.path.relpath(os.path.dirname(path), root),
                            'kind': _kind(os.path.basename(path)), 'mtime': os.path.getmtime(path)})
    return sorted(out, key=lambda r: -r['mtime'])


def _folders(records):
    """Records grouped by run folder, folders newest first by their newest file."""
    groups = {}
    for r in records:
        groups.setdefault(r['folder'], []).append(r)
    return [{'folder': f, 'files': fs} for f, fs in sorted(groups.items(), key=lambda kv: -kv[1][0]['mtime'])]


_IDENT_CACHE = {}  # (path, mtime) -> identity dict or None


def _epoch(v):
    """An ISO 8601 stamp or an epoch number -> epoch seconds, else None."""
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.datetime.fromisoformat(str(v).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def _identity(rec):
    """The module identity a record carries (records belong to cables, not units): vendor, part_number,
    serials, units, time. A record with a time but no module (a cdb reply, a T7 CSV) returns only units and time, so
    `_cables` can attach it by time. Cached by (path, mtime)."""
    key = (rec['path'], rec['mtime'])
    if key in _IDENT_CACHE:
        return _IDENT_CACHE[key]
    out = None
    try:
        if rec['name'].endswith('.json'):
            d = json.load(open(rec['path'], encoding='utf-8'))
            if isinstance(d, dict):
                t = _epoch(d.get('timestamp')) or _epoch(d.get('read_at')) or rec['mtime']
                units = d.get('ftdi_serials') or ([d['ftdi_serial']] if d.get('ftdi_serial') else []) or ([d['unit']] if d.get('unit') else [])
                ends = d.get('ends')
                m = d.get('decoded') if ('pages' in d or 'lower' in d) else d.get('module')
                if isinstance(ends, dict) and any(isinstance(e, dict) and e.get('part_number') for e in ends.values()):
                    ids = [e for e in ends.values() if isinstance(e, dict) and e.get('part_number')]
                    out = {'vendor': ids[0].get('vendor'), 'part_number': ids[0]['part_number'], 'serials': sorted({e['serial'] for e in ids if e.get('serial')}),
                           'units': list(units) or list(ends), 'time': t, 'module': _is_module(ids[0])}
                elif isinstance(m, dict) and m.get('part_number'):
                    out = {'vendor': m.get('vendor'), 'part_number': m['part_number'], 'serials': [m['serial']] if m.get('serial') else [], 'units': list(units), 'time': t,
                           'module': _is_module(m)}
                else:
                    out = {'units': list(units), 'time': t}
        elif rec['name'].endswith('.csv'):
            head = [l for l in open(rec['path'], encoding='utf-8', errors='replace').read(4000).splitlines() if l.startswith('# ')]
            meta = dict(l[2:].split(': ', 1) for l in head if ': ' in l)
            out = {'units': meta.get('ftdi_serials', '').strip('[]').replace("'", '').split(', ') if meta.get('ftdi_serials') else [], 'time': _epoch(meta.get('timestamp')) or rec['mtime']}
    except (OSError, ValueError):
        out = None
    _IDENT_CACHE[key] = out
    return out


def _is_module(ident):
    """An optical transceiver is one module with its own serial per end; a copper cable (00h:212 codes 0Ah-0Fh,
    Table 8-36) is one cable with two end serials. Unknown technology counts as a cable (the older rule)."""
    tech = str(ident.get('media_interface_technology_00h_212') or '')
    return None if not tech else 'Copper' not in tech


def _roots(serials):
    return {re.sub(r'-\d$', '', s) for s in serials}  # the two ends of one DAC read -1 and -2: one root


def _cable_key(ident):
    return f"{ident.get('vendor') or '?'}|{ident['part_number']}|{','.join(sorted(_roots(ident['serials'])))}"


def _find_cable(cables, ident):
    """The cable of this identity: same vendor and part, and a shared serial root (a pair record names both ends)."""
    roots = _roots(ident['serials'])
    for c in cables:
        if c['part_number'] == ident['part_number'] and c['vendor'] == ident.get('vendor') and (roots & _roots(c['serials']) or not roots and not c['serials']):
            return c
    return None


BY_TIME_WINDOW_S = 6 * 3600


def _cables(records):
    """The cable index: records grouped by the module they were read from. A record without an identity joins the cable of
    the nearest identified record in the same run folder, within six hours, marked by_time. The rest is unassigned."""
    cables, pending, unassigned = [], [], []
    for r in records:
        ident0 = _identity(r)
        if ident0 and ident0.get('part_number'):
            # keeps one card with both end serials (the -1 / -2 roots merge)
            idents = [{**ident0, 'serials': [s]} for s in ident0['serials']] if ident0.get('module') and len(ident0['serials']) > 1 else [ident0]
            for ident in idents:
                c = _find_cable(cables, ident)
                for other in [x for x in cables if x is not c and x is not None and _find_cable([x], ident)]:  # a pair record names both ends: it joins two single-end cables
                    c['serials'] += [s for s in other['serials'] if s not in c['serials']]
                    c['units'] += [u for u in other['units'] if u not in c['units']]
                    c['records'] += other['records']
                    c['first_seen'], c['last_seen'] = min(c['first_seen'], other['first_seen']), max(c['last_seen'], other['last_seen'])
                    cables.remove(other)
                if c is None:
                    c = {'key': _cable_key(ident), 'vendor': ident.get('vendor'), 'part_number': ident['part_number'], 'serials': [], 'units': [],
                         'first_seen': ident['time'], 'last_seen': ident['time'], 'records': [], 'kind': 'module' if ident.get('module') else 'cable'}
                    cables.append(c)
                if ident.get('module') is not None:  # a record that states the technology decides the kind; one without keeps it
                    c['kind'] = 'module' if ident['module'] else 'cable'
                c['serials'] += [s for s in ident['serials'] if s not in c['serials']]
                c['units'] += [u for u in ident['units'] if u not in c['units']]
                c['first_seen'], c['last_seen'] = min(c['first_seen'], ident['time']), max(c['last_seen'], ident['time'])
                c['records'].append({**r, 'time': ident['time'], 'unit': ', '.join(ident['units'])})
        elif ident0:
            pending.append((r, ident0))
        else:
            unassigned.append(r)
    for r, ident in pending:
        best = None
        for c in cables:
            for x in c['records']:
                if x['folder'] == r['folder'] and not x.get('by_time') and (best is None or abs(x['time'] - ident['time']) < best[0]):
                    best = (abs(x['time'] - ident['time']), c)
        if best and best[0] <= BY_TIME_WINDOW_S:
            best[1]['records'].append({**r, 'time': ident['time'], 'unit': ', '.join(ident['units']), 'by_time': True})
        else:
            unassigned.append(r)
    out = []
    for c in cables:
        c['key'] = _cable_key({'vendor': c['vendor'], 'part_number': c['part_number'], 'serials': c['serials']})
        c['records'].sort(key=lambda x: -x['time'])
        c['serials'].sort()
        c['kinds'] = dict(sorted(collections.Counter(x['kind'] for x in c['records']).items()))
        out.append(c)
    out.sort(key=lambda c: -c['last_seen'])
    return out, unassigned


def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%d %H:%MZ') if t else '-'


def records_table(roots, by_cable=True):
    """`aectest bringup records [--by-cable]`: the same index as /api/records, as text."""
    recs = _records(tuple(os.path.abspath(r) for r in roots))
    if not by_cable:
        return '\n'.join(f"{_iso(r['mtime'])}  {r['kind']:<9} {r['folder']}/{r['name']}" for r in recs) or 'no records'
    cables, un = _cables(recs)
    lines = []
    for c in cables:
        lines.append(f"{c.get('kind', 'cable')} {c['vendor']} {c['part_number']}  serial {', '.join(c['serials']) or '-'}  units {', '.join(c['units']) or '-'}  "
                     f"first {_iso(c['first_seen'])}  last {_iso(c['last_seen'])}  " + ', '.join(f'{k} {n}' for k, n in c['kinds'].items()))
        for r in c['records']:
            lines.append(f"    {_iso(r['time'])}  {r['kind']:<9} {r['folder']}/{r['name']}" + ('  (by time)' if r.get('by_time') else ''))
    if un:
        lines.append('unassigned: ' + ', '.join(f"{r['folder']}/{r['name']}" for r in un))
    return '\n'.join(lines) or 'no records'


def _record(path, roots):
    real = os.path.realpath(path)
    if not any(real.startswith(os.path.realpath(r) + os.sep) for r in roots):
        raise Refused('path outside the record roots')
    text = open(real, encoding='utf-8', errors='replace').read()
    if path.endswith('.json'):
        return json.loads(text)
    if path.endswith('.csv'):
        meta = dict(l[2:].split(': ', 1) for l in text.splitlines() if l.startswith('# ') and ': ' in l)
        rows = list(csv.DictReader(l for l in text.splitlines() if not l.startswith('#')))
        return {'meta': meta, 'rows': rows}
    return {'text': text}


def make_handler(units, roots):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _json(self, obj, code=200):
            data = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _static(self, name):
            path = os.path.join(STATIC, name)
            if not os.path.isfile(path):
                return self._json({'error': 'not found'}, 404)
            ctype = {'html': 'text/html', 'js': 'application/javascript', 'css': 'text/css', 'svg': 'image/svg+xml'}[name.rsplit('.', 1)[1]]
            data = open(path, 'rb').read()
            self.send_response(200)
            self.send_header('Content-Type', ctype + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = dict(urllib.parse.parse_qsl(u.query))
            try:
                if u.path in ('/', '/index.html'):
                    return self._static('index.html')
                if u.path == '/favicon.ico':
                    self.send_response(204)
                    self.end_headers()
                    return None
                if u.path in ('/app.js', '/style.css', '/conn.js', '/labels.js', '/help.js', '/md.js', '/steps.js', '/tabs.js', '/features.js'):
                    return self._static(u.path[1:])
                if u.path == '/api/units':
                    return self._json(_info(units))
                if u.path == '/api/info':  # the version and the release notes (CHANGELOG.md in the package) for the footer chip and the Help page
                    from . import __version__
                    return self._json({'version': __version__, 'changelog': open(os.path.join(os.path.dirname(STATIC), 'CHANGELOG.md')).read()})
                if u.path == '/api/status':
                    p = units.port(qs['serial'])
                    return self._json({**p.sense(), **unit_fru(p)})
                if u.path == '/api/skus':
                    root = qs.get('criteria') or derive.CRITERIA_DIR
                    return self._json({'dir': root, 'skus': derive.list_db(root)})
                if u.path == '/api/records':
                    recs = _records(roots)
                    cables, unassigned = _cables(recs)
                    return self._json({'records': recs, 'folders': _folders(recs), 'cables': cables, 'unassigned': unassigned})
                if u.path == '/api/record':
                    return self._json(_record(qs['path'], roots))
                if u.path == '/api/stream':
                    return self._stream(JOBS[qs['job']])
                if u.path == '/api/events':
                    return self._events(units)
                return self._json({'error': 'not found'}, 404)
            except (Refused, Nack, KeyError, ValueError, OSError) as e:
                return self._json({'error': f'{type(e).__name__}: {e}'}, 400)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}')
            try:
                if u.path == '/api/answer':
                    job = JOBS[body['job']]
                    job.reply = 'skip' if body.get('skip') else bool(body.get('yes'))
                    job.skip_reason = (body.get('reason') or '').strip() or None
                    job.answer.set()
                    return self._json({'ok': True})
                if u.path == '/api/fw/image':
                    data = base64.b64decode(body.get('data_b64') or '')
                    if not data or len(data) > FW_IMAGE_MAX:
                        raise ValueError(f'the image is empty or over {FW_IMAGE_MAX >> 20} MB')
                    sha = hashlib.sha256(data).hexdigest()
                    os.makedirs(FW_IMAGES, exist_ok=True)
                    path = os.path.join(FW_IMAGES, f'{sha}.bin')
                    if not os.path.exists(path):
                        with open(path, 'wb') as f:
                            f.write(data)
                    return self._json({'path': path, 'sha256': sha, 'size': len(data), 'name': os.path.basename(str(body.get('name') or 'image.bin'))})
                if u.path == '/api/fake-arrive':
                    return self._json({'units': units.fake_arrive()})
                if u.path == '/api/settings':
                    n = int(body.get('i2c_ramp_rounds') or 0)
                    if not 1 <= n <= 200:
                        raise ValueError('i2c_ramp_rounds is 1 to 200')
                    transport.RAMP_ROUNDS = n
                    return self._json({'ok': True, 'i2c_ramp_rounds': n})
                if u.path.startswith('/api/'):
                    job = run_command(units, u.path[5:], body)
                    return self._json({'job': job.id})
                return self._json({'error': 'not found'}, 404)
            except (Refused, Nack, KeyError, ValueError) as e:
                return self._json({'error': f'{type(e).__name__}: {e}'}, 400)

        def _stream(self, job):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            try:
                while True:
                    try:
                        ev = job.q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                        continue
                    self.wfile.write(f'data: {json.dumps(ev, default=str)}\n\n'.encode())
                    self.wfile.flush()
                    if ev['type'] in ('done', 'error'):
                        return
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _events(self, units):
            """The heartbeat: hello once, then a beat every 2 s with the instance id, the uptime and the open units."""
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            key = object()
            CLIENTS.add(key)
            try:
                kind, n, names = 'hello', 0, [u['serial'] for u in units.list()]
                while True:
                    running = sum(1 for j in JOBS.values() if j.result is None and j.error is None)
                    if n % 5 == 0 and not running:
                        names = units.scan()
                    n += 1
                    beat = {'type': kind, 'instance': units.instance, 'uptime_s': round(time.time() - units.started, 1), 't': time.time(),
                            'open_units': sorted(units.ports), 'jobs_running': running, 'units': names, 'clients': len(CLIENTS)}
                    self.wfile.write(f'data: {json.dumps(beat)}\n\n'.encode())
                    self.wfile.flush()
                    kind = 'beat'
                    time.sleep(2.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                CLIENTS.discard(key)
    return H


def unit_fru(p):
    """Batch 7 item 12: the unit's FRU area for the page: decoded fields, the summary, the raw bytes, the USB product string."""
    d = getattr(p.be, 'fru', None)
    raw = None
    ftdi = getattr(getattr(p.be, 'ctrl', None), 'ftdi', None)
    if ftdi is not None:
        try:
            raw = ftdi.read_eeprom(frumod.FRU_OFF, frumod.FRU_MAX, eeprom_size=256).hex()
        except Exception:  # noqa: BLE001  the hardware refused the EEPROM read: the decoded area still shows
            raw = None
    elif d:
        raw = frumod.build_board_area(d).hex()  # the fake: the area rebuilt from its fields
    return {'fru': fru_summary(d), 'fru_decoded': d, 'fru_raw_hex': raw, 'fru_product': getattr(p.be, 'product', None)}


def _info(units):
    if not any(j.result is None and j.error is None for j in JOBS.values()):
        units.scan()
    ulist = units.list()
    for u in ulist:
        p = units.ports.get(u['serial'])
        u['fru'] = unit_fru(p) if p else None
    return {'dry_run': units.dry_run, 'units': ulist, 'budget_w': BUDGET_W, 'instance': units.instance, 'started': units.started,
            'i2c': dict(CLOCKS), 'i2c_ramp_rounds': transport.RAMP_ROUNDS, 'speed': SPEED_MODE,
            'operator': getpass.getuser(), 'uptime_s': round(time.time() - units.started, 1), 'tech_steps': TECH_STEPS, 'record_roots': list(ROOTS),
            'firmware_engine': fwmod is not None, 'tech_timeout_s': tech.TIMEOUT_S}


class QuietServer(ThreadingHTTPServer):
    """A handler exception is one line on stderr, never a traceback in the operator's terminal."""

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        if isinstance(e, (BrokenPipeError, ConnectionResetError)):
            return
        print(f'aectest web: request from {client_address[0]} failed ({type(e).__name__}: {e})', file=sys.stderr)


def make_server(port=8765, dry_run=False, sku=None, roots=RECORD_ROOTS):
    """The server, not yet serving: the selftest binds port 0 and reads server_address."""
    image = None
    if dry_run and sku:
        from . import fake_modules
        image = fake_modules.load(sku)
    global OUT_ROOT, ROOTS
    units = Units(dry_run, image)
    roots = tuple(os.path.abspath(r) for r in roots)
    OUT_ROOT = roots[-1]
    ROOTS = roots
    srv = QuietServer(('127.0.0.1', port), make_handler(units, roots))
    srv.units = units
    outdir = os.path.join(roots[-1], 'web')  # every default record name lands here, not in the launch directory
    os.makedirs(outdir, exist_ok=True)
    os.chdir(outdir)
    return srv


def serve(port=8765, dry_run=False, sku=None, roots=RECORD_ROOTS, open_browser=False, speed='auto', ramp_rounds=None):
    global SPEED_MODE
    SPEED_MODE = speed
    if ramp_rounds:
        transport.RAMP_ROUNDS = int(ramp_rounds)
    srv = make_server(port, dry_run, sku, roots)
    url = f'http://127.0.0.1:{srv.server_address[1]}/'
    print(f'aectest web: {url}  ({"fake backend" if dry_run else "hardware"}; Ctrl-C stops; the CLI must not open a unit the browser holds)')
    print(f'aectest web: {START_HINT}')
    if open_browser:
        import webbrowser

        def later():
            time.sleep(1.5)
            if not CLIENTS:
                webbrowser.open(url)
        threading.Thread(target=later, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\naectest web: stopping (Ctrl-C); units left SAFE', file=sys.stderr)
    finally:
        srv.units.close_all()
        srv.server_close()
    return 0


def add_web(sub):
    ap = sub.add_parser('web', help='the browser UI on localhost')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--dry-run', action='store_true', help='the fake pair FAKE-A / FAKE-B (simulation, demo, selftest)')
    ap.add_argument('--sku', help='--dry-run: the fake module image of this SKU (fake_modules)')
    ap.add_argument('--open', action='store_true', help='open the page in the default browser')
    ap.add_argument('--speed', default='auto', choices=('auto', '100k', '1M'), help='I2C clock: auto (default: the browser ramps to 400k after the plate), 100k fixed, 1M when advertised')
    ap.add_argument('--ramp-rounds', type=int, default=20, metavar='N', help='the setting i2c_ramp_rounds (1..200), editable on the Settings page')
    ap.set_defaults(func=lambda a: serve(a.port, a.dry_run, a.sku, open_browser=a.open, speed=a.speed, ramp_rounds=a.ramp_rounds))
    return ap
