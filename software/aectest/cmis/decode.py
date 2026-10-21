"""The revision-aware CMIS decoder: `decode(dump)` on saved bytes, `read_pages(port)` for the pages a module advertises.

A dump is {'lower': 128 bytes, 'pages': {'00h': 128 bytes, '10h.b1': bank 1 of page 10h, ...}}; hex strings are accepted, so a
`build_dump` record (tests.py) or a `decode` record decodes as it is. Every field row of fields.py becomes one entry with its
register, value, raw bytes and unit. A block whose advertisement bit is clear reads 'not advertised' and keeps its raw bytes;
a block below the module's CmisRevision reads 'not in this revision'. Nothing here writes to a module.
"""
import time

from . import (BIAS_SCALE, CDB_RESULT, DURATION_MAX_S, PATTERNS, P_VDM_DESC, P_VDM_SAMPLE, decode_cdb_status, decode_duration, decode_id, f16,
               decode_vdm_descriptors)
from .fields import HOST_IDS, LENGTH_MULT, PAGES, UNREAD_PAGES

BIAS_SCALE_FROM_CODE = BIAS_SCALE  # Table 8-47 01h:160.4-3 -> x1 / x2 / x4 / None

SCHEMA = 'aectest.cmis.decode/1'
PATTERN_NAMES = {v: k for k, v in PATTERNS.items()}
BANK_PAGES = (0x10, 0x11, 0x13, 0x14)  # Table 8-2: lanes 8*bank + 1..8 on the banked pages 10h-1Fh
_ = CDB_RESULT, DURATION_MAX_S  # re-exported names stay importable from here for the selftest


def _bytes(v):
    return bytes.fromhex(v) if isinstance(v, str) else bytes(v)


def _bit(buf, byte, bit, base=128):
    return (buf[byte - base] >> bit) & 1


def _field(buf, byte, hi, lo):
    return (buf[byte] >> lo) & ((1 << (hi - lo + 1)) - 1)


def advertisements(lower, pages, rev):
    """The gate dict the field rows name (fields.F.adv) from 00h:2, 01h:142-163, 13h:128-131 (Tables 8-5, 8-41, 8-45 to 8-49, 8-94, 8-97)."""
    flat = bool(lower[2] & 0x80)
    a = {'paged': not flat, 'flat': flat}
    p1, p13 = pages.get('01h'), pages.get('13h')
    if p1 is None:
        return a
    b = lambda byte, bit: _bit(p1, byte, bit)  # noqa: E731
    a.update({'page03': bool(b(142, 2)), 'diag': bool(b(142, 5)) and rev >= 0x50, 'diag_bit': bool(b(142, 5)), 'vdm': bool(b(142, 6)),
              'banks': {0: 1, 1: 2, 2: 4}.get(p1[142 - 128] & 3, 1), 'tunable': bool(b(155, 6)),
              'cdb_instances': p1[163 - 128] >> 6, 'cdb': (p1[163 - 128] >> 6) in (1, 2), 'cdb2': (p1[163 - 128] >> 6) == 2,
              'mon.temp': bool(b(159, 0)), 'mon.vcc': bool(b(159, 1)), 'mon.aux1': bool(b(159, 2)), 'mon.aux2': bool(b(159, 3)),
              'mon.aux3': bool(b(159, 4)), 'mon.custom': bool(b(159, 5)), 'mon.tx_bias': bool(b(160, 0)), 'mon.tx_power': bool(b(160, 1)),
              'mon.rx_power': bool(b(160, 2)), 'bias_scale': BIAS_SCALE_FROM_CODE[(p1[160 - 128] >> 3) & 3],
              'ctl.polarity_tx': bool(b(155, 0)), 'ctl.disable_tx': bool(b(155, 1)), 'ctl.autosquelch_tx': bool(b(155, 2)),
              'ctl.force_squelch_tx': bool(b(155, 3)), 'ctl.polarity_rx': bool(b(156, 0)), 'ctl.disable_rx': bool(b(156, 1)),
              'ctl.autosquelch_rx': bool(b(156, 2)),
              'flag.failure_tx': bool(b(157, 0)), 'flag.los_tx': bool(b(157, 1)), 'flag.lol_tx': bool(b(157, 2)), 'flag.eq_fail_tx': bool(b(157, 3)),
              'flag.los_rx': bool(b(158, 1)), 'flag.lol_rx': bool(b(158, 2)),
              'si.cdr_tx': bool(b(161, 0)), 'si.fixed_eq': bool(b(161, 2)), 'si.adaptive_eq': bool(b(161, 3)), 'si.eq_freeze': bool(b(161, 4)),
              'si.eq_recall': bool((p1[161 - 128] >> 5) & 3), 'si.cdr_rx': bool(b(162, 0)), 'si.rx_amp': bool(b(162, 2)),
              'si.rx_eq': bool((p1[162 - 128] >> 3) & 3), 'si.scs1': bool(b(162, 5)), 'si.unidir': bool(b(162, 6))})
    if p13 is not None and a['diag']:
        loc = p13[131 - 128]
        a.update({'diag.gen_media': bool(loc & 0xC0), 'diag.chk_media': bool(loc & 0x30), 'diag.gen_host': bool(loc & 0x0C), 'diag.chk_host': bool(loc & 0x03),
                  'loop.media_out': bool(p13[0] & 1), 'loop.media_in': bool(p13[0] & 2), 'loop.host_out': bool(p13[0] & 4), 'loop.host_in': bool(p13[0] & 8)})
    return a


