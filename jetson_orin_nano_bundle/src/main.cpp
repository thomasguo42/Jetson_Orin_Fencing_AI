#include <Arduino.h>

// --- Analog Sensor Pins ---
const uint8_t PIN_LB = A0;  // Left Blade
const uint8_t PIN_LL = A1;  // Left Lamé
const uint8_t PIN_RB = A2;  // Right Blade
const uint8_t PIN_RL = A3;  // Right Lamé

// --- Driver Pins (Digital Outputs) ---
const uint8_t PIN_LB_DRV = 2;
const uint8_t PIN_LL_DRV = 3;
const uint8_t PIN_RB_DRV = 4;
const uint8_t PIN_RL_DRV = 5;

// --- Optional Physical LEDs ---
const int LED_LEFT  = 8;
const int LED_RIGHT = 9;
const int LED_B2B   = 10;

// --- Sensitivity / Timing Tuning ---
const int SAMPLE_AVG              = 3;
const int HIT_CLOSE_COUNTS        = 45;  // Reduced from 65 for higher sensitivity
const int HIT_DEV_MIN_COUNTS      = 28;  // Reduced from 40 for higher sensitivity
const int HIT_CLOSE_COUNTS_FAST   = 20;  // Reduced from 30 for higher sensitivity
const int HIT_DEV_MIN_FAST        = 12;  // Reduced from 20 for higher sensitivity
const int B2B_CLOSE_COUNTS        = 55;  // Reduced from 80 for higher sensitivity
const int B2B_DEV_MIN_COUNTS      = 25;  // Reduced from 35 for higher sensitivity
const int B2B_CLOSE_COUNTS_FAST   = 28;  // Reduced from 40 for higher sensitivity
const int B2B_DEV_MIN_FAST        = 12;  // Reduced from 18 for higher sensitivity
const int SELF_CLOSE_COUNTS       = 45;  // Reduced from 65 for higher sensitivity
const int SELF_DEV_MIN_COUNTS     = 28;  // Reduced from 40 for higher sensitivity
const int CONFIRM_SAMPLES         = 2;
const unsigned long SABER_LOCKOUT_MS = 200;
const unsigned long PRINT_EVERY_MS   = 80;

// --- Fast-touch detector tuning ---
const uint16_t SETTLE_US    = 1100;
const uint8_t  BURST_READS  = 10;
const uint16_t BURST_GAP_US = 100;
const uint16_t DISCHARGE_US = 250;
const uint16_t LATCH_MS     = 320;
const uint16_t LOCKOUT_MS   = 170;

// Threshold tables (listener order: 0=LB,1=LL,2=RB,3=RL)
int ABS_HI[4] = { 390, 390, 390, 390 };    // Reduced from 460 for higher sensitivity
int DELTA_HI[4] = { 95, 95, 95, 95 };      // Reduced from 120 for higher sensitivity
int PAIR_BOOST[4][4] = {
  { 0,   0, +50, 0 },
  { 0,   0,   0, +50 },
  { +50, 0,   0, 0 },
  { 0,   +50,   0, 0 }
};

const uint8_t DRIVER_NODES[4] = { PIN_LB_DRV, PIN_LL_DRV, PIN_RB_DRV, PIN_RL_DRV };
const uint8_t SENSE_NODES[4]  = { PIN_LB, PIN_LL, PIN_RB, PIN_RL };

// --- Scoring UI Timing ---
const unsigned long LED_HOLD_MILLISECONDS = 3000;
const unsigned long DETECTION_DEBOUNCE_MS = 15;

// --- State Machine Definition ---
enum class State {
  WAITING_FOR_COMMAND,
  RECORDING,
  LOCKOUT_PERIOD,
  DISPLAYING_RESULTS
};
State currentState = State::WAITING_FOR_COMMAND;

// --- Detection Event Types ---
enum class DetectionEvent {
  NONE,
  LEFT_HIT,
  RIGHT_HIT,
  SIMULTANEOUS,
  BLADE_TO_BLADE,
  LEFT_SELF,
  RIGHT_SELF
};

