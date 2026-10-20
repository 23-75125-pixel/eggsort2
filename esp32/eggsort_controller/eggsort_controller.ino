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

#define LOADCELL_CLOSED 305
#define LOADCELL_OPEN 190

// Direct movement, then allow the linkage to reach its commanded position.
#define LOADCELL_OPEN_SETTLE_TIME 250

// How long load-cell gate stays open
#define LOADCELL_OPEN_TIME 2500

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

float calibration_factor = 618.0;

#define EGG_THRESHOLD 30

// Ilang samples ang i-average sa bawat basa (mas mababa = mas mabilis)
#define WEIGHT_SAMPLES 3

// =====================================================
// VARIABLES
// =====================================================

bool processingEgg = false;
bool zeroPrinted = false;
bool eggDetected = false;
bool measurementAuthorized = false;
unsigned long measurementCaptureId = 0;
unsigned long automaticCycleId = 0;
const char* controllerFault = nullptr;
bool scaleInitialized = false;
bool releasedEggCleared = false;
bool watchingReleasedEgg = false;
int latestWeight = 0;
float latestRawWeight = 0;
StableWeightWindow stableWeight;
unsigned long lastStabilityReportAt = 0;
unsigned long weightSequence = 0;
unsigned long consumedWeightSequence = 0;
unsigned long lastLiveWeightAt = 0;
unsigned long lastScaleSampleAt = 0;
bool scaleFaultReported = false;

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
void printCaptureTag() {
  if (automaticCycleId > 0) {
    Serial.print("; CYCLE:");
    Serial.print(automaticCycleId);
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
      Serial.println(controllerFault ? "FAULT" : (processingEgg ? "SORTING" : "READY"));
      if (controllerFault) reportMeasurementFailure(controllerFault);
      continue;
    }

    if (command == "LOADCELL_CLOSE" || command == "LOADCELL_TEST") {
      if (!pcaReady) {
        Serial.println("LOAD CELL COMMAND REJECTED: PCA9685 NOT FOUND");
      } else if (processingEgg || eggDetected) {
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

  pwm.setPWM(LOADCELL_SERVO, 0, LOADCELL_OPEN);
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
  if (watchingReleasedEgg && latestWeight < EGG_THRESHOLD) releasedEggCleared = true;
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

void loop() {

  // Laging i-check ang serial at reject timer
  handleSerialCommands(false);
  updateRejectServo();
  updateBuzzer();

  serviceScale();
  if (!scaleInitialized || consumedWeightSequence == weightSequence) {
    delay(1);
    return;
  }
  consumedWeightSequence = weightSequence;
  int currentWeight = latestWeight;

  // ===================================================
  // NO EGG
  // ===================================================

  if (currentWeight < EGG_THRESHOLD) {
    stableWeight.reset();

    if (!zeroPrinted) {
      Serial.println("WEIGHT: 0 g");
      zeroPrinted = true;
    }

    if (eggDetected) {
      Serial.print("EGG LEFT");
      printCaptureTag();
      eggDetected = false;
      measurementAuthorized = false;
      measurementCaptureId = 0;
    }

    delay(1);

    return;
  }

  // ===================================================
  // EGG DETECTED
  // ===================================================

  zeroPrinted = false;

  if (!eggDetected) {
    eggDetected = true;
    measurementAuthorized = false;
    measurementCaptureId = 0;
    automaticCycleId++;
    Serial.print("EGG DETECTED");
    printCaptureTag();
  }

  Serial.print("WEIGHT: ");
  Serial.print(currentWeight);
  Serial.println(" g");

  // ===================================================
  // STABLE WEIGHT FOR 1.5 SECONDS
  // ===================================================

  // Restart the hold if weight changes or ADC samples stop. Final weight is
  // the average of the complete stable window, not a transient first reading.
  const bool weightReady = stableWeight.add(latestRawWeight, millis());
  if (!weightReady && millis() - lastStabilityReportAt >= 250) {
    Serial.print("WEIGHT STABILITY: ");
    Serial.print(stableWeight.elapsed(millis()));
    Serial.print("/1500 ms");
    printCaptureTag();
    lastStabilityReportAt = millis();
  }
  if (weightReady && !processingEgg && !controllerFault) {

    processingEgg = true;

    int finalWeight = round(stableWeight.average());
    int eggSize = 0;

    Serial.println();
    Serial.println("================================");

    Serial.print("FINAL WEIGHT: ");
    Serial.print(finalWeight);
    Serial.print(" g");
    printCaptureTag();

    // =================================================
    // CLASSIFICATION
    // =================================================

    if (finalWeight < 50) {

      eggSize = 1;
      Serial.print("SIZE: SMALL");
      printCaptureTag();

    } else if (finalWeight <= 55) {

      eggSize = 2;
      Serial.print("SIZE: MEDIUM");
      printCaptureTag();

    } else if (finalWeight <= 61) {

      eggSize = 3;
      Serial.print("SIZE: LARGE");
      printCaptureTag();

    } else if (finalWeight <= 66) {

      eggSize = 4;
      Serial.print("SIZE: EXTRA LARGE");
      printCaptureTag();

    } else {

      eggSize = 5;
      Serial.print("SIZE: JUMBO");
      printCaptureTag();
    }

    Serial.println("================================");

    // =================================================
    // OPEN LOAD-CELL GATE IMMEDIATELY
    // =================================================

    Serial.println();
    Serial.println("OPEN LOAD CELL GATE");

    releasedEggCleared = false;
    watchingReleasedEgg = true;
    if (!openLoadCellGate()) {
      watchingReleasedEgg = false;
      measurementAuthorized = false;
      reportMeasurementFailure("LOAD CELL GATE UNAVAILABLE; PCA9685 NOT FOUND AT 0x40");
      // Latch the failed cycle until reset; do not repeatedly release/count it.
      return;
    }

    // =================================================
    // START TRAVEL TIMER HERE
    // =================================================

    unsigned long travelStart = millis();
    unsigned long sizeOpenedAt = 0;

    Serial.println("EGG RELEASED");
    Serial.println("TRAVEL TIMER STARTED");

    // =================================================
    // KEEP LOAD-CELL GATE OPEN
    // Para sa Small/Medium: bubuksan ang size servo
    // habang bukas pa ang load-cell gate
    // =================================================

    if (eggSize == 1 || eggSize == 2) {

      // Hintayin ang S/M travel time (2 seconds)
      while (millis() - travelStart < SM_TRAVEL_TIME) {
        waitWithRejectService(10);
      }

      // Buksan agad ang size servo
      Serial.println();

      if (eggSize == 1) {
        Serial.println("SMALL EGG ARRIVING");
        Serial.println("OPENING SMALL SERVO");
        setServoAngle(SMALL_SERVO, SMALL_OPEN);
      } else {
        Serial.println("MEDIUM EGG ARRIVING");
        Serial.println("OPENING MEDIUM SERVO");
        setServoAngle(MEDIUM_SERVO, MEDIUM_OPEN);
      }

      sizeOpenedAt = millis();

      // Tapusin ang natitirang load-cell open time
      while (millis() - travelStart < LOADCELL_OPEN_TIME) {
        waitWithRejectService(10);
      }

    } else {

      waitWithRejectService(LOADCELL_OPEN_TIME);
    }

    // =================================================
    // FAST CLOSE LOAD-CELL GATE
    // =================================================

    Serial.println();
    Serial.println("CLOSE LOAD CELL GATE");

    closeLoadCellGate();

    // =================================================
    // SELECT TRAVEL TIME (Large / XL lang)
    // =================================================

    unsigned long selectedTravelTime = 0;

    if (eggSize == 3) {

      // LARGE
      selectedTravelTime = LARGE_TRAVEL_TIME;

      Serial.println();
      Serial.println("LARGE TRAVEL TIME: 8.7 SECONDS");

    } else if (eggSize == 4) {

      // EXTRA LARGE
      selectedTravelTime = XL_TRAVEL_TIME;

      Serial.println();
      Serial.println("XL TRAVEL TIME: 8.5 SECONDS");
    }

    // =================================================
    // WAIT UNTIL TRAVEL TIME IS REACHED
    // =================================================

    if (selectedTravelTime > 0) {

      Serial.println("EGG TRAVELLING...");

      while (millis() - travelStart < selectedTravelTime) {
        waitWithRejectService(10);
      }

      Serial.println("TRAVEL TIME REACHED");
    }

    // =================================================
    // SMALL
    // =================================================

    if (eggSize == 1) {

      finishSizeServo(SMALL_SERVO, SMALL_OPEN, SMALL_CLOSED, "SMALL", sizeOpenedAt);
    }

    // =================================================
    // MEDIUM
    // =================================================

    else if (eggSize == 2) {

      finishSizeServo(MEDIUM_SERVO, MEDIUM_OPEN, MEDIUM_CLOSED, "MEDIUM", sizeOpenedAt);
    }

    // =================================================
    // LARGE
    // =================================================

    else if (eggSize == 3) {

      activateSizeServo(LARGE_SERVO, LARGE_OPEN, LARGE_CLOSED, "LARGE");
    }

    // =================================================
    // EXTRA LARGE
    // =================================================

    else if (eggSize == 4) {

      activateSizeServo(XL_SERVO, XL_OPEN, XL_CLOSED, "EXTRA LARGE");
    }

    // =================================================
    // JUMBO
    // =================================================

    else if (eggSize == 5) {

      Serial.println();
      Serial.println("JUMBO EGG");
      Serial.println("NO SIZE SERVO ACTION");
      Serial.println("JUMBO GOES STRAIGHT");
    }

    const char* completedSize = eggSize == 1 ? "SMALL" :
      eggSize == 2 ? "MEDIUM" : eggSize == 3 ? "LARGE" :
      eggSize == 4 ? "EXTRA LARGE" : "JUMBO";

    // =================================================
    // CHECK LOAD CELL
    // =================================================

    Serial.println();
    Serial.println("CHECKING LOAD CELL...");

    unsigned long clearWaitStarted = millis();
    bool releaseTimedOut = false;
    while (!releasedEggCleared) {
      waitWithRejectService(1);
      if (!releaseTimedOut && millis() - clearWaitStarted >= 10000) {
        reportMeasurementFailure("RELEASE NOT CLEARED; CHECK SCALE OR BLOCKED EGG");
        releaseTimedOut = true;
      }
    }
    watchingReleasedEgg = false;
    if (!releaseTimedOut) {
      Serial.print("SERVO SORTED: ");
      Serial.print(completedSize);
      printCaptureTag();
    }

    // =================================================
    // RESET
    // =================================================

    Serial.println();
    Serial.println("NO EGG DETECTED");
    Serial.println("LOAD CELL IS CLEAR");

    // Emit the cycle boundary here, while the cleared scale reading is known.
    // The next egg may arrive before loop() gets another below-threshold read.
    if (eggDetected) {
      Serial.print("EGG LEFT");
      printCaptureTag();
      eggDetected = false;
    }
    measurementAuthorized = false;
    measurementCaptureId = 0;

    processingEgg = false;
    stableWeight.reset();

    zeroPrinted = false;

    Serial.println();
    Serial.println("--------------------------------");
    Serial.println("READY FOR NEXT EGG");
    Serial.println("--------------------------------");

    // Resume the next load-cell detection immediately. A long cooldown here
    // can skip the brief clear interval between consecutive roller-fed eggs.
    consumedWeightSequence = weightSequence;
  }

  delay(1);
}