def _lanes(byte, bank):
    return [8 * bank + i + 1 for i in range(8) if byte >> i & 1]


def _nibbles(buf, bank, enum):
    out = []
    for i, b in enumerate(buf):
        for lane, v in ((2 * i + 1, b & 0xF), (2 * i + 2, b >> 4)):
            out.append({'lane': 8 * bank + lane, 'value': enum.get(v, f'reserved {v}') if enum else v})
    return out


def _app(raw):
    if raw[0] in (0, 0xFF):
        return None
    return {'host_id': f'0x{raw[0]:02x}', 'host_id_name': HOST_IDS.get(raw[0]), 'media_id': f'0x{raw[1]:02x}', 'host_lanes': raw[2] >> 4,
            'media_lanes': raw[2] & 0xF, 'host_lane_assign': f'0x{raw[3]:02x}', 'host_lane_starts': _lanes(raw[3], 0)}


def _diagdata(buf, selector):
    sel = selector
    if sel == 0x01 or sel == 0x11:
        return {'host_ber': [f16(buf[2 * i], buf[2 * i + 1]) for i in range(8)], 'media_ber': [f16(buf[16 + 2 * i], buf[17 + 2 * i]) for i in range(8)]}
    if sel in (0x02, 0x03, 0x04, 0x05, 0x12, 0x13, 0x14, 0x15):
        side, first = ('host', 1) if sel in (0x02, 0x03, 0x12, 0x13) else ('media', 1)
        first = 5 if sel in (0x03, 0x05, 0x13, 0x15) else 1
        rows = []
        for i in range(4):
            err = int.from_bytes(buf[16 * i:16 * i + 8], 'little')
            tot = int.from_bytes(buf[16 * i + 8:16 * i + 16], 'little')
            rows.append({'lane': first + i, 'errors': err, 'total_bits': tot, 'odd_total_bits_sync_loss': bool(tot & 1), 'ber': (err / tot) if tot else None})
        return {'side': side, 'lanes': rows}
    if sel == 0x06:
        u = lambda i: int.from_bytes(buf[2 * i:2 * i + 2], 'little') / 256.0  # noqa: E731
        return {'host_snr_db': [u(i) for i in range(8)], 'media_snr_db': [u(8 + i) for i in range(8)]}
    return None


