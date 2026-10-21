"""`aectest-tech`: the technician entry point. One wheel, two entry points.

Registers `tech`, `inspect`, `--list` and `selftest` only. It never imports `rd` and never registers the `bringup` parser:
`aectest-tech rd` is refused by argparse, and `sys.modules` holds no `aectest.rd` after `aectest-tech --help` (selftest part f).
"""
import argparse
import sys

from . import __version__
from .lot import add_inspect
from .tech import add_tech
from .transport import Nack


def main(argv=None):
    ap = argparse.ArgumentParser(prog='aectest-tech', description=f'aectest-tech {__version__} -- AEC-CT2-MINI technician commands')
    ap.add_argument('--version', action='version', version=f'aectest {__version__}')
    ap.add_argument('--list', action='store_true', help='list the FTDI units; the serial is the unit identity (case UNIT field)')
    ap.add_argument('--pid', type=lambda x: int(x, 0), help='--list: the custom USB PID of the units')
    sub = ap.add_subparsers(dest='verb')
    add_tech(sub)
    add_inspect(sub)
    st = sub.add_parser('selftest', help='run the package selftest against the fake FT2232H and exit')
    st.add_argument('--criteria', metavar='FILE', help='the SKU criteria file for parts b-e (default: the repo file; a wheel has no repo)')
    a = ap.parse_args(argv)
    try:
        if a.list:
            from .cli import check_pyftdi, list_devices  # the enumerator lives with the full CLI; cli imports no rd at its top
            from .jig import register_pid
            if a.pid:
                register_pid(a.pid)
            check_pyftdi(False)
            list_devices(False, a.pid)
            return 0
        if a.verb == 'selftest':
            try:
                from .selftest import selftest
            except ImportError:
                sys.exit('the selftest is not part of this release')
            selftest(**({'criteria_file': a.criteria} if a.criteria else {}))
            return 0
        if not a.verb:
            ap.error('a command is required: tech, inspect, selftest, or --list')
        return a.func(a)
    except Nack as e:
        from .cli import NACK_MSG
        raise SystemExit(NACK_MSG.format(e)) from None


if __name__ == '__main__':
    sys.exit(main())
