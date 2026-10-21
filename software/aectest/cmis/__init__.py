"""CMIS register map and decode.

Lower page, Page 00h identity, 01h advertisement, 02h thresholds, 10h/11h Data Path, 13h/14h PRBS. Page select itself is
`transport.Port.page()`. The 01h durations, the 02h lane rows, VDM (20h-2Fh) and CDB (00h:37, 01h:163, 9Fh) are
read-only decoders; every one of them cites its table in the CMIS 5.2 specification.
"""
# [CMIS] Lower Memory: Table 8-5 (bytes 0-2), 8-6/8-7 (byte 3), 8-9 (byte 9), 8-10 (14-15), 8-11 (26), 8-22 (126-127)
L_ID, L_REV, L_MEM, L_STATE, L_FLAGS9, L_TEMP, L_VCC, L_CTRL, L_BANK, L_PAGE = 0, 1, 2, 3, 9, 14, 16, 26, 126, 127
CTRL_LOWPWR_ALLOW_HW, CTRL_LOWPWR_SW, CTRL_SW_RESET = 0x40, 0x10, 0x08  # Table 8-11 bits 6, 4, 3
TEMP_HIGH_FLAGS = 0x05  # Table 8-9 byte 9: bit 2 TempMonHighWarningFlag, bit 0 TempMonHighAlarmFlag (pre-flight refuses both)
TEMP_WARN_FLAG, TEMP_ALARM_FLAG = 0x04, 0x01
VCC_ALARM_FLAGS, VCC_WARN_FLAGS = 0x30, 0xC0  # Table 8-9 byte 9: bits 5/4 VccMonLow/HighAlarm, bits 7/6 Low/HighWarning
STATES = {1: 'ModuleLowPwr', 2: 'ModulePwrUp', 3: 'ModuleReady', 4: 'ModulePwrDn', 5: 'ModuleFault'}  # Table 8-7
ST_LOWPWR, ST_READY, ST_FAULT = 1, 3, 5
CMIS_MIN_REV = 0x50  # Page 13h/14h offsets below are OIF-CMIS-05.2 layouts; refuse PRBS writes below 5.0 (R6-W14)
# [CMIS] Page 00h: Table 8-25 (vendor block), 8-26 (date code), 8-28 (200/201)
P0_STR = {'vendor': (129, 145), 'part_number': (148, 164), 'revision': (164, 166), 'serial': (166, 182),
          'date_code': (182, 190)}
P0_PCLASS, P0_MAXP = 200, 201  # bits 7-5 = class-1 ; MaxPower in multiples of 0.25 W
P0_MEDIA_TECH = 212
MEDIA_TECH = {0x00: '850 nm VCSEL', 0x01: '1310 nm VCSEL', 0x02: '1550 nm VCSEL', 0x03: '1310 nm FP', 0x04: '1310 nm DFB',
              0x05: '1550 nm DFB', 0x06: '1310 nm EML', 0x07: '1550 nm EML', 0x08: 'Others', 0x09: '1490 nm DFB',
              0x0A: 'Copper cable unequalized', 0x0B: 'Copper cable passive equalized',
              0x0C: 'Copper cable, near and far end limiting active equalizers',
              0x0D: 'Copper cable, far end limiting active equalizers', 0x0E: 'Copper cable, near end limiting active equalizers',
              0x0F: 'Copper cable, linear active equalizers', 0x10: 'C-band tunable laser', 0x11: 'L-band tunable laser'}
