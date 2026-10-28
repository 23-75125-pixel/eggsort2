#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include "HX711.h"
#include "stable_weight.h"

// =====================================================
// PCA9685
// =====================================================

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

#define SDA_PIN 21
#define SCL_PIN 22

bool pcaReady = false;

// Active buzzer output. Drive a small buzzer through a transistor if its
// current exceeds the ESP32 GPIO rating; share ground with the ESP32.
#define BUZZER_PIN 16
#define BUZZER_DURATION 1000
#define BUZZER_FREQUENCY 2000
bool buzzerActive = false;
unsigned long buzzerStartedAt = 0;    

// =====================================================
// SERVO CHANNELS
// =====================================================

// Crack / Rotten reject gate
#define CRACK_SERVO 0

// Load-cell gate
#define LOADCELL_SERVO 1

// Size servos
#define SMALL_SERVO 5     // Servo 4
#define MEDIUM_SERVO 2    // Servo 3
#define LARGE_SERVO 4     // Servo 6
#define XL_SERVO 3        // Servo 5

// =====================================================
// CRACK / ROTTEN REJECT SERVO
// =====================================================

// Swap OPEN/CLOSED kung baliktad ang linkage
#define CRACK_CLOSED 0
#define CRACK_OPEN 80

// How long the reject gate stays open
#define CRACK_GATE_OPEN_TIME 6000
#define CRACK_RETRIGGER_CLOSE_TIME 150

bool rejectGateOpen = false;
bool rejectRestartPending = false;
unsigned long rejectOpenedAt = 0;
unsigned long rejectClosedAt = 0;

// =====================================================
// LOAD-CELL SERVO
// =====================================================

#define LOADCELL_CLOSED 310
#define LOADCELL_OPEN 185

// Direct movement, then allow the linkage to reach its commanded position.
#define LOADCELL_OPEN_SETTLE_TIME 250
#define LOADCELL_OPEN_DELAY_AFTER_WEIGHT 600

// How long load-cell gate stays open
#define LOADCELL_OPEN_TIME 2200
#define LOADCELL_OPEN_SPEED 5

// Commanded position only; this servo has no physical position feedback.
bool loadCellGateOpen = false;


// =====================================================
// SIZE SERVO RANGE
// =====================================================

#define SERVO_MIN 150
#define SERVO_MAX 600

// =====================================================
// SMALL - SERVO 4 / CHANNEL 5
// =====================================================

#define SMALL_CLOSED 70
#define SMALL_OPEN 0

// =====================================================
// MEDIUM - SERVO 3 / CHANNEL 2 (REVERSED)
// =====================================================

#define MEDIUM_CLOSED 0
#define MEDIUM_OPEN 70

// =====================================================
// LARGE - SERVO 6 / CHANNEL 4
// =====================================================

#define LARGE_CLOSED 80
#define LARGE_OPEN 0

// =====================================================
// XL - SERVO 5 / CHANNEL 3 (REVERSED)
// =====================================================

#define XL_CLOSED 0
#define XL_OPEN 80

// =====================================================
// SIZE SERVO SETTINGS
// =====================================================

// Higher = slower closing
#define SIZE_CLOSE_SPEED 20

// How long size gate stays open
#define SIZE_GATE_OPEN_TIME 1700

// =====================================================
// TRAVEL TIMES
// =====================================================

// Small and Medium
// Oras (mula nang mag-open ang loadcell gate) bago buksan ang size servo.
// Dapat mas maliit ito sa LOADCELL_OPEN_TIME.
#define SM_TRAVEL_TIME 1000

// Large
#define LARGE_TRAVEL_TIME 8500

// Extra Large
#define XL_TRAVEL_TIME 8500

// =====================================================
// HX711
// =====================================================

#define DOUT 19
#define CLK 18

HX711 scale;

float calibration_factor = 622.0;

#define EGG_THRESHOLD 30

// Ilang samples ang i-average sa bawat basa (mas mababa = mas mabilis)
#define WEIGHT_SAMPLES 3

// =====================================================
// VARIABLES
// =====================================================