enum class FastEvent : uint8_t {
  NONE         = 0,
  LEFT         = 1,
  RIGHT        = 2,
  CLASH        = 3,
  SIMULTANEOUS = 4
};

// --- Baseline & Detection Counters ---
int baseLB = 0, baseLL = 0, baseRB = 0, baseRL = 0;
int cntLeftHit = 0, cntRightHit = 0, cntB2B = 0, cntLeftSelf = 0, cntRightSelf = 0;
unsigned long lastEventMs = 0;
DetectionEvent pendingHit = DetectionEvent::NONE;
uint32_t pendingHitTimeMs = 0;
uint32_t pendingHitTimeUs = 0;

FastEvent fastLatched = FastEvent::NONE;
uint32_t fastLatchUntil = 0;
uint32_t fastLockoutUntil = 0;
int fastBaseline[4] = {0, 0, 0, 0};

// --- Debugging ---
bool DEBUG_PRINT = false;
unsigned long lastPrintMs = 0;

// --- Timing and Scoring Variables ---
uint32_t phraseStartMillis = 0;
uint32_t phraseStartMicros = 0;
unsigned long lockoutStartTime = 0;
bool fencer1Scored = false;
bool fencer2Scored = false;
bool fencer1LedActive = false;
bool fencer2LedActive = false;
bool b2bLedActive = false;
unsigned long fencer1LedOnTime = 0;
unsigned long fencer2LedOnTime = 0;
unsigned long b2bLedOnTime = 0;

// --- Helper Prototypes ---
int readAvg(uint8_t pin, int samples = SAMPLE_AVG);
inline int absi(int value) { return value < 0 ? -value : value; }
void calibrateBaselines();
DetectionEvent detectEvent();
void processDetectionEvent(DetectionEvent event);
void startLockoutWindow();
void resetDetectionCounters();
bool shouldRecordEvents() { return (currentState == State::RECORDING || currentState == State::LOCKOUT_PERIOD); }
void logEventAt(uint32_t eventUs, const String& message);
void logPendingSingleHit();

void driversHiZ();
void driversLow();
void dischargeNodes();
void driveNodeHigh(uint8_t pin);
int avgReadsFast(uint8_t aPin, uint8_t samples, uint16_t gapUs);
void measureFastBaseline(int base[4]);
bool detectPairFast(uint8_t drv, uint8_t lst, const int base[4]);
void scanOrderFast(const uint8_t order[4], bool hit[4][4], const int base[4]);
FastEvent classifyFast(const bool hit[4][4]);
FastEvent runFastDetector(int baseOut[4]);

void activateFencerLED(uint8_t fencer);
void deactivateFencerLED(uint8_t fencer);
void activateB2BLed();
void deactivateB2BLed();
void clearPendingHit();

void setPhysicalLEDs(bool left, bool right, bool b2b) {
  digitalWrite(LED_LEFT, left ? HIGH : LOW);
  digitalWrite(LED_RIGHT, right ? HIGH : LOW);
  digitalWrite(LED_B2B, b2b ? HIGH : LOW);
}

void clearAllLEDs() {
  fencer1LedActive = false;
  fencer2LedActive = false;
  b2bLedActive = false;
  setPhysicalLEDs(false, false, false);
  Serial.println("SCORE:RESET");
}

void clearPendingHit() {
  pendingHit = DetectionEvent::NONE;
  pendingHitTimeMs = 0;
  pendingHitTimeUs = 0;
}

void logEventAt(uint32_t eventUs, const String& message) {
  uint32_t elapsedUs = 0;
  if (phraseStartMicros > 0) {
    elapsedUs = eventUs - phraseStartMicros;
  }
  Serial.print("LOG:");
  Serial.print(elapsedUs);
  Serial.print("|");
  Serial.println(message);
}

void logEvent(const String& message) {
  logEventAt(micros(), message);
}

void logPendingSingleHit() {
  if (!shouldRecordEvents()) {
    clearPendingHit();
    return;
  }

  if (pendingHit == DetectionEvent::LEFT_HIT) {
    if (!fencer1Scored) fencer1Scored = true;
    logEventAt(pendingHitTimeUs, "HIT: Left scores on Right!");
  } else if (pendingHit == DetectionEvent::RIGHT_HIT) {
    if (!fencer2Scored) fencer2Scored = true;
    logEventAt(pendingHitTimeUs, "HIT: Right scores on Left!");
  }

  clearPendingHit();
}

