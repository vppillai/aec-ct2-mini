"""IPMI FRU Board Info Area (Platform Management FRU Information Storage Definition v1.0, section 11) in the FT2232H
93LC56B at bytes 0x1A..0x99. The chip never reads that span; the unit's EEPROM programming writes it and the
bring-up open path reads it. Fields in order: manufacturer, product, serial, part (the board id), file_id, then the
custom fields rev ('REV n'), order (the assembly order) and case (the preset, written at assembly)."""
from datetime import datetime, timedelta, timezone

FRU_OFF, FRU_MAX = 0x1A, 0x80
EPOCH = datetime(1996, 1, 1, tzinfo=timezone.utc)  # FRU v1.0 11.1: mfg date = minutes since 1996-01-01 00:00 UTC
FIELDS = ('manufacturer', 'product', 'serial', 'part', 'file_id', 'rev', 'order', 'case')


def build_board_area(fields, when=None):
    body = bytearray()
    for k in FIELDS:
        s = str(fields.get(k, '')).encode('ascii')
        assert len(s) < 0x3F, k
        body += bytes([0xC0 | len(s)]) + s  # type/length byte: 11b = 8-bit ASCII (sections 11.1 / 13)
    body += b'\xC1'  # end of fields
    mins = int(((when or datetime.now(timezone.utc)) - EPOCH).total_seconds() // 60)
    area = bytearray([1, 0, 0]) + mins.to_bytes(3, 'little') + body  # format version 1, length (below), language 0 = English
    area += bytes(-(len(area) + 1) % 8)  # zero pad so that the area with its checksum is a multiple of 8
    area[1] = (len(area) + 1) // 8
    area.append((-sum(area)) & 0xFF)  # zero checksum
    assert len(area) <= FRU_MAX, len(area)
    return bytes(area)


def parse_board_area(b):
    """The dict of FIELDS plus mfg_date, or None when the area is absent (zero) or its checksum fails."""
    b = bytes(b)
    if len(b) < 8 or b[0] != 1:
        return None
    n = b[1] * 8
    if n < 8 or n > len(b) or sum(b[:n]) & 0xFF:
        return None
    pos, vals = 6, []
    while pos < n and b[pos] != 0xC1:
        ln = b[pos] & 0x3F
        vals.append(b[pos + 1:pos + 1 + ln].decode('ascii', 'replace'))
        pos += 1 + ln
    d = dict(zip(FIELDS, vals + [''] * len(FIELDS)))
    d['mfg_date'] = (EPOCH + timedelta(minutes=int.from_bytes(b[3:6], 'little'))).date().isoformat()
    return d


def read_fru(ftdi):
    """ftdi = an open pyftdi Ftdi (one EEPROM read, no bus traffic) or the raw bytes of the whole EEPROM."""
    raw = bytes(ftdi) if isinstance(ftdi, (bytes, bytearray)) else ftdi.read_eeprom(FRU_OFF, FRU_MAX, eeprom_size=256)
    return parse_board_area(raw[FRU_OFF:FRU_OFF + FRU_MAX] if len(raw) > FRU_MAX else raw)


def rev_of(d):
    """'REV 0' -> '0' (the HW_SUPPORT key); '' when there is no FRU area."""
    r = (d or {}).get('rev', '')
    return r[4:] if r.startswith('REV ') else r


def summary(d):
    if not d:
        return 'none'
    return f"rev {rev_of(d)}, board {d['part']}, order {d['order']}" + (f", case {d['case']}" if d['case'] else '')
