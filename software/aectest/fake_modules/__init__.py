"""fake_modules - YAML memory images per SKU for the FakeBackend.

load(sku) -> {"sku", "cmis_revision", "lower": bytearray(128), "pages": {page_int: bytearray(128)}}.
The selftest decodes the image with the package decoder (aectest.cmis.decode_id). FakeBackend(image=load(sku)) runs it.

  python -m aectest.fake_modules --selftest
"""
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = 'aectest.fake_module/1'


def _bytes(lines):
    b = bytearray.fromhex(''.join(lines))
    if len(b) != 128:
        raise SystemExit(f'fake module image: {len(b)} bytes, want 128')
    return b


def load(sku):
    path = os.path.join(HERE, f'{sku}.yaml')
    d = yaml.safe_load(open(path))
    if d.get('schema') != SCHEMA:
        raise SystemExit(f'{path}: schema {d.get("schema")!r} is not {SCHEMA}')
    return {'sku': d['sku'], 'cmis_revision': str(d['cmis_revision']), 'file': path, 'lower': _bytes(d['lower']),
            'pages': {int(k.rstrip('h'), 16): _bytes(v) for k, v in d['pages'].items()}}


def selftest():
    sys.path.insert(0, os.path.join(HERE, '..', '..'))
    from aectest.cmis import decode_id, decode_thresholds
    img = load('credo-cac41x321d1d')
    d = decode_id(img['lower'], img['pages'][0])
    assert d['vendor'] == 'Credo' and d['part_number'] == 'CAC41X321D1D', d
    assert d['identifier'] == '0x18' and d['identifier_name'] == 'QSFP-DD' and d['cmis_revision'] == '4.0', d
    assert d['power_class'] == 3 and d['max_power_w'] == 5.5 and d['module_state'] == 'ModuleLowPwr', d
    assert d['memory_model'] == 'paged' and d['serial'] == 'CRD0000000000001' and d['temp_c'] == 35.0, d
    assert d['media_interface_technology_00h_212'].startswith('0x0c Copper cable, near and far end'), d
    thr = decode_thresholds(img['pages'][2])
    assert thr['temp_high_warning_c'] == 75.0 and thr['temp_high_alarm_c'] == 80.0 and thr['vcc_low_warning_v'] == 3.1, thr
    assert set(img['pages']) == {0x00, 0x01, 0x02, 0x03, 0x10, 0x11, 0x13, 0x14, 0x20, 0x24, 0x2F, 0x9F}
    assert img['pages'][0x10][0] == 0xFF and img['pages'][0x11][8] == 0xFF
    print(f'fake_modules selftest: OK ({img["sku"]} decodes: {d["vendor"]} {d["part_number"]} CMIS {d["cmis_revision"]} '
          f'class {d["power_class"]} {d["max_power_w"]} W)')


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        selftest()
    else:
        print(__doc__)