void driversHiZ() {
  for (uint8_t i = 0; i < 4; ++i) {
    pinMode(DRIVER_NODES[i], INPUT);
  }
}

void driversLow() {
  for (uint8_t i = 0; i < 4; ++i) {
    pinMode(DRIVER_NODES[i], OUTPUT);
    digitalWrite(DRIVER_NODES[i], LOW);
  }
}

void dischargeNodes() {
  driversLow();
  delayMicroseconds(DISCHARGE_US);
  driversHiZ();
}

void driveNodeHigh(uint8_t pin) {
  pinMode(pin, OUTPUT);
  digitalWrite(pin, HIGH);
}

int avgReadsFast(uint8_t aPin, uint8_t samples, uint16_t gapUs) {
  long total = 0;
  for (uint8_t i = 0; i < samples; ++i) {
    total += analogRead(aPin);
    if (gapUs) {
      delayMicroseconds(gapUs);
    }
  }
  return static_cast<int>(total / samples);
}

void measureFastBaseline(int base[4]) {
  driversHiZ();
  delayMicroseconds(300);
  base[0] = avgReadsFast(SENSE_NODES[0], 6, 60);
  base[1] = avgReadsFast(SENSE_NODES[1], 6, 60);
  base[2] = avgReadsFast(SENSE_NODES[2], 6, 60);
  base[3] = avgReadsFast(SENSE_NODES[3], 6, 60);
}

bool detectPairFast(uint8_t drv, uint8_t lst, const int base[4]) {
  int absThresh = ABS_HI[lst];
  int deltaThresh = DELTA_HI[lst];
  int boost = PAIR_BOOST[drv][lst];
  int maxValue = 0;

  for (uint8_t k = 0; k < BURST_READS; ++k) {
    int sample = analogRead(SENSE_NODES[lst]);
    if (sample > maxValue) {
      maxValue = sample;
    }
    delayMicroseconds(BURST_GAP_US);
  }

  int absNeed = max(0, absThresh - boost);
  int deltaNeed = max(0, deltaThresh - (boost / 2));
  return (maxValue >= absNeed) && ((maxValue - base[lst]) >= deltaNeed);
}

void scanOrderFast(const uint8_t order[4], bool hit[4][4], const int base[4]) {
  for (uint8_t idx = 0; idx < 4; ++idx) {
    uint8_t driver = order[idx];
    driveNodeHigh(DRIVER_NODES[driver]);
    delayMicroseconds(SETTLE_US);
    for (uint8_t listener = 0; listener < 4; ++listener) {
      if (listener == driver) {
        continue;
      }
      if (!hit[driver][listener]) {
        hit[driver][listener] = detectPairFast(driver, listener, base);
      }
    }
    driversHiZ();
    dischargeNodes();
  }
}

FastEvent classifyFast(const bool hit[4][4]) {
  bool left  = hit[0][3] || hit[3][0];
  bool right = hit[2][1] || hit[1][2];
  bool clash = hit[0][2] || hit[2][0];

  if (clash) {
    return FastEvent::CLASH;
  }
  if (left && right) {
    return FastEvent::SIMULTANEOUS;
  }
  if (left) {
    return FastEvent::LEFT;
  }
  if (right) {
    return FastEvent::RIGHT;
  }
  return FastEvent::NONE;
}

