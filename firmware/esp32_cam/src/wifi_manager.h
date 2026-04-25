#pragma once

#include <Arduino.h>
#include <Preferences.h>

enum class WifiMode {
    NONE,
    AP,       // setup hotspot active
    STATION,  // connected to user's network
};

class WifiManager {
public:
    WifiManager();

    // Load saved credentials and decide boot mode.
    // Returns true if station mode connected successfully.
    bool begin();

    // Must be called every loop() iteration while in AP mode.
    void handleClient();

    WifiMode mode() const { return _mode; }
    String   deviceName() const { return _deviceName; }

    // Clear saved credentials and reboot into AP mode.
    static void resetAndReboot();

private:
    void startAP();
    void startStation();
    void startCaptivePortal();
    void stopWebServer();

    // HTTP route handlers (static so they can be used as callbacks)
    static void handleRoot();
    static void handleScan();
    static void handleSave();
    static void handleNotFound();
    static String buildSetupPage();
    static String scanNetworksJson();

    WifiMode   _mode       = WifiMode::NONE;
    String     _deviceName = "My Incubator";
    Preferences _prefs;
    unsigned long _apStartMs = 0;
};
