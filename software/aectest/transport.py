"""Hardware transport: FtdiBackend, FakeBackend, Port and the pin / timing / guard constants.

Channel A (pyftdi interface 1) runs the MPSSE I2C engine on AD0 (SCL) / AD1+AD2 (SDA, tied on the board) and drives the
remaining ADBUS/ACBUS pins as GPIO. Dependencies: stdlib + pyftdi (imported only when a real backend opens).
"""
import os
import tempfile
import time
from contextlib import contextmanager

from .safety import GUARD, MAN_HINT, S10_CHECK  # noqa: F401 -- the S1-S11 texts live in safety.py; re-exported here

from .cmis import (CTRL_LOWPWR_ALLOW_HW, CTRL_LOWPWR_SW, CTRL_SW_RESET, DP_ACTIVATED, L_BANK, L_CTRL, L_ID, L_MEM, L_PAGE, L_REV,
                   L_STATE, L_TEMP, L_VCC, LOC_MEDIA_CHK, LOC_MEDIA_GEN, MON_TEMP, MON_VCC, P0_MAXP, P0_MEDIA_TECH, P0_PCLASS,
                   P1_DIAG_BIT, P1_DURATIONS, P1_MONITORS, P1_PAGE03_BIT, P1_PAGES, P2_THRESHOLDS, P10_DPDEINIT, P13_CAP_REP,
                   P13_LOC, P13_MCHK_PAT, P13_MGEN_PAT, P14_MEDIA_BER, REP_BER, REP_COUNTERS, REP_SNR_MEDIA, SEL_BER, SEL_MEDIA_CNT, SEL_SNR,
                   LOOP_MEDIA_IN, ST_READY,
                   T_NACK_NV_S, T_NACK_S)

#   every open() runs set_bitmode(0, RESET) -> purge -> set_bitmode(dir, MPSSE); RESET = UART mode, so for a few ms
#   AD0/TXD, AD2/RTS#, AD4/DTR#, AC0/TXDEN, AC3/RXLED# are UART OUTPUTS on SCL / SDA / ResetL / P/VS1 / P/VS3.
#   close(freeze=True) keeps MPSSE but STILL calls attach_kernel_driver(), so ftdi_sio re-binds on Linux after
#   every command. Consequence: MPSSE persists between commands, but every command re-opens through that window.
#   they reach the pads in MAN and AUTO alike through the straps JP401/JP402 -- only a CUT strap isolates the SDA glitch. P/VS3
#   (AC3/RXLED#) reaches the pad through R437 1 k with no switch at all and always sees the window (contention bounded by
#   `shell` opens once for a whole session -- open it in MAN, then go to AUTO; the
#   Exit (review R6-D01): pyftdi's I2cController always emits direction I2C_DIR|gpio_dir, so SCL (AD0) idles as a
#   push-pull HIGH and SDA_O (AD1) as an output even after every GPIO is an input. tristate()/close(dir == 0) therefore
#   send one raw MPSSE SET_BITS_LOW 0x80 0x00 0x00 (value 0, direction 0 on AD0-AD7) so the module rail can be switched
#   off without U201 sourcing into the unpowered module's SCL/SDA pads through JP401/JP402 (DESIGN §3.4 fault 8).
UART_WINDOW_NOTE = ('open: FT2232H passed through a UART-mode window (ResetL/SDA/P/VS3 driven for a few ms) -- '
                    'MODE=MAN shields ResetL (with ModSelL/LPMode) only; SDA reaches the pads unless the SCL/SDA straps JP401/JP402 are cut, '
                    'P/VS3 always (P/VS1: R426 DNP, no pad); open `shell` in MAN, then go to AUTO (one open per session)')
PVS_DNP_NOTE = 'P/VS1 / P/VS4: R426/R427 DNP -- the FT2232H pin AC0/AC1 toggles, nothing reaches the pad'
MPSSE_TRISTATE_LOW = bytes([0x80, 0x00, 0x00])  # SET_BITS_LOW, value, direction (pyftdi ftdi.py l.203 / l.754): AD0-AD7 inputs
MPSSE_TRISTATE_HIGH = bytes([0x82, 0x00, 0x00])
#                                                  write with direction 0 already covers the high byte; pyftdi forces only AD0/AD1)
PYFTDI_PINNED = '0.57.2'

I2C_ADDR = 0x50  # [MSA] §4.5 "TWI serial interface address ... 1010000X (A0h)"

