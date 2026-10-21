#!/usr/bin/env python3
"""records.py - the session directory, manifest, event log, unit state and zip export.

One Session = one directory under out_dir (5.1 layout). manifest.json is written first with open mode 'x' and rewritten
once at close (manifest.json.tmp + os.replace) with the verdict, the sha256 of every other file and `ended`. `with Session(...)
as s:` closes INCONCLUSIVE ABORTED:<exc> after an `abort` event when the block raises. events.jsonl is append-only. The unit
state file (5.4) is the only mutable record; <unit>_mating_log.csv is generated from it. export_zip packs one session (5.5).

  python -m aectest.records --selftest
"""
import csv
import hashlib
import json
import os
import platform
import sys
import zipfile
from datetime import datetime, timezone

OUT_DIR = os.environ.get('AECTEST_OUT', os.path.join('out', 'test'))  # the one `--out` default (5.1): env AECTEST_OUT, else out/test
MANIFEST_SCHEMA, EVENT_SCHEMA, UNIT_SCHEMA = 'aectest.manifest/1', 'aectest.event/1', 'aectest.unit/1'
HEALTH_COLUMNS = ['t_s', 'end', 'state', 'temp_c', 'vcc_v', 'flags9', 'intl', 'present']
LOT_COLUMNS = ['timestamp', 'SN_A', 'SN_B', 'class', 'result', 'key_number', 'label_sn', 'sku', 'lot', 'jig', 'unit_A',
               'unit_B', 'max_power_w', 'temp_max_A', 'temp_max_B', 'worst_errors_AB', 'worst_errors_BA', 'worst_ber_AB',
               'worst_ber_BA', 'mgmt_init_ms_A', 'mgmt_init_ms_B', 'reasons', 'session_id']
