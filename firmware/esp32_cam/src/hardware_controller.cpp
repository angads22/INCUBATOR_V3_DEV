#include "hardware_controller.h"
#include "config.h"

HardwareController::HardwareController()
    : _dht(PIN_DHT, DHT_TYPE) {}

void HardwareController::begin() {
    _dht.begin();

    // Relays — default OFF (HIGH for active-LOW modules)
    if (PIN_HEATER >= 0) { pinMode(PIN_HEATER, OUTPUT); digitalWrite(PIN_HEATER, RELAY_OFF); }
    if (PIN_FAN    >= 0) { pinMode(PIN_FAN,    OUTPUT); digitalWrite(PIN_FAN,    RELAY_OFF); }
    if (PIN_LOCK   >= 0) { pinMode(PIN_LOCK,   OUTPUT); digitalWrite(PIN_LOCK,   RELAY_OFF); }

    // Motor
    if (PIN_MOTOR_STEP >= 0) { pinMode(PIN_MOTOR_STEP, OUTPUT); digitalWrite(PIN_MOTOR_STEP, LOW); }
    if (PIN_MOTOR_DIR  >= 0) { pinMode(PIN_MOTOR_DIR,  OUTPUT); digitalWrite(PIN_MOTOR_DIR,  LOW); }

    // Candle/flashlight — off by default
    if (PIN_CANDLE >= 0) { pinMode(PIN_CANDLE, OUTPUT); digitalWrite(PIN_CANDLE, LOW); }

    Serial.println("[HW] Hardware controller initialised");
}

// ─── Sensors ──────────────────────────────────────────────────────────────────

float HardwareController::readTemp() {
    return _dht.readTemperature();  // Celsius
}

float HardwareController::readHumidity() {
    return _dht.readHumidity();
}

// ─── Actuators ────────────────────────────────────────────────────────────────

void HardwareController::setHeater(bool on) {
    _heaterOn = on;
    setRelay(PIN_HEATER, on);
    // Mirror on candle pin if wired to same relay bank
    if (PIN_CANDLE != PIN_HEATER) setCandle(on);
    Serial.printf("[HW] Heater %s\n", on ? "ON" : "OFF");
}

void HardwareController::setFan(bool on) {
    _fanOn = on;
    setRelay(PIN_FAN, on);
    Serial.printf("[HW] Fan %s\n", on ? "ON" : "OFF");
}

void HardwareController::setLock(bool open) {
    _lockOpen = open;
    setRelay(PIN_LOCK, open);
    Serial.printf("[HW] Lock %s\n", open ? "OPEN" : "CLOSED");
}

void HardwareController::setDoor(bool open) {
    _doorOpen = open;
    // Door uses same relay pattern; wire to a second relay on a free pin.
    // If no separate door pin is defined, we log only.
    Serial.printf("[HW] Door %s\n", open ? "OPEN" : "CLOSED");
}

void HardwareController::setCandle(bool on) {
    if (PIN_CANDLE >= 0) {
        digitalWrite(PIN_CANDLE, on ? HIGH : LOW);
    }
}

// ─── Motor ────────────────────────────────────────────────────────────────────

void HardwareController::stepMotor(int steps, bool forward) {
    if (PIN_MOTOR_STEP < 0) return;

    if (PIN_MOTOR_DIR >= 0) {
        digitalWrite(PIN_MOTOR_DIR, forward ? HIGH : LOW);
        delayMicroseconds(50);  // settle direction
    }

    for (int i = 0; i < abs(steps); i++) {
        digitalWrite(PIN_MOTOR_STEP, HIGH);
        delayMicroseconds(MOTOR_STEP_DELAY_US);
        digitalWrite(PIN_MOTOR_STEP, LOW);
        delayMicroseconds(MOTOR_STEP_DELAY_US);
        yield();  // keep watchdog happy during long moves
    }
}

void HardwareController::runTurnCycle() {
    int steps = MOTOR_STEPS_PER_TURN * MOTOR_TURN_REVOLUTIONS;
    Serial.printf("[HW] Turn cycle: %d steps forward then back\n", steps);
    stepMotor(steps, true);
    delay(500);
    stepMotor(steps, false);
}

// ─── Private ──────────────────────────────────────────────────────────────────

void HardwareController::setRelay(uint8_t pin, bool on) {
    if (pin == (uint8_t)-1) return;
    digitalWrite(pin, on ? RELAY_ON : RELAY_OFF);
}
