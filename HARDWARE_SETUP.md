# EggSort+ ESP32 hardware setup

EggSort+ now uses one ESP32 for the HX711 load cell, the PCA9685 servo board,
the load-cell release gate, and all four actuated size gates. The ESP32 talks
to the Flask application over its USB serial connection at 115200 baud.

The firmware is:

`esp32/eggsort_controller/eggsort_controller.ino`

## Required parts

- ESP32 development board (the firmware targets the common ESP32 Dev Module)
- HX711 load-cell amplifier and load cell
- PCA9685 16-channel PWM servo driver
- Six servos: load-cell, Crack, Small, Medium, Large, and Extra Large gates
- Regulated 5-6 V servo power supply sized for the combined servo stall current
- USB data cable for the ESP32
- Common ground wiring and suitable terminal blocks/connectors

Do not power the servos from the ESP32 3.3 V pin or USB 5 V pin. Servo current
can reset or damage the controller. Use the external servo supply on PCA9685
`V+`, and connect all grounds together.

## Wiring

### ESP32 to HX711

| ESP32 | HX711 | Purpose |
|---|---|---|
| `3V3` | `VCC` | HX711 logic/power |
| `GND` | `GND` | Common ground |
| GPIO `19` | `DT` / `DOUT` | Load-cell data |
| GPIO `18` | `SCK` / `CLK` | Load-cell clock |

Connect the load cell to `E+`, `E-`, `A+`, and `A-` on the HX711 according to
the load-cell manufacturer's diagram. Wire colors are not standardized.

### ESP32 to PCA9685 logic

| ESP32 | PCA9685 | Purpose |
|---|---|---|
| `3V3` | `VCC` | PCA9685 logic voltage |
| `GND` | `GND` | Common ground |
| GPIO `21` | `SDA` | I2C data |
| GPIO `22` | `SCL` | I2C clock |

Leave the PCA9685 at its default I2C address `0x40`. If the board exposes
`OE`, connect it to ground or leave it in the module's enabled default state.

### Servo power and channels

Connect the external regulated supply positive output to PCA9685 `V+` and its
negative output to PCA9685 `GND`. Connect that same ground to ESP32 `GND`.

| PCA9685 channel | Gate |
|---:|---|
| 0 | Shared reject gate (camera quality `CRACK` or `ROTTEN`, any weight size) |
| 1 | Load-cell release gate |
| 5 | Small gate (Peewee also uses this physical chute) |
| 2 | Medium gate |
| 4 | Large gate |
| 3 | Extra Large gate |
| none | Jumbo continues straight to the final chute |

On a standard servo connector, brown/black is normally ground, red is servo
power, and orange/yellow/white is signal. Confirm the markings for each servo.

## ESP32 firmware setup and upload

1. Install Arduino IDE 2.x.
2. In Boards Manager, install **esp32 by Espressif Systems**.
3. In Library Manager, install:
   - **Adafruit PWM Servo Driver Library**
   - **Adafruit BusIO**
   - **HX711 Arduino Library**
4. Open `esp32/eggsort_controller/eggsort_controller.ino`.
5. Select **ESP32 Dev Module** (or the exact ESP32 board being used).
6. Select its COM port and upload.
7. Open Serial Monitor at **115200 baud**. With the scale empty, reset the
   ESP32 and confirm that `Egg Sorting Ready` appears.
8. Close Serial Monitor before starting Flask. Only one program can own the
   COM port at a time.

To test every servo without an egg, open Serial Monitor at 115200 baud, set
the line ending to newline, and send `SERVO_TEST`. The controller moves channel
1, then channels 0, 5, 2, 4, and 3 in sequence and returns every gate to closed.
Stop Flask first so Serial Monitor can open the COM port.

If no port appears, use a known USB data cable and install the driver for the
board's USB-to-serial chip, commonly CP210x or CH340.

## Application configuration

The project `.env` is configured for the ESP32 firmware:

```dotenv
AUTO_START_SORTING_ON_LOGIN=1
ESP32_BAUD_RATE=115200
ESP32_PORT=
```

When exactly one USB serial controller is attached, `ESP32_PORT` can remain
blank. To specify it, list the ports in PowerShell:

```powershell
Get-CimInstance Win32_SerialPort | Select-Object DeviceID, Description
```

Then set the detected port, for example:

```dotenv
ESP32_PORT=COM6
```

Restart Flask whenever `.env` changes.

## Run and verify the complete system

From the project folder:

```powershell
.\.venv\Scripts\python.exe app.py
```

1. Open `http://127.0.0.1:5000` and sign in. A successful login starts the
   camera, YOLO detector, and ESP32 serial bridge automatically. Camera startup
   loads the file configured by `YOLO_MODEL_PATH` and rejects the session unless
   its embedded classes are exactly `Crack`, `Good`, and `Rotten` in that
   order. A frame with no detection is treated as no egg.
2. Open **Sorting Sessions** and confirm that **ESP32 link** shows
   `Connected on COM... @ 115200`.
3. Each egg is captured once when its detection center enters the marked
   center zone. After two consecutive no-egg observations, that passage exits
   the zone and increments the session egg count once. The first accepted
   Crack or Rotten detection immediately sends `REJECT:CRACK` or
   `REJECT:ROTTEN`. Channel 0 opens at the camera, before the load cell, holds
   for 10 seconds, and closes. Repeated frames of the same egg do not restart
   the timer. A separate rejected egg starts a new 10-second hold.
4. Rejected eggs are counted at zone exit but excluded from the weighing queue,
   even if later frames say Good. They receive no weight or size and do not
   create a weighed Egg Record. Only an egg whose YOLO bounding-box center is
   inside the centered, egg-sized auto-capture zone can trigger this flow.
   Good passages are queued once after leaving
   the zone. A frame with no detection is never counted as an egg.
5. When an accepted egg reaches the load cell, its gate stays closed and the ESP32
   sends `Egg Detected`. Flask matches it to the oldest queued zone exit capture
   and sends `MEASURE:<QUALITY>` with that capture number retained in diagnostics.
6. After Flask sends `MEASURE:GOOD`, the ESP32 takes two consecutive identical rounded gram readings and calculates their
   average as the final weight and size. Each reading averages five HX711
   conversions for quicker response. The second matching reading automatically
   triggers sorting; no separate route command is required.
7. The ESP32 opens servo channel 1 (the load-cell gate), waits for the egg's
   travel time, then moves exactly one correct size gate: channel 5 for Small,
   channel 2 for Medium, channel 4 for Large, or channel 3 for Extra Large.
   It then sends `SERVO SORTED : <SIZE>` with
   the measured size so the PC retains the egg's weight, size, and quality.
8. Only after that hardware confirmation does Flask save one row in Egg
   Records. Dashboard totals update automatically on their next poll.
9. Duplicate controller messages are ignored until `Egg Left` resets the
    cycle, so one physical egg cannot create multiple records.

Logging out stops both the ESP32 serial bridge and camera session. If an
operator manually stops the session, **Restart Camera & ESP32** starts it again.

The load-cell gate opens automatically after a stable weight is measured and
the egg is classified. Every sensor and servo is controlled through this one
ESP32.

## Weight sizes and physical routes

| Saved size | Weight | Physical route |
|---|---:|---|
| Small | below 45 g | Small chute, channel 5 |
| Medium | 45-54 g | Medium chute, channel 2 |
| Large | 55-62 g | Large chute, channel 4 |
| Extra Large | 63-69 g | Extra Large chute, channel 3 |
| Jumbo | 70 g and above | Straight/final chute |

## Calibration and mechanical tuning

