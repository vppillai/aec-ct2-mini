# aectest

The test software of the AEC-CT2-MINI cable tester: the web UI and the command line.

## Install

1. Install Python 3.11 or newer.
2. Run `pip install .` in this folder.
3. Linux only: run `aectest setup-linux` for the udev rule, then plug the PC port again.

## Start

- `aectest web --open` starts the web UI in your browser.
- `aectest --help` lists the commands. `aectest --version` prints the version.
- `aectest web --dry-run` starts the web UI on a fake module, with no hardware.

## Release notes

See `CHANGELOG.md` in the `aectest` package.