bool processingEgg = false;
bool zeroPrinted = false;
bool eggDetected = false;
unsigned long automaticCycleId = 0;
const char* controllerFault = nullptr;
bool scaleInitialized = false;
int latestWeight = 0;
float latestRawWeight = 0;
StableWeightWindow stableWeight;
unsigned long lastStabilityReportAt = 0;
unsigned long weightSequence = 0;
unsigned long consumedWeightSequence = 0;
unsigned long lastLiveWeightAt = 0;
unsigned long lastScaleSampleAt = 0;
bool scaleFaultReported = false;

// Each measured egg keeps its own release time and cycle ID while the scale
// starts measuring the next egg. This lets sorter gates run concurrently with
// weighing instead of blocking the load-cell cycle.
#define ROUTE_QUEUE_CAPACITY 32
struct RouteJob {
  unsigned long cycleId;
  unsigned long releasedAt;
  uint8_t size;
  bool active;
};
RouteJob routeJobs[ROUTE_QUEUE_CAPACITY] = {};

struct SizeGateRuntime {
  uint8_t channel;
  int openAngle;
  int closedAngle;
  const char* name;
  bool closing;
  int currentAngle;
  unsigned long closeAt;
  unsigned long lastStepAt;
};
SizeGateRuntime sizeGates[] = {
  {SMALL_SERVO, SMALL_OPEN, SMALL_CLOSED, "SMALL", false, SMALL_CLOSED, 0, 0},
  {MEDIUM_SERVO, MEDIUM_OPEN, MEDIUM_CLOSED, "MEDIUM", false, MEDIUM_CLOSED, 0, 0},
  {LARGE_SERVO, LARGE_OPEN, LARGE_CLOSED, "LARGE", false, LARGE_CLOSED, 0, 0},
  {XL_SERVO, XL_OPEN, XL_CLOSED, "EXTRA LARGE", false, XL_CLOSED, 0, 0},
};
const size_t SIZE_GATE_COUNT = sizeof(sizeGates) / sizeof(sizeGates[0]);
bool cycleMeasurementFinalized = false;
bool loadCellReleasePending = false;
unsigned long loadCellReleaseDueAt = 0;
unsigned long loadCellGateCloseAt = 0;
unsigned long loadCellReleaseCycleId = 0;
bool cycleHasBeenReleased = false;

// =====================================================
// FUNCTION PROTOTYPE
// (kailangan kasi tinatawag ito ng waitWithRejectService)
// =====================================================

void handleSerialCommands(bool urgentOnly);
bool openLoadCellGate();
void closeLoadCellGate();
void updateBuzzer();
void soundAlertBuzzer();
void serviceScale();
void serviceLoadCellCycle();
void serviceRouteJobs();
bool sortingWorkPending() {
  if (processingEgg || loadCellGateOpen || loadCellReleasePending) return true;
  for (size_t i = 0; i < ROUTE_QUEUE_CAPACITY; ++i) {
    if (routeJobs[i].active) return true;
  }
  for (size_t i = 0; i < SIZE_GATE_COUNT; ++i) {
    if (sizeGates[i].closing) return true;
  }
  return false;
}
void printCaptureTag() {
  if (automaticCycleId > 0) {
    Serial.print("; CYCLE:");
    Serial.print(automaticCycleId);
  }
  Serial.println();
}

void printCaptureTagFor(unsigned long cycleId) {
  if (cycleId > 0) {
    Serial.print("; CYCLE:");
    Serial.print(cycleId);
  }
  Serial.println();
}

void reportMeasurementFailure(const char* reason) {
  controllerFault = reason;
  Serial.print("MEASURE FAILED: ");
  Serial.print(reason);
  printCaptureTag();
}

void soundAlertBuzzer() {
  // tone() drives passive buzzers too; a steady HIGH only works with active
  // buzzer modules and was silent with common passive piezo buzzers.
  tone(BUZZER_PIN, BUZZER_FREQUENCY);
  buzzerStartedAt = millis();
  buzzerActive = true;
}

void updateBuzzer() {
  if (buzzerActive && millis() - buzzerStartedAt >= BUZZER_DURATION) {
    noTone(BUZZER_PIN);
    buzzerActive = false;
  }
}

// =====================================================
// SET SERVO ANGLE
// =====================================================

