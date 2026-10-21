"""CDB (Command Data Block) over the local payload, read-only queries ([CMIS] 8.20 Page 9Fh, chapter 9).

The host writes the header and payload on Page 9Fh, bytes 130 to 135 + LPL first, then the CMDID at 128-129 last:
a write that includes byte 129 triggers the module (Table 8-161 and the 'Triggering CDB Command Execution' text).
CdbChkCode (133) is the one's complement of the sum of bytes 128..(136 + LPLLength - 1) without bytes 133-135.
The module answers in CdbStatus 00h:37 (busy bit 7, failed bit 6, Table 8-14) and fills the reply header 134-135
and the reply LPL from 136. Only the queries below are sent by name; any other CMDID needs `raw`.
"""
import contextlib
import time

from .cmis import CDB_BUSY, L_CDB_STATUS, P1_CDB_ADV, decode_cdb_status
from .records import record_header, write_record
from .transport import Refused

P9F = 0x9F
READ_ONLY = {0x0000: 'query', 0x0040: 'features', 0x0041: 'fw-features', 0x0042: 'pm-features',
             0x0043: 'bert-features', 0x0100: 'fw-info'}  # Tables 9-3, 9-8, 9-9, 9-10, 9-11, 9-15
NAMES = {v: k for k, v in READ_ONLY.items()}
# capability commands l.13719-13726; firmware l.14042-14120; PM l.14685-14696; BERT / diagnostics l.15327-15352).
CMD_NAMES = {0x0000: 'Query Status', 0x0001: 'Enter Password', 0x0002: 'Change Password', 0x0004: 'Abort Processing',
             0x0040: 'Module Features', 0x0041: 'Firmware Management Features', 0x0042: 'Performance Monitoring Features',
             0x0043: 'BERT and Diagnostic Features',
             0x0100: 'Get Firmware Info', 0x0101: 'Start Firmware Download', 0x0102: 'Abort Firmware Download',
             0x0103: 'Write Firmware Block LPL', 0x0104: 'Write Firmware Block EPL', 0x0105: 'Read Firmware Block LPL',
             0x0106: 'Read Firmware Block EPL', 0x0107: 'Complete Firmware Download', 0x0108: 'Copy Firmware Image',
             0x0109: 'Run Firmware Image', 0x010A: 'Commit Firmware Image',
             0x0200: 'Control PM', 0x0201: 'Get PM Feature Information', 0x0210: 'Get PM Module LPL', 0x0211: 'Get PM Module EPL',
             0x0212: 'Get PM Host Side LPL', 0x0213: 'Get PM Host Side EPL', 0x0214: 'Get PM Media Side LPL',
             0x0215: 'Get PM Media Side EPL', 0x0216: 'Get PM Data Path LPL', 0x0217: 'Get PM Data Path EPL',
             0x0380: 'Loopbacks'}
FAMILIES = ((0x0000, 0x003F, 'Module Commands'), (0x0040, 0x005F, 'Capability Advertisement'), (0x0060, 0x006F, 'Bulk Read (reserved)'),
            (0x0070, 0x007F, 'Bulk Write (reserved)'), (0x0100, 0x011F, 'Firmware Management'), (0x0200, 0x027F, 'Performance Monitoring'),
            (0x0280, 0x02FF, 'Data Recording'), (0x0300, 0x037F, 'BERT (reserved)'), (0x0380, 0x03FF, 'Diagnostics and Debug'),
            (0x4000, 0x7FFF, 'OIF restricted'), (0x8000, 0xFFFF, 'Vendor custom'))


def family(cid):
    return next((n for lo, hi, n in FAMILIES if lo <= cid <= hi), 'Reserved')


def cmd_name(cid):
    """'0101h Start Firmware Download'; an id the spec does not name reads 'reserved' or 'vendor custom' with its family."""
    fam = family(cid)
    return f'{cid:04X}h ' + (CMD_NAMES.get(cid) or ('vendor custom' if fam == 'Vendor custom' else f'reserved ({fam})'))