def decode_field(row, buf, bank, adv, rev, ctx):
    """One fields.F row on a page buffer (index 0 = the row's base byte) -> the entry dict, or None below the field's revision."""
    if rev < row.rev:
        return None
    base = 0 if row.byte < 128 else 128
    i = row.byte - base
    raw = bytes(buf[i:i + row.n])
    reg = f'{ctx["page"]}:{row.byte}' + (f'-{row.byte + row.n - 1}' if row.n > 1 else '') + (f'.{row.bits[0]}-{row.bits[1]}' if row.bits and row.bits[0] != row.bits[1]
                                                                                          else f'.{row.bits[0]}' if row.bits else '')
    e = {'name': row.name, 'reg': reg, 'raw': raw.hex(), 'unit': row.unit or None}
    if row.adv is not None and not adv.get(row.adv, False):
        e.update({'advertised': False, 'value': 'not advertised'})
        return e
    if row.adv is not None:
        e['advertised'] = True
    k, a = row.kind, row.arg
    v = _field(buf, i, *row.bits) if row.bits else int.from_bytes(raw, 'big')
    if k == 'u':
        e['value'] = v
    elif k == 's':
        e['value'] = int.from_bytes(raw, 'big', signed=True)
    elif k == 'hex':
        e['value'] = raw.hex()
    elif k == 'ascii':
        e['value'] = raw.decode('ascii', 'replace').strip()
    elif k == 'enum':
        e['value'] = a.get(v, f'reserved 0x{v:02x}')
        e['code'] = v
    elif k == 'bits':
        e['value'] = [n for b, n in sorted(a.items(), reverse=True) if v >> b & 1]
    elif k == 'lanes':
        e['value'] = _lanes(v, bank)
    elif k == 'nib':
        e['value'] = _nibbles(raw, bank, a)
    elif k == 'scale':
        e['value'] = round(v * a, 6)
    elif k == 'sscale':
        e['value'] = round(int.from_bytes(raw, 'big', signed=True) * a, 4)
    elif k == 'rev':
        e['value'] = f'{v >> 4}.{v & 15}'
    elif k == 'ver':
        e['value'] = f'{raw[0]}.{raw[1]}' + (' (none)' if raw == b'\0\0' else ' (invalid)' if raw == b'\xff\xff' else '')
    elif k == 'cdbstatus':
        e['value'] = decode_cdb_status(v)
    elif k == 'app':
        e['value'] = _app(raw)
    elif k == 'length':
        e['value'] = None if v & 0x3F == 0 else round(LENGTH_MULT[v >> 6] * (v & 0x3F), 2)
    elif k == 'checksum':
        lo, hi = a
        calc = sum(buf[lo - 128:hi - 127]) & 0xFF
        e['value'] = {'stored': f'0x{v:02x}', 'computed': f'0x{calc:02x}', 'ok': v == calc}
    elif k == 'modsel':
        e['value'] = (v & 0x1F) * 2 ** (v >> 5) if v else None
    elif k == 'dur':
        e['value'], e['max_s'] = decode_duration(v)
    elif k == 'bias':
        m = adv.get('bias_scale')
        e['value'] = None if m is None else round(v * 2e-3 * m, 3)
    elif k == 'u16x8':
        e['value'] = [round(int.from_bytes(raw[2 * j:2 * j + 2], 'big') * a, 6) for j in range(8)]
        e['lanes'] = [8 * bank + j + 1 for j in range(8)]
    elif k == 'biasx8':
        m = adv.get('bias_scale')
        e['value'] = [None if m is None else round(int.from_bytes(raw[2 * j:2 * j + 2], 'big') * 2e-3 * m, 3) for j in range(8)]
    elif k == 'patterns':
        bits = raw[0] | (raw[1] << 8)
        e['value'] = [PATTERN_NAMES.get(j, f'id {j}') for j in range(16) if bits >> j & 1]
    elif k == 'dpconfig':
        e['value'] = {'apsel': v >> 4, 'data_path_id': (v >> 1) & 7, 'explicit_control': bool(v & 1)}
    elif k == 'diagdata':
        e['value'] = _diagdata(raw, ctx.get('selector', 0))
        e['selector'] = ctx.get('selector', 0)
    else:
        e['value'] = raw.hex()
    return e


