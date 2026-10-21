"""The cable passport: everything the tester can read about the module in one unit, in one dict, in low power.

Identity (read_id), the application descriptors (lower page 86-117, Table 8-21), the advertisements (01h:142 pages, 155
controls, 159 monitors, 163 CDB), the diagnostics capabilities (13h:128-139, Tables 8-94 to 8-100) when the pages are
advertised, the thresholds (02h), the CDB firmware info (0100h) when one CDB instance is advertised, plus the unit's
FRU and pins. `save()` writes the JSON and a one-page HTML beside it. No write to the module beyond the page selects
and the read-only CDB query.
"""
import html
import json
import os
import time

from .cdb import cdb_command
from .cmis import PATTERNS, P_VDM_DESC, decode_thresholds, module_flag_names
from .fru import summary as fru_summary
from .tests import read_id

PATTERN_NAMES = {v: k for k, v in PATTERNS.items()}
LOOPBACKS = {6: 'host and media side at once', 5: 'media side per lane', 4: 'host side per lane', 3: 'host side input',
             2: 'host side output', 1: 'media side input', 0: 'media side output'}  # Table 8-94
MEDIA_IDS_COPPER = {0x01: 'copper cable'}


def _apps(lower):
    out = []
    for i in range(8):
        a = lower[86 + 4 * i:90 + 4 * i]
        if len(a) < 4 or a[0] in (0, 0xFF):
            break
        out.append({'n': i + 1, 'host_id': f'0x{a[0]:02x}', 'media_id': f'0x{a[1]:02x}', 'host_lanes': a[2] >> 4,
                    'media_lanes': a[2] & 0xF, 'host_lane_assign': f'0x{a[3]:02x}'})
    return out


def _patterns(b0, b1):
    bits = b0 | (b1 << 8)
    return [PATTERN_NAMES.get(i, f'id {i}') for i in range(16) if bits >> i & 1]


def _caps(p13):
    b = lambda o: p13[o - 128]  # noqa: E731
    return {'loopbacks': [LOOPBACKS[k] for k in sorted(LOOPBACKS, reverse=True) if b(128) >> k & 1],
            'gating': bool(b(129) & 0xC0), 'periodic_updates': bool(b(129) & 0x10),
            'ber_results': bool(b(130) & 0x01), 'error_counts': bool(b(130) & 0x02),
            'snr_media': bool(b(130) & 0x20), 'snr_host': bool(b(130) & 0x10),
            'generator_host': _patterns(b(132), b(133)), 'generator_media': _patterns(b(134), b(135)),
            'checker_host': _patterns(b(136), b(137)), 'checker_media': _patterns(b(138), b(139))}


def _hollow(p11, vdm_desc):
    """Monitors that read as placeholders (seen on an Accelink: 25.6 uW on every lane, VDM pages all zero). Read-only:
    the user page write test is a bench experiment, not a passport read. Table 8-78: TxPower 11h:154-169, RxPower 186-201, 0.1 uW."""
    tx = [int.from_bytes(p11[154 - 128 + 2 * i:156 - 128 + 2 * i], 'big') for i in range(8)]
    rx = [int.from_bytes(p11[186 - 128 + 2 * i:188 - 128 + 2 * i], 'big') for i in range(8)]
    return {'tx_power_identical_on_every_lane': len(set(tx)) == 1 and tx[0] != 0, 'tx_power_uw': [t / 10 for t in tx],
            'rx_power_identical_on_every_lane': len(set(rx)) == 1 and rx[0] != 0, 'rx_power_uw': [r / 10 for r in rx],
            'vdm_descriptor_page_all_zero': (not any(vdm_desc)) if vdm_desc is not None else None,
            'user_page_03h_write_test': 'not run: the passport is read-only (EXP5 found the Accelink ignores writes)'}


def read(port, serial, budget_w, say=None):
    """The passport dict for the module in `port` (the unit `serial`). Says one line per block when `say` is given."""
    say = say or (lambda t, err=False: None)
    out = {'unit': serial, 'fru': fru_summary(getattr(port.be, 'fru', None)), 'read_at': time.time(), 'pins': port.sense()}
    if not out['pins'].get('module_present'):
        out['module'] = None
        say('no module present')
        return out
    with port.selected():
        f4 = port.read(8, 4)  # insert (00h:8-11, clear on read) are read FIRST and named; IntL releases when nothing else is pending
    out['flags_at_read'] = module_flag_names(f4)
    ident = read_id(port)
    out['module'] = ident
    out['budget'] = {'budget_w': budget_w, 'within': ident['max_power_w'] <= budget_w}
    flat = ident['memory_model'] == 'flat'
    with port.selected():
        lower = port.lower()
        out['applications'] = _apps(lower)
        out['active_apsel'] = None
        if not flat:
            p1 = port.read_page(0x01)
            p2 = port.read_page(0x02)
            p11 = port.read_page(0x11)
            out['active_apsel'] = p11[206 - 128] >> 4
            adv = p1[142 - 128]
            out['advertised'] = {'diagnostic_pages': bool(adv & 0x20), 'vdm': bool(adv & 0x40),
                                 'tx_output_disable': bool(p1[155 - 128] & 0x02), 'auto_squelch_disable': bool(p1[155 - 128] & 0x04),
                                 'temperature_monitor': bool(p1[159 - 128] & 0x01), 'vcc_monitor': bool(p1[159 - 128] & 0x02),
                                 'cdb_instances': p1[163 - 128] >> 6}
            out['thresholds'] = decode_thresholds(p2)
            out['diagnostics'] = _caps(port.read_page(0x13)) if adv & 0x20 else None
            out['hollow'] = _hollow(p11, port.read_page(P_VDM_DESC) if adv & 0x40 else None)
        else:
            out['advertised'] = {'diagnostic_pages': False, 'vdm': False, 'cdb_instances': 0}
            out['thresholds'] = None
            out['diagnostics'] = None
            out['hollow'] = None
    say(f"module {ident['vendor']} {ident['part_number']} {ident['serial']}, CMIS {ident['cmis_revision']}, "
        f"class {ident['power_class']} / {ident['max_power_w']:g} W")
    out['firmware'] = None
    if out['advertised'].get('cdb_instances'):
        try:
            r = cdb_command(port, 0x0100, say=say)
            out['firmware'] = r.get('decoded') if r.get('supported') else {'not_supported': r['status']}
        except Exception as e:  # noqa: BLE001 -- a CDB failure must not void the passport
            out['firmware'] = {'error': f'{type(e).__name__}: {e}'}
        with port.selected():  # the CDB reply latches CdbCmdCompleteFlag (00h:8, clear on read) and pulls IntL low again
            f4 = port.read(8, 4)
        out['flags_at_read'] += [n for n in module_flag_names(f4) if n not in out['flags_at_read']]
    out['pins_after_flags'] = port.sense()
    out['interrupt_pending'] = bool(out['pins_after_flags'].get('interrupt'))  # low after the read = a flag still latched elsewhere
    return out