void setServoAngle(uint8_t channel, int angle) {

  angle = constrain(angle, 0, 180);

  int pulse = map(angle, 0, 180, SERVO_MIN, SERVO_MAX);

  pwm.setPWM(channel, 0, pulse);
}

// =====================================================
// CRACK / ROTTEN REJECT SERVO FUNCTIONS
// =====================================================

// Isasara ang reject gate kapag lumipas na ang oras
void updateRejectServo() {

  // A second defect arriving while the gate is open starts a fresh cycle:
  // briefly command CLOSED so the linkage registers the change, then reopen.
  if (rejectRestartPending &&
      millis() - rejectClosedAt >= CRACK_RETRIGGER_CLOSE_TIME) {
    setServoAngle(CRACK_SERVO, CRACK_OPEN);
    rejectRestartPending = false;
    rejectGateOpen = true;
    rejectOpenedAt = millis();
    Serial.println("REJECT SERVO: REOPENED FOR NEXT EGG");
  }

  if (rejectGateOpen && !rejectRestartPending &&
      millis() - rejectOpenedAt >= CRACK_GATE_OPEN_TIME) {

    setServoAngle(CRACK_SERVO, CRACK_CLOSED);
    rejectGateOpen = false;

    Serial.println("REJECT SERVO: CLOSED");
  }
}

// Bubuksan ang reject gate at sisimulan ang timer
void startRejectServo() {

  if (rejectGateOpen && !rejectRestartPending) {
    setServoAngle(CRACK_SERVO, CRACK_CLOSED);
    rejectGateOpen = false;
    rejectRestartPending = true;
    rejectClosedAt = millis();
    Serial.println("REJECT SERVO: CLOSED FOR NEXT EGG");
    return;
  }

  // If another defect arrives during the short close interval, keep the
  // pending restart; the gate will reopen once the close pulse is registered.
  if (rejectRestartPending) return;

  setServoAngle(CRACK_SERVO, CRACK_OPEN);

  rejectOpenedAt = millis();
  rejectGateOpen = true;
  rejectRestartPending = false;

  Serial.print("REJECT SERVO: OPEN; HOLD ");
  Serial.print(CRACK_GATE_OPEN_TIME);
  Serial.println(" MS");
}

// =====================================================
// WAIT (kapalit ng delay)
// Habang naghihintay, chine-check pa rin ang serial
// at ang reject timer, para gumana ang REJECT kahit
// nagso-sort ang ibang servo.
// =====================================================

void waitWithRejectService(unsigned long duration) {

  unsigned long startedAt = millis();

  do {
    handleSerialCommands(true);
    updateRejectServo();
    updateBuzzer();
    serviceScale();
    delay(1);
  } while (millis() - startedAt < duration);
}

// Test: buksan ang gate at hintayin hanggang magsara
void activateCrackServo() {

  startRejectServo();

  while (rejectGateOpen) {
    waitWithRejectService(1);
  }
}

// =====================================================
// SERIAL COMMANDS
// urgentOnly = true  -> REJECT lang ang tinatanggap
//                       (habang nagso-sort)
// urgentOnly = false -> lahat ng commands
//
// REJECT:CRACK | REJECT:ROTTEN | MEASURE:GOOD | MEASURE:UNDEFINED | STATUS
// LOADCELL_CLOSE | LOADCELL_TEST (only when no egg cycle is active)
// =====================================================