def by_family(ids):
    """{family: [name, ...]} for a list of '0xNNNN' strings (the bitmap output)."""
    out = {}
    for h in ids:
        cid = int(h, 16)
        out.setdefault(family(cid), []).append(cmd_name(cid))
    return out
LPL_MAX = 120
EPL_MAX = 2048  # Table 8-49: up to 16 EPL pages A0h-AFh


def chk_code(cmd_id, lpl=b'', epl_len=0):
    """Table 8-161: one's complement of the sum of CMDID, EPLLength, LPLLength and the LPL bytes."""
    s = sum(cmd_id.to_bytes(2, 'big')) + sum(epl_len.to_bytes(2, 'big')) + len(lpl) + sum(lpl)
    return (~s) & 0xFF


def cdb_command(port, cmd_id, lpl=b'', timeout=5.0, say=None, raw=False, epl=b'', chunk=8, selected=False):
    """One CDB command on the local payload; returns the decoded status and the reply LPL. The port must be open; the
    module is selected inside (or already, with `selected`). `raw` lets a CMDID outside READ_ONLY through (the CLI gates
    it behind --raw). `epl` (firmware blocks) is written to Pages A0h.. before the header; `chunk` is the write
    length the module advertises for the command family (8 = the base limit of 01h:164 / 9Fh:140)."""
    say = say or (lambda text, err=False: None)
    if cmd_id not in READ_ONLY and not raw:
        raise Refused(f'CDB CMD 0x{cmd_id:04x} is not a read-only query; use `cdb raw` for it')
    if len(lpl) > LPL_MAX:
        raise Refused(f'LPL {len(lpl)} bytes > {LPL_MAX}: EPL commands are not supported here')
    if len(epl) > EPL_MAX:
        raise Refused(f'EPL {len(epl)} bytes > {EPL_MAX} (Pages A0h-AFh)')
    ctx = contextlib.nullcontext() if selected else port.selected()
    with ctx:
        adv = port.read_page(0x01)[P1_CDB_ADV - 128]
        if adv >> 6 == 0:
            raise Refused('no CDB instance advertised (01h:163.7-6 = 0)')
        for k in range(0, len(epl), 128):  # Table 9-19: the EPL block on Pages A0h.., 128 bytes a page, before the trigger
            port.page(0xA0 + k // 128)
            blk = epl[k:k + 128]
            for i in range(0, len(blk), chunk):
                port.write(128 + i, blk[i:i + chunk])
        port.page(P9F)
        body = len(epl).to_bytes(2, 'big') + bytes([len(lpl), chk_code(cmd_id, lpl, len(epl)), 0x00, 0x00]) + bytes(lpl)  # 134-135 zero: the Accelink RTXM600-578 folds them into its check and rejects 0xFF 0xFF (2026-10-20)
        for i in range(0, len(body), chunk):  # chunked writes: inside the advertised length limit (01h:164; 9Fh:140 for firmware commands)
            port.write(130 + i, body[i:i + chunk])
        port.write(128, cmd_id.to_bytes(2, 'big'))  # the trigger
        t0 = time.monotonic()
        while True:
            st = port.read(L_CDB_STATUS[0], 1)[0]
            if not st & CDB_BUSY or time.monotonic() - t0 > timeout:
                break
            time.sleep(0.01)
        port.page(P9F)
        rlen, rchk = port.read(134, 2)
        rlpl = port.read(136, rlen) if 0 < rlen <= LPL_MAX else b''
    status = decode_cdb_status(st)
    res = {'cmd': f'0x{cmd_id:04x}', 'name': READ_ONLY.get(cmd_id, 'raw'), 'lpl': lpl.hex(), 'status': status,
           'elapsed_s': round(time.monotonic() - t0, 3), 'rlpl_len': rlen, 'rlpl_chk': f'0x{rchk:02x}', 'rlpl': rlpl.hex(),
           'rlpl_chk_ok': (rchk == (~sum(rlpl)) & 0xFF) if rlen else (rchk == 0)}
    res['ok'] = status['coarse'] == 'SUCCESS' and res['rlpl_chk_ok'] and not (st & CDB_BUSY)
    res['supported'] = not (status['coarse'] == 'FAILED' and status['result_code'] == 1)  # 0x41 CMDID unknown = a clean 'not supported'
    if st & CDB_BUSY:
        res['status']['result'] = f'still busy after {timeout} s'
    if res['ok'] and cmd_id in DECODERS:
        res['decoded'] = DECODERS[cmd_id](rlpl)
    say(f"CDB 0x{cmd_id:04x} {res['name']}: {status['coarse']} ({status['result']}), reply {rlen} bytes"
        + ('' if res['rlpl_chk_ok'] else ', reply check code WRONG'), err=not res['ok'])
    return res


def _bitmap(buf, base):
    """A set bit j in byte k means CMD base + 8 k + j is supported (Tables 9-8, 9-10, 9-11)."""
    return [f'0x{base + 8 * k + j:04x}' for k, b in enumerate(buf) for j in range(8) if b >> j & 1]


def _u16(buf, i):
    return int.from_bytes(buf[i:i + 2], 'big') if len(buf) >= i + 2 else None


def _text(buf, lo, hi):
    return bytes(buf[lo:hi]).split(b'\x00')[0].decode('ascii', 'replace').strip() if len(buf) >= hi else ''


def decode_query(r):
    """Table 9-3: Length, Status (0 = module boot up, 1 = host password accepted, bit 7 = module password accepted)."""
    s = r[1] if len(r) > 1 else None
    text = None if s is None else 'module password accepted' if s & 0x80 else 'host password accepted' if s & 1 else 'module boot up'
    return {'length': r[0] if r else None, 'status': s, 'status_text': text}


def decode_features(r):
    """Table 9-8: bytes 138-169 = support bitmap for CMDs 0000h-00FFh, 170-171 MaxCompletionTime ms. The bitmap stops at
    00FFh (OIF-CMIS-05.2.txt l.13754-13775): the 0100h firmware family is advertised by 0041h (decode_fw_features), the
    0200h PM family by 0042h, the 0380h diagnostics by 0043h."""
    ids = _bitmap(r[2:34], 0x0000)
    return {'supported_cmds': ids, 'supported_cmd_names': [cmd_name(int(h, 16)) for h in ids], 'families': by_family(ids),
            'max_completion_ms': _u16(r, 34), 'note': 'Table 9-8 lists 0000h-00FFh; 0100h+ families come from 0041h / 0042h / 0043h'}


def decode_fw_features(r):
    """Table 9-9."""
    f = r[1] if len(r) > 1 else 0
    m = 10 if f & 0x08 else 1
    mech = {0x00: 'none', 0x01: 'LPL', 0x10: 'EPL', 0x11: 'LPL and EPL'}
    wm, rm = (r[5] if len(r) > 5 else 0), (r[6] if len(r) > 6 else 0)
    ids = [0x0100] + ([0x0101, 0x0107, 0x0109, 0x010A] if wm else []) + ([0x0103] if wm & 0x01 else []) + ([0x0104] if wm & 0x10 else [])
    ids += ([0x0102] if f & 0x01 else []) + ([0x0108] if f & 0x02 else []) + ([0x0105] if rm & 0x01 else []) + ([0x0106] if rm & 0x10 else [])
    return {'supported_cmd_names': [cmd_name(c) for c in sorted(ids)],
            'image_readback': bool(f & 0x80), 'skipping_erased_blocks': bool(f & 0x04), 'copy_cmd': bool(f & 0x02),
            'abort_cmd': bool(f & 0x01), 'start_cmd_payload_size': r[2] if len(r) > 2 else None,
            'erased_byte': f'0x{r[3]:02x}' if len(r) > 3 else None, 'rw_length_ext': r[4] if len(r) > 4 else None,
            'write_mechanism': mech.get(r[5], f'0x{r[5]:02x}') if len(r) > 5 else None,
            'read_mechanism': mech.get(r[6], f'0x{r[6]:02x}') if len(r) > 6 else None,
            'hitless_restart': bool(r[7]) if len(r) > 7 else None,
            'max_duration_ms': {k: None if _u16(r, i) is None else _u16(r, i) * m
                                for k, i in (('start', 8), ('abort', 10), ('write', 12), ('complete', 14), ('copy', 16))}}


def decode_pm_features(r):
    ids = _bitmap(r[0:32], 0x0200)
    return {'supported_cmds': ids, 'supported_cmd_names': [cmd_name(int(h, 16)) for h in ids]}


def decode_bert_features(r):
    ids = _bitmap(r[0:32], 0x0300)
    return {'supported_cmds': ids, 'supported_cmd_names': [cmd_name(int(h, 16)) for h in ids]}


def _bank(nib):
    return {'running': bool(nib & 1), 'committed': bool(nib & 2), 'valid': not nib & 4}


def _image(r, i):
    """Version triple (Table 9-15) read two ways: plain binary and BCD (the Accelink RTXM600-578 reports 60 44 00 00)."""
    if len(r) < i + 4:
        return None
    bcd = lambda b: f'{b >> 4}{b & 15}'  # noqa: E731
    return {'raw': bytes(r[i:i + 4]).hex(), 'major': r[i], 'minor': r[i + 1], 'build': _u16(r, i + 2),
            'bcd': f'{bcd(r[i])}.{bcd(r[i + 1])}.{bcd(r[i + 2])}{bcd(r[i + 3])}', 'extra': _text(r, i + 4, i + 36)}


def decode_fw_info(r):
    """Table 9-15 and Tables 7-1 to 7-4: FirmwareStatus nibbles A / B, ImageInformation bits, the three version triples."""
    if not r:
        return {}
    st, info = r[0], r[1] if len(r) > 1 else 0
    d = {'firmware_status': f'0x{st:02x}', 'bank_a': _bank(st & 0xF), 'bank_b': _bank(st >> 4),
         'running': 'A' if st & 1 else 'B' if st & 0x10 else 'factory or boot (none of A, B)',
         'committed': 'A' if st & 2 else 'B' if st & 0x20 else 'none', 'image_information': f'0x{info:02x}'}
    if info & 1:
        d['image_a'] = _image(r, 2)
    if info & 2:
        d['image_b'] = _image(r, 38)
    if info & 4:
        d['factory_boot'] = _image(r, 74)
    return d


DECODERS = {0x0000: decode_query, 0x0040: decode_features, 0x0041: decode_fw_features, 0x0042: decode_pm_features,
            0x0043: decode_bert_features, 0x0100: decode_fw_info}


def cmd_cdb(port, args, as_json, out, raw, serial, say):
    """CLI: `cdb <query|features|fw-features|pm-features|bert-features|fw-info|raw 0xNNNN [hexbytes]>`."""
    import json
    stdout = say  # the JSON alone goes here
    if as_json:  # `--json | jq` stays clean: every said line goes to stderr, the JSON alone to stdout
        say = (lambda inner: lambda t, err=False: inner(t, err=True))(say)
    if not args:
        raise SystemExit('cdb needs one of ' + ', '.join(READ_ONLY.values()) + ', raw 0xNNNN [hexbytes]')
    if args[0] == 'raw':
        if not raw:
            raise Refused('cdb raw needs --raw: the command set beyond the read-only queries can change the module')
        if len(args) < 2:
            raise SystemExit('cdb raw needs the CMDID, e.g. raw 0x0000 0000')
        cmd_id, lpl, is_raw = int(args[1], 0), bytes.fromhex(args[2]) if len(args) > 2 else b'', True
    elif args[0] in NAMES:
        cmd_id, lpl, is_raw = NAMES[args[0]], b'\x00\x00' if args[0] == 'query' else b'', False
    else:
        raise SystemExit(f'unknown cdb query {args[0]!r}; one of ' + ', '.join(READ_ONLY.values()) + ', raw')
    res = cdb_command(port, cmd_id, lpl, say=say, raw=is_raw)
    if as_json:
        stdout(json.dumps(res, indent=1))
    elif not res['supported']:
        say(f"  CMD {res['cmd']} not supported by this module (CdbStatus {res['status']['raw']})")
    else:
        for k, v in (res.get('decoded') or {}).items():
            say(f'  {k:<24} {v}')
    if out:
        write_record({**record_header([serial]), 'cdb': res}, out, f'cdb_{serial}_{{}}.json', say)
    return res
