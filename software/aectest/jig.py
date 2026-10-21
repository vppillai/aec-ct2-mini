#!/usr/bin/env python3
"""jig.py - units, roles, the custom PID and the signed jig profile.

A jig profile is a small YAML the commissioning engineer writes once per jig and signs; the technician never edits it.
`load_profile(path)` returns the dict plus `file` and `sha256` (of the whole file). It refuses a profile without a `signed`
line, without `signed_by`, or whose `signed` hash does not match the lines above it (the signed profile replaces
`--i-am-on-usbc-power` in technician mode), and one with `straps_intact: false` (2.9: tech mode refuses). `strict=False`
returns such a profile with `signed` None (the straps stay as written) so safety.preconditions records POWER-UNACKED /
STRAPS-CUT instead (2.10). `sign_profile(path, by)` writes the two lines. The signature is a plain sha256: it detects an edit, not the author (known limit).

  python -m aectest.jig --selftest
  python -m aectest.jig sign PROFILE.yaml "<name>"
"""
import hashlib
import os
import sys

import yaml

ROLES = ('A', 'B')  # end A is the left unit (2.9)
STOCK_PID = 0x6010
EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jig_profile.example.yaml')
REQUIRED = ('jig', 'units', 'power', 'straps_intact', 'hood')


def _split(text):
    """The signed text (every line before the first `signed:`, `signed_by:` included), and the `signed:` tail."""
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.startswith('signed:'):
            return ''.join(lines[:i]), ''.join(lines[i:])
    return text, ''


def _unsigned(text):
    """The profile without its `signed_by:` and `signed:` lines."""
    return ''.join(ln for ln in text.splitlines(keepends=True) if not ln.startswith(('signed_by:', 'signed:')))


def signature(body):
    return hashlib.sha256(body.encode()).hexdigest()


def sign_profile(path, by):
    """Append `signed_by: <by>` and `signed: <sha256 of the lines above>`; an already signed file is re-signed."""
    body = _unsigned(open(path).read())
    if not body.endswith('\n'):
        body += '\n'
    body += f'signed_by: {by}\n'
    with open(path, 'w') as f:
        f.write(body + f'signed: {signature(body)}\n')
    return load_profile(path)


def load_profile(path, strict=True):
    """The profile dict + `file`, `sha256`; SystemExit when a field is missing, the signature is absent or wrong, or the straps
    are cut; with strict=False a bad signature gives `signed` None and cut straps load (preconditions names them)."""
    raw = open(path, 'rb').read()
    text = raw.decode()
    body, tail = _split(text)
    d = yaml.safe_load(text) or {}
    missing = [k for k in REQUIRED if k not in d]
    if missing:
        raise SystemExit(f'jig profile {path}: missing {", ".join(missing)} (2.9)')
    if not tail or not d.get('signed_by'):
        bad = f'not signed by the commissioning engineer -- `python -m aectest.jig sign {path} "<name>"`'
    elif d['signed'] != signature(body):
        bad = 'signature does not match the lines above it -- edited after signing; sign it again'
    else:
        bad = None
    if bad and strict:
        raise SystemExit(f'jig profile {path}: {bad}')
    if bad:
        d['signed'] = None
    if strict and not d['straps_intact']:
        raise SystemExit(f'jig profile {path}: straps_intact false = R&D dongle session; the technician run refuses (2.9)')
    if not all(d['units'].get(r) for r in ROLES):
        raise SystemExit(f'jig profile {path}: units needs both {ROLES} serials')
    d['pid'] = d.get('pid') or STOCK_PID
    d['file'], d['sha256'] = path, hashlib.sha256(raw).hexdigest()
    return d


def hardware_manifest(profile):
    """The 5.2 `hardware` block of manifest.json from a loaded profile."""
    return {'board': 'AEC-CT2-MINI', 'rev': '0', 'units': dict(profile['units']), 'jig': profile['jig'],
            'jig_profile_sha256': profile['sha256'], 'straps_intact': profile['straps_intact'], 'hood': profile['hood']}


def register_pid(pid, ftdi_cls=None):
    """Custom PID programmed in the 93LC56: make pyftdi's URL parser and enumerator know it. Once per process
    -- Ftdi.add_custom_product() raises ValueError on a second registration (review R6-D18)."""
    if ftdi_cls is None:
        from pyftdi.ftdi import Ftdi as ftdi_cls
    try:
        ftdi_cls.add_custom_product(0x0403, pid, 'ct2m')
    except ValueError:
        pass  # already registered


# --------------------------------------------------------------------------- selftest
def selftest():
    import re
    import shutil
    import tempfile
    p = load_profile(EXAMPLE)
    assert p['jig'] == 'JIG-01' and p['units'] == {'A': 'CT2M-01', 'B': 'CT2M-02'} and p['pid'] == STOCK_PID
    assert len(p['sha256']) == 64 and p['sha256'] == hashlib.sha256(open(EXAMPLE, 'rb').read()).hexdigest()
    hw = hardware_manifest(p)
    assert set(hw) == {'board', 'rev', 'units', 'jig', 'jig_profile_sha256', 'straps_intact', 'hood'} and hw['straps_intact'] is True
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, 'jig.yaml')
        body = _unsigned(open(EXAMPLE).read())
        open(f, 'w').write(body)  # the lines without the signature
        signed = open(EXAMPLE).read()
        for bad, word in ((body, 'not signed'), (body + 'signed_by: x\nsigned: ' + '0' * 64 + '\n', 'does not match'),
                          (re.sub(r'(?m)^hood:.*\n', '', body) + 'signed_by: x\nsigned: 0\n', 'missing hood'),
                          (signed.replace('hood: "off"', 'hood: "on"'), 'does not match')):  # a value edited after signing
            open(f, 'w').write(bad)
            try:
                load_profile(f)
                raise AssertionError(word)
            except SystemExit as e:
                assert word in str(e), (word, e)
            if word != 'missing hood':
                assert load_profile(f, strict=False)['signed'] is None and load_profile(f, strict=False)['jig'] == 'JIG-01'
        shutil.copy(EXAMPLE, f)
        open(f, 'a').write('# a comment after the signature changes no value\n')
        assert load_profile(f)['signed'] == p['signed']
        open(f, 'w').write(body.replace('hood: "off"', 'hood: "on"'))
        q = sign_profile(f, 'tester')
        assert q['hood'] == 'on' and q['signed_by'] == 'tester' and q['sha256'] != p['sha256']
        assert load_profile(f)['signed'] == q['signed']
        open(f, 'w').write(body.replace('straps_intact: true', 'straps_intact: false'))
        try:
            sign_profile(f, 'tester')  # signs the file, then its strict load refuses the cut straps (2.9)
            raise AssertionError('cut straps accepted in strict mode')
        except SystemExit as e:
            assert 'straps_intact false' in str(e), e
        assert load_profile(f, strict=False)['straps_intact'] is False and load_profile(f, strict=False)['signed']

    class _F:
        calls = 0

        @staticmethod
        def add_custom_product(vid, pid, name):
            _F.calls += 1
            if _F.calls > 1:
                raise ValueError('already')
    register_pid(0x1234, _F)
    register_pid(0x1234, _F)
    assert _F.calls == 2
    print('jig selftest: OK (example profile loads, sha256, unsigned / edited / incomplete / cut straps refused, strict=False records, re-sign, PID once)')


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        selftest()
    elif len(sys.argv) == 4 and sys.argv[1] == 'sign':
        print(sign_profile(sys.argv[2], sys.argv[3])['signed'])
    else:
        print(__doc__)
