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

### ESP32 buzzer for defective eggs

The firmware sounds the buzzer for one second whenever it receives
`REJECT:CRACK` or `REJECT:ROTTEN` from the camera application.

| ESP32 | Buzzer / driver | Purpose |
|---|---|---|
| GPIO `16` | Buzzer positive/input | Buzzer control |
| `GND` | Buzzer negative/driver ground | Common ground |

GPIO 16 outputs a 2 kHz tone for one second, which supports passive piezo
buzzers and most active buzzer modules. Connect the buzzer positive/input to
GPIO 16 and negative/ground to ESP32 GND only if it is rated for direct GPIO
drive. If it is a bare buzzer or its current rating is unknown, use a
transistor driver and suitable external buzzer supply; do not draw excessive
current directly from the GPIO.
This output is currently reserved for reject alerts; bin-full detection needs
a separate bin sensor input.

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
   ESP32 and confirm that `TARE COMPLETE` and `READY FOR EGG` appear.
8. Close Serial Monitor before starting Flask. Only one program can own the
   COM port at a time.

To test the load-cell gate without an egg, open Serial Monitor at 115200 baud,
set the line ending to newline, and send `LOADCELL_TEST`. Use `CRACK_TEST` for
the reject gate and `STATUS` for controller diagnostics.
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
   loads the file configured by `YOLO_MODEL_PATH` and reads its embedded class
   names directly. The live camera status shows the model's class list. The
   sorter maps recognized `Crack`, `Good`, and `Rotten` labels to actions;
   `NoEgg` and other labels remain visible as detections but do not authorize
   weighing or sorting as Good.
2. Open **Sorting Sessions** and confirm that **ESP32 link** shows
   `Connected on COM... @ 115200`.
3. Each egg is captured once when its detection center enters the marked
   center zone. After two consecutive no-egg observations, that passage exits
   the zone and increments the session egg count once. The first accepted
   Crack or Rotten detection immediately sends `REJECT:CRACK` or
   `REJECT:ROTTEN`. Channel 0 opens at the camera, before the load cell, holds
   for 6 seconds, and closes. Repeated frames of the same egg do not restart
   the timer. A separate rejected egg starts a new 6-second hold.
4. Rejected eggs are counted at zone exit but excluded from the weighing queue,
   even if later frames say Good. They receive no weight or size and do not
   create a weighed Egg Record. Instead, each gets a rejected Egg Record with
   its camera quality, weight `0` (not weighed), and size `N/A`. Database writes
   run separately from inference and retry on failure while the session is active.
   Only an egg whose YOLO bounding-box center is
   inside the centered, egg-sized auto-capture zone can trigger this flow.
   Good passages are queued once after leaving
   the zone. A frame with no detection is never counted as an egg.
5. When an egg reaches the load cell at 30 g or above, the ESP32 holds the gate
   closed until fresh HX711 readings stay within a total 2 g band for 1.5 seconds.
   A larger change or a sample gap over 500 ms restarts the timer. Removing the
   egg resets the window. It does not wait for Flask or `MEASURE:GOOD`.
   Each arrival receives an independent `CYCLE` number for its physical route.
   Flask matches that cycle to the oldest pending Good camera capture for recording.
   If the loaded egg remains in the camera zone and there is no older queued
   egg, `CAMERA_QUALITY_SAMPLES` Good observations complete its inspection in
   place (default: one observation). The same setting applies to moving and
   loaded eggs; there is no hidden three-frame requirement.
   Any defect observed before completion blocks assignment as a Good record.
   Quality is locked when inspection completes; the same passage cannot be
   captured again until it exits the zone.
6. The controller rounds the average of all readings in the stable window to determine size and
   sort automatically. It samples every available HX711 conversion throughout
   servo movement and travel waits, including after the load-cell gate closes.
   `LIVE WEIGHT` telemetry is limited to one message per 100 ms.
7. The ESP32 opens servo channel 1 (the load-cell gate), waits for the egg's
   travel time, then moves exactly one correct size gate: channel 5 for Small,
   channel 2 for Medium, channel 4 for Large, or channel 3 for Extra Large.
   It then sends `SERVO SORTED : <SIZE>` with
   the measured size so the PC retains the egg's weight, size, and quality.