P1_PAGE03_BIT = 0x04  # [CMIS] Table 8-41: 01h:142.2 Page03hSupported (User EEPROM, Table 8-57)
P1_MONITORS, MON_TEMP, MON_VCC = 159, 0x01, 0x02  # [CMIS] Table 8-47 Supported Monitors Advertisement (Page 01h): 159.0 TempMonSupported,
TEMP_THR_RANGE_C, TEMP_SAMPLE_RANGE_C = (40.0, 110.0), (-10.0, 120.0)
T_NACK_S, T_NACK_NV_S = 0.010, 0.080
P1_DURATIONS = 167  # [CMIS] Table 8-51: 167.3-0 MaxDurationModulePwrUp, 167.7-4 MaxDurationModulePwrDn, encoded per Table 8-43
DURATION_MAX_S = [0.001, 0.005, 0.010, 0.050, 0.1, 0.5, 1.0, 5.0, 10.0, 60.0, 300.0, 600.0, 3000.0]  # Table 8-43 upper bounds,
#                 codes 0000b-1100b; 1101b = Tstate >= 50 min (no bound), 1110b/1111b reserved
# [CMIS] Page 02h Table 8-55: module-level monitor thresholds, static RO; "Page 02h is mandatory for paged memory modules" (§8.5)
P2_THRESHOLDS = {'temp_high_alarm_c': (128, True), 'temp_low_alarm_c': (130, True), 'temp_high_warning_c': (132, True),
                 'temp_low_warning_c': (134, True), 'vcc_high_alarm_v': (136, False), 'vcc_low_alarm_v': (138, False),
                 'vcc_high_warning_v': (140, False), 'vcc_low_warning_v': (142, False)}  # (offset, signed): S16 /256 C, U16 x100 uV
P1_PAGES, P1_DIAG_BIT = 142, 0x20  # [CMIS] Table 8-41: 01h:142.5 DiagnosticPagesSupported (13h/14h)
P1_VDM_BIT = 0x40  # [CMIS] Table 8-41: 01h:142.6 VDMPagesSupported (details on Page 2Fh)
P1_MODSEL_WAIT = 143  # [CMIS] Table 8-42: 01h:143 ModSelWaitTime = m (bits 4-0) * 2^e (bits 7-5) us; 00h = no data
P1_DURATION_FIELDS = {  # name: (byte, low bit) -- [CMIS] Table 8-42 (01h:144) and Table 8-51 (01h:167-168), each a Table 8-43 code
    'dp_init': (144, 0), 'dp_deinit': (144, 4), 'module_pwrup': (167, 0), 'module_pwrdn': (167, 4),
    'dp_tx_turnon': (168, 0), 'dp_tx_turnoff': (168, 4)}
T_MGMT_INIT_MAX_S = 2.0  # [CMIS] Table 10-2 tMgmtInit max 2000 ms: a timing parameter, no register advertises it
L_FW_MAJOR, L_FW_MINOR = 39, 40  # [CMIS] Table 8-15: ModuleActiveFirmwareMajor/MinorRevision; 0.0 = no firmware, FF.FF = invalid
P1_FW = {'inactive_fw_major': 128, 'inactive_fw_minor': 129, 'hw_major': 130, 'hw_minor': 131}  # [CMIS] Table 8-38
P1_BIAS_SCALE, BIAS_SCALE = 160, {0: 1, 1: 2, 2: 4, 3: None}  # [CMIS] Table 8-47: 01h:160.4-3 TxBiasCurrentScalingFactor x1/x2/x4
# [CMIS] Table 8-56: lane-related thresholds (all lanes), U16; optical power 0.1 uW, bias 2 uA x the 01h:160.4-3 multiplier
P2_LANE_THRESHOLDS = {'tx_power_high_alarm_mw': 176, 'tx_power_low_alarm_mw': 178, 'tx_power_high_warning_mw': 180,
                      'tx_power_low_warning_mw': 182, 'bias_high_alarm_ma': 184, 'bias_low_alarm_ma': 186, 'bias_high_warning_ma': 188,
                      'bias_low_warning_ma': 190, 'rx_power_high_alarm_mw': 192, 'rx_power_low_alarm_mw': 194,
                      'rx_power_high_warning_mw': 196, 'rx_power_low_warning_mw': 198}
# [CMIS] Table 8-13 / 8-14: 00h:37 CdbStatus1, 00h:38 CdbStatus2; bit 7 CdbIsBusy, bit 6 CdbHasFailed, bits 5-0 CdbCommandResult
L_CDB_STATUS, CDB_BUSY, CDB_FAILED = (37, 38), 0x80, 0x40
CDB_RESULT = {'IN PROGRESS': {1: 'command captured, not processed', 2: 'command checking in progress', 3: 'command execution in progress'},
              'SUCCESS': {1: 'command completed successfully', 3: 'previous CMD aborted by CMD Abort'},
              'FAILED': {1: 'CMDID unknown', 2: 'parameter range error or not supported', 3: 'previous CMD not properly aborted',
                         4: 'command checking time out', 5: 'CdbChkCode error', 6: 'password related error',
                         7: 'command not compatible with operating status'}}
