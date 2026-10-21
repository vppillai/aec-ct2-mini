# Release notes

One section per software version, newest first. `aectest --version` prints the version you run. The Help page shows it too.

## 0.2.0, 2026-10-21

The first public release.

- The web UI: Cable, Test a cable, Advanced, Records, Help and Settings.
- The guided test with eight steps, a live chart and a verdict card with the next step.
- The cable passport: identity, applications, test features, thresholds and firmware information.
- Decode every page: every register of the cable with its name and meaning.
- The cable database: one criteria file per part number, and a derived file from a plugged cable.
- Pattern tests between two ends, trend and loopback runs, with a lane table and charts.
- The feature matrix: every advertised feature in one of four states, with an exercise per row.
- Command block queries and the firmware update: plan, download, run, commit.
- Records grouped by cable, with charts for every record type.
- The command line: the same functions, plus a lot loop and a lot export.
- Simulation mode on simulated units with `--dry-run`.
- The management bus clock earns 400 kHz per cable after 20 clean rounds, else it stays at 100 kHz.
