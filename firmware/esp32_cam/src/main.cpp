#include <Arduino.h>
#include "config.h"
#include "wifi_manager.h"
#include "hardware_controller.h"
#include "camera_handler.h"
#include "uart_handler.h"

// ─── Globals ──────────────────────────────────────────────────────────────────

static WifiManager        wifiMgr;
static HardwareController hw;
static CameraHandler      cam;
static UartHandler        uart(hw, cam);

// ─── setup() ──────────────────────────────────────────────────────────────────

void setup() {
    // UART0 is shared with USB-to-serial; start it first so log messages appear.
    Serial.begin(HOST_BAUD);
    while (!Serial && millis() < 3000) {}   // wait up to 3 s on CDC boards

#if USE_SERIAL2
    Serial2.begin(HOST_BAUD, SERIAL_8N1, PIN_UART2_RX, PIN_UART2_TX);
#endif

    Serial.println("\n\n=== Incubator ESP32-CAM v1.0 ===");
    Serial.printf("Chip: %s  Rev: %d  Flash: %uMB  PSRAM: %s\n",
                  ESP.getChipModel(),
                  ESP.getChipRevision(),
                  ESP.getFlashChipSize() / (1024 * 1024),
                  psramFound() ? "yes" : "no");

    // Initialise hardware peripherals
    hw.begin();

    // Initialise camera (non-fatal if camera not present)
    cam.begin();

    // Start UART command handler
    uart.begin();

    // Start WiFi — boots into AP onboarding mode if no creds saved, else STA
    bool connected = wifiMgr.begin();
    if (connected) {
        Serial.printf("[Main] WiFi ready — mode: STA  IP: %s\n",
                      WiFi.localIP().toString().c_str());
    } else {
        Serial.println("[Main] WiFi in AP/setup mode — waiting for onboarding");
    }

    Serial.println("[Main] Setup complete — entering main loop");
}

// ─── loop() ───────────────────────────────────────────────────────────────────

void loop() {
    // Service captive portal web server while in AP/setup mode
    wifiMgr.handleClient();

    // Poll UART for incoming JSON commands from the Python host
    uart.poll();

    // Reconnect to WiFi if station link dropped (retry every 30 s)
    static unsigned long lastReconnectMs = 0;
    if (wifiMgr.mode() == WifiMode::STATION &&
        WiFi.status() != WL_CONNECTED &&
        millis() - lastReconnectMs > 30000)
    {
        lastReconnectMs = millis();
        Serial.println("[Main] WiFi disconnected — reconnecting...");
        WiFi.reconnect();
    }
}
