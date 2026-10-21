# Safety

Read this page before the first test. The rules protect the cable, the unit and your fingers.

## Power-up order

1. Leave the switches as shipped. The power switch (`MOD PWR` in the software) is at ON. **MODE** is at AUTO. The three slides are at O.
2. Take the hood off.
3. Plug the charger into the **5V 3A** port (`PWR ONLY` in the software). The **5V IN** light comes on.
4. Plug the host cable into the **PC** port. The **USB** light comes on.
5. Start the software. The **PC** light comes on when the software opens the unit.
6. Insert the cable. The **PRES** light comes on.

Power down in the reverse order. Let the software return the cable to low power first. The **LPMD** light comes on. Then pull the cable, the host cable and the charger.

## The charger rule

High power needs a charger rated 5 V at 3 A on the **5V 3A** port. A computer port cannot supply it. The unit cannot sense which supply is present. The software asks you to confirm the charger before it releases a cable to high power. Confirm only when the **5V IN** light is on.

Plug the charger in before the host cable. A charger plugged in after the host cable dips the supply for a moment.

Never unplug the charger while a cable is in high power. The load moves to the computer port. The computer port cannot carry it. The **POWER** light flickers and the cable loses power.

## Hot parts

The heat sink and the cable plug reach the temperature of the cable. That can be 70 °C. With the hood off the fins stand open.

- Do not touch the heat sink during a powered test or right after it.
- Hold the plug by its pull tab when you remove a cable.
- Let a cable cool before you pull it after a `THERMAL` stop.
- Use a fan with a finger guard. Keep cable tails out of the fan. A class 2 or higher cable needs the fan.

## Static electricity

- Handle the closed unit by its box.
- Wear a wrist strap at the bench when you handle cables.
- The cable plug makes ground first when you insert it. You need not switch the power switch off to insert a cable.
- Keep the hood on and the unit in an anti-static bag for storage.

## What never to do

- Never plug the host cable in before the charger.
- Never pull the cable, switch the power switch off, or unplug the charger while the **LPMD** light is dark.
- Never run a powered test with the hood on. The closed box cannot cool a powered cable.
- Never feed the unit from a 9 V, 12 V or 20 V supply. Both ports are 5 V only.
- Never use a computer port as the supply for high power.
- Never move a slide switch while the software runs a test.
- Never move **LPMD** off H in stand-alone mode. Without the software nothing watches the cable temperature.
- Never open the box, and never touch the board, its pins or its straps.
- Never lift the unit by the cable.
- Never put anything but a QSFP-DD plug into the cable port.

## What the software blocks for you

The table lists each check and what the software does.

| Check | What happens |
|---|---|
| No charger confirmed | The software refuses to release a cable to high power. |
| Cable declares more than 6.0 W | The software refuses to release it. The verdict reads `OVER-JIG-BUDGET`. |
| Cable gets warm | A `WARN` line at the warning level of the cable. The test continues. |
| Cable gets hot | The software pulls the cable back to low power and stops. The verdict reads `THERMAL`. |
| A switch is wrong | The pre-check names the switch. Fix it and press Retry. |
| A unit is missing | The verdict reads `JIG-UNIT-MISSING` before any step runs. |
| The jig profile records a cut strap | The software refuses the unit. Command line only. Send the unit back. |
| Cable end lost during a test | The test stops. The verdict reads `REMOVED-MID-TEST`. |
| Pattern test shorter than 60 s | The software treats it as a rehearsal, not a record. |

The software always returns a cable to low power at the end of a run. If you must stop a run, use the Stop button or Ctrl-C. Then wait for the **LPMD** light.
