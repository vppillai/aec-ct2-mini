#!/usr/bin/env python3
"""safety.py - the S1-S11 guard texts, the jig limits and the technician preconditions.

The guard texts, the posture table, the jig temperature cap and the PRBS minimum live here. The rules already in code stay where they are: the LPMode guard in transport.Port, the S4 budget /
pre-flight / S3 levels in tests.release_lpmode, the S5 stop in tests.stop_reason, the cleanup in tests.prbs_direction.

New: preconditions(observed, jig_profile) -> the 2.10 reason codes for a technician run, in the order they are checked:
POWER-UNACKED, STRAPS-CUT, JIG-UNIT-MISSING, NOT-PRESENT-A / NOT-PRESENT-B, OVER-JIG-BUDGET, THERMAL. Every code is in
criteria.CODES. The S1 posture table ships inside the package (posture_budget.yaml).

  python -m aectest.safety --selftest
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD_DIR = os.path.normpath(os.path.join(HERE, '..', 'board'))  # the bring-up tool's folder (udev rules, EEPROM notes)
RULES = os.path.join(HERE, 'data', '99-aec-ct2-mini.rules')
POSTURE_YAML = os.path.join(HERE, 'posture_budget.yaml')
JIG_TEMP_LIMIT_C = 70.0
#                          case (TempMonValue is internal and reads above the case) -- --temp-limit-c overrides
THROTTLE_MARGIN_C = 5.0
PRBS_MIN_S = 60.0
BUDGET_W = 6.0        # S4 default (cli --budget-w): a 3 A USB-C port gives ~6 W at 3.3 V (MINI_BRIEF §1)

GUARD = ('refusing to drive LPMode low: high-power mode needs BOTH --release-lpmode AND --i-am-on-usbc-power.\n'
         '  Test-plan rule S10: HIGH POWER ONLY WITH A 3 A CHARGER ON PWR ONLY (J102) and the host cable in PC (J101).\n'
         '  The board has no CC sensing and the software no view of VBUS_DET: the second flag acknowledges that the\n'
         '  charger is plugged in -- a laptop port alone (PC or PWR ONLY) is never a high-power source; dongle mode\n'
         '  (PWR ONLY only, no PC) stays in low power (LPMode = H). See the test plan rule S10.')
S10_CHECK = ('S10 check before LPMode goes low: 5V IN LED (D313) lit (charger on PWR ONLY); with the hood off TP112 '
             '(PWR_CC1) 1.31-2.04 V = 3 A source (0.70-1.16 V = 1.5 A, 0.25-0.61 V = 0.5 A: STOP). Do not unplug the '
             'charger while LPMode is low (the load moves to the PC port).')
MAN_HINT = 'SW_MODE = MAN? the override stage keeps the FTDI ModSelL/ResetL/LPMode off the pads -- move to AUTO (S2)'
POSTURE_WARN = ('posture: none given -- the posture table (hood-on 0.64 / hood-off 1.27 / fan 3.75 / forced-air 4.4 W at 25 C, '
                'x0.78 at 35 C) is INFORMATIONAL: no software refusal; '
                'the module\'s own temperature WARN/THROTTLE path and the S4 --budget-w gate are the controls (--posture P opts '
                'the S1 refusal in)')
FORCED_AIR_MSG = ('forced air: table fan through the heat-sink fins, hood off (the operating condition -- advanced tests on '
                  'class >= 3 modules need it); let TempMonValue fall below the WARN level, then re-run')
TRISTATE_LAST = 'all channel-A pins are inputs, SCL/SDA included (raw SET_BITS_LOW 80 00 00); SW_PWR may be switched off (S11)'


def posture_limit(posture, ambient_c):
    """S1 (optional, informational): Q_max [W] for the posture from the posture table of the package."""
    if not posture:
        return None
    try:
        import yaml
    except ImportError:
        raise SystemExit('--posture needs pyyaml (`pip install pyyaml`)') from None
    with open(POSTURE_YAML) as f:
        t = yaml.safe_load(f)
    return round(t['postures'][posture]['w_25c'] * t['ambient_scale'][ambient_c], 3)


def budget_figure(info, assume_w=None):
    """S4 gate figure: MaxPower (00h:201) per CMIS §6.3.2.4; a blank 0 falls back to the MSA class maximum (R6-W07),
    class 8 to --assume-w. Returns (watts, source) -- watts None for class 8 without either figure."""
    if info['max_power_w'] > 0:
        return info['max_power_w'], 'MaxPower 00h:201'
    if info['power_class_msa_max_w'] is None and assume_w:
        return assume_w, '--assume-w (class 8, 00h:201 = 0)'
    return info['power_class_msa_max_w'], f'MSA Table 12 class {info["power_class"]} max (00h:201 = 0)'


def preconditions(observed, jig_profile, budget_w=BUDGET_W):
    """Reason codes (criteria.CODES) that make a technician run INCONCLUSIVE before or during CMIS access (2.10).

    observed keys, all optional: units {A: bool, B: bool} (FT2232H opened), present {A: bool, B: bool} (ModPrsL),
    max_power_w (the S4 gate figure of either end), thermal ('THROTTLE' from tests.thermal_level).
    jig_profile: the dict jig.load_profile() returned (signature already checked), or None.
    """
    codes = []
    power = (jig_profile or {}).get('power') or {}
    if not jig_profile or not power.get('acknowledged_by') or not jig_profile.get('signed'):
        codes.append('POWER-UNACKED')
    if jig_profile and not jig_profile.get('straps_intact'):  # 2.9: a cut strap = the FTDI off the pads, R&D dongle session
        codes.append('STRAPS-CUT')
    units = observed.get('units', {})
    if not all(units.get(r) for r in observed.get('ends') or ('A', 'B')):
        codes.append('JIG-UNIT-MISSING')
    present = observed.get('present', {})
    codes += [f'NOT-PRESENT-{r}' for r in ('A', 'B') if r in present and not present[r]]
    w = observed.get('max_power_w')
    if w is not None and w > budget_w:  # BUDGET (S4): not a cable finding on the MINI
        codes.append('OVER-JIG-BUDGET')
    if observed.get('thermal') == 'THROTTLE':
        codes.append('THERMAL')
    return codes


# --------------------------------------------------------------------------- selftest
def selftest():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from aectest.criteria import CODES
    known = set(CODES)
    signed = {'jig': 'JIG-01', 'power': {'acknowledged_by': 'eng'}, 'signed': 'ab' * 32, 'straps_intact': True}
    ok = {'units': {'A': True, 'B': True}, 'present': {'A': True, 'B': True}, 'max_power_w': 5.5, 'thermal': None}
    assert preconditions(ok, signed) == []
    assert preconditions(ok, None) == ['POWER-UNACKED']
    assert preconditions(ok, {**signed, 'power': {}}) == ['POWER-UNACKED']
    assert preconditions(ok, {**signed, 'signed': None}) == ['POWER-UNACKED']
    assert preconditions(ok, {**signed, 'straps_intact': False}) == ['STRAPS-CUT']
    assert preconditions({**ok, 'units': {'A': True}}, signed) == ['JIG-UNIT-MISSING']
    assert preconditions({**ok, 'present': {'A': True, 'B': False}}, signed) == ['NOT-PRESENT-B']
    assert preconditions({**ok, 'max_power_w': 7.0}, signed) == ['OVER-JIG-BUDGET']
    assert preconditions({**ok, 'max_power_w': 7.0}, signed, budget_w=8.0) == []
    assert preconditions({**ok, 'thermal': 'THROTTLE'}, signed) == ['THERMAL']
    assert preconditions({**ok, 'thermal': 'WARN'}, signed) == []
    every = preconditions({'units': {}, 'present': {'A': False, 'B': False}, 'max_power_w': 9.0, 'thermal': 'THROTTLE'}, {'straps_intact': False})
    assert every == ['POWER-UNACKED', 'STRAPS-CUT', 'JIG-UNIT-MISSING', 'NOT-PRESENT-A', 'NOT-PRESENT-B', 'OVER-JIG-BUDGET', 'THERMAL'], every
    assert set(every) <= known, set(every) - known
    assert posture_limit(None, 25) is None and posture_limit('hood-on', 25) == 0.64 and posture_limit('hood-on', 35) == 0.499
    assert budget_figure({'max_power_w': 5.5, 'power_class_msa_max_w': 7.0, 'power_class': 3}) == (5.5, 'MaxPower 00h:201')
    assert budget_figure({'max_power_w': 0, 'power_class_msa_max_w': 7.0, 'power_class': 3})[0] == 7.0
    assert budget_figure({'max_power_w': 0, 'power_class_msa_max_w': None, 'power_class': 8})[0] is None
    assert budget_figure({'max_power_w': 0, 'power_class_msa_max_w': None, 'power_class': 8}, 9.0) == (9.0, '--assume-w (class 8, 00h:201 = 0)')
    assert '5V IN LED (D313)' in S10_CHECK and JIG_TEMP_LIMIT_C == 70.0 and PRBS_MIN_S == 60.0
    print(f'safety selftest: OK ({len(every)} precondition codes, each in criteria.CODES; posture table, budget figure)')


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        selftest()
    else:
        print(__doc__)