P1_CDB_ADV, P1_CDB_RW_EXT = 163, 164  # [CMIS] Table 8-49: 163.7-6 instances, .5 background, .4 auto paging, .3-0 EPL pages; 164 length ext.
CDB_EPL_PAGES = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 8, 6: 12, 7: 16}  # Table 8-49 CdbMaxPagesEPL -> number of A0h-AFh pages (128 B each)
P9F_RPL_LEN, P9F_RPL_CHK, P9F_LPL = 134, 135, 136  # [CMIS] Table 8-160 / 8-162: reply header; LPL 136-255; CdbChkCode is the host's (133)
# [CMIS] Table 8-150 / 8-151 / 8-154 / 8-159: descriptor pages 20h-23h, sample pages 24h-27h, advertisement 2Fh:128.1-0 (groups - 1)
P_VDM_DESC, P_VDM_SAMPLE, P_VDM_ADV, P2F_SUPPORT, P2F_FINE_INTERVAL = 0x20, 0x24, 0x2F, 128, 129
VDM_TYPES = {  # [CMIS] Table 8-153: type -> (name, data type, scale, unit); F16 types carry no unit
    1: ('Laser Age (Data Path)', 'U16', 1, '%'), 2: ('TEC Current (Module)', 'S16', 100 / 32767, '%'),
    3: ('Laser Frequency Error (Media Lane)', 'S16', 10, 'MHz'), 4: ('Laser Temperature (Media Lane)', 'S16', 1 / 256, 'C'),
    5: ('eSNR Media Input (Media Lane)', 'U16', 1 / 256, 'dB'), 6: ('eSNR Host Input (Lane)', 'U16', 1 / 256, 'dB'),
    7: ('PAM4 Level Transition Parameter Media Input (Media Lane)', 'U16', 1 / 256, 'dB'),
    8: ('PAM4 Level Transition Parameter Host Input (Lane)', 'U16', 1 / 256, 'dB'),
    9: ('Pre-FEC BER Minimum Sample Media Input (Data Path)', 'F16', 1, ''), 10: ('Pre-FEC BER Minimum Sample Host Input (Data Path)', 'F16', 1, ''),
    11: ('Pre-FEC BER Maximum Sample Media Input (Data Path)', 'F16', 1, ''), 12: ('Pre-FEC BER Maximum Sample Host Input (Data Path)', 'F16', 1, ''),
    13: ('Pre-FEC BER Sample Average Media Input (Data Path)', 'F16', 1, ''), 14: ('Pre-FEC BER Sample Average Host Input (Data Path)', 'F16', 1, ''),
    15: ('Pre-FEC BER Current Sample Media Input (Data Path)', 'F16', 1, ''), 16: ('Pre-FEC BER Current Sample Host Input (Data Path)', 'F16', 1, ''),
    17: ('FERC Minimum Sample Value Media Input (Data Path)', 'F16', 1, ''), 18: ('FERC Minimum Sample Value Host Input (Data Path)', 'F16', 1, ''),
    19: ('FERC Maximum Sample Value Media Input (Data Path)', 'F16', 1, ''), 20: ('FERC Maximum Sample Value Host Input (Data Path)', 'F16', 1, ''),
    21: ('FERC Sample Average Value Media Input (Data Path)', 'F16', 1, ''), 22: ('FERC Sample Average Value Host Input (Data Path)', 'F16', 1, ''),
    23: ('FERC Current Sample Value Media Input (Data Path)', 'F16', 1, ''), 24: ('FERC Current Sample Value Host Input (Data Path)', 'F16', 1, ''),
    25: ('FERC Total Accumulated Media Input (Data Path)', 'F16', 1, ''), 26: ('FERC Total Accumulated Host Input (Data Path)', 'F16', 1, '')}