void handleSerialCommands(bool urgentOnly) {

  // Consume only complete lines. readStringUntil can block all servo timers
  // for the serial timeout when a USB command arrives in separate packets.
  static String commandBuffer;
  static bool commandOverflow = false;
  while (Serial.available() > 0) {
    char incoming = Serial.read();
    if (incoming == '\r') continue;
    if (incoming != '\n') {
      if (commandBuffer.length() < 80 && !commandOverflow) commandBuffer += incoming;
      else commandOverflow = true;
      continue;
    }
    String command = commandBuffer;
    commandBuffer = "";
    if (commandOverflow) {
      commandOverflow = false;
      Serial.println("COMMAND REJECTED: TOO LONG");
      continue;
    }
    command.trim();
    command.toUpperCase();

    if (command.length() == 0) {
      continue;
    }

    // ---------- REJECT:CRACK / REJECT:ROTTEN ----------
    if (command.startsWith("REJECT:")) {

      String quality = command.substring(7);
      quality.trim();

      if (quality != "CRACK" && quality != "ROTTEN") {

        Serial.println("REJECT FAILED: INVALID QUALITY");

      } else if (!pcaReady) {

        Serial.println("REJECT FAILED: PCA9685 NOT FOUND");

      } else {

        soundAlertBuzzer();

        Serial.print("CAMERA REJECT : ");
        Serial.println(quality);

        startRejectServo();
      }

      continue;
    }

    // Old application instances may still send camera authorization. Automatic
    // release has no authorization state; never turn a late command into a fault.
    if (command.startsWith("MEASURE:")) {
      Serial.println("COMMAND IGNORED: AUTOMATIC RELEASE ACTIVE");
      continue;
    }

    // Habang busy, REJECT at STATUS lang ang pwede
    if (urgentOnly && command != "STATUS") {
      Serial.println("COMMAND IGNORED: BUSY");
      continue;
    }

    if (command == "STATUS") {
      Serial.println("PROTOCOL: EGGSORT_AUTO_V1");
      Serial.print("PCA9685 READY: ");
      Serial.println(pcaReady ? "YES" : "NO");
      Serial.print("HX711 READY: ");
      Serial.println(scaleInitialized && millis() - lastScaleSampleAt < 1500 ? "YES" : "NO");
      Serial.print("LOAD CELL GATE: ");
      Serial.println(loadCellGateOpen ? "OPEN" : "CLOSED");
      Serial.println("LOAD CELL POSITION: COMMANDED; NO POSITION SENSOR");
      Serial.print("CONTROLLER STATE: ");
      Serial.println(controllerFault ? "FAULT" : (sortingWorkPending() ? "SORTING" : "READY"));
      if (controllerFault) reportMeasurementFailure(controllerFault);
      continue;
    }

    if (command == "LOADCELL_CLOSE" || command == "LOADCELL_TEST") {
      if (!pcaReady) {
        Serial.println("LOAD CELL COMMAND REJECTED: PCA9685 NOT FOUND");
      } else if (sortingWorkPending() || eggDetected) {
        Serial.println("LOAD CELL COMMAND REJECTED: REMOVE EGG AND FINISH CYCLE FIRST");
      } else if (command == "LOADCELL_CLOSE") {
        closeLoadCellGate();
        Serial.print("LOAD CELL CLOSE PWM: ");
        Serial.println(LOADCELL_CLOSED);
      } else {
        Serial.println("LOAD CELL TEST STARTED");
        if (openLoadCellGate()) {
          waitWithRejectService(LOADCELL_OPEN_TIME);
          closeLoadCellGate();
          Serial.println("LOAD CELL TEST COMPLETE");
        }
      }
      continue;
    }

    // ---------- CRACK_TEST ----------
    if (command == "CRACK_TEST") {

      if (processingEgg) {
        Serial.println("CRACK TEST REJECTED: EGG CYCLE ACTIVE");
      } else if (!pcaReady) {
        Serial.println("CRACK TEST REJECTED: PCA9685 NOT FOUND");
      } else {
        Serial.println("CRACK TEST STARTED");
        activateCrackServo();
        Serial.println("CRACK TEST COMPLETE");
      }

      continue;
    }

    Serial.print("UNKNOWN COMMAND : ");
    Serial.println(command);
  }
}

// =====================================================
// SLOW SIZE SERVO MOVEMENT
// =====================================================

void moveServoSlow(
  uint8_t channel,
  int startAngle,
  int targetAngle,
  int stepDelay
) {

  if (startAngle < targetAngle) {

    for (int angle = startAngle; angle <= targetAngle; angle++) {
      setServoAngle(channel, angle);
      waitWithRejectService(stepDelay);
    }

  } else {

    for (int angle = startAngle; angle >= targetAngle; angle--) {
      setServoAngle(channel, angle);
      waitWithRejectService(stepDelay);
    }
  }
}

// =====================================================
// SLOW LOAD-CELL SERVO OPEN
// =====================================================