FastEvent runFastDetector(int baseOut[4]) {
  uint32_t now = millis();

  if (now < fastLockoutUntil) {
    if (fastLatched != FastEvent::NONE && now > fastLatchUntil) {
      fastLatched = FastEvent::NONE;
    }
    delayMicroseconds(120);
    return FastEvent::NONE;
  }

  int base[4];
  measureFastBaseline(base);
  for (uint8_t i = 0; i < 4; ++i) {
    fastBaseline[i] = base[i];
    baseOut[i] = base[i];
  }

  bool hit[4][4] = { { false, false, false, false },
                     { false, false, false, false },
                     { false, false, false, false },
                     { false, false, false, false } };

  const uint8_t orderA[4] = { 0, 2, 1, 3 };
  const uint8_t orderB[4] = { 3, 1, 2, 0 };

  scanOrderFast(orderA, hit, base);
  scanOrderFast(orderB, hit, base);

  FastEvent detected = classifyFast(hit);
  if (detected != FastEvent::NONE) {
    fastLatched = detected;
    fastLatchUntil = now + LATCH_MS;
    fastLockoutUntil = now + LOCKOUT_MS;

    switch (detected) {
      case FastEvent::LEFT:
        Serial.println(F("[EVENT] Left valid (LB <-> RL)"));
        break;
      case FastEvent::RIGHT:
        Serial.println(F("[EVENT] Right valid (RB <-> LL)"));
        break;
      case FastEvent::CLASH:
        Serial.println(F("[EVENT] Blade clash (LB <-> RB)"));
        break;
      case FastEvent::SIMULTANEOUS:
        Serial.println(F("[EVENT] Simultaneous valid hits (LB/RL & RB/LL)"));
        break;
      default:
        break;
    }

    delayMicroseconds(120);
    return detected;
  }

  if (fastLatched != FastEvent::NONE && now > fastLatchUntil) {
    fastLatched = FastEvent::NONE;
  }

  delayMicroseconds(120);
  return FastEvent::NONE;
}

const char* pinLabelLB = "A0";
const char* pinLabelLL = "A1";
const char* pinLabelRB = "A2";
const char* pinLabelRL = "A3";

void announceContact(const char* pinA, const char* pinB) {
  Serial.print("CONTACT:");
  Serial.print(pinA);
  Serial.print(",");
  Serial.println(pinB);
}

int readAvg(uint8_t pin, int samples) {
  long sum = 0;
  for (int i = 0; i < samples; ++i) {
    sum += analogRead(pin);
  }
  return static_cast<int>(sum / samples);
}

void calibrateBaselines() {
  dischargeNodes();
  int base[4];
  measureFastBaseline(base);
  baseLB = base[0];
  baseLL = base[1];
  baseRB = base[2];
  baseRL = base[3];
  for (uint8_t i = 0; i < 4; ++i) {
    fastBaseline[i] = base[i];
  }
  Serial.println(F("=== Saber Detector Ready (integrated) ==="));
  Serial.print(F("Baselines: LB=")); Serial.print(baseLB);
  Serial.print(F(" LL=")); Serial.print(baseLL);
  Serial.print(F(" RB=")); Serial.print(baseRB);
  Serial.print(F(" RL=")); Serial.println(baseRL);
}

void resetDetectionCounters() {
  cntLeftHit = cntRightHit = cntB2B = cntLeftSelf = cntRightSelf = 0;
  fastLatched = FastEvent::NONE;
  fastLatchUntil = 0;
  fastLockoutUntil = 0;
  driversHiZ();
}