VDM_AEC_TYPES = frozenset((5, 6, *range(9, 27)))  # ARCHITECTURE 2.3: eSNR, pre-FEC BER, FERC are the AEC observables of interest (INFERENCE)
P10_DPDEINIT = 128  # [CMIS] Table 8-61: DPDeinitLane1-8, 0b = initialize the Data Path (static request, not a trigger)
# [CMIS] Page 11h: Table 8-76 (128-131 DP states), 8-77 (codes), 8-80/8-81 (flags), 8-89 (235)
P11_DPSTATE, DP_ACTIVATED = 128, 4
DP_STATES = {1: 'DPDeactivated', 2: 'DPInit', 3: 'DPDeinit', 4: 'DPActivated', 5: 'DPTxTurnOn',
             6: 'DPTxTurnOff', 7: 'DPInitialized'}
P11_FLAGS = {  # byte: (name, host_side?) -- host-side = electrical host lanes, meaningless with no host SerDes
    135: ('FailureFlagTx (media lane)', False), 136: ('LOSFlagTx (host lane)', True),
    137: ('CDRLOLFlagTx (host lane)', True), 138: ('AdaptiveInputEqFailFlagTx (host lane)', True),
    147: ('LOSFlagRx (media lane)', False), 148: ('CDRLOLFlagRx (media lane)', False),
    235: ('DPInitPending', True)}
# [CMIS] Page 13h: Table 8-93 (layout), 8-97 (131), 8-98 (pattern IDs), 8-99 (134-135), 8-104/8-105 (152-159),
#        8-108/8-109 (168-175), 8-110 (177)
P13_LOC, P13_MGEN_PAT, P13_MGEN_EN, P13_MGEN_SEL, P13_MCHK_EN, P13_MCHK_SEL, P13_CTRL = 131, 134, 152, 156, 168, 172, 177
P13_MCHK_PAT = 138
P13_CAP_MEAS, P13_CAP_REP = 129, 130  # Table 8-95 (129.4 PeriodicUpdatesSupported), 8-96 (130.0 BER, 130.1 counters)
CAP_PERIODIC, REP_BER, REP_COUNTERS = 0x10, 0x01, 0x02
P13_MGEN_PREFEC, P13_MCHK_POSTFEC = 155, 171  # Tables 8-104 / 8-108: 155 = 1b encoded (pre-FEC-encoder) generation,
#                                                171 = 1b check the encoded pattern at the FEC-decoder output
LOC_MEDIA_GEN, LOC_MEDIA_CHK, CTRL_RESET_ERR = 0xC0, 0x30, 0x20  # 131.7-6 gen pre/post FEC, 131.5-4 chk, 177.5
LOC_GEN_PRE, LOC_GEN_POST, LOC_CHK_PRE, LOC_CHK_POST = 0x80, 0x40, 0x20, 0x10  # Table 8-97 bit names
PATTERNS = {'prbs31q': 0, 'prbs31': 1, 'prbs23q': 2, 'prbs23': 3, 'prbs15q': 4, 'prbs15': 5, 'prbs13q': 6,
            'prbs13': 7, 'prbs9q': 8, 'prbs9': 9, 'prbs7q': 10, 'prbs7': 11, 'ssprq': 12}  # Table 8-98
# [CMIS] Page 14h: Table 8-118 (128, "reverts to 0 if value not supported"), 8-119 (01h BER, 04h/05h media counters),
#        8-120 (139), 8-121 (data: BER F16 media lanes at 208-223 -- table prints "222-211" for lane 8: typo;
#        counters 04h/05h: 4 lanes x (U64 LE errors, U64 LE total bits) from 192)
P14_SEL, P14_CHK_LOL_MEDIA, P14_MEDIA_BER, P14_DATA = 128, 139, 208, 192
SEL_BER, SEL_MEDIA_CNT = 0x01, (0x04, 0x05)
SEL_SNR, REP_SNR_MEDIA = 0x06, 0x20
P13_MLOOP_IN, LOOP_MEDIA_IN = 181, 0x02
IDENTIFIERS = {0x18: 'QSFP-DD', 0x19: 'OSFP', 0x1E: 'QSFP+ (CMIS)', 0x21: 'OSFP-XD', 0x0D: 'QSFP+ (SFF-8636)',
               0x11: 'QSFP28 (SFF-8636)'}  # [8024] Table 4-1
MSA_CLASS_W = {1: 1.5, 2: 3.5, 3: 7.0, 4: 8.0, 5: 10.0, 6: 12.0, 7: 14.0, 8: None}  # [MSA] Table 12 (class 8: read byte 201)