void moveLoadCellServoSlow(
  int startPulse,
  int targetPulse,
  int stepDelay
) {

  if (startPulse > targetPulse) {

    for (int pulse = startPulse; pulse >= targetPulse; pulse--) {
      pwm.setPWM(LOADCELL_SERVO, 0, pulse);
      waitWithRejectService(stepDelay);
    }

  } else {

    for (int pulse = startPulse; pulse <= targetPulse; pulse++) {
      pwm.setPWM(LOADCELL_SERVO, 0, pulse);
      waitWithRejectService(stepDelay);
    }
  }
}

// =====================================================
// LOAD-CELL GATE CONTROL
// =====================================================

// Opens the load-cell gate during the automatic sorting cycle.
bool openLoadCellGate() {
  if (!pcaReady) {
    Serial.println("LOAD CELL GATE FAILED: PCA9685 NOT FOUND");
    return false;
  }

  for (int pos = LOADCELL_CLOSED; pos >= LOADCELL_OPEN; pos--) {
    pwm.setPWM(LOADCELL_SERVO, 0, pos);
    waitWithRejectService(LOADCELL_OPEN_SPEED);
  }

  waitWithRejectService(LOADCELL_OPEN_SETTLE_TIME);

  loadCellGateOpen = true;
  Serial.println("LOAD CELL GATE: OPEN");
  return true;
}

// Closes the load-cell gate immediately after its hold period.
void closeLoadCellGate() {

  if (pcaReady) {
    pwm.setPWM(LOADCELL_SERVO, 0, LOADCELL_CLOSED);
    loadCellGateOpen = false;
    Serial.println("LOAD CELL GATE: CLOSED");
  }
}

// =====================================================
// READ WEIGHT
// (3 samples na lang para mabilis ang basa)
// =====================================================

int readWeight() {

  float weight = scale.get_units(WEIGHT_SAMPLES);

  if (weight > -10 && weight < 10) {
    weight = 0;
  }

  return round(weight);
}

// Use one HX711 sample while waiting for the released egg to clear. Averaging
// three samples here can hide the short gap before the next roller-fed egg.
int readWeightFast() {

  float weight = scale.get_units(1);

  if (weight > -10 && weight < 10) {
    weight = 0;
  }
  latestRawWeight = weight;

  return round(weight);
}

// A ready HX711 conversion is consumed exactly once, including during travel,
// servo motion and gate holds. Never wait inside get_units for a missing ADC.
void serviceScale() {
  if (!scaleInitialized) return;
  if (!scale.is_ready()) {
    if (!scaleFaultReported && millis() - lastScaleSampleAt >= 1500) {
      Serial.println("HX711 READY: NO");
      scaleFaultReported = true;
    }
    return;
  }
  latestWeight = readWeightFast();
  lastScaleSampleAt = millis();
  weightSequence++;
  if (scaleFaultReported) {
    Serial.println("HX711 READY: YES");
    scaleFaultReported = false;
  }

  if (millis() - lastLiveWeightAt >= 100) {
    Serial.print("LIVE WEIGHT: ");
    Serial.print(latestWeight);
    Serial.println(" g");
    lastLiveWeightAt = millis();
  }
}

// =====================================================
// ACTIVATE SIZE SERVO (Large / XL)
// =====================================================

void activateSizeServo(
  uint8_t channel,
  int openAngle,
  int closedAngle,
  const char* sizeName
) {

  Serial.println();

  Serial.print(sizeName);
  Serial.println(" EGG ARRIVING");

  // OPEN FAST
  Serial.print("OPENING ");
  Serial.print(sizeName);
  Serial.println(" SERVO");

  setServoAngle(channel, openAngle);

  waitWithRejectService(SIZE_GATE_OPEN_TIME);

  // CLOSE SLOWLY
  Serial.print("SLOWLY CLOSING ");
  Serial.print(sizeName);
  Serial.println(" SERVO");

  moveServoSlow(channel, openAngle, closedAngle, SIZE_CLOSE_SPEED);

  Serial.print(sizeName);
  Serial.println(" SERVO: CLOSED");
}

