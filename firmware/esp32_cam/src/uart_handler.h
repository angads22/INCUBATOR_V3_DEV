#pragma once

#include <Arduino.h>

// Forward declarations
class HardwareController;
class CameraHandler;

// Reads newline-delimited JSON from the host UART and dispatches commands.
// Protocol (matches Python esp32_link.py):
//   Receive:  {"action":"<cmd>","value":<optional>}\n
//   Respond:  {"ok":true/false,"value":<optional>,"error":<optional>}\n
class UartHandler {
public:
    UartHandler(HardwareController& hw, CameraHandler& cam);

    // Call once in setup() after Serial has been started.
    void begin();

    // Call every loop() iteration — non-blocking.
    void poll();

private:
    void dispatch(const char* action, const char* value);

    // Response helpers
    void sendOk();
    void sendOkValue(float v);
    void sendOkStr(const char* v);
    void sendError(const char* msg);

    HardwareController& _hw;
    CameraHandler&      _cam;
    char                _buf[UART_LINE_BUF_SIZE];
    uint16_t            _pos = 0;
};