# pyftdi GPIO bitfield on an FT2232H (16-bit port): bit n = ADBUSn (n<8), bit 8+n = ACBUSn.
# Bits 0-2 (AD0 SCL, AD1 SDA_O, AD2 SDA_I) belong to the I2C engine and are not addressable as GPIO.
MODSELL, RESETL, LPMODE, MODPRSL, INTL = 1 << 3, 1 << 4, 1 << 5, 1 << 6, 1 << 7
PVS1, PVS4, PVS2, PVS3 = 1 << 8, 1 << 9, 1 << 10, 1 << 11
CTRL_PINS = MODSELL | RESETL | LPMODE  # host->module, active low; module pulls them up [MSA] §4.2, Table 7
SENSE_PINS = MODPRSL | INTL | PVS2 | PVS3  # module->host
PVS_OUT = PVS1 | PVS4  # host->module, "P/VSx I/O are disabled by default" [MSA] §4.2.6 -> tri-stated unless --pvs1/--pvs4
ALL_PINS = CTRL_PINS | SENSE_PINS | PVS_OUT
SAFE = CTRL_PINS  # deselected, not in reset, LOW-POWER: the only legal state at start (MINI_BRIEF §3c)
try:  # a unit unplugged under the tool: pyftdi raises FtdiError('UsbError: ... No such device'), pyusb USBError, else OSError
    from pyftdi.ftdi import FtdiError as _FtdiError
    from usb.core import USBError as _USBError
    GONE_ERRORS = (_FtdiError, _USBError, OSError)
except ImportError:  # the fake transport needs no pyftdi
    GONE_ERRORS = (OSError,)

NAMES = {MODSELL: 'ModSelL', RESETL: 'ResetL', LPMODE: 'LPMode', MODPRSL: 'ModPrsL', INTL: 'IntL',
         PVS1: 'P/VS1', PVS4: 'P/VS4', PVS2: 'P/VS2', PVS3: 'P/VS3'}

T_MODSEL_S = 0.002    # [MSA] Table 8: tSU.ModSelL = tHD.ModSelL = 2 ms
T_RESET_S = 0.001     # [MSA] Table 9: t_reset_init >= 10 us; 1 ms because USB latency makes anything shorter meaningless
T_MGMT_INIT_S = 2.0   # [MSA] Table 9: MgmtInitDuration max 2000 ms (reset edge -> first ACKed transaction)
T_DDCS_S = 0.010      # [CMIS] Table 10-5: tDDCS max 10 ms (DiagnosticsSelector write -> valid data)
T_BPC_S = 0.010
#                       the CMIS timing table was not re-read this session, 10 ms is the audit's figure)
RD_CHUNK = 32         # ponytail: conservative sequential-read length; raise to 128 if the cable copes


class Nack(Exception):
    """Module did not ACK (deselected, in reset/MgmtInit, or absent)."""


class Refused(SystemExit):
    def __init__(self, msg):
        super().__init__(f'REFUSED: {msg}')


# --------------------------------------------------------------------------- backends
def _lock_path(serial):
    return os.path.join(tempfile.gettempdir(), f'aectest-{serial}.lock')


CLOCKS = {}
SPEEDS = {'100k': 100_000, '400k': 400_000, '1M': 1_000_000}
RAMP_ROUNDS = 20


def _clock(serial, hz, ramp=None):
    if serial:
        CLOCKS[serial] = {'i2c_hz': int(hz), 'i2c_ramp': ramp}


def ramp_clock(port, say=None, target=None, rounds=None):
    """The clock plan: open at 100 kHz, then step up. The floor is 400 kHz, the QSFP-DD hardware spec's Fast Mode
    (QSFP-DD_Rev7.1.md: SCL/SDA IOL 3 mA Fast Mode). MciMaxSpeed 00h:2.3-2 (CMIS 5.0+) says 1 MHz when the module takes
    Fast Mode+, and 1 MHz needs target='1M' as well (no scope measurement of the board at 1 MHz). RAMP_ROUNDS reads of the
    lower page and a page-select read-back at the new clock decide: 0 NACK and 0 mismatch keeps it, else back to 100 kHz.
    Returns the record dict and stores it in CLOCKS[serial]."""
    say = say or (lambda text, err=False: None)
    rounds = int(rounds or RAMP_ROUNDS)
    be, base = port.be, port.be.hz
    serial = getattr(be, 'serial', None)
    port.ramp_done = True  # one ramp per open, whoever calls first (the after-identity hook or the browser's follow-on job)
    try:
        with port.selected():
            b1, b2 = port.read(1, 2)
    except Nack as e:  # no module yet (the technician flow opens before the insert): the clock stays, the caller ramps later
        rec = {'i2c_hz': base, 'i2c_ramp': f'kept {base} Hz: no module answered at the ramp ({e})'}
        _clock(serial, base, rec['i2c_ramp'])
        return rec
    major, flat = b1 >> 4, bool(b2 & 0x80)
    adv = ((b2 >> 2) & 3) if major >= 5 else None  # 0 = 400 kHz, 1 = 1 MHz; a 4.0 module advertises nothing
    if target == '1M' and adv == 1:
        want = 1_000_000
    else:
        want = 400_000
        if target == '1M':
            say(f'1 MHz not advertised (MciMaxSpeed {"absent on CMIS 4.x" if adv is None else adv}): the ramp stops at 400 kHz')
    if base >= want:
        rec = {'i2c_hz': base, 'i2c_ramp': f'fixed {base} Hz'}
        _clock(serial, base, rec['i2c_ramp'])
        return rec
    be.set_hz(want)
    nacks = mismatches = 0
    for _ in range(rounds):
        try:
            with port.selected():
                port.read(0, 128)
                if not flat:
                    port.page(0x00)
        except Nack as e:
            if 'read-back' in str(e):
                mismatches += 1
            else:
                nacks += 1
    if nacks or mismatches:
        be.set_hz(base)
        ramp = f'fell back to {base} Hz: {nacks} NACK, {mismatches} page read-back mismatch in {rounds} rounds at {want} Hz'
        say(ramp, err=True)
    else:
        ramp = f'ramped {base} -> {be.hz} Hz ({rounds} rounds, 0 NACK, 0 mismatch; MciMaxSpeed {adv if adv is not None else "n/a"})'
        say(ramp)
    rec = {'i2c_hz': be.hz, 'i2c_ramp': ramp}
    _clock(serial, be.hz, ramp)
    return rec


