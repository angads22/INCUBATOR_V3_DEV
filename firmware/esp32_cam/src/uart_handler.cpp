#include "uart_handler.h"
#include "config.h"
#include "hardware_controller.h"
#include "camera_handler.h"
#include "wifi_manager.h"

#include <ArduinoJson.h>

#if USE_SERIAL2
  #define HOST_SERIAL Serial2
#else
  #define HOST_SERIAL Serial
#endif

UartHandler::UartHandler(HardwareController& hw, CameraHandler& cam)
    : _hw(hw), _cam(cam) {}

void UartHandler::begin() {
    // Serial is initialised in main.cpp; nothing extra needed here.
}

// ─── poll() — called every loop() ─────────────────────────────────────────────

void UartHandler::poll() {
    while (HOST_SERIAL.available()) {
        char c = HOST_SERIAL.read();

        if (c == '\n' || c == '\r') {
            if (_pos == 0) continue;   // skip blank lines
            _buf[_pos] = '\0';
            _pos = 0;

            // Parse JSON
            JsonDocument doc;
            DeserializationError err = deserializeJson(doc, _buf);
            if (err) {
                sendError("json parse error");
                continue;
            }

            const char* action = doc["action"] | "";
            // value can be string, number, or null
            String valueStr;
            if (doc["value"].is<const char*>()) {
                valueStr = doc["value"].as<const char*>();
            } else if (doc["value"].is<float>()) {
                valueStr = String(doc["value"].as<float>());
            } else if (doc["value"].is<int>()) {
                valueStr = String(doc["value"].as<int>());
            }

            dispatch(action, valueStr.c_str());
        } else {
            if (_pos < UART_LINE_BUF_SIZE - 1) {
                _buf[_pos++] = c;
            }
            // Overflow: silently discard until newline resets the buffer
        }
    }
}

// ─── dispatch ─────────────────────────────────────────────────────────────────

void UartHandler::dispatch(const char* action, const char* value) {
    // ── Sensors ──────────────────────────────────────────────────────────────
    if (strcmp(action, "read_temp") == 0) {
        float t = _hw.readTemp();
        if (isnan(t)) { sendError("DHT read failed"); return; }
        sendOkValue(t);

    } else if (strcmp(action, "read_humidity") == 0) {
        float h = _hw.readHumidity();
        if (isnan(h)) { sendError("DHT read failed"); return; }
        sendOkValue(h);

    // ── Heater / candle (mapped the same way in Python provider) ─────────────
    } else if (strcmp(action, "set_candle") == 0) {
        bool on = (strcmp(value, "on") == 0 || strcmp(value, "1") == 0);
        _hw.setHeater(on);
        sendOk();

    } else if (strcmp(action, "set_heater") == 0) {
        bool on = (strcmp(value, "on") == 0 || strcmp(value, "1") == 0);
        _hw.setHeater(on);
        sendOk();

    // ── Fan ───────────────────────────────────────────────────────────────────
    } else if (strcmp(action, "set_fan") == 0) {
        bool on = (strcmp(value, "on") == 0 || strcmp(value, "1") == 0);
        _hw.setFan(on);
        sendOk();

    // ── Lock ──────────────────────────────────────────────────────────────────
    } else if (strcmp(action, "open_lock") == 0) {
        _hw.setLock(true);
        sendOk();

    } else if (strcmp(action, "close_lock") == 0) {
        _hw.setLock(false);
        sendOk();

    // ── Door ──────────────────────────────────────────────────────────────────
    } else if (strcmp(action, "open_door") == 0) {
        _hw.setDoor(true);
        sendOk();

    } else if (strcmp(action, "close_door") == 0) {
        _hw.setDoor(false);
        sendOk();

    // ── Motor / egg turner ────────────────────────────────────────────────────
    } else if (strcmp(action, "move_motor") == 0) {
        // value == "turn_cycle" or a number of steps
        if (strcmp(value, "turn_cycle") == 0 || strlen(value) == 0) {
            _hw.runTurnCycle();
        } else {
            int steps = atoi(value);
            _hw.stepMotor(steps, true);
        }
        sendOk();

    // ── Camera ────────────────────────────────────────────────────────────────
    } else if (strcmp(action, "capture_image") == 0) {
        // captureAndSave() writes its own JSON+binary frame directly to Serial
        // and returns "__raw_sent__" on success, or "" on failure.
        String ref = _cam.captureAndSave();
        if (ref.isEmpty()) {
            sendError("camera capture failed");
        }
        // If ref == "__raw_sent__" the response was already written; do nothing.

    // ── Device info ───────────────────────────────────────────────────────────
    } else if (strcmp(action, "ping") == 0) {
        sendOkStr("pong");

    } else if (strcmp(action, "reset_wifi") == 0) {
        // Allow the Python host to trigger a WiFi re-onboard
        sendOk();
        delay(500);
        WifiManager::resetAndReboot();

    } else {
        sendError("unknown action");
    }
}

// ─── Response helpers ─────────────────────────────────────────────────────────

void UartHandler::sendOk() {
    HOST_SERIAL.println("{\"ok\":true}");
}

void UartHandler::sendOkValue(float v) {
    // Use a fixed-point representation with 2 decimal places
    char buf[64];
    snprintf(buf, sizeof(buf), "{\"ok\":true,\"value\":%.2f}", v);
    HOST_SERIAL.println(buf);
}

void UartHandler::sendOkStr(const char* v) {
    JsonDocument doc;
    doc["ok"]    = true;
    doc["value"] = v;
    String out;
    serializeJson(doc, out);
    HOST_SERIAL.println(out);
}

void UartHandler::sendError(const char* msg) {
    JsonDocument doc;
    doc["ok"]    = false;
    doc["error"] = msg;
    String out;
    serializeJson(doc, out);
    HOST_SERIAL.println(out);
}
