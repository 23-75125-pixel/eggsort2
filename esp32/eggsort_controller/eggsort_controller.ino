#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include "HX711.h"

// =====================================================
// PCA9685
// =====================================================

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

#define SDA_PIN 21
#define SCL_PIN 22

bool pcaReady = false;

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

// How long the reject gate stays open (10 seconds)
#define CRACK_GATE_OPEN_TIME 10000

bool rejectGateOpen = false;
unsigned long rejectOpenedAt = 0;

// =====================================================
// LOAD-CELL SERVO
// =====================================================

#define LOADCELL_CLOSED 305
#define LOADCELL_OPEN 180

// Higher = slower opening
#define LOADCELL_OPEN_SPEED 15

// How long load-cell gate stays open
#define LOADCELL_OPEN_TIME 3000

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
#define SIZE_GATE_OPEN_TIME 1500

// =====================================================
// TRAVEL TIMES
// =====================================================

// Small and Medium
#define SM_TRAVEL_TIME 2000

// Large
#define LARGE_TRAVEL_TIME 8700

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

// =====================================================
// VARIABLES
// =====================================================

int lastWeight = -1;
int sameCount = 0;

bool processingEgg = false;
bool zeroPrinted = false;
bool eggDetected = false;
bool measurementAuthorized = false;

// =====================================================
// FUNCTION PROTOTYPE
// (kailangan kasi tinatawag ito ng waitWithRejectService)
// =====================================================

void handleSerialCommands(bool urgentOnly);

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

// Isasara ang reject gate kapag lumipas na ang 10 seconds
void updateRejectServo() {

  if (rejectGateOpen &&
      millis() - rejectOpenedAt >= CRACK_GATE_OPEN_TIME) {

    setServoAngle(CRACK_SERVO, CRACK_CLOSED);
    rejectGateOpen = false;

    Serial.println("REJECT SERVO: CLOSED");
  }
}