def ramp_after_identity(port):
    """The unit opens at 100 kHz, the first identity read runs there, the ramp follows it. cmis.read_id
    calls this; it ramps once when the Port carries a target ('auto' or '1M', set by the CLI open) and never in the browser,
    where the plate shows first and a follow-on job ramps."""
    target = getattr(port, 'ramp_target', None)
    if target and not getattr(port, 'ramp_done', False):
        ramp_clock(port, port.say, target='1M' if target == '1M' else None)


def lock_unit(serial):
    """One process per stick. The lock file holds the pid; a dead pid's lock is stale and taken over."""
    path = _lock_path(serial)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            try:
                pid = int(open(path).read().strip() or 0)
                os.kill(pid, 0)
                alive = True  # the same process too: a second Port on one stick is a bug, not a takeover
            except (ValueError, ProcessLookupError):
                alive = False
            except PermissionError:
                alive = True
            if alive:
                raise Refused(f'unit {serial} is open by another process (pid {pid}, {path}): close it there first')
            os.unlink(path)
    raise Refused(f'unit {serial}: lock file {path} could not be taken')


def unlock_unit(serial):
    try:
        os.unlink(_lock_path(serial))
    except FileNotFoundError:
        pass


class FtdiBackend:
    """pyftdi I2cController + its GPIO port. gpio_write() sends value and direction in ONE MPSSE SET_BITS
    command (opcode 0x80/0x82 carry both bytes), so a pin goes input -> output-high with no output-low window."""

    def __init__(self, url, hz):
        from pyftdi.i2c import I2cController, I2cNackError
        self.serial = url.rsplit('/', 1)[0].rsplit(':', 1)[-1] if url.count(':') >= 3 else None  # ftdi://ftdi:2232h:<serial>/1
        self._lock = lock_unit(self.serial) if self.serial else None
        self._nack = I2cNackError
        self.ctrl = I2cController()
        # direction=0: every GPIO starts as an input; initial=SAFE: the value latched for when they become outputs.
        self.ctrl.configure(url, frequency=hz, direction=0, initial=SAFE)
        self.gpio = self.ctrl.get_gpio()
        self.gpio.set_direction(ALL_PINS, 0)  # pyftdi replaces (not ORs) its pin mask -> always pass ALL_PINS
        self.port = self.ctrl.get_port(I2C_ADDR)
        _clock(self.serial, hz)
        self.hz = self.ctrl.frequency
        try:  # 7.5: the EEPROM product string (one control transfer, cached by pyusb); None when the descriptor is unreadable
            self.product = self.ctrl.ftdi.usb_dev.product
        except Exception:  # noqa: BLE001 -- a string descriptor the OS refuses to read is not a bus fault
            self.product = None
        try:
            from .fru import read_fru
            self.fru = read_fru(self.ctrl.ftdi)
        except Exception:  # noqa: BLE001
            self.fru = None

    def set_hz(self, hz):
        """Re-clock the MPSSE without a re-open (pyftdi 0.57.2 private state, pinned: configure() derives the four
        setup / hold delay counts from its timing table and the 3-phase clock is 1.5 x the bus clock)."""
        c = self.ctrl
        t = c.I2C_100K if hz <= 100e3 else c.I2C_400K if hz <= 400e3 else c.I2C_1M
        with c._lock:
            actual = c._ftdi.set_frequency(1.5 * hz)
            c._frequency = (2.0 * actual) / 3.0
            c._ck_hd_sta = c._compute_delay_cycles(t.t_hd_sta)
            c._ck_su_sto = c._compute_delay_cycles(t.t_su_sto)
            c._ck_idle = max(c._compute_delay_cycles(t.t_su_sta), c._compute_delay_cycles(t.t_buf))
            c._ck_delay = c._compute_delay_cycles(t.t_buf)
        self.hz = c.frequency  # actual MPSSE clock after pyftdi's divider, not the requested value
        _clock(self.serial, self.hz)

    def gpio_write(self, value, direction):
        self.gpio.set_direction(ALL_PINS, direction)  # software state only ...
        self.gpio.write(value & direction)            # ... pushed to the chip together with the value

    def gpio_read(self):
        return self.gpio.read(with_output=True)

    def tristate(self):
        """AD0-AD7 (SCL, SDA and the ADBUS GPIOs) to inputs with ONE raw SET_BITS_LOW. pyftdi's I2cController keeps
        SCL/SDA_O as outputs in every SET_BITS it emits, so this is the only way off the bus (review R6-D01). Called
        after gpio_write(value, 0), so the GPIO latch already holds the SAFE values and the direction 0. SET_BITS_HIGH follows
        for AC0-AC7."""
        self.ctrl.ftdi.write_data(MPSSE_TRISTATE_LOW + MPSSE_TRISTATE_HIGH)

    def i2c_read(self, off, n):
        try:
            return bytes(self.port.read_from(off, n))
        except self._nack as e:
            raise Nack(str(e)) from None

    def i2c_write(self, off, data):
        try:
            self.port.write_to(off, bytes(data))
        except self._nack as e:
            raise Nack(str(e)) from None

    def close(self):
        # freeze=True: release the USB interface but leave the chip in MPSSE with the SAFE pin states. The default
        # (terminate/close(freeze=False)) resets the FT2232H to UART mode -- AD4/DTR#, AD2/RTS#, AC3/RXLED# become
        self.ctrl.close(freeze=True)
        if getattr(self, '_lock', None):  # the selftest builds the backend without __init__ for the close test
            unlock_unit(self.serial)