def decode(dump, rev=None):
    """dump = {'lower': bytes|hex, 'pages': {'00h': ..., '10h.b1': ...}} -> the structured record (see the module docstring)."""
    lower = _bytes(dump['lower'])
    pages = {k: _bytes(v) for k, v in (dump.get('pages') or {}).items()}
    rev = lower[1] if rev is None else rev
    adv = advertisements(lower, pages, rev)
    out = {'schema': SCHEMA, 'revision': f'{rev >> 4}.{rev & 15}', 'revision_byte': f'0x{rev:02x}', 'memory_model': 'flat' if adv['flat'] else 'paged',
           'identity': decode_id(lower, pages['00h']) if '00h' in pages else None, 'advertised': adv, 'pages': {}, 'not_advertised': [],
           'not_read': dict(UNREAD_PAGES), 'vdm': None}
    for key, (title, rows, gate, rev_min) in PAGES.items():
        for pk in sorted(k for k in pages if k == key or k.startswith(key + '.b')) or [key]:
            bank = int(pk.split('.b')[1]) if '.b' in pk else 0
            blk = {'title': title + (f' (bank {bank}, lanes {8 * bank + 1}-{8 * bank + 8})' if bank else ''), 'present': pk in pages or key == 'lower',
                   'advertised': None if gate is None else bool(adv.get(gate)), 'fields': [], 'raw': None}
            buf = lower if key == 'lower' else pages.get(pk)
            if buf is not None:
                blk['raw'] = buf.hex()
            if rev < rev_min:
                blk['note'] = f'not in this revision (defined from CMIS {rev_min >> 4}.{rev_min & 15})'
            elif gate is not None and not adv.get(gate):
                blk['note'] = 'not advertised' + (' (01h:142.5 set, but the 13h/14h layout below CMIS 5.0 is not the one decoded here)'
                                                  if gate == 'diag' and adv.get('diag_bit') else '')
                out['not_advertised'].append(pk)
            elif buf is None:
                blk['note'] = 'advertised, not in this dump'
            else:
                ctx = {'page': key, 'selector': buf[0] if key == '14h' else 0}
                blk['fields'] = [e for e in (decode_field(r, buf, bank, adv, rev, ctx) for r in rows) if e]
            out['pages'][pk] = blk
    if adv.get('vdm'):
        vp = {int(k[:2], 16): v for k, v in pages.items() if len(k) == 3 and 0x20 <= int(k[:2], 16) <= 0x27}
        rows = decode_vdm_descriptors(vp) if vp else []
        desc = pages.get('20h')
        out['vdm'] = {'groups_read': sorted(f'{k:02X}h' for k in vp), 'instances': rows,
                      'descriptor_page_all_zero': (not any(desc)) if desc is not None else None}
    p11 = pages.get('11h')
    if p11 is not None:
        tx = [int.from_bytes(p11[154 - 128 + 2 * i:156 - 128 + 2 * i], 'big') for i in range(8)]
        out['hollow'] = {'tx_power_identical_on_every_lane': adv.get('mon.tx_power', False) and len(set(tx)) == 1 and tx[0] != 0,
                         'vdm_descriptor_page_all_zero': (out['vdm'] or {}).get('descriptor_page_all_zero')}
    return out