DetectionEvent detectEvent() {
  unsigned long now = millis();
  bool pendingExpired = (pendingHit != DetectionEvent::NONE) && (now - pendingHitTimeMs > SABER_LOCKOUT_MS);
  if (pendingExpired && currentState != State::LOCKOUT_PERIOD) {
    clearPendingHit();
    pendingExpired = false;
  }

  // Once lockout starts and no second hit is pending, ignore all further contacts.
  if (currentState == State::LOCKOUT_PERIOD && pendingHit == DetectionEvent::NONE) {
    return DetectionEvent::NONE;
  }

  // During lockout, keep the first-hit timestamp until phrase end so it can be logged accurately.
  if (currentState == State::LOCKOUT_PERIOD && pendingExpired) {
    return DetectionEvent::NONE;
  }
  bool doubleWindowActive = (pendingHit != DetectionEvent::NONE) && !pendingExpired;
  int baseline[4] = { baseLB, baseLL, baseRB, baseRL };
  FastEvent fastEvent = runFastDetector(baseline);
  baseLB = baseline[0];
  baseLL = baseline[1];
  baseRB = baseline[2];
  baseRL = baseline[3];

  bool fastLeftDetected  = (fastEvent == FastEvent::LEFT) || (fastEvent == FastEvent::SIMULTANEOUS);
  bool fastRightDetected = (fastEvent == FastEvent::RIGHT) || (fastEvent == FastEvent::SIMULTANEOUS);
  bool fastClashDetected = (fastEvent == FastEvent::CLASH);
  bool fastSimultaneous  = (fastEvent == FastEvent::SIMULTANEOUS);

  int LB = readAvg(PIN_LB);
  int LL = readAvg(PIN_LL);
  int RB = readAvg(PIN_RB);
  int RL = readAvg(PIN_RL);

  if (DEBUG_PRINT && now - lastPrintMs >= PRINT_EVERY_MS) {
    lastPrintMs = now;
    Serial.print(F("raw "));
    Serial.print(LB); Serial.print(' ');
    Serial.print(LL); Serial.print(' ');
    Serial.print(RB); Serial.print(' ');
    Serial.println(RL);
  }

  if (!doubleWindowActive && (now - lastEventMs < DETECTION_DEBOUNCE_MS)) {
    return DetectionEvent::NONE;
  }

  int dLB = absi(LB - baseLB);
  int dLL = absi(LL - baseLL);
  int dRB = absi(RB - baseRB);
  int dRL = absi(RL - baseRL);

  int diff_LB_RB = absi(LB - RB);
  int diff_LB_LL = absi(LB - LL);
  int diff_RB_RL = absi(RB - RL);
  int diff_LB_RL = absi(LB - RL);
  int diff_RB_LL = absi(RB - LL);

  bool candLeftPrimary  = (diff_LB_RL <= HIT_CLOSE_COUNTS) && (dLB >= HIT_DEV_MIN_COUNTS) && (dRL >= HIT_DEV_MIN_COUNTS);
  bool candRightPrimary = (diff_RB_LL <= HIT_CLOSE_COUNTS) && (dRB >= HIT_DEV_MIN_COUNTS) && (dLL >= HIT_DEV_MIN_COUNTS);

  bool fastLeftAnalog  = (diff_LB_RL <= HIT_CLOSE_COUNTS_FAST) && (dLB >= HIT_DEV_MIN_FAST) && (dRL >= HIT_DEV_MIN_FAST);
  bool fastRightAnalog = (diff_RB_LL <= HIT_CLOSE_COUNTS_FAST) && (dRB >= HIT_DEV_MIN_FAST) && (dLL >= HIT_DEV_MIN_FAST);

  bool candB2B       = (diff_LB_RB <= B2B_CLOSE_COUNTS) && (dLB >= B2B_DEV_MIN_COUNTS) && (dRB >= B2B_DEV_MIN_COUNTS);
  bool candLeftSelf  = (diff_LB_LL <= SELF_CLOSE_COUNTS) && (dLB >= SELF_DEV_MIN_COUNTS) && (dLL >= SELF_DEV_MIN_COUNTS);
  bool candRightSelf = (diff_RB_RL <= SELF_CLOSE_COUNTS) && (dRB >= SELF_DEV_MIN_COUNTS) && (dRL >= SELF_DEV_MIN_COUNTS);

  bool fastB2B = (diff_LB_RB <= B2B_CLOSE_COUNTS_FAST) && (dLB >= B2B_DEV_MIN_FAST) && (dRB >= B2B_DEV_MIN_FAST);

  cntLeftHit   = candLeftPrimary  ? (cntLeftHit + 1)   : 0;
  cntRightHit  = candRightPrimary ? (cntRightHit + 1)  : 0;
  cntB2B       = candB2B       ? (cntB2B + 1)       : 0;
  cntLeftSelf  = candLeftSelf  ? (cntLeftSelf + 1)  : 0;
  cntRightSelf = candRightSelf ? (cntRightSelf + 1) : 0;

  bool leftDetected        = fastLeftDetected  || fastLeftAnalog  || (cntLeftHit  >= CONFIRM_SAMPLES);
  bool rightDetected       = fastRightDetected || fastRightAnalog || (cntRightHit >= CONFIRM_SAMPLES);
  bool leftSelfDetected    = (cntLeftSelf  >= CONFIRM_SAMPLES);
  bool rightSelfDetected   = (cntRightSelf >= CONFIRM_SAMPLES);
  bool b2bDetected         = fastClashDetected || fastB2B || (cntB2B >= CONFIRM_SAMPLES);
  bool simultaneousAnalog  = leftDetected && rightDetected;

  // During the double-hit window, only accept valid scoring contacts.
  if (currentState == State::LOCKOUT_PERIOD && pendingHit != DetectionEvent::NONE) {
    if (fastSimultaneous || (simultaneousAnalog && !b2bDetected)) {
      resetDetectionCounters();
      lastEventMs = now;
      return DetectionEvent::SIMULTANEOUS;
    }
    if (pendingHit == DetectionEvent::LEFT_HIT && rightDetected) {
      resetDetectionCounters();
      lastEventMs = now;
      return DetectionEvent::RIGHT_HIT;
    }
    if (pendingHit == DetectionEvent::RIGHT_HIT && leftDetected) {
      resetDetectionCounters();
      lastEventMs = now;
      return DetectionEvent::LEFT_HIT;
    }
    return DetectionEvent::NONE;
  }

  // Priority 1: Simultaneous valid hits (highest priority)
  if (fastSimultaneous || (simultaneousAnalog && !b2bDetected)) {
    resetDetectionCounters();
    lastEventMs = now;
    return DetectionEvent::SIMULTANEOUS;
  }

  // Priority 2: Valid hits (blade-to-lame) take priority over blade-to-blade
  // This fixes the issue where blade-to-lame contact is missed during B2B
  if (leftDetected) {
    resetDetectionCounters();
    lastEventMs = now;
    return DetectionEvent::LEFT_HIT;
  }

  if (rightDetected) {
    resetDetectionCounters();
    lastEventMs = now;
    return DetectionEvent::RIGHT_HIT;
  }

  // Priority 3: Self contacts
  if (leftSelfDetected) {
    resetDetectionCounters();
    lastEventMs = now;
    return DetectionEvent::LEFT_SELF;
  }

  if (rightSelfDetected) {
    resetDetectionCounters();
    lastEventMs = now;
    return DetectionEvent::RIGHT_SELF;
  }

  // Priority 4: Blade-to-blade (lowest priority, only if no valid hits)
  if (b2bDetected) {
    resetDetectionCounters();
    lastEventMs = now;
    return DetectionEvent::BLADE_TO_BLADE;
  }

  return DetectionEvent::NONE;
}

