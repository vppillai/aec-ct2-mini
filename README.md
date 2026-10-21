# AEC-CT2-MINI cable tester

The AEC-CT2-MINI is a small USB-powered tester for QSFP-DD active electrical cables. It reads the management interface of one cable end and judges the cable against a criteria file. Two units test both ends of one cable at the same time.

## Who it is for

- A technician who receives cables and needs one word per cable: PASS, FAIL or INCONCLUSIVE.
- An engineer who wants to read a cable or run a pattern test between two ends. The same engineer can update the cable firmware.

You need no knowledge of the management protocol. The software reads the cable and explains each value.

## Quick start

1. Take the hood off. Plug a 5 V, 3 A USB-C charger into the **5V 3A** port, then the host cable into **PC**.
2. Install and start the software: `pip install ./software`, then `aectest web --open`.
3. Insert the cable end. On **Test a cable**, click **Read cable**, then **Start the test**.

[Getting started](docs/getting-started.md) has every step, from the box to the first verdict.

## Documentation

The pages live under `docs/`. They build into the site at [vppillai.github.io/aec-ct2-mini](https://vppillai.github.io/aec-ct2-mini/). The table lists them.

| Page | What it covers |
|---|---|
| [Introduction](docs/index.md) | What the tester is and a one-minute tour |
| [Getting started](docs/getting-started.md) | Unbox, power, install, first cable verdict |
| [Hardware](docs/hardware.md) | Connectors, switches, LEDs, the case |
| [Specifications](docs/specifications.md) | Power, supported cables, host requirements |
| [Safety](docs/safety.md) | Power order, the charger rule, hot surfaces |
| [Software](docs/software.md) | Install, the web UI pages, the command line |
| [Test a cable](docs/test-a-cable.md) | The guided test and what each verdict means |
| [Advanced](docs/advanced.md) | Passport, pattern tests, features, command block |
| [Firmware update](docs/firmware-update.md) | Update the cable firmware |
| [Records](docs/records.md) | Where results go and how to share one |
| [Troubleshooting](docs/troubleshooting.md) | Symptom, cause, fix |
| [Glossary](docs/glossary.md) | The terms in plain words |
| [Release notes](docs/release-notes.md) | What changed in each version |

To build the site yourself, run `pip install mkdocs-material`, then `mkdocs serve` in this folder. GitHub builds it on every push to `main`. For that, set the Pages source of the repository to **GitHub Actions**.

## Report a problem

Open an issue on [GitHub](https://github.com/vppillai/aec-ct2-mini/issues). [Troubleshooting](docs/troubleshooting.md#report-a-problem) says what to attach.

## Version

Software 0.2.0 for board revision 0 in case v4. See the [release notes](docs/release-notes.md).

## License and disclaimer

The software is under the MIT License, see `LICENSE`. The documentation is under CC BY 4.0, see `LICENSE-docs`. Read `DISCLAIMER.md` before you use anything here: no warranty, no liability, and your own checks come first.