def _rows(d):
    return ''.join(f'<tr><th>{html.escape(str(k))}</th><td>{html.escape(json.dumps(v) if isinstance(v, (dict, list)) else str(v))}</td></tr>'
                   for k, v in d.items())


def save(passport, folder, say=None):
    """passport_<unit>_<module serial>_<UTC>.json and .html in `folder`; returns both paths."""
    say = say or (lambda t, err=False: None)
    m = passport.get('module') or {}
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(passport['read_at']))
    base = os.path.join(folder, f"passport_{passport['unit']}_{m.get('serial', 'none')}_{stamp}")
    os.makedirs(folder, exist_ok=True)
    with open(base + '.json', 'w', encoding='utf-8') as f:
        json.dump(passport, f, indent=1, default=str)
    blocks = [('Unit', {'serial': passport['unit'], 'fru': passport['fru']}), ('Module', m), ('Budget', passport.get('budget') or {}),
              ('Advertised', passport.get('advertised') or {}), ('Diagnostics', passport.get('diagnostics') or {}),
              ('Thresholds', passport.get('thresholds') or {}), ('Firmware', passport.get('firmware') or {})]
    apps = ''.join(f"<tr><td>{a['n']}</td><td>{a['host_id']}</td><td>{a['media_id']}</td><td>{a['host_lanes']}</td><td>{a['media_lanes']}</td></tr>"
                   for a in passport.get('applications') or [])
    b = passport.get('budget') or {}
    badges = ''.join(f'<span class="b {c}">{html.escape(t)}</span>' for t, c in (
        (str(m.get('module_state', 'no module')), 'info'), (f"CMIS {m.get('cmis_revision', '-')}, {m.get('memory_model', '-')}", ''),
        (f"class {m.get('power_class', '-')}, {m.get('max_power_w', '-')} W {'within' if b.get('within') else 'above'} the {b.get('budget_w', '-')} W budget",
         'pass' if b.get('within') else 'fail'))) if m else ''
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Cable passport</title><style>'
            ':root{--bg:#eef1f4;--card:#fff;--line:#d5dbe2;--fg:#16212b;--fg2:#5b6975;--accent:#0e7c86;--pass:#2e8b57;--fail:#c0392b;--info:#2f6fe4}'
            'body{font:14px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif;margin:0;padding:24px;background:var(--bg);color:var(--fg)}'
            'main{max-width:960px;margin:0 auto}h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}.sub{color:var(--fg2);margin:0 0 12px}'
            '.b{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;background:#f6f8fa;border:1px solid var(--line);margin:0 6px 6px 0}'
            '.b.pass{background:#dff3e7;border-color:transparent;color:var(--pass)}.b.fail{background:#f9dedb;border-color:transparent;color:var(--fail)}'
            '.b.info{background:#e0e9fc;border-color:transparent;color:var(--info)}'
            'section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}h2{font-size:15px;margin:0 0 8px;color:var(--fg2)}'
            'table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}'
            'th{width:28%;font-weight:600;color:var(--fg2)}td{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}'
            '@media print{body{background:#fff;padding:0}section{break-inside:avoid}}</style></head><body><main>'
            f"<h1>Cable passport: {html.escape(m.get('vendor', ''))} {html.escape(m.get('part_number', ''))} "
            f"{html.escape(m.get('serial', ''))}</h1><p class=\"sub\">Read {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(passport['read_at']))} "
            f"on unit {html.escape(passport['unit'])}.</p><div>{badges}</div>"
            + ''.join(f'<section><h2>{t}</h2><table>{_rows(d)}</table></section>' for t, d in blocks)
            + f'<section><h2>Applications (active {passport.get("active_apsel")})</h2><table><tr><th>n</th><th>host id</th><th>media id</th>'
              f'<th>host lanes</th><th>media lanes</th></tr>{apps}</table></section></main></body></html>')
    with open(base + '.html', 'w', encoding='utf-8') as f:
        f.write(page)
    say(f'wrote {base}.json and .html')
    return base + '.json', base + '.html'