// =====================================================
// FINISH SIZE SERVO (Small / Medium)
// Nabuksan na ang servo nang maaga, isasara na lang
// pagkatapos ng SIZE_GATE_OPEN_TIME
// =====================================================

void finishSizeServo(
  uint8_t channel,
  int openAngle,
  int closedAngle,
  const char* sizeName,
  unsigned long openedAt
) {

  // Hintayin ang natitirang open time
  while (millis() - openedAt < SIZE_GATE_OPEN_TIME) {
    waitWithRejectService(10);
  }

  // CLOSE SLOWLY
  Serial.print("SLOWLY CLOSING ");
  Serial.print(sizeName);
  Serial.println(" SERVO");

  moveServoSlow(channel, openAngle, closedAngle, SIZE_CLOSE_SPEED);

  Serial.print(sizeName);
  Serial.println(" SERVO: CLOSED");
}

// =====================================================
// CLOSE ALL SERVOS
// =====================================================

void closeAllServos() {

  closeLoadCellGate();

  setServoAngle(CRACK_SERVO, CRACK_CLOSED);
  rejectGateOpen = false;
  rejectRestartPending = false;

  setServoAngle(SMALL_SERVO, SMALL_CLOSED);
  setServoAngle(MEDIUM_SERVO, MEDIUM_CLOSED);
  setServoAngle(LARGE_SERVO, LARGE_CLOSED);
  setServoAngle(XL_SERVO, XL_CLOSED);
}

// =====================================================
// SETUP
// =====================================================

void setup() {

  Serial.begin(115200);
  Serial.println("PROTOCOL: EGGSORT_AUTO_V1");
  Serial.setTimeout(100);   // para hindi mag-antay ng matagal sa serial

  pinMode(BUZZER_PIN, OUTPUT);
  noTone(BUZZER_PIN);

  Wire.begin(SDA_PIN, SCL_PIN);

  // Check muna kung nakikita ang PCA9685
  Wire.beginTransmission(0x40);
  pcaReady = (Wire.endTransmission() == 0);

  if (pcaReady) {

    pwm.begin();
    pwm.setPWMFreq(50);

    delay(500);

    // DEFAULT = ALL CLOSED (kasama na ang crack servo)
    closeAllServos();
  }

  Serial.println();
  Serial.println("================================");
  Serial.println("EGGSOR+ SORTING TEST");
  Serial.println("================================");

  Serial.print("PCA9685 READY: ");
  Serial.println(pcaReady ? "YES" : "NO");

  Serial.println("REJECT SERVO: CLOSED");
  Serial.println("LOAD CELL GATE: CLOSED");
  Serial.println("SMALL SERVO: CLOSED");
  Serial.println("MEDIUM SERVO: CLOSED");
  Serial.println("LARGE SERVO: CLOSED");
  Serial.println("XL SERVO: CLOSED");

  // ===================================================
  // HX711
  // ===================================================

  scale.begin(DOUT, CLK);

  scale.set_scale(calibration_factor);

  Serial.println();
  Serial.println("REMOVE ALL WEIGHT");

  delay(2500);

  if (!scale.wait_ready_timeout(2000)) {
    Serial.println("HX711 READY: NO");
    reportMeasurementFailure("HX711 STARTUP TIMEOUT; RESTART AFTER CHECKING WIRING");
    return;
  }
  long tareTotal = 0;
  for (int sample = 0; sample < 25; sample++) {
    if (!scale.wait_ready_timeout(500)) {
      Serial.println("HX711 READY: NO");
      reportMeasurementFailure("HX711 TARE TIMEOUT; RESTART AFTER CHECKING WIRING");
      return;
    }
    tareTotal += scale.read();
  }
  scale.set_offset(tareTotal / 25);
  scaleInitialized = true;
  lastScaleSampleAt = millis();
  Serial.println("HX711 READY: YES");

  Serial.println("TARE COMPLETE");

  Serial.println();
  Serial.println("--------------------------------");
  Serial.println("READY FOR EGG");
  Serial.println("Commands: REJECT:CRACK | REJECT:ROTTEN | CRACK_TEST");
  Serial.println("--------------------------------");
}

// =====================================================
// LOOP
// =====================================================

