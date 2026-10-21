# aectest

The test software of the AEC-CT2-MINI cable tester: the web UI and the command line.

## Install

1. Install Python 3.11 or newer.
2. Run `pip install .` in this folder.
3. Linux only: run `aectest setup-linux` for the udev rule, then plug the PC port again.

## Start

- `aectest web --open` starts the web UI in your browser.
- `aectest --help` lists the commands. `aectest --version` prints the version.
- `aectest web --dry-run` starts the web UI on a fake pair of units, with no hardware.

Every command accepts `--help`. The help text is the reference. This page is the map.

## Commands

| Command | What it does |
|---|---|
| `aectest web` | The browser UI on localhost. `--open` opens the browser. `--port N` picks the port. |
| `aectest tech run` | The guided test of one cable: checklist, insert, identity, soak, PRBS, verdict, report. |
| `aectest tech last` | Print the newest report. |
| `aectest inspect start` | Test a lot: scan, test, scan, until you type `done`. An existing lot continues. |
| `aectest inspect resume` | Record an override and lift a stop in a lot. |
| `aectest inspect summary` | Write `lot.csv` and `lot.md` from the sessions of a lot. |
| `aectest inspect export` | The summary, then one zip of the lot folder for engineering. |
| `aectest calibrate` | Statistics over golden cables and a PROPOSED criteria file. See [Calibrate](#calibrate). |
| `aectest bringup VERB` | One bench command on one unit. See [Bench commands](#bench-commands). |
| `aectest rd VERB` | A recorded engineering session, one verb per call. See [R&D session](#rd-session). |
| `aectest unit eeprom FILE` | Hash a unit EEPROM read-back into the unit record. |
| `aectest selftest` | The built-in checks on simulated units. No hardware. |
| `aectest setup-linux` | Install the udev rule and reload udev. Linux only. `--dry-run` prints the commands. |
| `aectest --version` | Print the version. |

`aectest-tech` is the same program with the technician commands only: `tech`, `inspect`, `--list` and `selftest`.

## Connection flags

These flags select the unit and the bus. They apply to `bringup`, `rd open` and `web`.

| Flag | Meaning |
|---|---|
| `--list` | List the units on this computer and exit. `bringup` only. |
| `--serial NAME` | The unit to use. Required when more than one unit is attached. |
| `--speed auto\|100k\|400k\|1M` | The I2C clock. `auto` opens at 100 kHz and ramps to 400 kHz when the bus passes a stress test. |
| `--ramp-rounds N` | The stress rounds the ramp needs. Default 20. |
| `--dry-run` | A fake unit and a fake cable. No hardware. |
| `--fake-sku SKU` | With `--dry-run`: the fake cable answers with this image. |
| `--pid 0xNNNN` | A custom USB product id. Default `0x6010`. |

## Permission flags

High power needs two flags on the command. The software refuses without them.

| Flag | Meaning |
|---|---|
| `--release-lpmode` | Permission 1 of 2. You ask for high power. |
| `--i-am-on-usbc-power` | Permission 2 of 2. A 3 A USB-C charger is on the **5V 3A** port of every unit in the test. |
| `--budget-w W` | Refuse high power when the cable asks for more than W watts. Default 6.0. |
| `--external-temp-ack` | The cable has no temperature monitor. A person watches a thermocouple on the heat sink. |
| `--posture hood-on\|hood-off\|fan\|forced-air` | The cooling posture of the unit. It sets the temperature limits. |
| `--temp-limit-c C`, `--temp-warn-c C`, `--temp-throttle-c C` | Override one temperature limit. Recorded. |

The commands that need the permission flags: `release-lpmode`, `prbs`, `media-link`, `trend`, `loopback`, `lane-integrity`, `si-sweep`, `flag-matrix`, and `rd release`, `rd hold`, `rd prbs`, `rd lpmode`.

## Bench commands

`aectest bringup VERB [--serial NAME] [--json] [--out DIR]`. One verb, one unit, one result. `--json` prints the result as JSON on stdout. `--out DIR` sets the record folder.

### Read-only

| Verb | What it does |
|---|---|
| `status` | The control pins and the module state. |
| `id` | The identity of the cable: vendor, part, serial, firmware, power class. |
| `dump` | The raw pages as hex, into a record file. |
| `decode` | Every advertised page, decoded by field. `--dump FILE` decodes a saved record, no unit needed. |
| `flags` | The module and lane flags, with the clear-on-read behaviour. |
| `features` | The feature matrix: what the cable advertises. `--exercise all` tries each feature. `--pair A:B` adds the high-power ones. |
| `cdb query\|features\|fw-features\|pm-features\|bert-features\|fw-info` | One read-only command block query. `cdb raw 0xNNNN [hexbytes]` sends one raw command. |
| `fw-info` | The firmware banks: running, committed, versions. |
| `audit` | The compliance audit of the cable against the management standard: required pages, fields and values. |
| `timing` | The state machine timings against the advertised maxima. The power steps need the permission flags. |
| `records` | Index the record files. `--by-cable` groups them by cable. `--root DIR` adds a folder. |

### Control

| Verb | What it does |
|---|---|
| `reset` | Pulse the reset line and time the first answer. |
| `watch` | Wait for an insertion and time the first answer. |
| `release-lpmode` | Release low power, time the module to ready, return to low power. Needs the permission flags. |
| `idle` | Hold the unit open with the safe pin values. `--hold S` sets the time. |
| `tristate` | Release the I2C lines and exit. For a measurement with another master. |
| `i2c-stress` | N rounds of page reads: NACKs, page mismatches, time per read. `--count N`. |
| `cdb-password` | Enter the host password from `--password` and show the command block status. Never echoed. |
| `shell` | An interactive shell. One verb per line on one open port. |

### Tests on a pair

The pair tests take `--pair A:B`, the serials of the two units. The generator end is A. The checker end is B.

| Verb | What it does |
|---|---|
| `prbs` | PRBS both ways between the two ends. `--pattern`, `--time S`, `--ber-limit`, `--lane-mask 0xNN`. |
| `media-link` | Both ends to high power. Watch the data path state and the media flags for `--time` seconds. |
| `trend` | One PRBS direction, A to B, sampled every second at the checker. |
| `loopback` | The far end loops its media input. A sends and checks. |
| `lane-integrity` | A PRBS window per lane, one lane at a time. |
| `si-sweep` | A set of signal settings applied at one end, with a BER window each. |
| `flag-matrix` | Provoke each fault at the checker end and record the flags it raises. |

### Firmware

| Verb | What it does |
|---|---|
| `fw-update --image FILE --plan` | Read the image header and the banks. Print the plan. Write nothing. |
| `fw-update --image FILE --run-image` | Download the image to the inactive bank and run it. Not committed. |
| `fw-update --image FILE --commit --confirm TEXT` | Commit the running image. `TEXT` is the word the plan asks for. |
| `fw-abort` | Abort a download in progress. |

`--header-bytes N`, `--image-version X.Y`, `--per-firmware` and `--force` are the expert flags. Read the help before you use them.

### The cable database

| Verb | What it does |
|---|---|
| `derive-criteria` | Write a PROPOSED criteria file from the records of this cable. Engineering confirms it by hand. |

## R&D session

`aectest rd VERB`. The session records every verb, every override and every acknowledgement into one folder. `open` starts it. `close` ends it. The connection flags go on `open`. The later verbs reuse them.

| Verb | What it does |
|---|---|
| `open` | Open a session and the unit. |
| `id` | Identity, the advertised durations and the firmware version. |
| `pages` | Raw pages as hex, into a record file. |
| `dump` | The pages 00, 01, 02, 10 and 11. |
| `thresholds` | The module and lane thresholds of page 02. |
| `vdm` | The VDM advertisement, descriptors and samples. Read-only. |
| `cdb` | The command block status and advertisement. Read-only. |
| `release` | Release low power, time the module to ready, return to low power. |
| `hold` | Release and hold high power for S seconds. |
| `prbs` | PRBS both ways between two units. |
| `reset` | Pulse the reset line and time the first answer. |
| `watch` | Wait for an insertion and time the first answer. |
| `lpmode` | Drive the low-power line for a timing capture. |
| `script FILE` | One verb per line on one open port. |
| `log start`, `log stop` | A health poll every second into the session: temperature, voltage, flags. |
| `close` | Stop a running log, write the manifest, end the session. |

## Calibrate

`aectest calibrate --sku NAME --sessions DIR [DIR ...]`

The command reads every session manifest under the folders. It takes the golden cables, computes the statistics and writes a PROPOSED candidate criteria file. Engineering confirms the file by hand.

| Flag | Meaning |
|---|---|
| `--sku NAME` | The criteria file name, or a path. |
| `--lot DIR` | A lot folder instead of `--sessions`. |
| `--known-bad SN ...` | The known-bad cables. They stay out of the statistics. The candidate must fail them. |
| `--min N` | Golden cables needed. Default 10. `--force` runs under it and records that. |
| `--check CANDIDATE` | Judge every session under this candidate instead of writing one. |
| `--out FILE` | The candidate file. |

## Records

Every command writes its records under `out/test` unless `--out` says otherwise. A session has `events.jsonl`, `health.csv` and `manifest.json`. A guided test has `report.md`. `aectest bringup records` indexes them.

## Release notes

See `CHANGELOG.md` in the `aectest` package.