The crack gate starts closed at `CRACK_CLOSED = 0` degrees. On the camera's
`REJECT:CRACK` or `REJECT:ROTTEN` command, channel 0 opens immediately to
`CRACK_OPEN = 80` degrees without waiting for load-cell detection or weighing.
For both camera qualities, the crack gate stays open for
`CRACK_GATE_OPEN_TIME = 10000` ms (10 seconds), then receives a direct command
to close to 0 degrees, matching the reference code's closing motion.
These are starting values: tune the angles and timing to the actual crack
chute. `SERVO_TEST` also exercises channel 0 without an egg.

Upload the updated firmware and restart Flask together: older firmware does
not understand `REJECT:`. With Flask stopped, send `REJECT:CRACK` or
`REJECT:ROTTEN` in Serial Monitor (115200 baud, newline) to test channel 0
without putting an egg on the scale. Expect `CAMERA REJECT`, then
`REJECT SERVO: OPEN; HOLD 10000 MS`, and `REJECT SERVO: CLOSED` after 10 seconds.
The timer and reject commands are serviced during normal weighing and size
gate travel. Camera send failures appear on the sorting page and retry while
the same egg is visible; rejected eggs are never queued for weighing.

The firmware starts with an HX711 calibration factor of `622.0` until you
calibrate it for your own load cell. With the scale empty, send `TARE` in
Serial Monitor and wait for `TARE COMPLETE`. Place a known mass on the scale,
then send `CALIBRATE:<grams>` (for example, `CALIBRATE:100` for a 100 g mass).
Wait for `CALIBRATION COMPLETE` and check `LIVE WEIGHT`. The new scale factor
is saved on the ESP32 and reused after a restart. Remove the mass before
sorting. Stop Flask while using Serial Monitor so the COM port is available.

All mechanism-specific values are near the top of the firmware:

- `DEFAULT_CALIBRATION_FACTOR` (used only until the first calibration)
- `LOADCELL_CLOSED` and `LOADCELL_OPEN`
- `LOADCELL_OPEN_SPEED`, `LOADCELL_CLOSE_SPEED`, and
  `LOADCELL_CLOSE_SETTLE_TIME`
- each size gate's `*_CLOSED` and `*_OPEN` angles
- `SIZE_GATE_OPEN_TIME`, `SIZE_GATE_CLOSE_DELAY`,
  `LARGE_SIZE_GATE_CLOSE_DELAY`, `SIZE_CLOSE_SPEED`,
  `LARGE_SIZE_OPEN_SPEED`, and `LARGE_SIZE_CLOSE_SPEED`
- `SM_TRAVEL_TIME`, `LARGE_TRAVEL_TIME`, and `EXTRA_LARGE_TRAVEL_TIME`

Disconnect servo power before changing linkages. Tune one gate at a time with
small angle changes so a servo is not driven against a mechanical stop.
Channel 1 remains energized at `LOADCELL_CLOSED` after its settling delay so
it can hold the next egg. Its available holding torque comes from the external
5-6 V servo supply; software cannot increase torque beyond the supplied power.

## Troubleshooting

- **Access denied on COM port:** close the IDE Serial Monitor and any other
  serial program, then restart the sorting session.
- **ESP32 repeatedly disconnects or resets:** use a separate servo supply,
  verify common ground, and check the supply's current capacity.
- **No controller found:** set `ESP32_PORT` explicitly and confirm the CP210x
  or CH340 driver is installed.
- **Weight never becomes ready:** check HX711 wiring and calibration, and make
  sure the egg exceeds the 30 g detection threshold.
- **Wrong chute:** verify PCA9685 channel wiring first, then tune gate angles
  and travel time constants.
- **No servo moves after weighing:** check Serial Monitor for
  `SERVO ERROR: PCA9685 NOT FOUND AT 0x40`, verify the external 5-6 V servo
  supply, and confirm that PCA9685 ground and ESP32 ground are connected.
- **Record saves but gate does not move:** check the latest hardware event for
  `SORT:...`, then verify PCA9685 power, common ground, address `0x40`, and the
  channel mapping above.