int routeTravelTime(uint8_t eggSize) {
  if (eggSize == 1 || eggSize == 2) return SM_TRAVEL_TIME;
  if (eggSize == 3) return LARGE_TRAVEL_TIME;
  if (eggSize == 4) return XL_TRAVEL_TIME;
  return 0;
}

bool queueRoute(unsigned long cycleId, uint8_t eggSize) {
  for (size_t i = 0; i < ROUTE_QUEUE_CAPACITY; ++i) {
    if (!routeJobs[i].active) {
      routeJobs[i] = {cycleId, 0, eggSize, true};
      return true;
    }
  }
  controllerFault = "SORT QUEUE FULL; STOP FEEDING EGGS AND RESTART";
  Serial.print("MEASURE FAILED: ");
  Serial.print(controllerFault);
  printCaptureTagFor(cycleId);
  return false;
}

void serviceSizeGateClosures() {
  const unsigned long now = millis();
  for (size_t i = 0; i < SIZE_GATE_COUNT; ++i) {
    SizeGateRuntime& gate = sizeGates[i];
    if (!gate.closing || now - gate.closeAt < SIZE_GATE_OPEN_TIME ||
        now - gate.lastStepAt < SIZE_CLOSE_SPEED) continue;
    gate.lastStepAt = now;
    if (gate.currentAngle < gate.closedAngle) gate.currentAngle++;
    else if (gate.currentAngle > gate.closedAngle) gate.currentAngle--;
    setServoAngle(gate.channel, gate.currentAngle);
    if (gate.currentAngle == gate.closedAngle) {
      gate.closing = false;
      Serial.print(gate.name);
      Serial.println(" SERVO: CLOSED");
    }
  }
}

void serviceRouteJobs() {
  const unsigned long now = millis();
  for (size_t i = 0; i < ROUTE_QUEUE_CAPACITY; ++i) {
    RouteJob& job = routeJobs[i];
    if (!job.active || job.releasedAt == 0 ||
        (long)(now - job.releasedAt) < routeTravelTime(job.size)) continue;

    const char* sizeName = job.size == 1 ? "SMALL" : job.size == 2 ? "MEDIUM" :
      job.size == 3 ? "LARGE" : job.size == 4 ? "EXTRA LARGE" : "JUMBO";
    if (job.size >= 1 && job.size <= 4) {
      SizeGateRuntime& gate = sizeGates[job.size - 1];
      if (!gate.closing && gate.currentAngle != gate.openAngle) {
        Serial.print("OPENING ");
        Serial.print(gate.name);
        Serial.println(" SERVO");
      }
      setServoAngle(gate.channel, gate.openAngle);
      gate.currentAngle = gate.openAngle;
      gate.closing = true;
      gate.closeAt = now;
      gate.lastStepAt = now;
      Serial.print(gate.name);
      Serial.println(" EGG ARRIVING");
    } else {
      Serial.println("JUMBO GOES STRAIGHT");
    }
    Serial.print("SERVO SORTED: ");
    Serial.print(sizeName);
    printCaptureTagFor(job.cycleId);
    job.active = false;
  }
  serviceSizeGateClosures();
}