void startLockoutWindow() {
  if (currentState == State::RECORDING) {
    currentState = State::LOCKOUT_PERIOD;
    lockoutStartTime = millis();
    Serial.println("STATE:LOCKOUT_PERIOD");
    logEvent("Lockout period started (0.200s window)");
  }
}

void processDetectionEvent(DetectionEvent event) {
  if (event == DetectionEvent::NONE) {
    return;
  }

  unsigned long now = millis();
  uint32_t nowUs = micros();
  switch (event) {
    case DetectionEvent::LEFT_HIT: {
      if (pendingHit == DetectionEvent::RIGHT_HIT && (now - pendingHitTimeMs <= SABER_LOCKOUT_MS)) {
        uint32_t firstHitTimeUs = pendingHitTimeUs;
        clearPendingHit();
        setPhysicalLEDs(true, true, false);
        activateFencerLED(1);
        activateFencerLED(2);
        announceContact(pinLabelLB, pinLabelRL);
        announceContact(pinLabelRB, pinLabelLL);
        if (shouldRecordEvents()) {
          if (!fencer2Scored) fencer2Scored = true;
          if (!fencer1Scored) fencer1Scored = true;
          logEventAt(firstHitTimeUs, "HIT: Right scores on Left!");
          logEventAt(nowUs, "HIT: Left scores on Right!");
          logEventAt(nowUs, "HIT: Simultaneous valid hits!");
          startLockoutWindow();
        }
      } else {
        bool pendingSameSide = (pendingHit == DetectionEvent::LEFT_HIT) && (now - pendingHitTimeMs <= SABER_LOCKOUT_MS);
        if (!pendingSameSide) {
          pendingHit = DetectionEvent::LEFT_HIT;
          pendingHitTimeMs = now;
          pendingHitTimeUs = nowUs;
        }
        setPhysicalLEDs(true, false, false);
        announceContact(pinLabelLB, pinLabelRL);
        activateFencerLED(1);
        if (shouldRecordEvents()) {
          startLockoutWindow();
        }
      }
      break;
    }
    case DetectionEvent::RIGHT_HIT: {
      if (pendingHit == DetectionEvent::LEFT_HIT && (now - pendingHitTimeMs <= SABER_LOCKOUT_MS)) {
        uint32_t firstHitTimeUs = pendingHitTimeUs;
        clearPendingHit();
        setPhysicalLEDs(true, true, false);
        activateFencerLED(1);
        activateFencerLED(2);
        announceContact(pinLabelLB, pinLabelRL);
        announceContact(pinLabelRB, pinLabelLL);
        if (shouldRecordEvents()) {
          if (!fencer1Scored) fencer1Scored = true;
          if (!fencer2Scored) fencer2Scored = true;
          logEventAt(firstHitTimeUs, "HIT: Left scores on Right!");
          logEventAt(nowUs, "HIT: Right scores on Left!");
          logEventAt(nowUs, "HIT: Simultaneous valid hits!");
          startLockoutWindow();
        }
      } else {
        bool pendingSameSide = (pendingHit == DetectionEvent::RIGHT_HIT) && (now - pendingHitTimeMs <= SABER_LOCKOUT_MS);
        if (!pendingSameSide) {
          pendingHit = DetectionEvent::RIGHT_HIT;
          pendingHitTimeMs = now;
          pendingHitTimeUs = nowUs;
        }
        setPhysicalLEDs(false, true, false);
        announceContact(pinLabelRB, pinLabelLL);
        activateFencerLED(2);
        if (shouldRecordEvents()) {
          startLockoutWindow();
        }
      }
      break;
    }
    case DetectionEvent::SIMULTANEOUS: {
      setPhysicalLEDs(true, true, false);
      announceContact(pinLabelLB, pinLabelRL);
      announceContact(pinLabelRB, pinLabelLL);
      activateFencerLED(1);
      activateFencerLED(2);
      if (shouldRecordEvents()) {
        if (!fencer1Scored) fencer1Scored = true;
        if (!fencer2Scored) fencer2Scored = true;
        logEventAt(nowUs, "HIT: Left scores on Right!");
        logEventAt(nowUs, "HIT: Right scores on Left!");
        logEventAt(nowUs, "HIT: Simultaneous valid hits!");
        startLockoutWindow();
      }
      clearPendingHit();
      break;
    }
    case DetectionEvent::BLADE_TO_BLADE: {
      setPhysicalLEDs(false, false, true);
      announceContact(pinLabelLB, pinLabelRB);
      activateB2BLed();
      logEvent("Off-Target: Blade-to-blade contact.");
      clearPendingHit();
      break;
    }
    case DetectionEvent::LEFT_SELF: {
      setPhysicalLEDs(false, false, false);
      announceContact(pinLabelLB, pinLabelLL);
      if (shouldRecordEvents()) {
        logEvent("Self contact detected on Left fencer.");
      }
      clearPendingHit();
      break;
    }
    case DetectionEvent::RIGHT_SELF: {
      setPhysicalLEDs(false, false, false);
      announceContact(pinLabelRB, pinLabelRL);
      if (shouldRecordEvents()) {
        logEvent("Self contact detected on Right fencer.");
      }
      clearPendingHit();
      break;
    }
    default:
      break;
  }
}