8. Only after a matching size-route confirmation does Flask save one Good row in Egg
   Records. Dashboard totals update automatically on their next poll.
   Arrival, final-weight, size, sort and removal replies include `; CYCLE:<id>`.
   Weight and route must belong to the same physical cycle. Good eggs
   leave the camera queue in arrival order; Crack/Rotten eggs never enter it.
   If the camera result arrives later, the completed cycle remains pending;
   recording never invents a Good label from weight alone. Update Flask and
   upload this firmware together to enable automatic release and cycle matching.
9. Duplicate cycle events are ignored. Camera captures are assigned once in
   FIFO order and are saved through the original capture's deduplication key.

Logging out stops both the ESP32 serial bridge and camera session. If an
operator manually stops the session, **Restart Camera & ESP32** starts it again.

The load-cell gate opens automatically after 1.5 seconds of stable weight,
independently of camera authorization. Every sensor and servo is controlled through this one
ESP32.

## Weight sizes and physical routes

| Saved size | Weight | Physical route |
|---|---:|---|
| Small | below 50 g | Small chute, channel 5 |
| Medium | 50-55 g | Medium chute, channel 2 |
| Large | 56-61 g | Large chute, channel 4 |
| Extra Large | 62-66 g | Extra Large chute, channel 3 |
| Jumbo | 67 g and above | Straight/final chute |

## Calibration and mechanical tuning

The crack gate starts closed at `CRACK_CLOSED = 0` degrees. On the camera's
`REJECT:CRACK` or `REJECT:ROTTEN` command, channel 0 opens immediately to
`CRACK_OPEN = 80` degrees without waiting for load-cell detection or weighing.
For both camera qualities, the crack gate stays open for
`CRACK_GATE_OPEN_TIME = 6000` ms (6 seconds), then receives a direct command
to close to 0 degrees, matching the reference code's closing motion.
These are starting values: tune the angles and timing to the actual crack
chute. `CRACK_TEST` also exercises channel 0 without an egg.

Upload the updated firmware and restart Flask together: older firmware does
not understand `REJECT:`. With Flask stopped, send `REJECT:CRACK` or
`REJECT:ROTTEN` in Serial Monitor (115200 baud, newline) to test channel 0
without putting an egg on the scale. Expect `CAMERA REJECT`, then
`REJECT SERVO: OPEN; HOLD 6000 MS`, and `REJECT SERVO: CLOSED` after 6 seconds.
The timer and reject commands are serviced during normal weighing and size
gate travel. Camera send failures appear on the sorting page and retry while
the same egg is visible; rejected eggs are never queued for weighing.

The firmware uses `calibration_factor = 618.0`. Adjust that value in the sketch
against a known mass and upload again when calibration is needed. On reset it
tares 25 samples with the scale empty. This sketch has no serial `TARE` or
`CALIBRATE` commands and does not save calibration in flash. Stop Flask while
using Serial Monitor so the COM port is available.

All mechanism-specific values are near the top of the firmware:

- `calibration_factor`
- `LOADCELL_CLOSED` and `LOADCELL_OPEN`
- `LOADCELL_OPEN_SETTLE_TIME` (250 ms after a direct open command) and `LOADCELL_OPEN_TIME`
- each size gate's `*_CLOSED` and `*_OPEN` angles
- `SIZE_GATE_OPEN_TIME` and `SIZE_CLOSE_SPEED`
- `SM_TRAVEL_TIME`, `LARGE_TRAVEL_TIME`, and `XL_TRAVEL_TIME`

Disconnect servo power before changing linkages. Tune one gate at a time with
small angle changes so a servo is not driven against a mechanical stop.
Channel 1 remains energized at `LOADCELL_CLOSED` after closing so
it can hold the next egg. Its available holding torque comes from the external
5-6 V servo supply; software cannot increase torque beyond the supplied power.

## Troubleshooting

The controller reports `PROTOCOL: EGGSORT_AUTO_V1` at boot and in every `STATUS`
reply. Flask uses this handshake to disable obsolete camera authorization
commands. A late `MEASURE:` command is ignored by automatic firmware.
The sorting page preserves the exact `MEASURE FAILED` reason, including its
cycle ID in the serial event, rather than replacing it with a generic failure.
`SERVO SORTED` is emitted only after the route finishes and scale clearance is
observed. A clearance timeout retains the incomplete record and keeps servicing
readings and diagnostics while waiting for the egg to be removed.

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