# --------------------------------------------------------------------------- decode
def f16(b0, b1):
    """[CMIS] §3.4 F16: byte1 bits 7-3 = scaled exponent s, bits 2-0 = mantissa MSBs, byte2 = mantissa LSBs; m*10^(s-24)."""
    return (((b0 & 7) << 8) | b1) * 10.0 ** ((b0 >> 3) - 24)


def ascii_field(buf, lo, hi):
    return buf[lo:hi].decode('ascii', 'replace').strip()


def decode_id(lower, page0):
    mem_flat = bool(lower[L_MEM] & 0x80)  # Table 8-5 byte 2 bit 7: 1 = flat memory
    up = lambda off: page0[off - 128]  # noqa: E731
    pclass = (up(P0_PCLASS) >> 5) + 1
    d = {'identifier': f'0x{lower[L_ID]:02x}', 'identifier_name': IDENTIFIERS.get(lower[L_ID], 'unknown (see SFF-8024 Table 4-1)'),
         'cmis_revision': f'{lower[L_REV] >> 4}.{lower[L_REV] & 0xF}', 'memory_model': 'flat' if mem_flat else 'paged',
         'module_state_raw': lower[L_STATE], 'module_state': STATES.get((lower[L_STATE] >> 1) & 7, 'reserved'),
         'temp_c': int.from_bytes(lower[L_TEMP:L_TEMP + 2], 'big', signed=True) / 256.0,
         'module_flags_byte9': lower[L_FLAGS9], 'module_ctrl_byte26': lower[L_CTRL],
         'power_class': pclass, 'power_class_msa_max_w': MSA_CLASS_W[pclass],
         'max_power_w': up(P0_MAXP) * 0.25,
         'media_interface_technology_00h_212': f'0x{up(P0_MEDIA_TECH):02x} {MEDIA_TECH.get(up(P0_MEDIA_TECH), "reserved (Table 8-36)")}',
         'firmware': f'{lower[L_FW_MAJOR]}.{lower[L_FW_MINOR]}'}
    d.update({k: ascii_field(page0, lo - 128, hi - 128) for k, (lo, hi) in P0_STR.items()})
    return d


def read_id(port):
    with port.selected():
        lower = port.lower()
        d = decode_id(lower, port.read_page(0, flat=bool(lower[L_MEM] & 0x80)))
    from ..transport import ramp_after_identity  # here: transport imports this package
    ramp_after_identity(port)
    return d


def module_flag_names(flags4):
    """00h:8-11 (Table 8-9, latched, clear on read) -> the set bit names, e.g. ['ModuleStateChangedFlag'] after an insert."""
    from .fields import LOWER  # here: fields imports nothing from here
    names = []
    for row in LOWER:
        if 8 <= row.byte <= 11 and row.kind == 'bits' and isinstance(row.arg, dict):
            v = flags4[row.byte - 8]
            names += [n for bit, n in row.arg.items() if v >> bit & 1]
    return names