def read_pages(port, say=None, cdb=True):
    """Read the lower memory and every page the module advertises (bank-aware), then decode. Read-only: page selects and, with
    `cdb`, the read-only CDB queries 0000h and 0040h of cdb.py. Returns the decode record plus 'raw_dump' (re-decodable), 'unit', 'read_at'."""
    say = say or (lambda t, err=False: None)
    with port.selected():
        lower = port.lower()
        flat = bool(lower[2] & 0x80)
        pages = {'00h': port.read_page(0, flat=flat)}
        if not flat:
            for pg in (0x01, 0x02, 0x10, 0x11):
                pages[f'{pg:02X}h'] = port.read_page(pg, bank=0)
            adv = advertisements(lower, pages, lower[1])
            if adv['page03']:
                pages['03h'] = port.read_page(0x03)
            if adv['tunable']:
                pages['04h'], pages['12h'] = port.read_page(0x04), port.read_page(0x12)
            if adv['diag_bit']:
                pages['13h'], pages['14h'] = port.read_page(0x13), port.read_page(0x14)
            if adv['vdm']:
                pages['2Fh'] = port.read_page(0x2F)
                for g in range((pages['2Fh'][0] & 3) + 1):
                    pages[f'{P_VDM_DESC + g:02X}h'], pages[f'{P_VDM_SAMPLE + g:02X}h'] = port.read_page(P_VDM_DESC + g), port.read_page(P_VDM_SAMPLE + g)
            if adv['cdb']:
                pages['9Fh'] = port.read_page(0x9F)
            for bank in range(1, adv['banks']):  # Table 8-2: the banked pages repeat per 8 lanes
                for pg in BANK_PAGES:
                    if pg in (0x13, 0x14) and not adv['diag_bit']:
                        continue
                    pages[f'{pg:02X}h.b{bank}'] = port.read_page(pg, bank=bank)
    rec = decode({'lower': lower, 'pages': pages})
    rec.update({'unit': getattr(port.be, 'serial', '?'), 'read_at': time.time(), 'raw_dump': {'lower': lower.hex(), 'pages': {k: v.hex() for k, v in pages.items()}}})
    say(f"CMIS {rec['revision']}, {rec['memory_model']}: {len(pages)} page(s) read, {len(rec['not_advertised'])} block(s) not advertised")
    if cdb and rec['advertised'].get('cdb'):
        from ..cdb import cdb_command  # here: cdb imports the package
        rec['cdb_query'] = {}
        for cmd, name in ((0x0000, 'query_status'), (0x0040, 'features')):
            try:
                r = cdb_command(port, cmd, say=say)
                rec['cdb_query'][name] = r.get('decoded') if r.get('ok') else {'supported': r.get('supported'), 'status': r['status']}
            except Exception as e:  # noqa: BLE001 -- a CDB failure must not void the read
                rec['cdb_query'][name] = {'error': f'{type(e).__name__}: {e}'}
    return rec


def _fmt(v):
    if isinstance(v, list) and v and isinstance(v[0], dict) and 'lane' in v[0]:
        return ' '.join(f"{d['lane']}:{d['value']}" for d in v)
    if isinstance(v, dict):
        return ', '.join(f'{k} {x}' for k, x in v.items() if x is not None)
    if isinstance(v, list):
        return ', '.join(str(x) for x in v) if v else '-'
    return '-' if v is None else str(v)


def render(rec, width=46):
    """The readable table: one block per page, one line per field (the CLI's default view)."""
    L = [f"CMIS {rec['revision']} ({rec['memory_model']} memory)" + (f", unit {rec['unit']}" if rec.get('unit') else '')]
    ident = rec.get('identity') or {}
    if ident:
        L.append(f"{ident.get('vendor')} {ident.get('part_number')} SN {ident.get('serial')}, {ident.get('module_state')}, class {ident.get('power_class')} "
                 f"{ident.get('max_power_w')} W")
    for key, blk in rec['pages'].items():
        L.append('')
        L.append(f"== {blk['title']}" + (f": {blk['note']}" if blk.get('note') else ''))
        for e in blk['fields']:
            unit = f" {e['unit']}" if e.get('unit') and e.get('advertised', True) and not isinstance(e['value'], (list, dict)) else ''
            L.append(f"  {e['reg']:16} {e['name'][:width]:{width}} {_fmt(e['value'])}{unit}")
    if rec.get('vdm'):
        L.append('')
        L.append(f"== VDM: pages {', '.join(rec['vdm']['groups_read']) or 'none'}, {len(rec['vdm']['instances'])} instance(s) in use"
                 + (' (descriptor page all zero: hollow)' if rec['vdm'].get('descriptor_page_all_zero') else ''))
        for r in rec['vdm']['instances'][:64]:
            L.append(f"  VDM {r['instance']:3d} {r['page']}:{r['byte']} type {r['type']:3d} {r['name'][:width]:{width}} {r['resource']:8} sample {r.get('sample')} {r['unit']}")
    if rec.get('cdb_query'):
        L.append('')
        L.append('== CDB queries: ' + '; '.join(f'{k} {_fmt(v)}' for k, v in rec['cdb_query'].items()))
    if rec.get('not_read'):
        L.append('')
        L.append('== Not read (no verified gate bit): ' + '; '.join(f'{k} {v}' for k, v in rec['not_read'].items()))
    return '\n'.join(L)
