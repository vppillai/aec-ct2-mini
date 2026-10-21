# Glossary

The terms the tester and its pages use, in plain words. The same lines appear behind the `?` bubbles in the web UI.

| Term | Meaning |
|---|---|
| AEC | Active electrical cable. A copper cable with a chip in each plug that cleans up the signal. |
| Application | One advertised way to run the cable: a host interface, a media interface and the lane counts. |
| Bank | One of two firmware slots in a cable end. One runs, the other can take a new image. |
| BER | Bit error ratio: errors divided by bits during a pattern test. |
| CDB | Command data block: the command interface of the cable for firmware queries and updates. |
| Clock ramp rounds | The clean reads a unit needs at the fast bus clock before it stays there. 20 by default. |
| CMIS | The management interface specification the cable implements. The version decides which pages exist. |
| Committed | The firmware bank the cable boots at the next reset. |
| Criteria file | The limits one cable type must meet. One file per part number. |
| Data path | The set of lanes that carry traffic together. Each lane reports a state. |
| End A, end B | The two plugs of one cable, in unit A and unit B. |
| Flags | Latched alarm and warning bits. A set bit means the condition happened since the last read. |
| FRU | The small record in the unit's own memory: product, serial, revision, build. |
| FTDI | The USB chip in the unit. The command line names it when it finds no unit. |
| High power | The working state of the cable. It draws its full power. |
| Hollow feature | A feature the cable advertises but answers with a placeholder value. |
| Host interface | The electrical interface on the switch side, for example 400GAUI-8. |
| INCONCLUSIVE | The test could not finish. The cable is not judged. |
| Jig profile | The signed file that names the two units for a command-line test. |
| Label serial | The serial on the cable label. The test compares it with the serial inside the cable. |
| Lane mask | Which lanes a run measures, as a hex mask. Bit 0 is lane 1. |
| Loopback | A path that sends a pattern out of one end and back through the other. |
| Low power | The safe state of the cable, under 1.5 W. A cable starts here. |
| Loss of lock | The checker lost the pattern on a lane. Read once per second during a trend. |
| MaxPower | The highest power the cable declares. The unit refuses a cable above its budget. |
| Media interface | The interface on the cable side, for example an active cable class. |
| MgmtInit | The time from power up until the cable answers on the management bus. |
| Module | The chip side of one cable end, as the management interface sees it. |
| NACK | The cable did not answer on the management bus. |
| Passport | The full description of one cable end at one time, as a file. |
| PASS, FAIL | Every criterion met, or the cable produced the evidence of a miss. |
| Plate | The identity card of one cable end: vendor, part number, serial, power class, features. |
| Power budget | The power the unit can give a cable: 6 W. |
| Power class | The cable's power class, 1 to 8. Each class has a maximum power. |
| PRBS | A known test pattern. One end generates it, the other checks it. |
| Reason code | One short code per miss, for example `BER-LANE-n`. |
| Record | A file the tester wrote. Never edited. |
| Report | The one-page summary of a run, written as text. |
| Running | The firmware bank the cable executes now. |
| Session | One guided test and its folder of records. |
| SNR | Signal to noise ratio per lane in dB, as the cable measures it. |
| Soak | The time the cable stays in high power while the test watches it. 120 s by default. |
| Throttle | The software pulls the cable back to low power because it got too hot. |
| Unit | One tester. Its serial reads `CT2M-NN`. Unit A is the one with the lower serial. |
| Vcc | The supply voltage the cable measures at its pins. |
| VDM | Extra monitors some cables advertise. Some report zeros. |
| Verdict | PASS, FAIL or INCONCLUSIVE with its reason codes. |
| WARN | The cable reached its warning temperature. The test continues. |