def decode_flags(page11):
    """Mark host-side flags 'expected-without-host' (MINI_BRIEF §3c); media-side and module flags stay pass/fail."""
    up = lambda off: page11[off - 128]  # noqa: E731
    flags = {}
    for lane in range(8):
        code = (up(128 + lane // 2) >> (4 * (lane % 2))) & 0xF
        flags[f'11h:{128 + lane // 2} DPStateHostLane{lane + 1}'] = {
            'value': DP_STATES.get(code, 'reserved'), 'status': 'expected-without-host'}
    for off, (name, host) in P11_FLAGS.items():
        v = up(off)
        flags[f'11h:{off} {name}'] = {'value': f'0x{v:02x}',
                                     'status': ('expected-without-host' if host else 'ok' if not v else 'CHECK')}
    return flags


def decode_duration(code):
    """[CMIS] Table 8-43 4-bit state-duration code -> (text, upper bound in s or None when unbounded/reserved)."""
    if code == 13:
        return f'{code:04b}b: Tstate >= 50 min', None
    if code > 13:
        return f'{code:04b}b: reserved', None
    hi = DURATION_MAX_S[code]
    return (f'{code:04b}b: {DURATION_MAX_S[code - 1]:g} s <= Tstate < {hi:g} s' if code else f'0000b: Tstate < {hi:g} s'), hi


def read_release_advert(port, flat):
    """Page 02h thresholds, 01h:167 durations and 01h:159 monitors advertisement, read ONCE before LPMode
    falls; (None, None, None) for flat memory (no upper pages)."""
    if flat:
        return None, None, None
    with port.selected():
        thr = decode_thresholds(port.read_page(0x02))
        p1 = port.read_page(0x01)
    return thr, p1[P1_DURATIONS - 128], p1[P1_MONITORS - 128]


def decode_durations(page01):
    """[CMIS] Table 8-42 / 8-51 (codes per Table 8-43) + 01h:143 ModSelWaitTime: every advertised maximum state duration."""
    up = lambda off: page01[off - 128]  # noqa: E731
    out = {}
    for k, (off, lo) in P1_DURATION_FIELDS.items():
        text, hi = decode_duration((up(off) >> lo) & 0xF)
        out[k] = {'reg': f'01h:{off}.{lo + 3}-{lo}', 'code': text, 'max_s': hi}
    b = up(P1_MODSEL_WAIT)
    out['modsel_wait_us'] = (b & 0x1F) * 2 ** (b >> 5) if b else None  # Table 8-42 example: 0xD9 = 25 * 2^6 = 1600 us
    return out


def decode_firmware(lower, page01):
    """[CMIS] Table 8-15 (00h:39-40 active) and Table 8-38 (01h:128-131 inactive firmware, hardware revision)."""
    maj, mnr = lower[L_FW_MAJOR], lower[L_FW_MINOR]
    d = {'active_fw': f'{maj}.{mnr}', 'active_fw_state': ('no firmware' if (maj, mnr) == (0, 0) else 'invalid' if (maj, mnr) == (255, 255)
                                                           else 'active')}
    d.update({k: page01[off - 128] for k, off in P1_FW.items()})
    return d


def decode_thresholds(page02, lanes=False, bias_scale_bits=0):
    """[CMIS] Table 8-55: Page 02h module-level monitor thresholds (static, RO) -- S16 1/256 C, U16 100 uV.
    lanes=True adds Table 8-56 (optical power 0.1 uW -> mW, bias 2 uA x 01h:160.4-3 -> mA); bias_scale_bits = that field."""
    out = {}
    for k, (off, signed) in P2_THRESHOLDS.items():
        raw = int.from_bytes(page02[off - 128:off - 126], 'big', signed=signed)
        out[k] = round(raw / 256.0, 2) if signed else round(raw * 100e-6, 4)
    if lanes:
        mult = BIAS_SCALE[bias_scale_bits & 3]
        for k, off in P2_LANE_THRESHOLDS.items():
            raw = int.from_bytes(page02[off - 128:off - 126], 'big')
            out[k] = round(raw * 1e-4, 4) if 'power' in k else (None if mult is None else round(raw * 2e-3 * mult, 3))
        out['bias_scale_01h_160'] = f'x{mult}' if mult else 'reserved (11b)'
    return out


def decode_vdm_descriptors(pages):
    """[CMIS] Table 8-151 / 8-152 / 8-153: pages = {0x20: bytes, 0x21: ..., 0x24: samples, ...}. One row per instance whose type
    is not 0 (Not Used). The sample (Table 8-154, same byte on page 24h + group) is decoded when that page is given. The list
    is a record, never a judgement: the Credo set is unknown."""
    out = []
    for group in range(4):
        desc = pages.get(P_VDM_DESC + group)
        if desc is None:
            continue
        samples = pages.get(P_VDM_SAMPLE + group)
        for i in range(64):
            even, typ = desc[2 * i], desc[2 * i + 1]
            if typ == 0:
                continue
            res = even & 0xF
            name, dtype, scale, unit = VDM_TYPES.get(typ, ('custom' if 100 <= typ <= 127 else 'restricted OIF' if typ >= 128 else 'reserved', '?', 1, ''))
            row = {'instance': 64 * group + i + 1, 'page': f'{P_VDM_DESC + group:02X}h', 'byte': 128 + 2 * i, 'type': typ, 'name': name,
                   'data_type': dtype, 'unit': unit, 'threshold_set': 16 * group + 1 + (even >> 4),
                   'resource': 'module' if res == 15 else f'lane {res + 1}' if res < 8 else f'reserved {res}',
                   'aec_observable': typ in VDM_AEC_TYPES}
            if samples is not None:
                row['sample'] = decode_vdm_sample(typ, samples[2 * i], samples[2 * i + 1])
            out.append(row)
    return out


def decode_vdm_sample(typ, b0, b1):
    """[CMIS] Table 8-154 X16 sample, big endian; F16 per §3.4, else U16/S16 times the Table 8-153 scale. None for an unknown type."""
    if typ not in VDM_TYPES:
        return None
    _, dtype, scale, _ = VDM_TYPES[typ]
    if dtype == 'F16':
        return f16(b0, b1)
    return round(int.from_bytes(bytes([b0, b1]), 'big', signed=dtype == 'S16') * scale, 4)


def decode_vdm_advert(page01, page2f):
    """[CMIS] Table 8-41 01h:142.6 + Table 8-159 2Fh:128.1-0 (groups) and 129-130 FineIntervalLength (0.1 ms)."""
    adv = bool(page01[P1_PAGES - 128] & P1_VDM_BIT)
    d = {'advertised_01h_142_6': adv, 'groups': None, 'fine_interval_ms': None}
    if adv and page2f is not None:
        d['groups'] = (page2f[P2F_SUPPORT - 128] & 3) + 1
        d['fine_interval_ms'] = int.from_bytes(page2f[P2F_FINE_INTERVAL - 128:P2F_FINE_INTERVAL - 126], 'big') * 0.1
    return d


def decode_cdb_status(b):
    """[CMIS] Table 8-14: one CdbStatus byte -> coarse status and the CdbCommandResult text."""
    coarse = 'IN PROGRESS' if b & CDB_BUSY else 'FAILED' if b & CDB_FAILED else 'SUCCESS'
    code = b & 0x3F
    text = CDB_RESULT[coarse].get(code, 'custom' if 0x30 <= code <= 0x3F else 'STS task error' if coarse == 'FAILED' and 0x20 <= code <= 0x2F
                                  else 'reserved')
    return {'raw': f'0x{b:02x}', 'coarse': coarse, 'result_code': code, 'result': text}


def decode_cdb_advert(page01):
    """[CMIS] Table 8-49: 01h:163 CDB advertisement and 01h:164 CdbReadWriteLengthExtension."""
    b, i = page01[P1_CDB_ADV - 128], page01[P1_CDB_RW_EXT - 128]
    inst = b >> 6
    pages = CDB_EPL_PAGES.get(b & 0xF)
    return {'instances': inst if inst < 3 else 'reserved (11b)', 'background_mode': bool(b & 0x20), 'auto_paging': bool(b & 0x10),
            'epl_pages': pages if pages is not None else f'reserved ({b & 0xF})', 'epl_bytes': None if pages is None else 128 * pages,
            'rw_length_extension': i, 'max_access_bytes_lpl': 8 * (1 + min(i, 15)), 'max_access_bytes_epl': 8 * (1 + i)}


def decode_cdb_reply_header(page9f):
    """[CMIS] Table 8-162: 9Fh:134 RPLLength, 9Fh:135 RPLChkCode; the LPL check code is recomputed when the body is in the LPL.
    Read-only: no command is sent, so the header is whatever the last command (if any) left."""
    n, chk = page9f[P9F_RPL_LEN - 128], page9f[P9F_RPL_CHK - 128]
    d = {'rpl_length': n, 'rpl_chk_code': f'0x{chk:02x}', 'body': 'none' if n == 0 else 'LPL' if n <= 120 else 'EPL' if n >= 240 else 'reserved',
         'lpl_check_ok': None}
    if 0 < n <= 120:
        d['lpl_check_ok'] = chk == (~sum(page9f[P9F_LPL - 128:P9F_LPL - 128 + n])) & 0xFF
    elif n == 0:
        d['lpl_check_ok'] = chk == 0
    return d


# `fields.py` holds the page/byte maps per revision; `decode.py` runs them: decode(dump) on saved bytes, read_pages(port) live.
# `from .cmis import X` keeps working).
from .decode import decode, read_pages, render  # noqa: E402, F401