void activateFencerLED(uint8_t fencer) {
  unsigned long now = millis();
  if (fencer == 1) {
    fencer1LedOnTime = now;
    if (!fencer1LedActive) {
      fencer1LedActive = true;
      Serial.println("SCORE:F1_ON");
    }
    digitalWrite(LED_LEFT, HIGH);
  } else if (fencer == 2) {
    fencer2LedOnTime = now;
    if (!fencer2LedActive) {
      fencer2LedActive = true;
      Serial.println("SCORE:F2_ON");
    }
    digitalWrite(LED_RIGHT, HIGH);
  }
}

void deactivateFencerLED(uint8_t fencer) {
  if (fencer == 1 && fencer1LedActive) {
    fencer1LedActive = false;
    Serial.println("SCORE:F1_OFF");
    digitalWrite(LED_LEFT, LOW);
  } else if (fencer == 2 && fencer2LedActive) {
    fencer2LedActive = false;
    Serial.println("SCORE:F2_OFF");
    digitalWrite(LED_RIGHT, LOW);
  }
}

void activateB2BLed() {
  b2bLedActive = true;
  b2bLedOnTime = millis();
  digitalWrite(LED_B2B, HIGH);
}

void deactivateB2BLed() {
  if (b2bLedActive) {
    b2bLedActive = false;
    digitalWrite(LED_B2B, LOW);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_LEFT, OUTPUT);
  pinMode(LED_RIGHT, OUTPUT);
  pinMode(LED_B2B, OUTPUT);
  setPhysicalLEDs(false, false, false);

  analogReference(DEFAULT);
  driversHiZ();
  calibrateBaselines();
  Serial.println("STATE:WAITING_FOR_COMMAND");
}