MATING_COLUMNS = ['date', 'board_serial', 'cable_serial', 'cycle_no', 'event', 'test_ref', 'I_A', 'V_pad_V', 'VccMon_V',
                  'R_proxy_mOhm', 'notes']  # MINI_TEST_PLAN T12; the hand-measured columns stay empty until entered


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def sha256(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


class Session:
    on_event = None  # the technician screen hook (tui.Screen.on_event, set by tech.run_cable): one call per event; a class attribute so `attach` has it too

    def __init__(self, out_dir, mode, name, tool=None, hardware=None, cable=None, criteria=None, operator=None, lot=None):
        """Create <out_dir>/<UTC stamp>_<name>/ and write manifest.json first (mode 'x': a second open fails)."""
        self.session_id = f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{name}'
        self.dir = os.path.join(out_dir, self.session_id)
        os.makedirs(self.dir, exist_ok=True)
        self.seq = 0
        self.manifest = {'schema': MANIFEST_SCHEMA, 'session_id': self.session_id, 'mode': mode, 'tool': tool or {},
                         'hardware': hardware or {}, 'cable': cable or {},
                         'criteria': {k: criteria[k] for k in ('file', 'sha256', 'owner') if k in criteria} if criteria else {},
                         'operator': operator, 'lot': lot, 'verdict': None, 'files': {}, 'started': utc(), 'ended': None}
        with open_new(self.path('manifest.json')) as f:  # a second session with this id in the same second refuses by name
            json.dump(self.manifest, f, indent=1, default=str)
        self.event('open', data={'session_id': self.session_id})

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if ev is not None and self.manifest['ended'] is None:
            self.event('abort', data={'error': f'{et.__name__}: {ev}'})
            self.close({'status': 'INCONCLUSIVE', 'reasons': [f'ABORTED:{ev or et.__name__}'], 'key_numbers': {}})

    @classmethod
    def attach(cls, session_dir):
        """Re-open an existing session directory (rd mode runs one process per verb): manifest read back, `seq` continues."""
        s = cls.__new__(cls)
        s.dir = session_dir
        s.manifest = json.load(open(s.path('manifest.json')))
        s.session_id = s.manifest['session_id']
        s.seq = sum(1 for _ in open(s.path('events.jsonl'))) if os.path.exists(s.path('events.jsonl')) else 0
        return s

    def path(self, name):
        return os.path.join(self.dir, name)

    def event(self, type, end=None, data=None):
        self.seq += 1
        row = {'schema': EVENT_SCHEMA, 't': utc(), 'seq': self.seq, 'type': type, 'end': end, 'data': data or {}}
        with open(self.path('events.jsonl'), 'a') as f:
            f.write(json.dumps(row, default=str) + '\n')
        if self.on_event:
            self.on_event(row)
        return row

    def write_json(self, name, obj):
        with open(self.path(name), 'x') as f:
            json.dump(obj, f, indent=1, default=str)
            f.write('\n')

    def write_health_row(self, t_s, end, h):
        """One health.csv line from a poll_health() dict (state, temp_c, vcc_v, flags9, intl, present from ModPrsL)."""
        new = not os.path.exists(self.path('health.csv'))
        with open(self.path('health.csv'), 'a', newline='') as f:
            w = csv.writer(f)
            if new:
                w.writerow(HEALTH_COLUMNS)
            w.writerow([t_s, end, h.get('state'), h.get('temp_c'), h.get('vcc_v'), h.get('flags9'), h.get('intl'), int(h['present'])])

    def close(self, verdict):
        """Record the verdict (Verdict dataclass or dict), hash every other file, set `ended`, rewrite the manifest once."""
        v = verdict.as_dict() if hasattr(verdict, 'as_dict') else verdict
        self.event('verdict', data=v)
        self.event('close')
        self.manifest['verdict'] = v
        self.manifest['files'] = {n: sha256(self.path(n)) for n in sorted(os.listdir(self.dir)) if n != 'manifest.json'}
        self.manifest['ended'] = utc()
        with open(self.path('manifest.json.tmp'), 'w') as f:  # atomic: a crash mid-write leaves the open manifest, not half of one
            json.dump(self.manifest, f, indent=1, default=str)
        os.replace(self.path('manifest.json.tmp'), self.path('manifest.json'))
        return self.manifest


def lot_csv_row(manifest):
    """One lot CSV row in the 5.3 column order (the lot id first). key_number = the worse of the two error counts (PROPOSED)."""
    m = manifest
    cmis, v = m.get('cable', {}).get('cmis', {}), m.get('verdict') or {}
    k = v.get('key_numbers', {})
    errs = [k.get(f'worst_errors_{d}') for d in ('AB', 'BA') if k.get(f'worst_errors_{d}') is not None]
    row = {'timestamp': m.get('ended') or m.get('started'), 'SN_A': cmis.get('A', {}).get('serial'),
           'SN_B': cmis.get('B', {}).get('serial'), 'class': cmis.get('A', {}).get('power_class'), 'result': v.get('status'),
           'key_number': max(errs) if errs else None, 'label_sn': m.get('cable', {}).get('label_sn'),
           'sku': m.get('cable', {}).get('sku'), 'lot': m.get('lot'), 'jig': m.get('hardware', {}).get('jig'),
           'unit_A': m.get('hardware', {}).get('units', {}).get('A'), 'unit_B': m.get('hardware', {}).get('units', {}).get('B'),
           'reasons': ' | '.join(v.get('reasons', [])), 'session_id': m.get('session_id')}
    for c in LOT_COLUMNS:
        row.setdefault(c, k.get(c))
    return [row[c] for c in LOT_COLUMNS]


class UnitState:
    """out/test/units/<CT2M-NN>.json (5.4): the only mutable record. Every touch appends a log row and regenerates mating_log.csv."""

    def __init__(self, path, unit_id):
        self.dir, self.unit_id = os.path.join(path, 'units'), unit_id
        self.file = os.path.join(self.dir, f'{unit_id}.json')
        os.makedirs(self.dir, exist_ok=True)
        self.state = json.load(open(self.file)) if os.path.exists(self.file) else {
            'schema': UNIT_SCHEMA, 'unit_id': unit_id, 'insertions': 0, 'first_seen': None, 'last_seen': None,
            'eeprom_sha256': None, 'last_self_check': None, 'log': []}

    def touch(self, session_id, event='insert', cable_serial=None, test_ref=None, eeprom_hash=None, self_check=None, notes=''):
        s, now = self.state, utc()
        s['first_seen'] = s['first_seen'] or now
        s['last_seen'] = now
        if event == 'insert':
            s['insertions'] += 1
        if eeprom_hash:
            s['eeprom_sha256'] = eeprom_hash
        if self_check is not None:
            s['last_self_check'] = {'t': now, 'result': self_check, 'session_id': session_id}
        s['log'].append({'date': now, 'board_serial': self.unit_id, 'cable_serial': cable_serial, 'cycle_no': s['insertions'],
                         'event': event, 'test_ref': test_ref, 'notes': notes, 'session_id': session_id})
        with open(self.file, 'w') as f:
            json.dump(s, f, indent=1)
        with open(os.path.join(self.dir, f'{self.unit_id}_mating_log.csv'), 'w', newline='') as f:
            w = csv.DictWriter(f, MATING_COLUMNS, extrasaction='ignore')
            w.writeheader()
            w.writerows(s['log'])
        return s


def export_zip(session_dir):
    """<session_dir>.zip with every file of the session under its session id (5.5); returns the zip path."""
    out = session_dir.rstrip('/') + '.zip'
    base = os.path.basename(session_dir.rstrip('/'))
    with zipfile.ZipFile(out, 'x', zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(session_dir):
            for n in sorted(files):
                p = os.path.join(root, n)
                z.write(p, os.path.join(base, os.path.relpath(p, session_dir)))
    return out


# --------------------------------------------------------------------------- the bring-up tool's records (moved verbatim)
# open_new, write_record). The per-run JSON and T7 CSV formats stay as the bring-up tool writes them today.
CSV_HAND_COLS = 'hand_I_A,hand_Vpad_V,hand_T_UM_C,hand_T_D101_C,hand_T_D102_C,hand_T_sink_C,hand_TP107_V,hand_MAN'


def pyftdi_version():
    try:
        from importlib.metadata import version
        return version('pyftdi')
    except Exception:  # noqa: BLE001 -- dry-run without pyftdi installed
        return 'n/a'


def tool_commit():
    """`git describe --always --dirty` of the checkout this file lives in; 'n/a' outside a checkout."""
    try:
        import subprocess
        r = subprocess.run(['git', 'describe', '--always', '--dirty', '--abbrev=9'], capture_output=True, text=True,
                           timeout=5, cwd=os.path.dirname(os.path.abspath(__file__)))
        return r.stdout.strip() or 'n/a'
    except Exception:  # noqa: BLE001 -- no git
        return 'n/a'


def tool_block():
    """The 5.2 `tool` block of the manifest: name, version, git, python, pyftdi, os."""
    from . import __version__
    return {'name': 'aectest', 'version': __version__, 'git': tool_commit(), 'python': platform.python_version(),
            'pyftdi': pyftdi_version(), 'os': platform.platform()}


def pid_alive(pid):
    """True while a process with this pid exists (signal 0); the liveness check of a state file."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return True
    return True


def record_header(serials):
    """Every JSON record names the tool commit, the pyftdi version and the FT2232H serial(s)."""
    try:
        from .transport import CLOCKS  # here: transport imports nothing from records, records stays import-light
    except ImportError:
        from aectest.transport import CLOCKS
    clocks = {s: CLOCKS[s] for s in serials if s in CLOCKS}
    one = next(iter(clocks.values()), None) if len(clocks) == 1 else None
    return {'timestamp': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'tool': 'aectest',
            'tool_commit': tool_commit(), 'pyftdi_version': pyftdi_version(), 'ftdi_serials': list(serials),
            'i2c_hz': one['i2c_hz'] if one else {s: c['i2c_hz'] for s, c in clocks.items()},
            'i2c_ramp': one['i2c_ramp'] if one else {s: c['i2c_ramp'] for s, c in clocks.items()}}


def open_new(path):
    """Records are per run: the file must not exist yet (no overwrite, no append)."""
    try:
        return open(path, 'x')
    except FileExistsError:
        raise SystemExit(f'not overwriting {path} -- pick another name (records are per run)') from None


def write_record(d, out, default, say=None):
    """JSON to stdout (`--out -`) or to a file that must not exist yet; the default name `default` gets a UTC stamp with
    microseconds in place of `{}`. `say` renders the JSON text or the `wrote` line (2.4: the caller prints, not the record)."""
    try:
        from .transport import CLOCKS
    except ImportError:
        from aectest.transport import CLOCKS
    for serial in d.get('ftdi_serials') or []:
        if serial in CLOCKS and isinstance(d.get('i2c_hz'), dict):
            d['i2c_hz'][serial] = CLOCKS[serial].get('i2c_hz', d['i2c_hz'].get(serial))
            if isinstance(d.get('i2c_ramp'), dict):
                d['i2c_ramp'][serial] = CLOCKS[serial].get('i2c_ramp', d['i2c_ramp'].get(serial))

    say = say or (lambda text, err=False: None)
    text = json.dumps(d, indent=1)
    if out == '-':
        say(text)
        return
    out = out or default.format(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ'))
    with open_new(out) as f:
        f.write(text + '\n')
    say(f'wrote {out}')


# --------------------------------------------------------------------------- selftest
def selftest():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        crit = {'file': 'criteria/credo-cac41x321d1d.yaml', 'sha256': 'ab' * 32, 'owner': '<name>', 'identity': {}}
        s = Session(d, 'tech', 'CRD000001', tool={'name': 'aectest', 'version': '0.1.0'},
                    hardware={'board': 'AEC-CT2-MINI', 'units': {'A': 'CT2M-01', 'B': 'CT2M-02'}, 'jig': 'JIG-01'},
                    cable={'sku': 'credo-cac41x321d1d', 'label_sn': 'CRD000001',
                           'cmis': {'A': {'serial': 'CRD000001', 'power_class': 3}, 'B': {'serial': 'CRD000001'}}},
                    criteria=crit, operator='op', lot=None)
        try:
            open(s.path('manifest.json'), 'x')
            raise AssertionError('second manifest open must fail')
        except FileExistsError:
            pass
        s.event('identity', 'A', {'vendor': 'Credo'})
        s.write_json('pages_A.json', {'lower': '00' * 128})
        for t in (0, 1):
            s.write_health_row(t, 'A', {'state': 1, 'temp_c': 31.0, 'vcc_v': 3.3, 'flags9': 0, 'intl': 1, 'present': True})
        verdict = {'status': 'PASS', 'reasons': [], 'key_numbers': {'worst_errors_AB': 0, 'worst_errors_BA': 1, 'max_power_w': 5.5,
                                                                    'temp_max_A': 50.0, 'mgmt_init_ms_A': 400}}
        m = s.close(verdict)
        assert m['ended'] and set(m['files']) == {'events.jsonl', 'pages_A.json', 'health.csv'}
        for n, h in m['files'].items():
            assert sha256(s.path(n)) == h, n
        events = [json.loads(l) for l in open(s.path('events.jsonl'))]
        assert [e['seq'] for e in events] == [1, 2, 3, 4] and events[-1]['type'] == 'close' and events[-2]['data'] == verdict
        assert all(e['schema'] == EVENT_SCHEMA and e['t'].endswith('Z') for e in events)
        s_att = Session.attach(s.dir)
        assert s_att.seq == 4 and s_att.session_id == s.session_id and s_att.event('identity')['seq'] == 5
        assert open(s.path('health.csv')).readline().strip() == ','.join(HEALTH_COLUMNS)
        assert json.load(open(s.path('manifest.json')))['schema'] == MANIFEST_SCHEMA
        row = lot_csv_row(m)
        assert len(row) == len(LOT_COLUMNS) == 23 and row[4] == 'PASS' and row[5] == 1 and row[12] == 5.5 and row[-1] == s.session_id
        u = UnitState(d, 'CT2M-01')
        u.touch(s.session_id, 'insert', cable_serial='CRD000001', test_ref='T11', eeprom_hash='cd' * 32, self_check='OK')
        u.touch(s.session_id, 'remove', cable_serial='CRD000001', test_ref='T11')
        u2 = UnitState(d, 'CT2M-01')
        assert u2.state['insertions'] == 1 and u2.state['eeprom_sha256'] == 'cd' * 32 and u2.state['last_self_check']['result'] == 'OK'
        assert u2.state['first_seen'] <= u2.state['last_seen'] and len(u2.state['log']) == 2
        log = list(csv.reader(open(os.path.join(d, 'units', 'CT2M-01_mating_log.csv'))))
        assert log[0] == MATING_COLUMNS and len(log) == 3 and log[2][4] == 'remove'
        z = export_zip(s.dir)
        names = sorted(os.path.basename(n) for n in zipfile.ZipFile(z).namelist())
        assert names == sorted(os.listdir(s.dir)) == ['events.jsonl', 'health.csv', 'manifest.json', 'pages_A.json'], names
        said = []
        write_record({'a': 1}, os.path.join(d, 'r.json'), '', say=lambda t, err=False: said.append(t))
        assert said == [f'wrote {os.path.join(d, "r.json")}'] and json.load(open(os.path.join(d, 'r.json'))) == {'a': 1}
        try:
            write_record({}, os.path.join(d, 'r.json'), '')
            raise AssertionError('overwrote a per-run record')
        except SystemExit as e:
            assert 'not overwriting' in str(e)
        assert set(record_header(['X'])) == {'timestamp', 'tool', 'tool_commit', 'pyftdi_version', 'ftdi_serials', 'i2c_hz', 'i2c_ramp'}
        assert CSV_HAND_COLS.count(',') == 7
        import datetime
        try:
            with Session(d, 'tech', 'ABORT', hardware={'power': {'date': datetime.date(2026, 10, 11)}}) as s2:
                raise ValueError('cable pulled')
        except ValueError:
            pass
        m2 = json.load(open(s2.path('manifest.json')))
        assert m2['verdict'] == {'status': 'INCONCLUSIVE', 'reasons': ['ABORTED:cable pulled'], 'key_numbers': {}} and m2['ended'], m2
        assert m2['hardware']['power']['date'] == '2026-10-11' and not os.path.exists(s2.path('manifest.json.tmp'))
        types = [json.loads(l)['type'] for l in open(s2.path('events.jsonl'))]
        assert types == ['open', 'abort', 'verdict', 'close'], types
        try:
            Session(d, 'tech', 'ABORT')  # same name in the same second: refused by name, no raw FileExistsError
            raise AssertionError('duplicate session id accepted')
        except SystemExit as e:
            assert 'not overwriting' in str(e) and 'manifest.json' in str(e), e
    print('records selftest: OK (manifest x-open refused, hashes verified, lot row 23 columns, zip complete, per-run record helpers, '
          'abort path + atomic manifest + default=str)')


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        selftest()
    else:
        print(__doc__)
