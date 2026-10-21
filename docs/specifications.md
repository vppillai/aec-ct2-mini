# Specifications

The numbers a user needs. Every value comes from the design files of the unit.

## Power

The table gives the power figures.

| Item | Value |
|---|---|
| Charger | A USB-C charger rated 5 V at 3 A (15 W), with a USB-C to USB-C cable. Plug it into the **5V 3A** port. |
| Host port supply | The **PC** port also takes 5 V from the computer. A computer port is never enough for high power. |
| Which source feeds the unit | The charger, when present. The host port then carries data only. |
| Input voltage | 5 V only. Never feed 9 V, 12 V or 20 V. The unit has no power-delivery contract. |
| Cable rail | 3.3 V, up to 2 A. The power switch (`MOD PWR` in the software) switches the rail. |
| Stand-alone draw | About 0.6 A at 5 V with a cable in low power. |
| Software budget | 6.0 W. The software releases a cable to high power only when the cable declares 6.0 W or less. |

### Power classes

A QSFP-DD cable declares a power class. The software reads it before it releases the cable. The table shows what the tester allows.

| Class | Declared maximum | The tester |
|---|---|---|
| 1 | 1.5 W | Allowed. Hood off. |
| 2 | 3.5 W | Allowed. Hood off and a fan over the heat sink. |
| 3 | 7 W | Allowed only when the cable declares 6.0 W or less. Hood off and a fan over the heat sink. |
| 4 to 7 | 8 W to 14 W | Refused. The verdict reads `OVER-JIG-BUDGET`. |
| 8 | above 14 W | Refused. |

The software watches the cable temperature every second during a powered step. It warns at the warning level of the cable. It pulls the cable back to low power at the throttle point. That point is 5 °C below the cable's alarm level, or 70 °C. The lower value wins. The verdict then reads `THERMAL`.

## Cables supported

The table lists what the tester reads and tests.

| Item | Value |
|---|---|
| Form factor | QSFP-DD. |
| Management | CMIS over the two-wire bus. We read cables with revisions 4.0 and 5.x. |
| Active electrical cable (AEC) | Yes. Both ends go into two units for the full test. |
| Optical transceiver | Yes, one per unit. For a two-end test, join two transceivers with a fibre trunk. |
| Pattern test | Runs inside the cable, when the cable offers it. The tester has no data lanes of its own. |
| What the tester does not do | No traffic from a host. No bit-error test from the host side. |

## Host

The table lists what the host computer needs.

| Item | Value |
|---|---|
| Connection | One USB 2.0 port per unit. A pair needs two ports. |
| Operating system | Tested on Linux and macOS. Not tested on Windows. |
| Linux | `aectest setup-linux` installs the device rule. |
| macOS | Install `libusb` with Homebrew before the first run. |
| Python | 3.11 or newer. |
| Software | The `aectest` package. It installs its own driver library. |

## Environment

The table gives the limits of the room and the box.

| Item | Value |
|---|---|
| Room temperature | We checked the thermal design at 25 °C and 35 °C. No wider range is specified. |
| Case material | PLA. It softens at about 55 °C. Keep the unit under 50 °C. |
| Cooling | Hood off for every powered test. A class 2 or 3 cable needs a desk fan or a USB fan over the fins. You must feel the air at the fins. |
| Heat sink temperature | Up to 70 °C during a powered test. |

## Dimensions and mass

The table gives the size and the mass of one unit.

| Item | Value |
|---|---|
| Length | 147.1 mm, hood on. |
| Width | 47.5 mm. |
| Height | 32.6 mm over the hood, 23.6 mm over the lid. |
| Circuit board | 42 mm by 136 mm. |
| Mass | We did not weigh the unit. The printed parts are about 80 g by the slicer. The board with its cage is about 54 g by calculation. |