void loop() {
  if (Serial.available() > 0) {
    char command = Serial.read();
    if (command == 's') {
      if (currentState == State::WAITING_FOR_COMMAND || currentState == State::DISPLAYING_RESULTS) {
        fencer1Scored = false;
        fencer2Scored = false;
        fencer1LedActive = false;
        fencer2LedActive = false;
        b2bLedActive = false;
        fencer1LedOnTime = fencer2LedOnTime = b2bLedOnTime = 0;
        lastEventMs = 0;
        resetDetectionCounters();
        clearPendingHit();
        setPhysicalLEDs(false, false, false);
        Serial.println("SCORE:RESET");
        phraseStartMillis = millis();
        phraseStartMicros = micros();
        currentState = State::RECORDING;
        Serial.print("PHRASE_START_MS:");
        Serial.println(phraseStartMillis);
        Serial.println("STATE:RECORDING");
        logEventAt(phraseStartMicros, "Phrase recording started");
      }
    } else if (command == 'u') {
      Serial.print("TIME_MS:");
      Serial.println(millis());
    } else if (command == 'c') {
      logEvent("Phrase cancelled by controller.");
      deactivateFencerLED(1);
      deactivateFencerLED(2);
      deactivateB2BLed();
      setPhysicalLEDs(false, false, false);
      fencer1Scored = false;
      fencer2Scored = false;
      clearPendingHit();
      resetDetectionCounters();
      currentState = State::WAITING_FOR_COMMAND;
      phraseStartMillis = 0;
      phraseStartMicros = 0;
      Serial.println("SCORE:RESET");
      Serial.println("STATE:WAITING_FOR_COMMAND");
    }
  }

  DetectionEvent event = detectEvent();
  processDetectionEvent(event);

  if (currentState == State::LOCKOUT_PERIOD) {
    if (millis() - lockoutStartTime > SABER_LOCKOUT_MS) {
      if (pendingHit != DetectionEvent::NONE) {
        logPendingSingleHit();
      }
      logEvent("Phrase recording ended");
      currentState = State::DISPLAYING_RESULTS;
      Serial.println("STATE:DISPLAYING_RESULTS");
      clearPendingHit();
      phraseStartMillis = 0;
      phraseStartMicros = 0;
    }
  }

  unsigned long now = millis();
  if (fencer1LedActive && (now - fencer1LedOnTime >= LED_HOLD_MILLISECONDS)) {
    deactivateFencerLED(1);
  }
  if (fencer2LedActive && (now - fencer2LedOnTime >= LED_HOLD_MILLISECONDS)) {
    deactivateFencerLED(2);
  }
  if (b2bLedActive && (now - b2bLedOnTime >= LED_HOLD_MILLISECONDS)) {
    deactivateB2BLed();
  }
}