class FakeBackend:
    """Bench double for --dry-run/--selftest: FT2232H GPIO latch + one paged CMIS 5.x cable end (the built-in ACME image), or
    the image of fake_modules.load(sku) when `image` is given (6.1: data-driven; the behaviour knobs stay the class's).
    Records every SET_BITS as (value, direction) in .log; answers only while ModSelL is driven low."""

    max_speed_adv = None
    man = set()
    gone = set()

    def __init__(self, serial='FAKE-A', hz=100_000, image=None):
        self.serial, self.log, self.val, self.dir, self.hz = serial, [], 0, 0, hz
        self.nack_above_hz = None
        _clock(serial, hz)
        self.product = 'AEC-CT2-MINI'
        self.fru = None
        self.raw = []  # raw MPSSE bytes (tristate)
        self.ready_at, self.in_reset, self.page_writes = 0.0, False, []
        self.present_at = 0.0  # ModPrsL falls at this time (selftest sets it in the future to exercise `watch`)
        self.lo = bytearray(128)
        self.mem = {pg: bytearray(128) for pg in (0x00, 0x01, 0x02, 0x03, 0x10, 0x11, 0x13, 0x14, 0x9F)}
        self.cdb_status, self.cdb_busy_polls = 0, 0
        self._fw_init()
        lo = self.lo
        lo[L_ID], lo[L_REV], lo[L_MEM] = 0x18, 0x50, 0x00
        lo[L_TEMP:L_TEMP + 2] = (43 * 256).to_bytes(2, 'big')
        lo[L_VCC:L_VCC + 2] = (33000).to_bytes(2, 'big')  # 3.300 V in 100 µV units
        lo[L_CTRL] = CTRL_LOWPWR_ALLOW_HW  # Table 8-11: LowPwrAllowRequestHW = 1 by default
        p0 = self.mem[0]
        p0[0] = 0x18
        p0[1:17], p0[20:36], p0[36:38] = b'ACME CABLES     ', b'AEC-800G-2M     ', b'A1'
        p0[38:54], p0[54:62] = b'FAKE000000000001', b'26091300'
        p0[P0_PCLASS - 128], p0[P0_MAXP - 128] = 0b010 << 5, 22  # class 3, 5.5 W
        p0[P0_MEDIA_TECH - 128] = 0x0C  # Table 8-36: copper, near and far end limiting active equalizers (an AEC)
        self.lo[85] = 0x04
        self.lo[86:90] = bytes([0x11, 0x0C, 0x88, 0x01])
        self.mem[1][P1_PAGES - 128] = P1_DIAG_BIT | P1_PAGE03_BIT
        self.mem[1][163 - 128] = 0x40
        self.mem[1][P1_DURATIONS - 128] = 0x43  # Table 8-51/8-43: PwrDn 50-100 ms, PwrUp 10-50 ms
        self.mem[1][144 - 128] = 0x33
        self.mem[1][P1_MONITORS - 128] = MON_TEMP | MON_VCC
        self.nack_after_write_ms, self.busy_until = 0, 0.0
        p2 = self.mem[2]  # Table 8-55: temp alarm 80 / warning 75 C (low -10 / -5), Vcc alarm 3.6 / 3.0 V, warning 3.5 / 3.1 V
        for (off, signed), v in zip(P2_THRESHOLDS.values(), (80, -10, 75, -5, 36000, 30000, 35000, 31000)):
            p2[off - 128:off - 126] = (v * 256 if signed else v).to_bytes(2, 'big', signed=signed)
        self.mem[0x10][P10_DPDEINIT - 128] = 0xFF  # every Data Path deinitialised until the host writes 10h:128 = 0
        p11 = self.mem[0x11]
        p11[136 - 128] = p11[137 - 128] = 0xFF  # host-side LOS/LOL on all lanes: nothing is plugged into the host side
        p13 = self.mem[0x13]
        p13[P13_LOC - 128] = LOC_MEDIA_GEN | LOC_MEDIA_CHK  # media gen/chk advertised, pre and post FEC
        p13[P13_CAP_REP - 128] = REP_BER | REP_COUNTERS | REP_SNR_MEDIA  # 13h:130.0/130.1/130.5; 129.4 = 0: no periodic update (freeze path)
        p13[0] = LOOP_MEDIA_IN
        p13[P13_MGEN_PAT - 128] = p13[P13_MCHK_PAT - 128] = 0x03  # generator and checker: patterns 0-1
        if image:
            self.lo[:] = image['lower']
            self.mem.update({pg: bytearray(b) for pg, b in image['pages'].items()})
        if FakeBackend.max_speed_adv:
            self.lo[2] = (self.lo[2] & ~0x0C) | ((1 if FakeBackend.max_speed_adv == '1M' else 0) << 2)
        self.last_state = None
        self._checksums()

    def _checksums(self):
        """The page checksums the audit verifies (8.3.10: 00h:222 = sum 128-221; 8.4.15: 01h:255 = sum 130-254; 8.5.3: 02h:255 = sum 128-254)."""
        self.mem[0][94] = sum(self.mem[0][0:94]) & 0xFF
        self.mem[1][127] = sum(self.mem[1][2:127]) & 0xFF
        self.mem[2][127] = sum(self.mem[2][0:127]) & 0xFF

    def _state(self):
        lp_hw = not (self.dir & LPMODE) or bool(self.val & LPMODE)  # tri-stated pin reads high via module pull-up
        low = (lp_hw and self.lo[L_CTRL] & CTRL_LOWPWR_ALLOW_HW) or self.lo[L_CTRL] & CTRL_LOWPWR_SW
        return 1 if low else 3

    def gpio_write(self, value, direction):
        self.log.append((value, direction))
        rst_low = bool(direction & RESETL) and not value & RESETL
        if rst_low:
            self.in_reset = True
        elif self.in_reset:
            self.in_reset, self.ready_at = False, time.monotonic() + 0.05  # fake MgmtInit 50 ms
        self.val, self.dir = value, direction

    def gpio_read(self):
        pulled_up = ALL_PINS & ~SENSE_PINS & ~self.dir
        prs = 0 if time.monotonic() >= self.present_at or self.serial in FakeBackend.man else MODPRSL  # ModPrsL low = present
        self._latch_flags()
        intl = 0 if ((self.lo[8] & ~self.lo[31]) | (self.lo[9] & ~self.lo[32])) & 0xFF else INTL  # IntL low while an unmasked flag is pending
        return (intl | PVS2 | PVS3) | prs | pulled_up | (self.val & self.dir)

    def _latch_flags(self):
        st = self._state()
        if self.last_state is not None and st != self.last_state:
            self.lo[8] |= 0x01  # Table 6-9: ModuleStateChangedFlag on a module-initiated state entry (the fake sets it on every change)
        self.last_state = st

    def _check(self):
        if self.serial in FakeBackend.gone:
            raise OSError(19, 'No such device (it may have been disconnected) (fake: unit gone)')
        if self.serial in FakeBackend.man:
            raise Nack('fake: MODE MAN, ModSelL does not reach the module')
        if (not (self.dir & MODSELL) or self.val & MODSELL or self.in_reset
                or time.monotonic() < max(self.ready_at, self.present_at + 0.05)):  # 50 ms MgmtInit after insertion
            raise Nack('fake: not selected / in MgmtInit')
        if time.monotonic() < self.busy_until:
            raise Nack('fake: busy after a write (tNACK)')

    def _view(self):
        lo = bytearray(self.lo)
        lo[L_STATE] = self._state() << 1 | 1
        lo[37] = 0x81 if self.cdb_busy_polls else self.cdb_status  # CdbStatus1: busy capturing, then the result (Table 9-3)
        up = bytearray(self.mem.get(lo[L_PAGE], bytearray(128)))
        if lo[L_PAGE] == 0x11:  # DP states: DPActivated once ModuleReady and 10h:128 DPDeinit cleared, else DPDeactivated
            act = self._state() == ST_READY and not self.mem[0x10][P10_DPDEINIT - 128]
            up[0:4] = (bytes([DP_ACTIVATED * 0x11]) if act else b'\x11') * 4
        if lo[L_PAGE] == 0x14:
            if up[0] == SEL_BER:  # F16 1.0e-12 on media lanes (m=1, s=12)
                up[P14_MEDIA_BER - 128:P14_MEDIA_BER - 128 + 16] = bytes([12 << 3, 1]) * 8
            elif up[0] in SEL_MEDIA_CNT:  # 4 lanes x (errors 0, total bits 1e12) U64 LE
                up[64:128] = (bytes(8) + (10 ** 12).to_bytes(8, 'little')) * 4
            elif up[0] == SEL_SNR:
                up[64:96] = (21 * 256 + 64).to_bytes(2, 'little') * 16
            elif up[0]:
                up[0] = 0  # Table 8-118: unsupported selector reverts to 0, data area all zeroes
        return lo + up

    def set_hz(self, hz):
        self.hz = hz
        _clock(self.serial, hz)

    def i2c_read(self, off, n):
        self._check()
        if self.nack_above_hz is not None and self.hz > self.nack_above_hz:
            raise Nack(f'fake: no answer at {self.hz} Hz (nack_above_hz {self.nack_above_hz})')
        self._latch_flags()
        out = bytes(self._view()[off:off + n])
        if self.cdb_busy_polls and off <= 37 < off + n:
            self.cdb_busy_polls -= 1
        for b in (8, 9):  # Table 8-9 flags are latched, clear on read
            if off <= b < off + n:
                self.lo[b] = 0
        return out

    def i2c_write(self, off, data):
        self._check()
        data = bytes(data)
        if off in (L_BANK, L_PAGE):
            self.page_writes.append((off, len(data)))
        if off < 128:
            self.lo[off:off + len(data)] = data
            if off == L_CTRL and data[0] & CTRL_SW_RESET:
                self.ready_at = time.monotonic() + 0.05
        else:
            pg = self.mem.setdefault(self.lo[L_PAGE], bytearray(128))
            pg[off - 128:off - 128 + len(data)] = data
            if self.lo[L_PAGE] == 0x9F and off <= 129 < off + len(data):  # a write that includes 9Fh:129 triggers the CDB command
                self._cdb_run()
            if self.lo[L_PAGE] == 0x10 and off <= 143 < off + len(data):
                self.mem[0x11][202 - 128:206 - 128] = b'\x11' * 4
        if self.nack_after_write_ms:
            self.busy_until = time.monotonic() + self.nack_after_write_ms / 1000.0

    CDB_REPLIES = {  # the Accelink RTXM600-578 replies of 2026-10-20 (EXP2 records in the CT2M-02 run folder)
        0x0041: bytes.fromhex('00 00 50 ff ff 11 00 00 00 c8 00 00 00 50 00 c8 00 00'),
        0x0100: bytes([0x43, 0x05, 0x60, 0x44, 0x00, 0x00]) + bytes(104)}
    # (0x05) so the abort and skip paths run; banks: A running + committed at 60.44, B invalid (FirmwareStatus 0x43, Table 7-2).
    FW_FEATURES = bytes.fromhex('00 05 50 ff ff 11 00 00 00 c8 00 00 00 50 00 c8 00 00')
    FW_IMAGE_A = bytes([0x60, 0x44, 0x00, 0x00])

    def _fw_init(self):
        self.fw_banks = {'A': {'image': self.FW_IMAGE_A, 'valid': True}, 'B': {'image': None, 'valid': False}}
        self.fw_running, self.fw_committed = 'A', 'A'
        self.fw_dl = None            # the open download: {'size', 'header', 'data': bytearray, 'blocks'}
        self.fw_chk_fail_at = None   # knob: the write block index that answers 0x45 (a CdbChkCode error) once
        self.fw_reject = set()       # knob: command names that fail 0x40 ('start', 'write', 'complete', 'run', 'commit', 'abort')
        self.fw_boot_s = 0.0         # knob: seconds of NACK after Run (the module reset)
        self.fw_log = []             # every firmware command name, for the selftest

    def _fw_info_reply(self):
        a, b = self.fw_banks['A'], self.fw_banks['B']
        nib = lambda k, bank: (self.fw_running == k) | (self.fw_committed == k) << 1 | (not bank['valid']) << 2  # noqa: E731
        st = nib('A', a) | nib('B', b) << 4
        info = (1 if a['image'] else 0) | (2 if b['image'] else 0) | 4
        return bytes([st, info]) + (a['image'] or bytes(4)) + bytes(32) + (b['image'] or bytes(4)) + bytes(32) + bytes([0x10, 0x00, 0, 0]) + bytes(32)

    def _fw_cmd(self, cmd, lpl, epl_len):
        """The firmware commands of Tables 9-16 to 9-25 on the fake store; returns (reply, status)."""
        name = {0x0101: 'start', 0x0102: 'abort', 0x0103: 'write', 0x0104: 'write', 0x0107: 'complete', 0x0108: 'copy',
                0x0109: 'run', 0x010A: 'commit'}[cmd]
        self.fw_log.append(name)
        if name in self.fw_reject:
            return b'', 0x40
        inactive = 'B' if self.fw_running == 'A' else 'A'
        if name == 'start':
            if len(lpl) < 8 + self.FW_FEATURES[2]:
                return b'', 0x42
            self.fw_dl = {'size': int.from_bytes(lpl[0:4], 'big'), 'header': bytes(lpl[8:]), 'data': bytearray(), 'blocks': 0}
            self.fw_banks[inactive] = {'image': None, 'valid': False}  # 7.3.1.2: the bank is marked empty until Complete
            return b'', 0x01
        if name == 'abort':
            self.fw_dl = None
            return b'', 0x01
        if name == 'write':
            if self.fw_dl is None:
                return b'', 0x40
            if self.fw_chk_fail_at == self.fw_dl['blocks']:
                self.fw_chk_fail_at = None
                return b'', 0x45
            addr = int.from_bytes(lpl[0:4], 'big')
            data = bytes(lpl[4:]) if cmd == 0x0103 else bytes(self._epl(epl_len))
            if addr < len(self.fw_dl['data']):
                return b'', 0x42
            self.fw_dl['data'] += bytes([self.FW_FEATURES[3]]) * (addr - len(self.fw_dl['data'])) + data  # a gap = skipped erased blocks (7.3.1.2)
            self.fw_dl['blocks'] += 1
            return b'', 0x01
        if name == 'complete':
            dl, self.fw_dl = self.fw_dl, None
            if dl is not None:  # a short tail = skipped erased blocks at the end (7.3.1.2)
                dl['data'] += bytes([self.FW_FEATURES[3]]) * (dl['size'] - len(dl['header']) - len(dl['data']))
            if dl is None or len(dl['header']) + len(dl['data']) != dl['size']:
                return b'', 0x40
            self.fw_banks[inactive] = {'image': bytes(dl['data'][:4]), 'valid': True}  # the image's first four bytes are its version
            return b'', 0x01
        if name == 'run':
            if len(lpl) < 4 or not self.fw_banks[inactive]['valid']:
                return b'', 0x42
            self.fw_running = inactive
            self.ready_at = time.monotonic() + self.fw_boot_s
            return b'', 0x01
        if name == 'commit':
            self.fw_committed = self.fw_running
            return b'', 0x01
        return b'', 0x41  # copy: not advertised

    def _epl(self, n):
        out = bytearray()
        for k in range(0, n, 128):
            out += self.mem.get(0xA0 + k // 128, bytearray(128))[:min(128, n - k)]
        return out

    def _cdb_run(self):
        """The fake's CDB engine on Page 9Fh. 0000h with its 2-byte LPL, 0041h and 0100h answer like the Accelink;
        0040h / 0042h / 0043h fail 0x41 (CMDID unknown) as that module does; a bad CdbChkCode fails 0x45. The
        firmware commands 0101h-010Ah run on the fake store (_fw_cmd); 0041h / 0100h read it."""
        pg = self.mem[0x9F]
        cmd, epl_len, lpl_len, chk = int.from_bytes(pg[0:2], 'big'), int.from_bytes(pg[2:4], 'big'), pg[4], pg[5]
        lpl = bytes(pg[8:8 + lpl_len])
        want = (~(sum(pg[0:5]) + sum(lpl))) & 0xFF
        reply, status = b'', 0x01
        if chk != want:
            status = 0x45
        elif cmd == 0x0000:
            reply, status = (b'\x02\x00', 0x01) if lpl_len == 2 else (b'', 0x42)
        elif cmd == 0x0041:
            reply = self.FW_FEATURES
        elif cmd == 0x0100:
            reply = self._fw_info_reply()
        elif 0x0101 <= cmd <= 0x010A and cmd not in (0x0105, 0x0106):
            reply, status = self._fw_cmd(cmd, lpl, epl_len)
        elif cmd in self.CDB_REPLIES:
            reply = self.CDB_REPLIES[cmd]
        else:
            status = 0x41
        pg[6], pg[7] = len(reply), ((~sum(reply)) & 0xFF) if reply else 0
        pg[8:8 + len(reply)] = reply
        self.cdb_status, self.cdb_busy_polls = status, 1

    def tristate(self):
        self.raw += [MPSSE_TRISTATE_LOW, MPSSE_TRISTATE_HIGH]

    def close(self):
        pass


# --------------------------------------------------------------------------- port
class Port:
    """One QSFP-DD port behind one FT2232H channel A. Holds the pin state and the LPMode permission."""

    def __init__(self, be, allow_lp_low=False, say=None):
        self.be, self.allow_lp_low = be, allow_lp_low
        self.say = say or (lambda text, err=False: None)  # 2.4: the CLI renders the two stderr lines (PVS note, close summary)
        self.val, self.dir = SAFE, 0
        self.ramp_target, self.ramp_done = None, False
        self.temp_stop_c = None
        self.temp_warn_c = None
        self._t_write = -1.0
        # Power-up safety (MINI_BRIEF §3c): (1) latch the SAFE values while every pin is still an input,
        # (2) switch the control pins to output in a command that carries those same high values.
        self.be.gpio_write(self.val, self.dir)
        self._apply(direction=CTRL_PINS)

    def _apply(self, value=None, direction=None):
        if value is not None:
            self.val = value
        if direction is not None:
            self.dir = direction
        if (self.dir & LPMODE) and not (self.val & LPMODE) and not self.allow_lp_low:
            raise Refused(GUARD)  # defence in depth: no code path may drive LPMode low without the flags
        self.be.gpio_write(self.val, self.dir)

    def pin(self, mask, level):
        self._apply(value=(self.val | mask) if level else (self.val & ~mask))

    def set_lpmode(self, level):
        if not level and not self.allow_lp_low:
            raise Refused(GUARD)
        self.pin(LPMODE, level)

    def drive_pvs(self, mask, level):
        self.say(PVS_DNP_NOTE, err=True)
        self._apply(value=(self.val | mask) if level else (self.val & ~mask), direction=self.dir | mask)

    def reset_pulse(self):
        self.pin(RESETL, 0)
        time.sleep(T_RESET_S)
        self.pin(RESETL, 1)

    def sense(self):
        v = self.be.gpio_read()
        d = {NAMES[m]: int(bool(v & m)) for m in (MODPRSL, INTL, PVS2, PVS3)}
        d.update({NAMES[m]: 'n/a (R426/R427 DNP)' for m in (PVS1, PVS4)})
        d.update({NAMES[m] + '(drv)': int(bool(self.val & m)) for m in (MODSELL, RESETL, LPMODE)})
        d['module_present'] = not v & MODPRSL  # [MSA] Table 7: ModPrsL low = module present
        d['interrupt'] = not v & INTL          # IntL low = interrupt asserted
        return d

    def tristate(self):
        """Every channel-A pin back to input, SCL/SDA included (values kept latched) -- before SW_PWR is switched off
        (S11, MM-018, R6-D01): the GPIO update first, then the raw SET_BITS_LOW that pyftdi cannot express."""
        self._apply(direction=0)
        self.be.tristate()

    def close(self):
        gone = None
        try:
            self._apply(value=self.val | SAFE)  # never leave LPMode low / ResetL low / module selected behind
            if not self.dir:
                self.be.tristate()  # the SAFE write above re-asserted pyftdi's I2C direction bits: undo it
        except GONE_ERRORS as e:  # the unit was unplugged (or latched) under us: nothing to leave SAFE, say so once
            gone = e
        finally:
            try:
                self.be.close()
            except GONE_ERRORS as e:
                gone = gone or e
            if gone is not None:
                self.say(f'unit gone before close ({type(gone).__name__}: {gone}); nothing driven', err=True)
                return
            drv = [NAMES[m] + ('=1' if self.val & m else '=0') for m in (MODSELL, RESETL, LPMODE, PVS1, PVS4) if self.dir & m]
            self.say('FT2232H left in MPSSE, ' + (f'driving: {", ".join(drv)}; SCL push-pull HIGH / SDA idle HIGH (I2C engine) '
                                                   '-- run `tristate` or remove the module before SW_PWR OFF (S11)' if drv else
                                                   'all AD/AC pins inputs incl. SCL/SDA (raw SET_BITS_LOW 80 00 00)'), err=True)

    # ---- CMIS memory access (module must be selected: ModSelL low, [MSA] §4.5)
    @contextmanager
    def selected(self):
        self.pin(MODSELL, 0)
        time.sleep(T_MODSEL_S)
        try:
            yield self
        finally:
            time.sleep(T_MODSEL_S)
            self.pin(MODSELL, 1)

    def _ack(self, fn, window):
        """A NACK inside the module's tNACK hold-off is not a fault -- retry at 1 ms until ACK or the
        window ends ([CMIS] §B.2.7.3.2: tWRITE 10 ms volatile, tWRITENV 80 ms); the Nack propagates only after the window."""
        t0 = time.monotonic()
        while True:
            try:
                return fn()
            except Nack:
                if time.monotonic() - t0 >= window:
                    raise
                time.sleep(0.001)

    def read(self, off, n):
        out = b''
        window = T_NACK_S if time.monotonic() - self._t_write < T_NACK_NV_S else 0.0  # the reads after a write retry; others NACK at once (watch / reset timing)
        while len(out) < n:
            out += self._ack(lambda: self.be.i2c_read(off + len(out), min(RD_CHUNK, n - len(out))), window)
        return out

    def write(self, off, data):
        self._ack(lambda: self.be.i2c_write(off, bytes(data)), T_NACK_S)
        self._t_write = time.monotonic()

    def page(self, pg, bank=0):
        """[CMIS] §8.2.13/Table 8-22: BankSelect and PageSelect in ONE write, read back; one retry after
        T_BPC_S when the module NACKs while it remaps."""
        for attempt in (0, 1):
            try:
                self.write(L_BANK, bytes([bank, pg]))
                back = self.read(L_BANK, 2)
                break
            except Nack:
                if attempt:
                    raise
                time.sleep(T_BPC_S)
        if back != bytes([bank, pg]):
            raise Nack(f'PageSelect read-back 126/127 = {back.hex()} after writing {bank:02x}{pg:02x}')

    def read_page(self, pg, bank=0, flat=False):
        if not flat:
            self.page(pg, bank)
        return self.read(128, 128)

    def lower(self):
        return self.read(0, 128)
