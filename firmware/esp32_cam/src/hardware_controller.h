#pragma once

#include <Arduino.h>
#include <DHT.h>

class HardwareController {
public:
    HardwareController();

    void begin();

    // Sensors — returns NAN on read failure
    float readTemp();
    float readHumidity();

    // Actuators
    void setHeater(bool on);
    void setFan(bool on);
    void setLock(bool open);
    void setDoor(bool open);
    void setCandle(bool on);

    // Motor
    void runTurnCycle();
    void stepMotor(int steps, bool forward);

    // State accessors
    bool heaterOn()  const { return _heaterOn; }
    bool fanOn()     const { return _fanOn; }
    bool lockOpen()  const { return _lockOpen; }
    bool doorOpen()  const { return _doorOpen; }

private:
    DHT   _dht;
    bool  _heaterOn = false;
    bool  _fanOn    = false;
    bool  _lockOpen = false;
    bool  _doorOpen = false;

    void setRelay(uint8_t pin, bool on);
};