// Bubuksan ang reject gate at sisimulan ang 10 second timer
void startRejectServo() {

  setServoAngle(CRACK_SERVO, CRACK_OPEN);

  rejectOpenedAt = millis();
  rejectGateOpen = true;

  Serial.println("REJECT SERVO: OPEN; HOLD 10000 MS");
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
// =====================================================

void handleSerialCommands(bool urgentOnly) {

  while (Serial.available() > 0) {

    String command = Serial.readStringUntil('\n');
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

        Serial.print("CAMERA REJECT : ");
        Serial.println(quality);

        startRejectServo();
      }

      continue;
    }

    // Habang busy, REJECT lang ang pwede
    if (urgentOnly) {
      Serial.println("COMMAND IGNORED: BUSY");
      continue;
    }

    // Flask only authorizes a non-defective egg to leave the load cell after
    // its camera result is matched to this physical egg.
    if (command.startsWith("MEASURE:")) {
      String quality = command.substring(8);
      quality.trim();
      if (!eggDetected || processingEgg) {
        Serial.println("MEASURE FAILED: NO EGG WAITING");
      } else if (quality != "GOOD" && quality != "UNDEFINED") {
        Serial.println("MEASURE FAILED: INVALID QUALITY");
      } else {
        measurementAuthorized = true;
        Serial.print("CAMERA QUALITY: ");
        Serial.println(quality);
      }
      continue;
    }

    if (command == "STATUS") {
      Serial.print("PCA9685 READY: ");
      Serial.println(pcaReady ? "YES" : "NO");
      Serial.println("HX711 READY: YES");
      Serial.print("LOAD CELL GATE: ");
      Serial.println("CLOSED");
      Serial.print("CONTROLLER STATE: ");
      Serial.println(processingEgg ? "SORTING" : (eggDetected ? "WAITING FOR CAMERA" : "READY"));
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
// READ WEIGHT
// =====================================================

int readWeight() {

  float weight = scale.get_units(10);

  if (weight > -10 && weight < 10) {
    weight = 0;
  }

  return round(weight);
}

// =====================================================
// ACTIVATE SIZE SERVO
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
// CLOSE ALL SERVOS
// =====================================================

void closeAllServos() {

  pwm.setPWM(LOADCELL_SERVO, 0, LOADCELL_CLOSED);

  setServoAngle(CRACK_SERVO, CRACK_CLOSED);
  rejectGateOpen = false;

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
  Serial.setTimeout(100);   // para hindi mag-antay ng matagal sa serial

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

  scale.tare(25);

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

  int currentWeight = readWeight();

  // ===================================================
  // NO EGG
  // ===================================================

  if (currentWeight < EGG_THRESHOLD) {

    sameCount = 0;
    lastWeight = -1;

    if (!zeroPrinted) {
      Serial.println("WEIGHT: 0 g");
      zeroPrinted = true;
    }

    if (eggDetected) {
      Serial.println("EGG LEFT");
      eggDetected = false;
      measurementAuthorized = false;
    }

    waitWithRejectService(600);

    return;
  }

  // ===================================================
  // EGG DETECTED
  // ===================================================

  zeroPrinted = false;

  if (!eggDetected) {
    eggDetected = true;
    measurementAuthorized = false;
    Serial.println("EGG DETECTED");
  }

  Serial.print("WEIGHT: ");
  Serial.print(currentWeight);
  Serial.println(" g");

  // ===================================================
  // EXACT SAME READING CHECK
  // ===================================================

  if (currentWeight == lastWeight) {

    sameCount++;

  } else {

    lastWeight = currentWeight;
    sameCount = 1;
  }

  Serial.print("SAME READING COUNT: ");
  Serial.println(sameCount);

  // ===================================================
  // THREE IDENTICAL READINGS
  // ===================================================

  if (sameCount >= 3 && !processingEgg && measurementAuthorized) {

    processingEgg = true;

    int finalWeight = currentWeight;
    int eggSize = 0;

    Serial.println();
    Serial.println("================================");

    Serial.print("FINAL WEIGHT: ");
    Serial.print(finalWeight);
    Serial.println(" g");

    // =================================================
    // CLASSIFICATION
    // =================================================

    if (finalWeight < 45) {

      eggSize = 1;
      Serial.println("SIZE: SMALL");

    } else if (finalWeight <= 54) {

      eggSize = 2;
      Serial.println("SIZE: MEDIUM");

    } else if (finalWeight <= 62) {

      eggSize = 3;
      Serial.println("SIZE: LARGE");

    } else if (finalWeight <= 69) {

      eggSize = 4;
      Serial.println("SIZE: EXTRA LARGE");

    } else {

      eggSize = 5;
      Serial.println("SIZE: JUMBO");
    }

    Serial.println("================================");

    // =================================================
    // SLOW OPEN LOAD-CELL GATE
    // =================================================

    Serial.println();
    Serial.println("SLOW OPEN LOAD CELL GATE");

    moveLoadCellServoSlow(
      LOADCELL_CLOSED,
      LOADCELL_OPEN,
      LOADCELL_OPEN_SPEED
    );

    Serial.println("LOAD CELL GATE: OPEN");

    // =================================================
    // START TRAVEL TIMER HERE
    // =================================================

    unsigned long travelStart = millis();

    Serial.println("EGG RELEASED");
    Serial.println("TRAVEL TIMER STARTED");

    // =================================================
    // KEEP LOAD-CELL GATE OPEN
    // =================================================

    waitWithRejectService(LOADCELL_OPEN_TIME);

    // =================================================
    // FAST CLOSE LOAD-CELL GATE
    // =================================================

    Serial.println();
    Serial.println("CLOSE LOAD CELL GATE");

    pwm.setPWM(LOADCELL_SERVO, 0, LOADCELL_CLOSED);

    Serial.println("LOAD CELL GATE: CLOSED");

    // =================================================
    // SELECT TRAVEL TIME
    // =================================================

    unsigned long selectedTravelTime = 0;

    if (eggSize == 1 || eggSize == 2) {

      // SMALL / MEDIUM
      selectedTravelTime = SM_TRAVEL_TIME;

      Serial.println();
      Serial.println("S/M TRAVEL TIME: 2.0 SECONDS");

    } else if (eggSize == 3) {

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

      activateSizeServo(SMALL_SERVO, SMALL_OPEN, SMALL_CLOSED, "SMALL");
    }

    // =================================================
    // MEDIUM
    // =================================================

    else if (eggSize == 2) {

      activateSizeServo(MEDIUM_SERVO, MEDIUM_OPEN, MEDIUM_CLOSED, "MEDIUM");
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
    Serial.print("SERVO SORTED: ");
    Serial.println(completedSize);

    // =================================================
    // CHECK LOAD CELL
    // =================================================

    Serial.println();
    Serial.println("CHECKING LOAD CELL...");

    int remainingWeight = readWeight();

    while (remainingWeight >= EGG_THRESHOLD) {

      Serial.print("EGG STILL DETECTED: ");
      Serial.print(remainingWeight);
      Serial.println(" g");

      Serial.println("WAITING FOR EGG TO LEAVE...");

      waitWithRejectService(500);

      remainingWeight = readWeight();
    }

    // =================================================
    // RESET
    // =================================================

    Serial.println();
    Serial.println("NO EGG DETECTED");
    Serial.println("LOAD CELL IS CLEAR");

    sameCount = 0;
    lastWeight = -1;
    processingEgg = false;

    zeroPrinted = false;

    Serial.println();
    Serial.println("--------------------------------");
    Serial.println("READY FOR NEXT EGG");
    Serial.println("--------------------------------");

    waitWithRejectService(600);
  }

  waitWithRejectService(600);
}