void serviceLoadCellCycle() {
  const unsigned long now = millis();

  static int pos = LOADCELL_CLOSED;
  static unsigned long lastStep = 0;

  if (loadCellReleasePending && (long)(now - loadCellReleaseDueAt) >= 0) {
    if (!pcaReady) {
      controllerFault = "LOAD CELL GATE UNAVAILABLE; PCA9685 NOT FOUND AT 0x40";
      Serial.print("MEASURE FAILED: ");
      Serial.print(controllerFault);
      printCaptureTagFor(loadCellReleaseCycleId);
      loadCellReleasePending = false;
      pos = LOADCELL_CLOSED;
      return;
    }

    if (now - lastStep < LOADCELL_OPEN_SPEED) return;
    lastStep = now;

    pwm.setPWM(LOADCELL_SERVO, 0, pos);

    if (pos > LOADCELL_OPEN) {
      pos--;
      return;
    }

    pos = LOADCELL_CLOSED;
    loadCellGateOpen = true;
    cycleHasBeenReleased = true;
    loadCellGateCloseAt = now + LOADCELL_OPEN_TIME;

    for (size_t i = 0; i < ROUTE_QUEUE_CAPACITY; ++i) {
      if (routeJobs[i].active &&
          routeJobs[i].cycleId == loadCellReleaseCycleId) {
        routeJobs[i].releasedAt = now + LOADCELL_OPEN_SETTLE_TIME;
        break;
      }
    }

    Serial.println("LOAD CELL GATE: OPEN");
    Serial.println("EGG RELEASED");
    Serial.println("TRAVEL TIMER STARTED");
    loadCellReleasePending = false;
  }

  if (loadCellGateOpen && (long)(now - loadCellGateCloseAt) >= 0) {
    const unsigned long releasedCycleId = loadCellReleaseCycleId;
    closeLoadCellGate();

    if (eggDetected && automaticCycleId == releasedCycleId) {
      Serial.print("EGG LEFT");
      printCaptureTagFor(releasedCycleId);
      eggDetected = false;
      processingEgg = false;
      cycleMeasurementFinalized = false;
      cycleHasBeenReleased = false;
      stableWeight.reset();
    }
  }
}

void loop() {
  handleSerialCommands(false);
  updateRejectServo();
  updateBuzzer();
  serviceScale();
  if (scaleInitialized && consumedWeightSequence != weightSequence) {
    consumedWeightSequence = weightSequence;
    const int currentWeight = latestWeight;
    const unsigned long now = millis();

    if (currentWeight < EGG_THRESHOLD) {
      stableWeight.reset();
      if (!zeroPrinted) {
        Serial.println("WEIGHT: 0 g");
        zeroPrinted = true;
      }
      if (eggDetected && cycleHasBeenReleased) {
        Serial.print("EGG LEFT");
        printCaptureTag();
        eggDetected = false;
        processingEgg = false;
        cycleMeasurementFinalized = false;
        cycleHasBeenReleased = false;
      }
    } else {
      zeroPrinted = false;
      if (!eggDetected) {
        eggDetected = true;
        processingEgg = true;
        cycleMeasurementFinalized = false;
        cycleHasBeenReleased = false;
        automaticCycleId++;
        Serial.print("EGG DETECTED");
        printCaptureTag();
      }
      Serial.print("WEIGHT: ");
      Serial.print(currentWeight);
      Serial.println(" g");

      if (!cycleMeasurementFinalized) {
        const bool weightReady = stableWeight.add(latestRawWeight, now);
        if (!weightReady && now - lastStabilityReportAt >= 250) {
          Serial.print("WEIGHT STABILITY: ");
          Serial.print(stableWeight.elapsed(now));
          Serial.print("/1000 ms");
          printCaptureTag();
          lastStabilityReportAt = now;
        }
        if (weightReady && !controllerFault) {
          const unsigned long cycleId = automaticCycleId;
          const int finalWeight = round(stableWeight.average());
          const uint8_t eggSize = finalWeight <= 55 ? 1 : finalWeight <= 60 ? 2 :
            finalWeight <= 65 ? 3 : finalWeight <= 70 ? 4 : 5;
          const char* sizeName = eggSize == 1 ? "SMALL" : eggSize == 2 ? "MEDIUM" :
            eggSize == 3 ? "LARGE" : eggSize == 4 ? "EXTRA LARGE" : "JUMBO";

          if (!queueRoute(cycleId, eggSize)) {
            cycleMeasurementFinalized = true;
            return;
          }

          Serial.print("FINAL WEIGHT: ");
          Serial.print(finalWeight);
          Serial.print(" g");
          printCaptureTagFor(cycleId);
          Serial.print("SIZE: ");
          Serial.print(sizeName);
          printCaptureTagFor(cycleId);
          cycleMeasurementFinalized = true;
          loadCellReleaseCycleId = cycleId;
          loadCellReleaseDueAt = now + LOADCELL_OPEN_DELAY_AFTER_WEIGHT;
          loadCellReleasePending = true;
          Serial.println("LOAD CELL WEIGHT CONFIRMED; GATE OPENS IN 1 SECOND");
        }
      }
    }
  }

  serviceLoadCellCycle();
  serviceRouteJobs();
  delay(1);
}
