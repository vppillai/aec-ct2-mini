# Troubleshooting

One table per area. Find the symptom, read the cause, do the fix. When the fix does not help, see [Report a problem](#report-a-problem).

## Power and LEDs

| Symptom | Cause | What to do |
|---|---|---|
| **5V IN** is dark with the charger in | The charger has no power, or it is in the wrong port. | Check the charger. Use the **5V 3A** port (`PWR ONLY` in the software). |
| **USB** is dark with the host cable in | The host cable is in **5V 3A**. | Move it to the **PC** port. |
| **USB** is lit but **PC** stays dark | The host has not opened the unit. | Start the software. On Linux, run `aectest setup-linux` and plug the unit again. |
| **POWER** flickers | The 5 V source is too weak. The rail restarts. | Use a 5 V, 3 A charger on **5V 3A**. Never a computer port alone for high power. |
| **PRES** or **INT** is lit with no cable | Normal while the power switch (`MOD PWR`) is off. | Ignore it unless **POWER** is lit. |
| **LPMD** is dark while on a charger alone | The cable is in high power without a host. | Not allowed. Plug the host and let the software return the cable to low power. |

## Connection

| Symptom | Cause | What to do |
|---|---|---|
| `No unit on the host` | The **PC** port is not connected, or another program holds the unit. | Plug the **PC** port. Close the other program. Click **Refresh units**. |
| `no FTDI device found` on the command line | The USB chip of the unit is not on the bus, or the rules are missing. | Check the cable. On Linux, run `aectest setup-linux`. |
| `Unit disconnected` or `No such device` | The unit left the USB bus during a run. | Unplug the host cable and the charger. Wait 30 s. Plug the host cable. Click **Refresh units**. |
| `Server not reachable` | The server process stopped. | Start it again. The page reconnects on its own. |
| `Server restarted` toast | The server was started again. | Nothing. The page reloads and keeps your form values. |
| A second program is refused with a pid | The server owns the unit. | Stop the server before a command-line run on the same unit. |

## Reading a cable

| Symptom | Cause | What to do |
|---|---|---|
| `NACK from slave` | The cable did not answer. **MODE** is at MAN and the **MSEL** slide deselects the cable. | Set **MODE** to AUTO. Click **Read cable** again. |
| `pins low: module present or rail off` | The cable rail is off, so the pins read low. | Set the power switch (`MOD PWR`) to ON. Read the cable. |
| `interrupt latched since insert` | The cable raised its flags at insertion. | Read the cable. The read clears them and names them. |
| `interrupt pending` after a read | The cable holds a flag after the read. | Open the flags on the Read tab. A real alarm shows there. |
| The clock badge says `100 kHz, fallback` | The cable did not answer cleanly at the faster clock. | Nothing. The unit stays at the slower clock for this cable. |
| The plate shows a class above the budget | The cable needs more than 6 W. | The unit cannot test it in high power. Read-only commands still work. |

## The guided test

| Symptom | Cause | What to do |
|---|---|---|
| **Start the test** stays grey | The cable is not read yet. | Click **Read cable** first. |
| `No criteria for this cable type yet` | No file matches the part number. | **Test without criteria**, or derive a file on the Advanced page and ask engineering to confirm it. |
| The pre-check names a switch | A switch is off its shipped position. | Set it as the card says. Click **Retry**. |
| `Label serial differs` | The scanned label and the cable serial do not match. | Scan once more. A real mismatch is a FAIL by design. |
| `Cable removed during the test` | An end came out of the cage. | Seat it again. Start the test over. |
| `Connection lost during the run` | The server stopped mid-run. | Wait for **Live** in the corner. Start the test again. |
| `Criteria proposed, not confirmed` | The criteria file is a proposal. | Hand the record to engineering. Do not bin the cable. |
| INCONCLUSIVE with a code | See the code table. | [Test a cable](test-a-cable.md) lists every code with its fix. |

## High power and temperature

| Symptom | Cause | What to do |
|---|---|---|
| A yellow WARN line | The cable reached its own warning level. | Nothing. The test continues. Check the fan. |
| THROTTLE, then INCONCLUSIVE THERMAL | The cable came within 5 °C of its alarm level, or reached 70 °C. | Let it cool. Put air over the heat sink. Run again. |
| THERMAL repeats with the fan running | The cable runs hot on its own. | Hand the cable to engineering with the record. |
| `Budget refused` | The cable declares more power than the budget. | Raise the budget with a reason on Advanced, or test a cable within the budget. |
| `Refused: confirmations` | A confirmation box is not ticked. | Tick both boxes before a high-power command. |

## Firmware

| Symptom | Cause | What to do |
|---|---|---|
| Every firmware command answers status `46h` | The cable wants the host password. | Type the password. Try again. |
| **Plan** and **Download** stay grey | No image file is chosen. | Choose the file. |
| `RUN REFUSED` | The cable refused to run the new image. | The old image runs. The new one sits in its bank. Try **Run** again or use the vendor tool. |
| `MODULE SILENT` after Run | The cable did not answer after the reset. | Power cycle the unit. The committed image boots. |
| `The firmware update engine is not in this build` | The engine is not installed. | Firmware info still works. Ask for a build with the engine. |

More on each case: [Firmware update](firmware-update.md).

## Report a problem

Open an issue on [GitHub](https://github.com/vppillai/aec-ct2-mini/issues). One issue per problem. Attach:

- the unit serial `CT2M-NN` and the cable serial,
- the output of `aectest --version`,
- the report from the verdict card, or the session folder as a zip (see [Records](records.md)),
- the terminal output of the run,
- for a wrong page, the page name and the sentence.
